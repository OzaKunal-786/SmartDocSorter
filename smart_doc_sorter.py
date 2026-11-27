#!/usr/bin/env python3
"""
Smart Document Sorter - CLI Entry Point & Backward-Compatible Facade.
Thin wrapper delegating all concerns to modular sub-packages in core/.
100% backward compatible with existing batch files, CLI scripts, and run_gui.py.
"""

import sys
import argparse
from pathlib import Path

# Re-export all symbols from core for complete backward compatibility
from core import *
from core.config import *
from core.extractor import *
from core.rules import *
from core.deduplicator import *
from core.person_matcher import *
from core.classifier import *
from core.storage import *
from core.reporter import *
from core.pipeline import *


def main():
    setup_logger()
    settings = load_user_settings()

    parser = argparse.ArgumentParser(description="Smart Document Sorter - Two-Stage Offline Organization Engine")
    parser.add_argument("--input", "-i", type=str, default=settings.get("input_dir", DEFAULT_INPUT_DIR))
    parser.add_argument("--output", "-o", type=str, default=settings.get("output_dir", DEFAULT_OUTPUT_DIR))
    parser.add_argument("--plan", "--dry-run", dest="plan_mode", action="store_true", help="Stage 1: Build plan and review_dashboard.html without copying")
    parser.add_argument("--apply", action="store_true", help="Stage 2: Apply plan and copy files immediately")
    parser.add_argument("--clean", action="store_true", help="Clean output folder before active sorting")
    parser.add_argument("--ignore-cache", action="store_true", help="Re-evaluate all files ignoring cached state")
    parser.add_argument("--preset", choices=["balanced", "strict", "fast_relaxed"], default=None,
                        help="Sorting preset (default: sorting_preset from user_settings.json)")
    parser.add_argument("--python-confidence", type=float, default=None, help="Advanced: override preset threshold")
    parser.add_argument("--review-confidence", type=float, default=None, help="Advanced: override preset threshold")
    parser.add_argument("--model", "-m", type=str, default=MODEL_NAME)
    parser.add_argument("--limit", "-l", type=int, default=None)
    
    # Performance & Speed Optimization Flags
    parser.add_argument("--fast", action="store_true", help="Enable fast mode: truncate text payload and limit OCR pages")
    parser.add_argument("--max-chars", type=int, default=2000, help="Max characters sent to LLM for classification (default: 2000)")
    parser.add_argument("--ocr-max-pages", type=int, default=2, help="Max pages to run OCR on for scanned PDFs (default: 2)")

    args = parser.parse_args()
    if args.preset:
        settings["sorting_preset"] = args.preset
        save_user_settings(settings)
    dry_run = False if args.apply else True
    out_dir = Path(args.output)

    run_kwargs = dict(
        input_dir=Path(args.input),
        output_dir=out_dir,
        dry_run=dry_run,
        model_name=args.model,
        limit=args.limit,
        python_confidence_threshold=args.python_confidence,
        review_confidence_threshold=args.review_confidence,
        family_config=settings.get("family_config", DEFAULT_FAMILY_CONFIG),
        exception_rules=settings.get("exception_rules", []),
        clean_before_run=args.clean,
        ignore_cache=args.ignore_cache,
        # Performance parameters passed to core pipeline
        fast_mode=args.fast,
        max_chars=args.max_chars,
        ocr_max_pages=args.ocr_max_pages
    )
    summary = run_sorter(**run_kwargs)

    asks = {k: v for k, v in summary.get("folder_decisions", {}).items() if v.get("action") == "ask"}
    if dry_run and asks and sys.stdin.isatty():
        print("\n[!] Topic folders detected - keep each one exactly as it is?  (n = sort its files)")
        choices = settings.setdefault("folder_decisions", {})
        for fname, d in asks.items():
            ans = input(f"    -> '{fname}' ({d['files']} files: {d.get('detail', '')}). Keep as-is? [y/N]: ").strip().lower()
            choices[fname] = "keep" if ans == "y" else "sort"
        save_user_settings(settings)
        print("    [+] Saved. Rebuilding the plan with your answers...")
        summary = run_sorter(**run_kwargs)

    recurring = summary.get("recurring_new_names", {})
    if dry_run and recurring and sys.stdin.isatty():
        print("\n[!] Recurring New Names Detected in Primary/General Documents:")
        approved = []
        for cand_name, doc_list in recurring.items():
            ans = input(f"    -> Found '{cand_name}' in {len(doc_list)} documents. Create Family folder & add to Family list? [y/N]: ").strip().lower()
            if ans == "y":
                approved.append(cand_name)
        if approved:
            promote_recurring_names_in_plan(
                out_dir, approved, recurring,
                args.review_confidence if args.review_confidence is not None
                else SORTING_PRESETS[resolve_preset(settings.get("sorting_preset"))]["rev_conf"]
            )
            print(f"    [+] Updated plan with {len(approved)} new family folder(s)!")


if __name__ == "__main__":
    main()