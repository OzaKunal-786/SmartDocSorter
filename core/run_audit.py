"""
core/run_audit.py - Per-file audit trail for the debug report.
Records, for every file: what the scan did, what the plan decided (dry run) and what actually
happened on apply. A module-level singleton is used so no function signatures need to change.
"""

import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


class RunAudit:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.total_files_seen = 0
        self.scan_skipped: List[Tuple[str, str]] = []      # (path, reason)
        self.ignored_dirs: List[Tuple[str, int, str]] = []  # (dir, files_inside, reason)
        self.queued: List[str] = []                         # supported files queued for processing
        self.journey: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.from_dry_run = False
        self.timings: List[Tuple[float, str, bool]] = []   # (seconds, path, from_cache)

    # ---- scan stage ---------------------------------------------------------
    def seen(self, n: int = 1) -> None:
        self.total_files_seen += n

    def skip_file(self, path: Any, reason: str) -> None:
        self.scan_skipped.append((str(path), reason))

    def skip_dir(self, path: Any, files_inside: int, reason: str) -> None:
        self.ignored_dirs.append((str(path), files_inside, reason))

    def queue(self, path: Any) -> None:
        self.queued.append(str(path))
        self._entry(path)

    # ---- plan / apply stages -----------------------------------------------
    def _entry(self, source: Any) -> Dict[str, Any]:
        key = str(source)
        if key not in self.journey:
            self.journey[key] = {"plan": "", "apply": []}
        return self.journey[key]

    def plan(self, source: Any, text: str) -> None:
        self._entry(source)["plan"] = text

    def apply(self, source: Any, action: str, detail: str = "") -> None:
        self._entry(source)["apply"].append(f"{action}{(' | ' + detail) if detail else ''}")

    def adopt(self, scan_path: Any, resolved: Any) -> None:
        """A file the scan skipped but that is processed anyway (e.g. unsupported file inside a kept folder)."""
        self.scan_skipped = [(p, w) for p, w in self.scan_skipped if p != str(scan_path)]
        self.queued.append(str(resolved))
        self._entry(resolved)

    def timing(self, source: Any, seconds: float, cached: bool) -> None:
        self.timings.append((float(seconds), str(source), bool(cached)))

    def plan_if_missing(self, source: Any, text: str) -> None:
        e = self._entry(source)
        if not e["plan"]:
            e["plan"] = text

    # ---- persistence: lets the Apply report include the dry-run scan + plan ----
    def save_scan(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "total_files_seen": self.total_files_seen, "scan_skipped": self.scan_skipped,
                "ignored_dirs": self.ignored_dirs, "queued": self.queued,
                "plans": {k: v["plan"] for k, v in self.journey.items()},
            }
            path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def load_scan(self, path: Path) -> None:
        try:
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            self.total_files_seen = data.get("total_files_seen", 0)
            self.scan_skipped = [tuple(x) for x in data.get("scan_skipped", [])]
            self.ignored_dirs = [tuple(x) for x in data.get("ignored_dirs", [])]
            self.queued = list(data.get("queued", []))
            for src, text in data.get("plans", {}).items():
                self._entry(src)["plan"] = text
            self.from_dry_run = True
        except Exception:
            pass

    # ---- rendering ----------------------------------------------------------
    def render_sections(self) -> List[str]:
        W = "-" * 84
        out: List[str] = []

        # A. Reconciliation: does every file on disk have an outcome?
        no_plan = [s for s in self.queued if not self.journey.get(s, {}).get("plan") and not self.journey.get(s, {}).get("apply")]
        counts: Dict[str, int] = {}
        for j in self.journey.values():
            for a in j["apply"]:
                k = a.split(" | ")[0]
                counts[k] = counts.get(k, 0) + 1
        if self.from_dry_run:
            out += [W, " NOTE: scan details and PLAN lines below come from the last dry run (saved plan); APPLY lines are from this run."]
        out += [W, " 8. FILE RECONCILIATION (does every file have an outcome?):",
                f"    Files seen on disk (all types)   : {self.total_files_seen}",
                f"    Skipped at scan (not queued)     : {len(self.scan_skipped)}",
                f"    Files inside ignored folders     : {sum(n for _, n, _ in self.ignored_dirs)}",
                f"    Queued for sorting               : {len(self.queued)}",
                f"    Queued but NO outcome recorded   : {len(no_plan)}   <-- these would be 'missing' files"]
        for k, v in sorted(counts.items()):
            out.append(f"    APPLY result {k:<28}: {v}")
        for s in no_plan[:200]:
            out.append(f"      ! no outcome: {s}")

        # B. Everything the scan skipped, with the reason
        out += [W, f" 9. SKIPPED AT SCAN ({len(self.scan_skipped)} files, {len(self.ignored_dirs)} folders):"]
        if self.ignored_dirs:
            out.append("    Ignored folders:")
            for d, n, why in self.ignored_dirs:
                out.append(f"      [FOLDER] {d}  ({n} files inside)  reason: {why}")
        if self.scan_skipped:
            out.append("    Ignored files:")
            for p, why in self.scan_skipped:
                out.append(f"      [FILE]   {p}  reason: {why}")
        if not self.ignored_dirs and not self.scan_skipped:
            out.append("    (None)")

        # C. Per-file journey (all files, no truncation)
        out += [W, f" 10. PER-FILE JOURNEY ({len(self.journey)} files, plan = dry run, apply = real copy):"]
        for i, (src, j) in enumerate(self.journey.items(), start=1):
            out.append(f"  [{i:04d}] {src}")
            out.append(f"         PLAN : {j['plan'] or '(no plan recorded in this run)'}")
            if j["apply"]:
                for a in j["apply"]:
                    out.append(f"         APPLY: {a}")
            else:
                out.append("         APPLY: (not applied in this run)")
        if self.timings:
            fresh = [t for t in self.timings if not t[2]]
            out += [W, " 11. TIMING (where did the dry run spend its time?):",
                    f"    Files read fresh (text/OCR) : {len(fresh)}  total {sum(t[0] for t in fresh):.1f}s",
                    f"    Files served from cache     : {len(self.timings) - len(fresh)}  total {sum(t[0] for t in self.timings if t[2]):.1f}s",
                    "    Slowest 15 files:"]
            for sec, src, cached in sorted(self.timings, reverse=True)[:15]:
                out.append(f"      {sec:6.1f}s  {'(cache)' if cached else '       '} {src}")
        return out

    def csv_rows(self) -> List[List[str]]:
        rows = [["Source", "Plan", "Apply"]]
        for src, j in self.journey.items():
            rows.append([src, j["plan"], " ## ".join(j["apply"])])
        for p, why in self.scan_skipped:
            rows.append([p, "SKIPPED AT SCAN", why])
        return rows


audit = RunAudit()
