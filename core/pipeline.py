"""
core/pipeline.py - Core workflow orchestration pipeline.
Runs document scanning, deduplication, deterministic/AI classification, and plan generation.
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
import shutil
import re
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Callable

from tqdm import tqdm

from core.config import (
    strip_scanner_tool_folders,
    DRY_RUN,
    MODEL_NAME,
    DEFAULT_PYTHON_CONFIDENCE_THRESHOLD,
    DEFAULT_REVIEW_CONFIDENCE_THRESHOLD,
    DEFAULT_FAMILY_CONFIG,
    SORTING_PRESETS,
    resolve_preset,
    RUNTIME_ROOT_DIR,
    INTERNAL_DATA_DIR,
    CURRENT_RUN_DIR,
    LEARNING_CSV_PATH,
    STATE_SAVE_INTERVAL,
    RECURRING_NAME_MIN_DOCS,
    LEARNING_STOPWORDS,
    normalize_for_matching
)
from core.rules import load_vocabulary_entries, save_learned_keyword
from core.extractor import should_skip_heavy_ocr, extract_document_data
from core.classifier import (
    DocumentClassification,
    evaluate_exception_rules,
    classify_deterministically,
    classify_with_ollama,
    calculate_expiration_status
)
from core.deduplicator import compute_sha256, _is_true_near_duplicate
from core.person_matcher import extract_unlisted_person_candidates, unify_vendor_names
from core.storage import (
    atomic_write_json,
    load_user_settings,
    apply_existing_plan,
    clean_output_directory,
    scan_and_summarize_files,
    save_folder_decisions,
    load_progress_state,
    save_progress_state,
    update_progress_status,
    determine_destination,
    resolve_collision,
    write_plan_artifacts,
    load_manual_edits,
    _compute_family_category_counts,
    sanitize_folder_name
)
from core.reporter import write_debug_report
from core.run_audit import audit
from core.folder_policy import (
    decide_folders, kept_root_for, preserved_destination, DEFAULT_MIN_FILES_FOR_FOLDER_DECISION
)
from core.logger import get_logger

log = get_logger("pipeline")


def run_sorter(
    input_dir: Path,
    output_dir: Path,
    dry_run: bool = DRY_RUN,
    model_name: str = MODEL_NAME,
    limit: Optional[int] = None,
    python_confidence_threshold: Optional[float] = None,
    review_confidence_threshold: Optional[float] = None,
    family_config: Optional[Dict[str, List[str]]] = None,
    exception_rules: Optional[List[Dict[str, Any]]] = None,
    clean_before_run: bool = False,
    ignore_cache: bool = False,
    progress_callback: Optional[Callable[[int, int, str, float, int, int], None]] = None,
    stop_event: Optional[Any] = None,
    fast_mode: bool = False,
    max_chars: int = 2000,
    ocr_max_pages: int = 2
) -> Dict[str, Any]:
    """
    Main orchestration function:
    1. Loads user configuration and exception rules.
    2. Fast-paths to apply_existing_plan if executing --apply without limits.
    3. Scans input directory for supported files.
    4. Computes SHA-256 for exact deduplication.
    5. Runs deterministic regex classifier, falling back to Ollama AI.
    6. Checks near-duplicate textual / perceptual matches.
    7. Emits progress, plan artifacts, and a diagnostic debug report.
    """
    settings = load_user_settings()
    auto_rename = settings.get("auto_rename", True)

    if family_config is None:
        family_config = settings.get("family_config", DEFAULT_FAMILY_CONFIG)
    primary_person = settings.get("primary_person") or (next(iter(family_config.keys())) if family_config else "General")

    if exception_rules is None:
        exception_rules = settings.get("exception_rules", [])
    preset_key = resolve_preset(settings.get("sorting_preset"))
    preset_cfg = SORTING_PRESETS[preset_key]
    if python_confidence_threshold is None:
        python_confidence_threshold = float(preset_cfg["py_conf"])
    if review_confidence_threshold is None:
        review_confidence_threshold = float(preset_cfg["rev_conf"])

    INTERNAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Run started — input: {input_dir}, output: {output_dir}, dry_run: {dry_run}, auto_rename: {auto_rename}, preset: {preset_key} (py={python_confidence_threshold}, review={review_confidence_threshold})")

    audit.reset()
    if not dry_run and (INTERNAL_DATA_DIR / "sort_plan.json").exists() and not limit:
        audit.load_scan(INTERNAL_DATA_DIR / "scan_audit.json")
        fast_result = apply_existing_plan(
            output_dir=output_dir,
            input_dir=input_dir,
            primary_person=primary_person,
            review_threshold=review_confidence_threshold,
            clean_before_run=clean_before_run,
            auto_rename=auto_rename,
            progress_callback=progress_callback,
            stop_event=stop_event
        )
        if fast_result is not None:
            return fast_result

    if clean_before_run and not dry_run:
        clean_output_directory(output_dir, input_dir)

    state_file = INTERNAL_DATA_DIR / "progress_state.json"
    latest_vocabulary_file = INTERNAL_DATA_DIR / "latest_learned_vocabulary.json"

    all_files, ext_counts, ignored_count = scan_and_summarize_files(input_dir)
    if limit and limit > 0:
        all_files = all_files[:limit]

    total_scanned = len(all_files)
    if total_scanned == 0:
        atomic_write_json(latest_vocabulary_file, [])
        log.warning(f"No supported files found in {input_dir}")
        return {
            "total": 0,
            "code": 0,
            "ollama": 0,
            "duplicates": 0,
            "elapsed": 0.0,
            "python_classification_seconds": 0.0,
            "ollama_classification_seconds": 0.0,
            "recurring_new_names": {},
        }

    log.info(f"Scan complete — {total_scanned} files queued ({ext_counts}), {ignored_count} ignored")

    vocabulary = load_vocabulary_entries(LEARNING_CSV_PATH)
    learned_rules = {row["key"]: row["category"] for row in vocabulary if row["kind"] != "manual"}
    manual_rules = {row["key"]: row["category"] for row in vocabulary if row["kind"] == "manual"}
    manual_signature = (
        hashlib.sha256(json.dumps(manual_rules, sort_keys=True).encode("utf-8")).hexdigest()
        if manual_rules else ""
    )
    newly_learned_terms: List[str] = []
    # SDS-FIX-TIMING: phase counters
    phase_stats = {"extract_seconds": 0.0, "extract_files": 0, "cache_hits": 0,
                   "ollama_calls": 0, "ollama_fallbacks": 0, "ollama_fallback_seconds": 0.0,
                   "per_file": []}
    state = {} if ignore_cache else load_progress_state(state_file)
    run_seen_hashes: Dict[str, str] = {}
    seen_docs_for_neardup: List[Dict[str, Any]] = []
    duplicate_rows: List[List[str]] = []
    unlisted_name_docs: Dict[str, List[str]] = {}

    # Folders the user marked "keep": no OCR, no AI, they are copied as-is
    _keep = {k.lower() for k, v in (settings.get("folder_decisions") or {}).items() if v == "keep"}

    def _in_kept(rel: str) -> bool:
        parts = [p.lower() for p in re.split(r"[\\/]+", rel) if p]
        return any(p in _keep or "/".join(parts[:i + 1]) in _keep for i, p in enumerate(parts))

    # Parallel pre-extraction for uncached files (speeds up heavy PDF/Image OCR)
    pre_extracted_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
    uncached_tasks: List[Tuple[Path, str, bool]] = []
    for fp in all_files:
        try:
            fh = compute_sha256(fp)
        except Exception:
            continue
        is_cached = (
            not ignore_cache
            and fh in state
            and "classification" in state[fh]
            and state[fh].get("schema_v") == 9
            and state[fh].get("manual_vocabulary_signature", "") == manual_signature
        )
        if not is_cached:
            try:
                rp = fp.parent.resolve().relative_to(input_dir.resolve())
                rf = "" if str(rp) == "." else str(rp)
            except Exception:
                rf = fp.parent.name
            rf = strip_scanner_tool_folders(rf)
            skip_ocr_flag = _in_kept(rf) or should_skip_heavy_ocr(fp, rf, family_config)
            uncached_tasks.append((fp, str(fp.resolve()), skip_ocr_flag))

    if uncached_tasks:
        log.info(f"Extracting and OCRing {len(uncached_tasks)} uncached files...")
        total_uncached = len(uncached_tasks)
        for idx, (fp, res_src, skip_flag) in enumerate(uncached_tasks, 1):
            log.info(f"[{idx}/{total_uncached}] Processing: {fp.name}")
            
            # Write progress directly to runtime cache so GUI tick() picks it up live
            try:
                progress_file = input_dir.parent / "runtime" / "_cache" / "progress_status.json"
                progress_file.parent.mkdir(parents=True, exist_ok=True)
                import json
                progress_file.write_text(json.dumps({
                    "current": idx,
                    "total": total_uncached,
                    "filename": fp.name,
                    "status": f"Extracting & OCRing [{idx}/{total_uncached}]: {fp.name}"
                }), encoding="utf-8")
            except Exception:
                pass

            t0 = time.perf_counter()
            data = extract_document_data(fp, input_root=input_dir, skip_ocr=skip_flag, ocr_max_pages=ocr_max_pages)
            dt = time.perf_counter() - t0
            pre_extracted_cache[res_src] = (data, dt)

    today = date.today()
    staged_items: List[Dict[str, Any]] = []
    folder_decisions: Dict[str, Any] = {}
    duplicates_count = 0
    code_resolved_count = 0
    ollama_resolved_count = 0
    newly_learned_count = 0
    unsaved_state_changes = 0
    start_time = time.time()

    classification_seconds = {"python": 0.0, "ollama": 0.0}

    def timed_classification(engine, classify, *args, **kwargs):
        started = time.perf_counter()
        try:
            return classify(*args, **kwargs)
        finally:
            classification_seconds[engine] += time.perf_counter() - started

    disable_tqdm = progress_callback is not None
    pbar = tqdm(all_files, desc="Sorting Documents", unit="doc", dynamic_ncols=True, disable=disable_tqdm)

    try:
        for idx, file_path in enumerate(pbar, start=1):
            if stop_event is not None and stop_event.is_set():
                print("\n[!] Run stopped by user. Saving partial progress...")
                break
            try:
                file_hash = compute_sha256(file_path)
            except Exception as e:
                audit.skip_file(file_path, f"UNREADABLE (hash failed: {type(e).__name__}: {e}) - file was NOT processed")
                log.warning(f"Unreadable file skipped: {file_path} ({e})")
                continue

            resolved_source = str(file_path.resolve())

            try:
                rel_parent = file_path.parent.resolve().relative_to(input_dir.resolve())
                rel_folder_str = "" if str(rel_parent) == "." else str(rel_parent)
            except Exception:
                rel_folder_str = file_path.parent.name
            rel_folder_str = strip_scanner_tool_folders(rel_folder_str)

            if file_hash in run_seen_hashes:
                orig = run_seen_hashes[file_hash]
                if orig != resolved_source:
                    duplicates_count += 1
                    duplicate_rows.append([file_hash, orig, resolved_source, datetime.now().isoformat()])
                    staged_items.append({
                        "file_path": file_path,
                        "resolved_source": resolved_source,
                        "relative_folder": rel_folder_str,
                        "file_hash": file_hash,
                        "previous_dest_str": "",
                        "classification": DocumentClassification(
                            category="Miscellaneous",
                            person_name=primary_person,
                            confidence=1.0,
                            expiration_status="none",
                            summary=f"Exact byte-for-byte duplicate of {Path(orig).name} (Skipped copying).",
                            reasoning="100% identical SHA-256 hash.",
                            resolved_by="Code"
                        ),
                        "ocr_preview": "(Exact Duplicate - Skipped)",
                        "possible_duplicate_of": f"EXACT DUP OF: {Path(orig).name}",
                        "possible_duplicate_path": orig,
                        "is_exact_duplicate": True
                    })
                    continue
            else:
                run_seen_hashes[file_hash] = resolved_source

            previous_dest_str = state.get(file_hash, {}).get("destination_path")
            doc_phash: Optional[str] = state.get(file_hash, {}).get("phash")
            doc_is_landscape: bool = bool(state.get(file_hash, {}).get("is_landscape", False))
            ocr_preview: str = state.get(file_hash, {}).get("ocr_preview", "")
            unlisted_candidates: List[str] = state.get(file_hash, {}).get("unlisted_candidates", [])
            skip_heavy_ocr = _in_kept(rel_folder_str) or should_skip_heavy_ocr(file_path, rel_folder_str, family_config)
            _file_t0 = time.time()
            use_cache = (
                not ignore_cache
                and file_hash in state
                and "classification" in state[file_hash]
                and state[file_hash].get("schema_v") == 9
                and state[file_hash].get("manual_vocabulary_signature", "") == manual_signature
            )
            if use_cache:
                cached_cls = DocumentClassification(**state[file_hash]["classification"])
                if cached_cls.resolved_by == "Code" and cached_cls.confidence < python_confidence_threshold:
                    use_cache = False

            if use_cache:
                phase_stats["cache_hits"] += 1
                classification = DocumentClassification(**state[file_hash]["classification"])
                exp_date, exp_status = calculate_expiration_status(
                    classification.expiration_date, classification.category, today
                )
                classification.expiration_date = exp_date
                classification.expiration_status = exp_status

                if classification.resolved_by == "Code":
                    code_resolved_count += 1
                else:
                    ollama_resolved_count += 1
            else:
                if resolved_source in pre_extracted_cache:
                    doc_data, _extract_dt = pre_extracted_cache.pop(resolved_source)
                else:
                 _ex_t0 = time.perf_counter()
                 t_start = time.time()
                 doc_data = extract_document_data(file_path, input_root=input_dir, skip_ocr=skip_heavy_ocr, ocr_max_pages=ocr_max_pages)
                 _extract_dt = time.perf_counter() - _ex_t0
                _ollama_dt = 0.0
                phase_stats["extract_seconds"] += _extract_dt
                phase_stats["extract_files"] += 1
                doc_phash = doc_data.get("phash")
                doc_is_landscape = bool(doc_data.get("is_landscape", False))
                ocr_preview = re.sub(r"\s+", " ", doc_data.get("first_text", "")[:220]).strip()

                should_skip, forced_cls = timed_classification(
                    "python", evaluate_exception_rules,
                    doc_data, exception_rules, family_config, primary_person, today
                )
                if should_skip:
                    continue

                if forced_cls is not None:
                    classification = forced_cls
                    code_resolved_count += 1
                else:
                    classification = timed_classification(
                        "python", classify_deterministically,
                        doc_data, learned_rules, family_config, primary_person, today,
                        min_python_confidence=python_confidence_threshold,
                        manual_rules=manual_rules,
                    )

                    if classification is not None:
                        code_resolved_count += 1
                    else:
                        py_backup = timed_classification(
                            "python", classify_deterministically,
                            doc_data, learned_rules, family_config, primary_person, today,
                            min_python_confidence=0.50,
                            manual_rules=manual_rules,
                        )
                        if py_backup is None and _in_kept(rel_folder_str):
                            # Kept folder: no point asking the AI, the folder is copied as-is
                            classification = DocumentClassification(
                                category="Miscellaneous", person_name=primary_person, confidence=1.0,
                                expiration_status="none", summary="File in a kept folder.",
                                reasoning="Kept folder: not classified.", resolved_by="Code"
                            )
                            code_resolved_count += 1
                        else:
                            _ol_before = classification_seconds["ollama"]
                            _ol_t0 = time.perf_counter()
                            classification = timed_classification(
                                "ollama", classify_with_ollama,
                                doc_data, family_config, primary_person, today, model_name=model_name, max_chars=max_chars
                            )
                            _ollama_dt = time.perf_counter() - _ol_t0
                            phase_stats["ollama_calls"] += 1
                            if "Inference fallback:" in (classification.reasoning or ""):
                                # failed AI call: not real AI work
                                phase_stats["ollama_fallbacks"] += 1
                                phase_stats["ollama_fallback_seconds"] += _ollama_dt
                                classification_seconds["ollama"] = _ol_before
                            if "Inference fallback:" in classification.reasoning and py_backup is not None:
                                classification = py_backup
                                code_resolved_count += 1
                            else:
                                ollama_resolved_count += 1

                        if (
                            classification.confidence >= 0.75
                            and classification.category not in {"Miscellaneous", "Photos & People"}
                        ):
                            kw_to_save = classification.suggested_keyword
                            if not kw_to_save:
                                # SDS-FIX-LEARNING: only learn a filename word if it also appears in the document text
                                _doc_txt = " " + normalize_for_matching(str(doc_data.get("text") or doc_data.get("first_text") or "")) + " "
                                fn_words = [w for w in normalize_for_matching(file_path.stem).split() if 5 <= len(w) <= 18 and w.isalpha() and w not in LEARNING_STOPWORDS and f" {w} " in _doc_txt]
                                kw_to_save = fn_words[0] if fn_words else None
                            if save_learned_keyword(kw_to_save, classification.category, family_config, LEARNING_CSV_PATH):
                                saved_term = " ".join(kw_to_save.lower().split())
                                learned_rules[saved_term] = classification.category
                                newly_learned_terms.append(saved_term)
                                newly_learned_count += 1

                if classification.person_name in {primary_person, "General"}:
                    unlisted_candidates = list(extract_unlisted_person_candidates(doc_data, family_config))

                phase_stats["per_file"].append((round(_extract_dt + _ollama_dt, 2), file_path.name,
                                                 round(_extract_dt, 2), round(_ollama_dt, 2)))
                state[file_hash] = {
                    "schema_v": 9,
                    "extract_seconds": round(_extract_dt, 3),
                    "ollama_seconds": round(_ollama_dt, 3),
                    "manual_vocabulary_signature": manual_signature,
                    "py_threshold": python_confidence_threshold,
                    "source_path": resolved_source,
                    "destination_path": previous_dest_str or "",
                    "processed_at": datetime.now().isoformat(),
                    "classification": classification.model_dump(),
                    "phash": doc_phash,
                    "is_landscape": doc_is_landscape,
                    "ocr_preview": ocr_preview,
                    "unlisted_candidates": unlisted_candidates,
                    "file_size": file_path.stat().st_size
                }
                unsaved_state_changes += 1
                if unsaved_state_changes >= STATE_SAVE_INTERVAL:
                    save_progress_state(state_file, state)
                    unsaved_state_changes = 0

            if classification.person_name in {primary_person, "General"} and unlisted_candidates:
                for cand in unlisted_candidates:
                    unlisted_name_docs.setdefault(cand, []).append(resolved_source)

            possible_duplicate_of = ""
            possible_duplicate_path = ""
            for prev_doc in seen_docs_for_neardup:
                if prev_doc["filename"] != file_path.name and _is_true_near_duplicate(
                    file_path.name, classification.person_name, classification.category, ocr_preview, doc_phash,
                    prev_doc["filename"], prev_doc["person_name"], prev_doc["category"], prev_doc["ocr_preview"], prev_doc["phash"]
                ):
                    possible_duplicate_of = f"NEAR-DUP OF: {prev_doc['filename']}"
                    possible_duplicate_path = prev_doc["source_path"]
                    break

            seen_docs_for_neardup.append({
                "filename": file_path.name,
                "source_path": resolved_source,
                "person_name": classification.person_name,
                "category": classification.category,
                "ocr_preview": ocr_preview,
                "phash": doc_phash
            })

            staged_items.append({
                "file_path": file_path,
                "resolved_source": resolved_source,
                "relative_folder": rel_folder_str,
                "file_hash": file_hash,
                "previous_dest_str": previous_dest_str,
                "classification": classification,
                "ocr_preview": ocr_preview,
                "possible_duplicate_of": possible_duplicate_of,
                "possible_duplicate_path": possible_duplicate_path,
                "is_exact_duplicate": False,
                "is_landscape": doc_is_landscape,
                "elapsed": time.time() - _file_t0,
                "from_cache": bool(use_cache)
            })
            audit.timing(resolved_source, time.time() - _file_t0, bool(use_cache))

            elapsed_so_far = time.time() - start_time
            speed = idx / elapsed_so_far if elapsed_so_far > 0 else 0.0
            remaining_docs = total_scanned - idx
            eta_sec = int(remaining_docs / speed) if speed > 0 else 0

            if idx % 5 == 0 or idx == total_scanned:
                update_progress_status(
                    status="PROCESSING",
                    current_index=idx,
                    total_files=total_scanned,
                    current_file=file_path.name,
                    resolved_by_code=code_resolved_count,
                    resolved_by_ollama=ollama_resolved_count,
                    rules_learned=newly_learned_count,
                    speed=speed,
                    eta_sec=eta_sec
                )

            if progress_callback:
                progress_callback(idx, total_scanned, file_path.name, speed, code_resolved_count, ollama_resolved_count)

            if not disable_tqdm:
                total_proc = code_resolved_count + ollama_resolved_count
                code_pct = int((code_resolved_count / total_proc) * 100) if total_proc > 0 else 0
                pbar.set_postfix({
                    "Code": f"{code_pct}%",
                    "Ollama": f"{100 - code_pct}%",
                    "Learned": newly_learned_count
                })

        # Merge vendor spelling variants (ABNA / ABN AMRO / Abn_Amro ...) into ONE vendor per category
        _pairs = [
            (it["classification"].category, it["classification"].vendor_group)
            for it in staged_items
            if not it.get("is_exact_duplicate") and it["classification"].vendor_group
        ]
        _vendor_map = unify_vendor_names(_pairs)
        for it in staged_items:
            _c = it["classification"]
            if not it.get("is_exact_duplicate") and _c.vendor_group:
                _c.vendor_group = _vendor_map.get((_c.category, _c.vendor_group), _c.vendor_group)

        # ---- Source-folder decisions: keep custom folders (e.g. GYM) intact instead of sorting them ----
        _folder_files = [
            (it["relative_folder"], it["classification"].category,
             it["classification"].confidence >= review_confidence_threshold)
            for it in staged_items if not it.get("is_exact_duplicate")
        ]
        folder_decisions = decide_folders(
            _folder_files, family_config,
            settings.get("folder_decisions"),
            int(settings.get("folder_keep_min_files", DEFAULT_MIN_FILES_FOR_FOLDER_DECISION))
        )
        if dry_run:
            save_folder_decisions({
                f.folder: {"files": f.file_count, "action": f.action, "reason": f.reason, "detail": f.detail}
                for f in folder_decisions.values()
            })
        for fd in folder_decisions.values():
            log.info(f"Folder decision: '{fd.folder}' ({fd.file_count} files) -> {fd.action.upper()} ({fd.reason})")
        for it in staged_items:
            _root = kept_root_for(it["relative_folder"], folder_decisions)
            if _root is not None:
                if it.get("is_exact_duplicate"):
                    duplicates_count -= 1          # inside a kept folder every file is kept, duplicates included
                    it["is_exact_duplicate"] = False
                it["preserved"] = True
                it["preserve_dest"] = preserved_destination(it["relative_folder"], _root, it["file_path"].name)
                it["folder_note"] = f"KEEP: {folder_decisions[_root].reason} ({_root})"

        # ---- Kept folders stay complete: unsupported / ignored-name files inside them are copied too ----
        _any_kept = any(f.preserved for f in folder_decisions.values())
        if _any_kept:
            for _p, _why in list(audit.scan_skipped):
                if not _why.startswith(("unsupported file type", "ignored filename")):
                    continue
                _fp = Path(_p)
                try:
                    _rp = _fp.parent.resolve().relative_to(input_dir.resolve())
                except Exception:
                    continue
                _rel_folder = strip_scanner_tool_folders("" if str(_rp) == "." else str(_rp))
                _root = kept_root_for(_rel_folder, folder_decisions)
                if _root is None:
                    continue
                try:
                    _h = compute_sha256(_fp)
                except Exception as e:
                    audit.skip_file(_fp, f"UNREADABLE (hash failed: {type(e).__name__}: {e}) - not copied with its kept folder")
                    continue
                _note = f"KEEP: file type not sortable, copied with its kept folder ({_root})"
                staged_items.append({
                    "file_path": _fp,
                    "resolved_source": str(_fp.resolve()),
                    "relative_folder": _rel_folder,
                    "file_hash": _h,
                    "previous_dest_str": "",
                    "classification": DocumentClassification(
                        category="Miscellaneous", person_name=primary_person, confidence=1.0,
                        expiration_status="none",
                        summary="Unsupported or non-sortable file kept inside its preserved folder.",
                        reasoning="Preserved folder: copied as-is.", resolved_by="Code"
                    ),
                    "ocr_preview": "(not read - copied with kept folder)",
                    "possible_duplicate_of": "",
                    "possible_duplicate_path": "",
                    "is_exact_duplicate": False,
                "is_landscape": doc_is_landscape,
                    "preserved": True,
                    "preserve_dest": preserved_destination(_rel_folder, _root, _fp.name),
                    "folder_note": _note,
                })
                audit.adopt(_p, _fp.resolve())

        # ---- Re-apply the user's saved Review edits (survive every new dry run) ----
        _manual = load_manual_edits()
        for it in staged_items:
            _e = _manual.get(it["file_hash"])
            if _e and not it.get("preserved") and not it.get("is_exact_duplicate"):
                _c = it["classification"]
                _c.category = _e.get("category") or _c.category
                _c.person_name = _e.get("person_name") or _c.person_name
                _c.confidence = 1.0
                it["manual"] = True

        # ---- Same counts for plan and apply (3-file rule) ----
        category_counts = _compute_family_category_counts([
            {
                "person_name": it["classification"].person_name,
                "category": it["classification"].category,
                "resolved_by": "Skipped (Exact Dup)" if it.get("is_exact_duplicate") else "",
                "preserved": it.get("preserved"),
            }
            for it in staged_items
        ], primary_person)

        vendor_doc_counts: Dict[str, int] = {}
        for item in staged_items:
            if item.get("is_exact_duplicate") or item.get("preserved"):
                continue
            cls: DocumentClassification = item["classification"]
            p_clean = sanitize_folder_name(cls.person_name or primary_person or "General")
            if cls.vendor_group:
                v_key = f"{p_clean}:{cls.category}:{cls.vendor_group}"
                vendor_doc_counts[v_key] = vendor_doc_counts.get(v_key, 0) + 1

        claimed_dests: set = set()   # destinations already written this run (never delete these as "previous copies")
        plan_records: List[Dict[str, Any]] = []
        for item in staged_items:
            file_path: Path = item["file_path"]
            file_hash: str = item["file_hash"]
            cls: DocumentClassification = item["classification"]
            previous_dest_str = item["previous_dest_str"]

            if item.get("is_exact_duplicate"):
                rel_dest = "(Skipped - Exact Duplicate)"
                if not dry_run:
                    audit.apply(item["resolved_source"], "SKIPPED_EXACT_DUPLICATE", item.get("possible_duplicate_of", ""))
            else:
                try:
                    mtime_ts = file_path.stat().st_mtime
                    time_trailer_str = datetime.fromtimestamp(mtime_ts).strftime("%H%M%S")
                except Exception:
                    time_trailer_str = datetime.now().strftime("%H%M%S")

                if item.get("preserved"):
                   destination = output_dir / (sanitize_folder_name(primary_person) if primary_person else "Main") / "Others" / item["preserve_dest"]   # kept folder: same name, same structure
                else:
                    destination = determine_destination(
                        output_dir, cls, file_path.name,
                        primary_person=primary_person,
                        review_threshold=review_confidence_threshold,
                        vendor_doc_counts=vendor_doc_counts,
                        auto_rename=auto_rename,
                        file_hash=file_hash,
                        time_trailer=time_trailer_str,
                        family_category_counts=category_counts,
                        source_folder=item["relative_folder"],
                        is_landscape=bool(item.get("is_landscape", False))
                    )

                if not dry_run:
                    if previous_dest_str:
                        prev_path = Path(previous_dest_str)
                        if (
                            prev_path.exists()
                            and prev_path.resolve() != destination.resolve()
                            and prev_path.resolve().is_relative_to(output_dir.resolve())
                            and prev_path.resolve() not in claimed_dests
                        ):
                            try:
                                prev_path.unlink()
                                audit.apply(item["resolved_source"], "REMOVED_PREVIOUS_COPY", str(prev_path))
                            except Exception:
                                pass

                    planned_dest = destination
                    final_dest, should_copy = resolve_collision(destination, file_hash)
                    try:
                        if should_copy and final_dest.resolve() != file_path.resolve():
                            final_dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(file_path, final_dest)
                            outcome = "COPIED_WITH_SUFFIX" if final_dest != planned_dest else "COPIED"
                        else:
                            outcome = "SKIPPED_IDENTICAL_FILE_ALREADY_THERE"
                        audit.apply(item["resolved_source"], outcome, str(final_dest))
                    except Exception as e:
                        audit.apply(item["resolved_source"], "ERROR_COPY_FAILED", f"{type(e).__name__}: {e}")
                        log.warning(f"Copy failed for {file_path}: {e}")
                    destination = final_dest
                    claimed_dests.add(final_dest.resolve())

                if file_hash in state:
                    state[file_hash]["destination_path"] = str(destination.resolve())

                rel_dest = str(destination.relative_to(output_dir) if destination.is_relative_to(output_dir) else destination)

            audit.plan(
                item["resolved_source"],
                f"{('PRESERVED FOLDER (' + item.get('folder_note', '') + ') | ') if item.get('preserved') else ''}{cls.category} | {cls.person_name} | vendor={cls.vendor_group or '-'} | "
                f"{'EXACT DUPLICATE' if item.get('is_exact_duplicate') else cls.resolved_by} "
                f"{int(cls.confidence * 100)}% | -> {rel_dest} | {(cls.reasoning or '')[:90]}"
            )
            plan_records.append({
                "filename": file_path.name,
                "relative_folder": item["relative_folder"],
                "source_path": item["resolved_source"],
                "destination": rel_dest,
                "category": cls.category,
                "person_name": cls.person_name,
                "vendor_group": cls.vendor_group or "",
                "document_date": cls.document_date or "",
                "expiration_date": cls.expiration_date or "",
                "expiration_status": cls.expiration_status,
                "confidence": cls.confidence,
                "possible_duplicate_of": item["possible_duplicate_of"],
                "possible_duplicate_path": item.get("possible_duplicate_path", ""),
                "ocr_preview": item["ocr_preview"],
                "summary": cls.summary,
                "tags": cls.tags,
                "reasoning": cls.reasoning,
                "resolved_by": "Skipped (Exact Dup)" if item.get("is_exact_duplicate") else cls.resolved_by,
                "sha256": file_hash,
                "is_landscape": bool(item.get("is_landscape", False)),
                "preserved": bool(item.get("preserved")),
                "preserve_dest": item.get("preserve_dest", ""),
                "folder_note": item.get("folder_note", ""),
                "manual_override": bool(item.get("manual"))
            })

    finally:
        save_progress_state(state_file, state)

    elapsed = time.time() - start_time
    fps = len(plan_records) / elapsed if elapsed > 0 else 0
    start_dt_str = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S")
    end_dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    update_progress_status(
        status="COMPLETED",
        current_index=total_scanned,
        total_files=total_scanned,
        current_file="All Done",
        resolved_by_code=code_resolved_count,
        resolved_by_ollama=ollama_resolved_count,
        rules_learned=newly_learned_count,
        speed=fps,
        eta_sec=0
    )

    CURRENT_RUN_DIR.mkdir(parents=True, exist_ok=True)
    write_plan_artifacts(INTERNAL_DATA_DIR, plan_records)
    write_plan_artifacts(CURRENT_RUN_DIR, plan_records)

    if dry_run:
        audit.save_scan(INTERNAL_DATA_DIR / "scan_audit.json")
    write_debug_report(
        output_dir=CURRENT_RUN_DIR,
        mode_str="DRY-RUN PLAN (--plan)" if dry_run else "ACTIVE APPLY (--apply)",
        start_dt=start_dt_str,
        end_dt=end_dt_str,
        elapsed=elapsed,
        records=plan_records,
        duplicate_rows=duplicate_rows,
        py_threshold=python_confidence_threshold,
        rev_threshold=review_confidence_threshold
    )

    atomic_write_json(latest_vocabulary_file, newly_learned_terms)

    recurring_new_names = {
        name: docs for name, docs in unlisted_name_docs.items()
        if len(docs) >= RECURRING_NAME_MIN_DOCS and name not in family_config
    }

    log.info(
        f"Run complete — {len(plan_records)} docs processed in {elapsed:.1f}s "
        f"(code={code_resolved_count}, ollama={ollama_resolved_count}, "
        f"dupes={duplicates_count}, learned={newly_learned_count})"
    )
    _slowest = sorted(phase_stats["per_file"], reverse=True)[:10]
    log.info(
        f"Timing — extraction/OCR {phase_stats['extract_seconds']:.1f}s over {phase_stats['extract_files']} files, "
        f"python {classification_seconds['python']:.1f}s, "
        f"ollama {classification_seconds['ollama']:.1f}s over "
        f"{phase_stats['ollama_calls'] - phase_stats['ollama_fallbacks']} real calls, "
        f"fallbacks {phase_stats['ollama_fallbacks']} ({phase_stats['ollama_fallback_seconds']:.1f}s), "
        f"cache hits {phase_stats['cache_hits']}"
    )
    for _t, _n, _e, _o in _slowest:
        log.info(f"  slow: {_t:6.2f}s  (extract {_e:.2f}s, ollama {_o:.2f}s)  {_n}")

    return {
        "total": len(plan_records),
        "code": code_resolved_count,
        "ollama": ollama_resolved_count,
        "duplicates": duplicates_count,
        "learned": newly_learned_count,
        "elapsed": elapsed,
        "python_classification_seconds": classification_seconds["python"],
        "ollama_classification_seconds": classification_seconds["ollama"],
        "extraction_seconds": round(phase_stats["extract_seconds"], 2),
        "extracted_files": phase_stats["extract_files"],
        "cache_hits": phase_stats["cache_hits"],
        "ollama_calls": phase_stats["ollama_calls"],
        "ollama_fallbacks": phase_stats["ollama_fallbacks"],
        "ollama_fallback_seconds": round(phase_stats["ollama_fallback_seconds"], 2),
        "slowest_files": [{"file": n, "total": t, "extract": e, "ollama": o}
                          for t, n, e, o in _slowest],
        "start_time": start_dt_str,
        "end_time": end_dt_str,
        "debug_report": str((CURRENT_RUN_DIR / "debug_run_report.txt").resolve()),
        "folder_decisions": {
            f.folder: {"files": f.file_count, "action": f.action, "reason": f.reason, "detail": f.detail}
            for f in folder_decisions.values()
        },
        "recurring_new_names": recurring_new_names
    }
