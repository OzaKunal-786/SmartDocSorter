"""
core/reporter.py - Diagnostic text reports, per-file CSV audit output, and HTML review dashboards.
"""

import csv
from pathlib import Path
from typing import List, Dict, Any, Optional

from core.config import CURRENT_RUN_DIR
from core.extractor import rapid_ocr
from core.run_audit import audit
from core.logger import get_logger

log = get_logger("reporter")

# SDS-FIX-REPORTING: detect files labelled Ollama that were really fallbacks
_FALLBACK_HINTS = ("fallback", "not found", "unavailable", "error", "status code")


def _is_fallback(r) -> bool:
    eng = str(r.get("resolved_by", "")).lower()
    why = str(r.get("reasoning", "")).lower()
    return "ollama" in eng and any(h in why for h in _FALLBACK_HINTS)


def _engine_label(r) -> str:
    return "Fallback (AI unavailable)" if _is_fallback(r) else str(r.get("resolved_by", "Unknown"))


def generate_review_dashboard(
    records: List[Dict[str, Any]],
    output_dir: Optional[Path] = None,
    review_threshold: float = 0.85,
    primary_person: str = "",
    *args,
    **kwargs
) -> Path:
    """
    Generates an HTML review dashboard summarizing sorted documents, needs-review items,
    and confidence breakdowns, writing it to disk and returning the Path.
    """
    out_dir = output_dir if output_dir else CURRENT_RUN_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    
    needs_review = [r for r in records if "_NEEDS_REVIEW" in str(r.get("destination", "")).replace("\\", "/")]
    
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>SmartDocSorter Review Dashboard</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #f4f6f8; color: #333; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        h1 {{ color: #1a73e8; margin-top: 0; }}
        .stats {{ display: flex; gap: 20px; margin-bottom: 30px; }}
        .card {{ background: #f8f9fa; padding: 15px 20px; border-radius: 6px; border-left: 4px solid #1a73e8; flex: 1; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #dee2e6; }}
        th {{ background: #f1f3f4; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>SmartDocSorter Review Dashboard</h1>
        <div class="stats">
            <div class="card">
                <h3>Total Records</h3>
                <p>{len(records)}</p>
            </div>
            <div class="card" style="border-left-color: #ea4335;">
                <h3>Needs Review</h3>
                <p>{len(needs_review)}</p>
            </div>
        </div>
        <h2>Items Requiring Review</h2>
        <table>
            <tr>
                <th>Filename</th>
                <th>Category</th>
                <th>Person</th>
                <th>Confidence</th>
                <th>Destination</th>
                <th>Reasoning</th>
            </tr>
"""
    for r in needs_review:
        html_content += f"""
            <tr>
                <td>{r.get('filename', '')}</td>
                <td>{r.get('category', '')}</td>
                <td>{r.get('person_name', '')}</td>
                <td>{int(float(r.get('confidence', 0)) * 100)}%</td>
                <td>{r.get('destination', '')}</td>
                <td>{r.get('reasoning', '')}</td>
            </tr>
"""
    html_content += """
        </table>
    </div>
</body>
</html>
"""
    
    dashboard_path = out_dir / "review_dashboard.html"
    try:
        dashboard_path.write_text(html_content, encoding="utf-8")
        log.info(f"Review dashboard written -> {dashboard_path}")
    except Exception as e:
        log.warning(f"Could not write review dashboard: {e}")
        
    return dashboard_path


def write_debug_report(
    output_dir: Path,
    mode_str: str,
    start_dt: str,
    end_dt: str,
    elapsed: float,
    records: List[Dict[str, Any]],
    duplicate_rows: List[List[str]],
    py_threshold: float,
    rev_threshold: float
):
    """
    Writes diagnostic plain-text report summarizing classification breakdown, confidence, and engine stats.
    """
    cat_counts: Dict[str, int] = {}
    person_counts: Dict[str, int] = {}
    engine_counts: Dict[str, int] = {}
    needs_review_list: List[str] = []
    near_dup_list: List[str] = []
    family_routed_list: List[str] = []

    for r in records:
        cat = r.get("category", "Unknown")
        per = r.get("person_name", "Unknown")
        eng = _engine_label(r)
        dest = r.get("destination", "")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        person_counts[per] = person_counts.get(per, 0) + 1
        engine_counts[eng] = engine_counts.get(eng, 0) + 1

        if "_NEEDS_REVIEW" in dest.replace("\\", "/"):
            needs_review_list.append(
                f"  - {r['filename']} (From: {r.get('relative_folder','.')}) -> Conf: {r['confidence']:.2f}, Reason: {r.get('reasoning','')}"
            )
        if r.get("possible_duplicate_of"):
            near_dup_list.append(f"  - {r['filename']}  [ {r['possible_duplicate_of']} ]")
        if dest.startswith("Family") or "/Family/" in dest.replace("\\", "/"):
            family_routed_list.append(f"  - {r['filename']} (From: {r.get('relative_folder','.')}) ==> {dest} [{cat}]")

    lines = [
        "=" * 84,
        " SMART DOCUMENT SORTER - DIAGNOSTIC DEBUG REPORT",
        "=" * 84,
        f" Run Mode:              {mode_str}",
        f" Start Time:            {start_dt}",
        f" End Time:              {end_dt}",
        f" Total Elapsed:         {elapsed:.2f} seconds",
        f" RapidOCR Engine:       {'ACTIVE (1-Pass Page-0 Fast Mode)' if rapid_ocr is not None else 'MISSING'}",
        f" AI Fallbacks:          {sum(1 for x in records if _is_fallback(x))} (should be 0 when Ollama works)",
        f" Python Handoff Gate:   {int(py_threshold * 100)}%",
        f" Review Threshold Gate: {int(rev_threshold * 100)}%",
        f" Total Files in Report: {len(records)} (Exact Duplicates Flagged: {len(duplicate_rows)})",
        "-" * 84,
        " 1. ENGINE BREAKDOWN:",
    ]
    for k, v in sorted(engine_counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"    * {k:<24}: {v}")

    lines.append("-" * 84)
    lines.append(" 2. CATEGORY BREAKDOWN:")
    for k, v in sorted(cat_counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"    * {k:<24}: {v}")

    lines.append("-" * 84)
    lines.append(" 3. PERSON BREAKDOWN:")
    for k, v in sorted(person_counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"    * {k:<24}: {v}")

    lines.append("-" * 84)
    lines.append(f" 4. FAMILY FOLDER ROUTING ({len(family_routed_list)}):")
    lines.extend(family_routed_list[:100] if family_routed_list else ["    (None)"])

    lines.append("-" * 84)
    lines.append(f" 5. NEEDS REVIEW FILES ({len(needs_review_list)}):")
    lines.extend(needs_review_list[:60] if needs_review_list else ["    (None)"])

    lines.append("-" * 84)
    lines.append(f" 6. DUPLICATES & NEAR-DUPLICATES ({len(near_dup_list)}):")
    lines.extend(near_dup_list[:60] if near_dup_list else ["    (None)"])

    lines.append("-" * 84)
    lines.append(" 7. SAMPLE SORTED RECORDS (First 60):")
    for r in records[:60]:
        lines.append(
            f"  [{_engine_label(r):<6} {int(float(r.get('confidence',0))*100):>3}%] "
            f"{r['filename']} (From: {r.get('relative_folder','.')}) ==> {r['destination']} "
            f"| OCR({len(r.get('ocr_preview',''))}c): {r.get('ocr_preview','')[:55]}"
        )
    lines.extend(audit.render_sections())
    lines.append("=" * 84)

    report_text = "\n".join(lines)
    CURRENT_RUN_DIR.mkdir(parents=True, exist_ok=True)
    _mode_tag = "plan" if "DRY" in mode_str.upper() else "apply"
    for target in [CURRENT_RUN_DIR / "debug_run_report.txt", CURRENT_RUN_DIR / f"debug_run_report_{_mode_tag}.txt", output_dir / "debug_run_report.txt"]:
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(report_text)
            log.info(f"Debug report written → {target}")
        except Exception:
            pass

    try:
        with open(CURRENT_RUN_DIR / "debug_file_audit.csv", "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(audit.csv_rows())
    except Exception:
        pass