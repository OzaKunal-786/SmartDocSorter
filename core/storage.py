"""
core/storage.py - Atomic JSON/CSV I/O, cache management, file copying and folder organization logic.
Encapsulates all filesystem writes, collision resolutions, and plan reads/applies.
"""

import os
import json
import time
import shutil
import re
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Callable

from core.config import (
    OLD_FILENAME_RE,
    WORK_CATEGORIES,
    normalize_for_matching,
    USER_SETTINGS_PATH,
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PYTHON_CONFIDENCE_THRESHOLD,
    DEFAULT_REVIEW_CONFIDENCE_THRESHOLD,
    RUNTIME_ROOT_DIR,
    CURRENT_RUN_DIR,
    INTERNAL_DATA_DIR,
    PROGRESS_STATUS_PATH,
    MIN_DOCS_FOR_VENDOR_CLUBBING,
    VALID_CATEGORIES,
    IGNORED_DIRS,
    IGNORED_FILENAMES,
    SUPPORTED_EXTENSIONS
)
from core.classifier import DocumentClassification
from core.deduplicator import compute_sha256
from core.person_matcher import unify_vendor_names
from core.run_audit import audit
from core.reporter import write_debug_report
from core.logger import get_logger
from core.renamer import generate_standardized_filename

log = get_logger("storage")

# Category -> pillar folder for the main person
PILLAR_FOR = {
    "Banking": "Finance", "Invoices": "Finance", "Taxes": "Finance",
    "Identity": "Identity", "Insurance": "Insurance",
    "Real Estate": "Housing & Vehicles", "Vehicles": "Housing & Vehicles",
    "Medical": "Health", "Education": "Education", "Miscellaneous": "Miscellaneous",
}
MANUAL_EDITS_PATH = INTERNAL_DATA_DIR / "manual_edits.json"


def load_manual_edits() -> Dict[str, Any]:
    try:
        return json.loads(MANUAL_EDITS_PATH.read_text(encoding="utf-8")) if MANUAL_EDITS_PATH.exists() else {}
    except Exception:
        return {}


# ==============================================================================
# ATOMIC FILE HELPER & USER SETTINGS
# ==============================================================================
def atomic_write_json(target_path: Path, data: Any):
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        for attempt in range(3):
            try:
                temp_path.replace(target_path)
                return
            except PermissionError:
                time.sleep(0.08 * (attempt + 1))
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_user_settings(settings_path: Path = USER_SETTINGS_PATH) -> Dict[str, Any]:
    default_settings = {
        "input_dir": DEFAULT_INPUT_DIR,
        "output_dir": DEFAULT_OUTPUT_DIR,
        "python_confidence_threshold": DEFAULT_PYTHON_CONFIDENCE_THRESHOLD,
        "review_confidence_threshold": DEFAULT_REVIEW_CONFIDENCE_THRESHOLD,
        "primary_person": "",
        "family_config": {},
        "exception_rules": [],
        "auto_rename": True,
        "sorting_preset": "balanced"
    }
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                default_settings.update(loaded)
        except Exception as e:
            log.warning(f"Could not load user_settings.json: {e}. Using defaults.")

    fam = default_settings.get("family_config", {})
    if not default_settings.get("primary_person") and fam:
        default_settings["primary_person"] = next(iter(fam.keys()))

    return default_settings


def save_user_settings(settings: Dict[str, Any], settings_path: Path = USER_SETTINGS_PATH):
    atomic_write_json(settings_path, settings)


# ==============================================================================
# PROGRESS STATUS & STATE MANAGEMENT
# ==============================================================================
def update_progress_status(
    status: str,
    current_index: int,
    total_files: int,
    current_file: str,
    resolved_by_code: int,
    resolved_by_ollama: int,
    rules_learned: int,
    speed: float,
    eta_sec: int,
    status_path: Path = PROGRESS_STATUS_PATH
):
    pct = round((current_index / total_files * 100), 1) if total_files > 0 else 0.0
    payload = {
        "status": status,
        "current_index": current_index,
        "total_files": total_files,
        "percentage": pct,
        "current_file": current_file,
        "speed_docs_per_sec": round(speed, 2),
        "eta_seconds": max(0, eta_sec),
        "resolved_by_code": resolved_by_code,
        "resolved_by_ollama": resolved_by_ollama,
        "rules_learned": rules_learned,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    atomic_write_json(status_path, payload)


def load_progress_state(state_file: Path) -> Dict[str, Any]:
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_progress_state(state_file: Path, state_data: Dict[str, Any]):
    atomic_write_json(state_file, state_data)


# ==============================================================================
# DIRECTORY SCANNING & CLEANING
# ==============================================================================
def _count_files(folder: Path, cap: int = 20000) -> int:
    n = 0
    for _, _, fs in os.walk(folder):
        n += len(fs)
        if n >= cap:
            break
    return n


def scan_and_summarize_files(input_dir: Path) -> Tuple[List[Path], Dict[str, int], int]:
    all_files: List[Path] = []
    ext_counts: Dict[str, int] = {}
    ignored_count = 0

    for root, dirs, files in os.walk(input_dir):
        kept = []
        for d in dirs:
            if d.startswith("."):
                audit.skip_dir(Path(root) / d, _count_files(Path(root) / d), "hidden folder")
            elif d.lower() in IGNORED_DIRS:
                audit.skip_dir(Path(root) / d, _count_files(Path(root) / d), "ignored folder")
            else:
                kept.append(d)
        dirs[:] = kept

        for f in files:
            audit.seen()
            p = Path(root) / f
            if f.lower() in IGNORED_FILENAMES or f.lower().startswith("readme"):
                ignored_count += 1
                audit.skip_file(p, "ignored filename")
                continue
            ext = p.suffix.lower()
            if p.name.startswith("~$") or p.name.startswith("."):
                ignored_count += 1
                audit.skip_file(p, "temporary or hidden file")
            elif ext in SUPPORTED_EXTENSIONS:
                all_files.append(p)
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                audit.queue(p.resolve())
            else:
                ignored_count += 1
                audit.skip_file(p, f"unsupported file type '{ext}'")

    return all_files, ext_counts, ignored_count


def clean_output_directory(output_dir: Path, input_dir: Path):
    if not output_dir.exists():
        return
    if output_dir.resolve() == input_dir.resolve() or input_dir.resolve().is_relative_to(output_dir.resolve()):
        raise ValueError("Safety abort: Output directory overlaps with Input directory.")

    for child in output_dir.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except Exception:
            pass


def sanitize_folder_name(name: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*]', '_', name.strip())
    return clean if clean else "General"


# ==============================================================================
# DESTINATION ROUTING & COLLISION RESOLUTION
# ==============================================================================
def determine_destination(
    output_dir: Path,
    classification: DocumentClassification,
    original_filename: str,
    primary_person: str = "",
    review_threshold: float = DEFAULT_REVIEW_CONFIDENCE_THRESHOLD,
    vendor_doc_counts: Optional[Dict[str, int]] = None,
    auto_rename: bool = False,
    file_hash: Optional[str] = None,
    time_trailer: Optional[str] = None,
    family_category_counts: Optional[Dict[str, int]] = None,
    source_folder: str = "",
    is_landscape: bool = False
) -> Path:
    category = classification.category or "Miscellaneous"
    person = sanitize_folder_name(classification.person_name or primary_person or "General")
    primary_clean = sanitize_folder_name(primary_person) if primary_person else ""
    is_primary = (not primary_clean) or (person.lower() in {primary_clean.lower(), "general"})

    ext = Path(original_filename).suffix.lower()
    is_image = ext in {".jpg", ".jpeg", ".png"}

    if auto_rename and is_image and category != "Photos & People":
        final_filename = generate_standardized_filename(
            original_filename=original_filename,
            category=category,
            person_name=classification.person_name,
            document_date=classification.document_date,
            vendor_group=classification.vendor_group,
            primary_person=primary_person,
            file_hash=file_hash,
            time_trailer=time_trailer
        )
    else:
        final_filename = original_filename

    is_old = bool(OLD_FILENAME_RE.search(normalize_for_matching(Path(original_filename).stem)))
    main_dir = output_dir / (primary_clean or "Main")
    key_person = primary_clean if is_primary else person
    n = (family_category_counts or {}).get(f"{key_person}:{category}", 0)

    # Photos: always a Photos folder (a photoshoot folder keeps its own subfolder)
    if category == "Photos & People":
        base = output_dir / "Photos"
        last = [p for p in re.split(r"[\\/]+", source_folder or "") if p]
        if last and "shoot" in last[-1].lower():
            base = base / sanitize_folder_name(last[-1])
        return base / final_filename

    if not is_primary:
        pillar_root, pillar_name = output_dir / "Family" / person, ""
    elif category in WORK_CATEGORIES:
        pillar_root, pillar_name = output_dir / "Work", "Work"
    else:
        pillar_name = PILLAR_FOR.get(category, "Miscellaneous")
        pillar_root = main_dir / pillar_name

    if is_primary and classification.confidence < review_threshold:
        return main_dir / "_NEEDS_REVIEW" / final_filename

    folder = pillar_root / category if (n >= 3 and category != pillar_name) else pillar_root
    if is_old:
        return folder / "Archive" / final_filename

    vendor = classification.vendor_group
    if (is_primary and vendor and vendor_doc_counts is not None
            and vendor_doc_counts.get(f"{person}:{category}:{vendor}", 0) >= MIN_DOCS_FOR_VENDOR_CLUBBING):
        folder = folder / sanitize_folder_name(vendor)
    return folder / final_filename


def resolve_collision(target_path: Path, source_hash: str) -> Tuple[Path, bool]:
    if not target_path.exists():
        return target_path, True
    try:
        if compute_sha256(target_path) == source_hash:
            return target_path, False
    except Exception:
        return target_path, False

    for n in range(2, 1000):
        cand = target_path.with_name(f"{target_path.stem}_{n}{target_path.suffix}")
        if not cand.exists():
            return cand, True
        try:
            if compute_sha256(cand) == source_hash:
                return cand, False
        except Exception:
            continue
    return target_path, False


# ==============================================================================
# PLAN ARTIFACTS I/O
# ==============================================================================
def write_plan_artifacts(internal_dir: Path, records: List[Dict[str, Any]]):
    internal_dir.mkdir(parents=True, exist_ok=True)
    sort_plan_json = internal_dir / "sort_plan.json"
    atomic_write_json(sort_plan_json, records)

    plan_csv = internal_dir / "sorting_plan.csv"
    with open(plan_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Filename", "Source Path", "Planned Destination", "Category",
            "Person Name", "Vendor / Series", "Document Date", "Expiration Date", "Expiration Status",
            "Confidence", "Resolved By", "Possible Duplicate Of", "OCR Text Preview", "Summary", "Reasoning", "SHA256"
        ])
        for r in records:
            writer.writerow([
                r["filename"], r["source_path"], r["destination"], r["category"],
                r["person_name"], r.get("vendor_group") or "", r["document_date"],
                r["expiration_date"], r["expiration_status"],
                f"{float(r['confidence']):.2f}", r["resolved_by"], r.get("possible_duplicate_of", ""),
                r.get("ocr_preview", ""), r["summary"], r["reasoning"], r["sha256"]
            ])


def load_plan_with_manual_edits(internal_dir: Path) -> Optional[List[Dict[str, Any]]]:
    sort_plan_json = internal_dir / "sort_plan.json"
    sorting_plan_csv = internal_dir / "sorting_plan.csv"

    if not sort_plan_json.exists():
        return None

    try:
        with open(sort_plan_json, "r", encoding="utf-8") as f:
            records: List[Dict[str, Any]] = json.load(f)
    except Exception:
        return None

    if sorting_plan_csv.exists():
        try:
            csv_edits_by_source: Dict[str, Dict[str, str]] = {}
            with open(sorting_plan_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    src = (row.get("Source Path") or "").strip()
                    if src:
                        csv_edits_by_source[src] = row

            for r in records:
                src = r.get("source_path", "")
                if src in csv_edits_by_source:
                    row = csv_edits_by_source[src]
                    edited_cat = (row.get("Category") or "").strip()
                    edited_person = (row.get("Person Name") or "").strip()
                    edited_vendor = (row.get("Vendor / Series") or "").strip()
                    if edited_cat in VALID_CATEGORIES:
                        r["category"] = edited_cat
                    if edited_person:
                        r["person_name"] = edited_person
                    if edited_vendor:
                        r["vendor_group"] = edited_vendor
        except Exception:
            pass

    return records


def save_folder_decisions(decisions: Dict[str, Any]) -> None:
    atomic_write_json(INTERNAL_DATA_DIR / "folder_decisions.json", decisions)


def load_folder_decisions() -> Dict[str, Any]:
    path = INTERNAL_DATA_DIR / "folder_decisions.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def load_plan_for_review() -> List[Dict[str, Any]]:
    return load_plan_with_manual_edits(INTERNAL_DATA_DIR) or []


def save_plan_edits(edits: Dict[str, Dict[str, str]]) -> int:
    records = load_plan_with_manual_edits(INTERNAL_DATA_DIR)
    if not records:
        return 0
    changed = 0
    store = load_manual_edits()
    for r in records:
        e = edits.get(r.get("source_path", ""))
        if not e:
            continue
        cat = (e.get("category") or "").strip()
        person = (e.get("person_name") or "").strip()
        r["manual_override"] = True
        if r.get("sha256"):
            store[r["sha256"]] = {
                "category": cat or r.get("category"),
                "person_name": person or r.get("person_name"),
            }
        if cat in VALID_CATEGORIES and cat != r.get("category"):
            r["category"] = cat
            changed += 1
        if person and person != r.get("person_name"):
            r["person_name"] = person
            changed += 1
    write_plan_artifacts(INTERNAL_DATA_DIR, records)
    atomic_write_json(MANUAL_EDITS_PATH, store)
    return changed


def _unify_record_vendors(records: List[Dict[str, Any]]) -> None:
    live = [r for r in records if r.get("resolved_by") != "Skipped (Exact Dup)" and r.get("vendor_group")]
    vmap = unify_vendor_names([(r.get("category") or "Miscellaneous", r["vendor_group"]) for r in live])
    for r in live:
        key = (r.get("category") or "Miscellaneous", r["vendor_group"])
        r["vendor_group"] = vmap.get(key, r["vendor_group"])


def _compute_vendor_counts(records: List[Dict[str, Any]]) -> Dict[str, int]:
    vendor_counts: Dict[str, int] = {}
    for r in records:
        if r.get("resolved_by") == "Skipped (Exact Dup)" or r.get("preserved"):
            continue
        p = sanitize_folder_name(r.get("person_name") or "General")
        c = r.get("category") or "Miscellaneous"
        v = r.get("vendor_group")
        if v:
            key = f"{p}:{c}:{v}"
            vendor_counts[key] = vendor_counts.get(key, 0) + 1
    return vendor_counts


def _compute_family_category_counts(records: List[Dict[str, Any]], primary_person: str) -> Dict[str, int]:
    """Counts documents per person and category (main person included)."""
    counts: Dict[str, int] = {}
    primary_clean = sanitize_folder_name(primary_person) if primary_person else ""
    for r in records:
        if r.get("resolved_by") == "Skipped (Exact Dup)" or r.get("preserved"):
            continue
        person = sanitize_folder_name(r.get("person_name") or primary_person or "General")
        if (not primary_clean) or person.lower() in {primary_clean.lower(), "general"}:
            person = primary_clean
        key = f"{person}:{r.get('category') or 'Miscellaneous'}"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ==============================================================================
# TWO-STAGE APPLY & RECURRING NAME PROMOTION
# ==============================================================================
def _plan_text(r: Dict[str, Any]) -> str:
    return (
        f"{r.get('category','?')} | {r.get('person_name','?')} | vendor={r.get('vendor_group') or '-'} | "
        f"{r.get('resolved_by','?')} {int(float(r.get('confidence',0))*100)}% | -> {r.get('destination','')} | "
        f"{(r.get('reasoning') or '')[:90]}"
    )


def apply_existing_plan(
    output_dir: Path,
    input_dir: Path,
    primary_person: str = "",
    review_threshold: float = DEFAULT_REVIEW_CONFIDENCE_THRESHOLD,
    clean_before_run: bool = False,
    auto_rename: bool = True,
    progress_callback: Optional[Callable[[int, int, str, float, int, int], None]] = None,
    stop_event: Optional[Any] = None
) -> Optional[Dict[str, Any]]:
    records = load_plan_with_manual_edits(INTERNAL_DATA_DIR)
    if not records:
        return None

    if clean_before_run:
        clean_output_directory(output_dir, input_dir)

    start_time = time.time()
    start_dt_str = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S")
    state_file = INTERNAL_DATA_DIR / "progress_state.json"
    state = load_progress_state(state_file)

    _unify_record_vendors(records)
    claimed_dests: set = set()
    vendor_counts = _compute_vendor_counts(records)
    family_counts = _compute_family_category_counts(records, primary_person)

    total = len(records)
    code_cnt = sum(1 for r in records if r.get("resolved_by") == "Code")
    ollama_cnt = sum(1 for r in records if r.get("resolved_by") == "Ollama")
    dup_cnt = sum(1 for r in records if r.get("resolved_by") == "Skipped (Exact Dup)")

    for idx, r in enumerate(records, start=1):
        if stop_event is not None and stop_event.is_set():
            break
        _src_key = str(Path(r.get("source_path", "")).resolve()) if r.get("source_path") else ""
        audit.plan_if_missing(_src_key, _plan_text(r))
        if r.get("resolved_by") == "Skipped (Exact Dup)":
            audit.apply(_src_key, "SKIPPED_EXACT_DUPLICATE", r.get("possible_duplicate_of", ""))
            continue

        src_path = Path(r["source_path"])
        if not src_path.exists():
            audit.apply(_src_key, "SOURCE_MISSING", "source file no longer exists")
            continue

        file_hash = r.get("sha256") or compute_sha256(src_path)
        temp_cls = DocumentClassification(
            category=r["category"] if r["category"] in VALID_CATEGORIES else "Miscellaneous",
            person_name=r["person_name"] or primary_person or "General",
            document_date=r.get("document_date") or None,
            expiration_date=r.get("expiration_date") or None,
            expiration_status=r.get("expiration_status") if r.get("expiration_status") in {"valid", "expiring_soon", "expired", "none"} else "none",
            confidence=1.0 if r.get("manual_override") else float(r.get("confidence", 0.90)),
            summary=r.get("summary", ""),
            tags=r.get("tags", []),
            reasoning=r.get("reasoning", ""),
            vendor_group=r.get("vendor_group") or None,
            resolved_by=r.get("resolved_by", "Code")
        )

        try:
            mtime_ts = src_path.stat().st_mtime
            time_trailer_str = datetime.fromtimestamp(mtime_ts).strftime("%H%M%S")
        except Exception:
            time_trailer_str = datetime.now().strftime("%H%M%S")

        if r.get("preserved"):
            target_dest = output_dir / (sanitize_folder_name(primary_person) if primary_person else "Main") / "Others" / r["preserve_dest"]
        else:
            target_dest = determine_destination(
                output_dir, temp_cls, src_path.name,
                primary_person=primary_person,
                review_threshold=review_threshold,
                vendor_doc_counts=vendor_counts,
                auto_rename=auto_rename,
                file_hash=file_hash,
                time_trailer=time_trailer_str,
                family_category_counts=family_counts,
                source_folder=r.get("relative_folder", ""),
                is_landscape=bool(r.get("is_landscape", False))
            )

        prev_dest_str = state.get(file_hash, {}).get("destination_path")
        if prev_dest_str:
            prev_path = Path(prev_dest_str)
            if (
                prev_path.exists()
                and prev_path.resolve() != target_dest.resolve()
                and prev_path.resolve().is_relative_to(output_dir.resolve())
                and prev_path.resolve() not in claimed_dests
            ):
                try:
                    prev_path.unlink()
                    audit.apply(_src_key, "REMOVED_PREVIOUS_COPY", str(prev_path))
                except Exception:
                    pass

        planned_rel = r.get("destination", "")
        final_dest, should_copy = resolve_collision(target_dest, file_hash)
        try:
            if should_copy and final_dest.resolve() != src_path.resolve():
                final_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, final_dest)
                outcome = "COPIED_WITH_SUFFIX" if final_dest != target_dest else "COPIED"
            else:
                outcome = "SKIPPED_IDENTICAL_FILE_ALREADY_THERE"
            audit.apply(_src_key, outcome, f"{final_dest}  (planned: {planned_rel})")
        except Exception as e:
            audit.apply(_src_key, "ERROR_COPY_FAILED", f"{type(e).__name__}: {e}")
            log.warning(f"Copy failed for {src_path}: {e}")
            continue

        claimed_dests.add(final_dest.resolve())
        rel_dest = final_dest.relative_to(output_dir) if final_dest.is_relative_to(output_dir) else final_dest
        r["destination"] = str(rel_dest)

        if file_hash in state:
            state[file_hash]["destination_path"] = str(final_dest.resolve())
            state[file_hash]["classification"] = temp_cls.model_dump()

        elapsed_so_far = max(0.001, time.time() - start_time)
        speed = idx / elapsed_so_far
        if progress_callback:
            progress_callback(idx, total, src_path.name, speed, code_cnt, ollama_cnt)

    save_progress_state(state_file, state)
    write_plan_artifacts(INTERNAL_DATA_DIR, records)

    CURRENT_RUN_DIR.mkdir(parents=True, exist_ok=True)
    elapsed = time.time() - start_time
    end_dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_debug_report(
        output_dir=CURRENT_RUN_DIR,
        mode_str="ACTIVE APPLY (--apply)",
        start_dt=start_dt_str,
        end_dt=end_dt_str,
        elapsed=elapsed,
        records=records,
        duplicate_rows=[],
        py_threshold=DEFAULT_PYTHON_CONFIDENCE_THRESHOLD,
        rev_threshold=review_threshold
    )

    return {
        "total": total,
        "code": code_cnt,
        "ollama": ollama_cnt,
        "duplicates": dup_cnt,
        "learned": 0,
        "elapsed": elapsed,
        "start_time": start_dt_str,
        "end_time": end_dt_str,
        "debug_report": str((CURRENT_RUN_DIR / "debug_run_report.txt").resolve()),
        "recurring_new_names": {}
    }


def promote_recurring_names_in_plan(
    output_dir: Path,
    approved_names: List[str],
    candidate_doc_map: Dict[str, List[str]],
    review_threshold: float = DEFAULT_REVIEW_CONFIDENCE_THRESHOLD
):
    if not approved_names:
        return

    settings = load_user_settings()
    primary_person = settings.get("primary_person", "")
    auto_rename = settings.get("auto_rename", True)
    family_config = settings.setdefault("family_config", {})
    for name in approved_names:
        if name not in family_config:
            family_config[name] = [name]
    save_user_settings(settings)

    records = load_plan_with_manual_edits(INTERNAL_DATA_DIR)
    if not records:
        return

    source_to_new_person: Dict[str, str] = {}
    for name in approved_names:
        for src in candidate_doc_map.get(name, []):
            source_to_new_person[src] = name

    for r in records:
        src = r.get("source_path", "")
        if src in source_to_new_person:
            r["person_name"] = source_to_new_person[src]

    _unify_record_vendors(records)
    vendor_counts = _compute_vendor_counts(records)
    family_counts = _compute_family_category_counts(records, primary_person)

    for r in records:
        if r.get("resolved_by") == "Skipped (Exact Dup)" or r.get("preserved"):
            continue
        temp_cls = DocumentClassification(
            category=r["category"],
            person_name=r["person_name"],
            document_date=r.get("document_date") or None,
            expiration_date=r.get("expiration_date") or None,
            expiration_status=r.get("expiration_status", "none"),
            confidence=1.0 if r.get("manual_override") else float(r.get("confidence", 0.90)),
            summary=r.get("summary", ""),
            tags=r.get("tags", []),
            reasoning=r.get("reasoning", ""),
            vendor_group=r.get("vendor_group") or None,
            resolved_by=r.get("resolved_by", "Code")
        )
        new_dest = determine_destination(
            output_dir, temp_cls, r["filename"],
            primary_person=primary_person,
            review_threshold=review_threshold,
            vendor_doc_counts=vendor_counts,
            auto_rename=auto_rename,
            file_hash=r.get("sha256"),
            family_category_counts=family_counts,
            source_folder=r.get("relative_folder", "")
        )
        rel_dest = new_dest.relative_to(output_dir) if new_dest.is_relative_to(output_dir) else new_dest
        r["destination"] = str(rel_dest)

    write_plan_artifacts(INTERNAL_DATA_DIR, records)
