#!/usr/bin/env python3
"""
Smart Document Sorter - Browser UI (NiceGUI).

Left-sidebar layout, native folder pickers, live run status, plan review,
folder decisions, family and exception-rule management, and a setup wizard.

Only uses the public API of smart_doc_sorter (core/ is untouched).
"""

import asyncio
import csv
import json
import time
import os
import subprocess
import sys
import threading
import logging
from datetime import datetime
from pathlib import Path

from nicegui import ui

import smart_doc_sorter as sds


PRESETS = {
    "Balanced": {
        "py_conf": 0.78,
        "rev_conf": 0.60,
        "desc": "Balanced (recommended): high accuracy with rapid rule checks. Best for general personal archives.",
    },
    "Strict": {
        "py_conf": 0.85,
        "rev_conf": 0.75,
        "desc": "Strict: sends more files to AI for validation and flags edge cases for review.",
    },
    "Fast & Relaxed": {
        "py_conf": 0.65,
        "rev_conf": 0.45,
        "desc": "Fast & Relaxed: maximum throughput with broad rule matching. Minimal manual review.",
    },
}

BANNER_STYLES = {
    "info": "bg-sky-50 text-sky-800 border-sky-300",
    "success": "bg-emerald-50 text-emerald-800 border-emerald-300",
    "warning": "bg-amber-50 text-amber-800 border-amber-300",
    "error": "bg-red-50 text-red-800 border-red-300",
}

BUILD_PLAN_INSTRUCTION = "Go to Run & Status → 1. Build Plan to update the plan."
SETTINGS = sds.load_user_settings()

for _key, _preset in PRESETS.items():
    for _field in ("py_conf", "rev_conf"):
        _value = SETTINGS.get(f"{_key}_{_field}")
        if isinstance(_value, (int, float)):
            _preset[_field] = float(_value)

STATE = {
    "running": False,
    "stop_event": threading.Event(),
    "log": [],
    "log_gen": 0,
    "progress": 0.0,
    "status": "Ready. Step 1: Build Plan (Dry-Run)  |  Step 2: Apply Plan & Copy Files.",
    "kpi_total": "0 / 0",
    "kpi_code": "0 (0%)",
    "kpi_ollama": "0 (0%)",
    "kpi_start": "--:--:--",
    "kpi_end": "--:--:--",
    "banner": ("info", "Ready. Build a plan first, review it, then apply."),
    "finished": None,
    "has_completed": False,
    "dismissed": set(),
}


def persist():
    sds.save_user_settings(SETTINGS)


def log(msg: str):
    STATE["log"].append(msg)
    STATE["log_gen"] += 1


def generate_auto_aliases(full_name: str) -> list:
    parts = [p.strip() for p in full_name.strip().split() if p.strip()]
    if not parts:
        return []
    aliases = [full_name.strip()]
    candidates = []
    if len(parts) == 3:
        first, mid, last = parts
        m_init, f_init = mid[0].upper(), first[0].upper()
        candidates = [
            f"{first} {m_init} {last}", f"{first} {m_init}. {last}",
            f"{first} {last}", f"{last} {first}",
            f"{f_init} {m_init} {last}", f"{f_init}{m_init} {last}",
            f"{f_init}. {m_init}. {last}", f"{f_init} {last}", f"{f_init}. {last}",
            first,
        ]
    elif len(parts) == 2:
        first, last = parts
        f_init = first[0].upper()
        candidates = [
            f"{first} {last}", f"{last} {first}",
            f"{f_init} {last}", f"{f_init}. {last}", first,
        ]
    for candidate in candidates:
        if candidate not in aliases:
            aliases.append(candidate)
    return aliases


async def pick_folder(initial: str = "") -> str:
    """Run the native folder picker in a separate process."""
    start = initial if initial and Path(initial).exists() else str(Path.home())
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk()\n"
        "r.withdraw()\n"
        "r.attributes('-topmost', True)\n"
        f"p = filedialog.askdirectory(initialdir={start!r}, title='Select folder')\n"
        "print(p or '')\n"
    )

    def _run():
        try:
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
            return lines[-1] if lines else ""
        except Exception:
            return ""

    chosen = await asyncio.get_running_loop().run_in_executor(None, _run)
    return str(Path(chosen)) if chosen else ""


def open_path(p):
    p = str(p)
    try:
        if sys.platform == "win32":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        ui.notify(f"Could not open: {e}", type="negative")


async def confirm(message: str, yes_label: str = "Continue") -> bool:
    with ui.dialog() as dlg, ui.card().style("max-width:95vw; width:480px"):
        ui.label(message).classes("text-sm whitespace-pre-line")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat no-caps")
            ui.button(yes_label, on_click=lambda: dlg.submit(True)).props("no-caps")
    result = await dlg
    dlg.delete()
    return bool(result)


async def prompt_text(title: str, label: str) -> str:
    with ui.dialog() as dlg, ui.card().style("max-width:95vw; width:480px"):
        ui.label(title).classes("text-base font-bold")
        inp = ui.input(label).props("outlined dense autofocus").classes("w-full")
        inp.on("keydown.enter", lambda: dlg.submit(inp.value))
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=lambda: dlg.submit("")).props("flat no-caps")
            ui.button("Add", on_click=lambda: dlg.submit(inp.value)).props("no-caps")
    result = await dlg
    dlg.delete()
    return (result or "").strip()


def card():
    return ui.card().classes("w-full p-4 gap-3").props("flat bordered")


# SDS-FIX-REPORTING: preflight check and fallback detection
_FALLBACK_HINTS = ("fallback", "not found", "unavailable", "error", "status code")


def _is_ai_fallback(r) -> bool:
    eng = str(r.get("resolved_by", "")).lower()
    why = str(r.get("reasoning", "")).lower()
    return "ollama" in eng and any(h in why for h in _FALLBACK_HINTS)


def preflight_check() -> list:
    """Return a list of problems; empty list means everything needed is available."""
    import importlib
    import urllib.request
    problems = []
    if sys.version_info[:2] > (3, 12):
        problems.append(f"Python {sys.version.split()[0]} is too new; start with Start_App_GUI_venv.bat")
    for mod, label in (("fitz", "PyMuPDF"), ("docx", "python-docx"), ("openpyxl", "openpyxl")):
        try:
            importlib.import_module(mod)
        except Exception:
            problems.append(f"{label} is not installed (PDF/Word/Excel text will be empty)")
    try:
        from core.extractor import rapid_ocr
        if rapid_ocr is None:
            problems.append("RapidOCR engine is not loaded (images will have no text)")
    except Exception as e:
        problems.append(f"RapidOCR check failed: {e}")
    model = getattr(sds, "MODEL_NAME", "qwen2.5:1.5b")
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not host.startswith("http"):
        host = "http://" + host
    try:
        handler = urllib.request.ProxyHandler({})  # local call: never use a proxy
        with urllib.request.build_opener(handler).open(host + "/api/tags", timeout=5) as resp:
            names = [m.get("name", "") for m in json.loads(resp.read().decode()).get("models", [])]
        if not any(n == model or n.startswith(model + ":") or n == model + ":latest" for n in names):
            problems.append(f"Ollama model '{model}' is not installed (ollama list)")
    except Exception as e:
        problems.append(f"Ollama server not reachable at {host} ({type(e).__name__})")
    return problems


def worker(in_path, out_path, dry_run, limit, py_conf, rev_conf, clean, ignore_cache):
    start_str = datetime.now().strftime("%H:%M:%S")
    
    class GuiLogHandler(logging.Handler):
        def emit(self, record):
            msg = self.format(record)
            log(msg)

    pipeline_logger = logging.getLogger("SmartDocSorter.pipeline")
    handler = GuiLogHandler()
    pipeline_logger.addHandler(handler)
    pipeline_logger.setLevel(logging.INFO)

    try:
        def on_progress(current, total, filename, speed, code_cnt, ollama_cnt):
            STATE["progress"] = current / total if total else 0.0
            STATE["status"] = f"Processing [{current}/{total}]: {filename} ({speed:.1f} docs/s)"
            log(f"[{current}/{total}] Processing: {filename} ({speed:.1f} docs/s)")
            resolved = max(1, code_cnt + ollama_cnt)
            code_pct = int(code_cnt / resolved * 100)
            STATE["kpi_total"] = f"{current} / {total}"
            STATE["kpi_code"] = f"{code_cnt} ({code_pct}%)"
            STATE["kpi_ollama"] = f"{ollama_cnt} ({100 - code_pct}%)"
            try:
                refs["progress"].set_value(STATE["progress"])
                refs["status"].set_text(STATE["status"])
            except Exception:
                pass
            if current % 2 == 0 or current == total or current <= 3:
                log(f"[{current}/{total}] {filename}  ({speed:.1f} docs/s)")

        summary = sds.run_sorter(
            input_dir=in_path,
            output_dir=out_path,
            dry_run=dry_run,
            model_name=sds.MODEL_NAME,
            limit=limit,
            python_confidence_threshold=py_conf,
            review_confidence_threshold=rev_conf,
            family_config=SETTINGS.get("family_config", {}),
            exception_rules=SETTINGS.get("exception_rules", []),
            clean_before_run=clean,
            ignore_cache=ignore_cache,
            progress_callback=on_progress,
            stop_event=STATE["stop_event"],
        )
        end_str = datetime.now().strftime("%H:%M:%S")
        STATE["kpi_end"] = end_str
        log(
            f"[OK] Start {start_str} | End {end_str} | Elapsed {summary.get('elapsed', 0):.2f}s | "
            f"Total {summary.get('total', 0)} | Python {summary.get('code', 0)} | Ollama {summary.get('ollama', 0)}"
        )
        stopped = STATE["stop_event"].is_set()
        if not stopped:
            STATE["has_completed"] = True
        STATE["running"] = False
        STATE["finished"] = {
            "ok": True, "summary": summary, "dry_run": dry_run, "stopped": stopped,
            "start": start_str, "end": end_str,
        }
    except Exception as e:
        log(f"[ERROR] {type(e).__name__}: {e}")
        STATE["kpi_end"] = "failed"
        STATE["running"] = False
        STATE["finished"] = {
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "dry_run": dry_run, "stopped": False,
        }


@ui.page("/")
def index():
    ui.query(".nicegui-content").classes("p-0 gap-0")
    ui.colors(primary="#0284c7")

    pages = {}
    nav = {}
    refs = {}
    people_cache = {"list": []}
    log_idx = {"i": 0, "gen": STATE["log_gen"]}

    def show(name: str):
        for key, col in pages.items():
            col.set_visibility(key == name)
        for key, btn in nav.items():
            if key == name:
                btn.classes(add="bg-sky-700")
            else:
                btn.classes(remove="bg-sky-700")
        if name == "ml_vocab":
            refresh_vocab_table()

    def current_preset() -> str:
        value = refs["preset"].value
        return value if value in PRESETS else "Balanced"

    def save_paths():
        SETTINGS["input_dir"] = (refs["in"].value or "").strip()
        SETTINGS["output_dir"] = (refs["out"].value or "").strip()
        preset = current_preset()
        SETTINGS["sorting_preset"] = preset
        SETTINGS["python_confidence_threshold"] = PRESETS[preset]["py_conf"]
        SETTINGS["review_confidence_threshold"] = PRESETS[preset]["rev_conf"]
        persist()
        if "in_display" in refs:
            refs["in_display"].set_text(SETTINGS["input_dir"] or "(not set)")
        if "out_display" in refs:
            refs["out_display"].set_text(SETTINGS["output_dir"] or "(not set)")

    def set_banner(kind: str, text: str):
        STATE["banner"] = (kind, text)

    with ui.row().classes("w-full h-screen overflow-hidden no-wrap items-stretch gap-0"):
        # Sidebar
        with ui.column().classes(
            "w-44 sm:w-56 shrink-0 h-screen bg-slate-900 text-white p-3 sm:p-4 justify-between no-wrap"
        ):
            with ui.column().classes("w-full gap-1"):
                ui.label("Smart Doc Sorter").classes("text-lg font-extrabold text-sky-400 leading-tight")
                ui.label("Offline AI Archive Studio").classes("text-xs text-slate-400 mb-4")
                for key, label, icon in (
                    ("run", "Run & Status", "rocket_launch"),
                    ("review", "Review Plan", "fact_check"),
                    ("family", "Family Members", "groups"),
                    ("ml_vocab", "ML Vocabulary", "psychology"),
                    ("settings", "Settings & Rules", "settings"),
                ):
                    nav[key] = ui.button(
                        label, icon=icon, on_click=lambda k=key: show(k)
                    ).props("flat no-caps align=left").classes("w-full text-white rounded-lg")
                ui.separator().classes("bg-slate-700 my-2")
                ui.button(
                    "Setup Wizard", icon="auto_fix_high", on_click=lambda: open_wizard()
                ).props("flat no-caps align=left").classes("w-full text-sky-300 rounded-lg")
            with ui.column().classes("w-full gap-0 pt-3 border-t border-slate-700"):
                ui.label("PRIMARY OWNER").classes("text-xs font-bold text-slate-500 tracking-wider")
                refs["owner_lbl"] = ui.label(SETTINGS.get("primary_person") or "(not set)").classes(
                    "text-sm text-sky-300 break-words"
                )

        # Main content
        with ui.column().classes("flex-1 min-w-0 h-screen overflow-y-auto p-3 sm:p-6 gap-4 bg-slate-100 no-wrap"):
            with ui.row().classes("w-full items-center justify-between gap-2 bg-white rounded-xl p-3 shadow-sm"):
                ui.label("Archive Management Console").classes("text-base sm:text-lg font-bold text-slate-800")
                with ui.row().classes("gap-2"):
                    ui.button("Open Output Folder", icon="folder_open", on_click=lambda: open_output()).props(
                        "outline no-caps dense"
                    )

            # Run & Status
            pages["run"] = ui.column().classes("w-full gap-3")
            with pages["run"]:
                with card():
                    with ui.row().classes("w-full items-center no-wrap gap-4"):
                        with ui.column().classes("flex-1 min-w-0 gap-0"):
                            ui.label("INPUT").classes("text-[10px] font-bold tracking-wider text-slate-400")
                            refs["in_display"] = ui.label(SETTINGS.get("input_dir") or "(not set)").classes(
                                "text-sm font-mono text-slate-800 truncate w-full"
                            )
                        ui.icon("arrow_forward").classes("text-slate-300")
                        with ui.column().classes("flex-1 min-w-0 gap-0"):
                            ui.label("OUTPUT").classes("text-[10px] font-bold tracking-wider text-slate-400")
                            refs["out_display"] = ui.label(SETTINGS.get("output_dir") or "(not set)").classes(
                                "text-sm font-mono text-slate-800 truncate w-full"
                            )
                    ui.button(
                        "Change folders in Settings", icon="settings", on_click=lambda: show("settings")
                    ).props("flat dense no-caps color=grey-7")

                with card():
                    with ui.row().classes("w-full items-center gap-4"):
                        ui.label("MODE").classes("text-[10px] font-bold tracking-wider text-slate-400")
                        initial = SETTINGS.get("sorting_preset", "Balanced")
                        if initial not in PRESETS:
                            initial = "Balanced"
                        refs["preset"] = ui.toggle(list(PRESETS.keys()), value=initial).props(
                            "no-caps unelevated dense toggle-color=primary"
                        )
                    refs["preset_desc"] = ui.label(PRESETS[initial]["desc"]).classes("text-xs text-slate-500")

                    def on_preset(_):
                        refs["preset_desc"].set_text(PRESETS[current_preset()]["desc"])
                        save_paths()

                    refs["preset"].on_value_change(on_preset)

                with card():
                    with ui.row().classes("w-full items-center gap-6"):
                        ui.label("OPTIONS").classes("text-[10px] font-bold tracking-wider text-slate-400")
                        refs["limit"] = ui.checkbox("Quick test (25 files)").props("dense")
                        refs["clean"] = ui.checkbox("Clean output").props("dense")
                        refs["ignore"] = ui.checkbox("Ignore cache").props("dense")
                    ui.label(
                        "Quick test: process up to 25 files when scanning. "
                        "Clean output: remove previously sorted copies before applying; "
                        "has no effect when building a plan. "
                        "Ignore cache: re-evaluate files when building a plan."
                    ).classes("text-sm text-slate-600")

                with card():
                    with ui.row().classes("w-full items-center gap-2"):
                        refs["btn_plan"] = ui.button(
                            "1. Build Plan", icon="search", on_click=lambda: start_sorting(True)
                        ).props("no-caps unelevated color=primary")
                        refs["btn_apply"] = ui.button(
                            "2. Apply & Copy", icon="content_copy", on_click=lambda: start_sorting(False)
                        ).props("no-caps unelevated color=positive")
                        refs["btn_resort"] = ui.button(
                            "Re-Apply", icon="refresh", on_click=lambda: resort()
                        ).props("no-caps outline color=grey-8")
                        ui.space()
                        refs["btn_stop"] = ui.button(
                            "Stop", icon="stop_circle", on_click=lambda: stop_run()
                        ).props("no-caps outline color=negative")
                        refs["btn_stop"].set_enabled(False)
                    ui.label(
                        "Build Plan scans and classifies without copying. "
                        "Apply & Copy uses the saved plan after review. "
                        "Re-Apply repeats the copy step using that plan. "
                        "If you changed rules, family settings, or folder decisions, build a new plan first."
                    ).classes("text-sm text-slate-600")

                with card().classes("mt-6"):
                    with ui.row().classes("w-full no-wrap items-center"):
                        kpis = (
                            ("kpi_total", "PROCESSED", "text-slate-800"),
                            ("kpi_code", "PYTHON RULES", "text-green-600"),
                            ("kpi_ollama", "OLLAMA AI", "text-purple-600"),
                            ("kpi_start", "START TIME", "text-sky-600"),
                            ("kpi_end", "END TIME", "text-sky-600"),
                        )
                        for n, (key, title, color) in enumerate(kpis):
                            if n:
                                ui.separator().props("vertical")
                            with ui.column().classes("flex-1 min-w-0 gap-0 px-3"):
                                ui.label(title).classes("text-[10px] font-bold tracking-wider text-slate-400")
                                refs[key] = ui.label(STATE[key]).classes(f"text-base font-bold truncate {color}")

                with card():
                    refs["banner"] = ui.row().classes(
                        f"w-full items-center p-2 rounded-lg border {BANNER_STYLES['info']}"
                    )
                    with refs["banner"]:
                        refs["banner_lbl"] = ui.label(STATE["banner"][1]).classes("text-sm font-semibold")
                    refs["status"] = ui.label(STATE["status"]).classes("text-xs text-slate-500")
                    refs["progress"] = ui.linear_progress(value=0.0, show_value=False).classes("w-full")
                    refs["log"] = ui.log(max_lines=400).classes(
                        "w-full h-48 bg-slate-900 text-emerald-300 text-xs font-mono rounded-lg"
                    )

            # Review Plan
            pages["review"] = ui.column().classes("w-full gap-4")
            with pages["review"]:
                with card():
                    with ui.row().classes("w-full items-center justify-between gap-2"):
                        refs["review_info"] = ui.label(
                            "No plan loaded yet. Go to Run & Status → 1. Build Plan."
                        ).classes("text-sm font-bold text-slate-800")
                        ui.button("Refresh Plan", icon="refresh", on_click=lambda: reload_all()).props(
                            "outline no-caps dense"
                        )

                    with ui.row().classes("w-full items-center gap-4"):
                        refs["search"] = ui.input(
                            "Search any column (file, category, person, status, destination...)"
                        ).props("outlined dense clearable").classes("flex-1 min-w-[200px]")
                        refs["low_only"] = ui.checkbox("Needs review only (low confidence)")
                    refs["search"].on_value_change(lambda _: refresh_rows())
                    refs["low_only"].on_value_change(lambda _: refresh_rows())

                    review_cols = [
                        {"name": "file", "label": "File Name", "field": "file", "align": "left", "sortable": True},
                        {"name": "category", "label": "Category", "field": "category", "align": "left", "sortable": True},
                        {"name": "person", "label": "Person", "field": "person", "align": "left", "sortable": True},
                        {"name": "conf", "label": "Confidence (%)", "field": "conf", "align": "center", "sortable": True},
                        {"name": "source", "label": "Source", "field": "source", "align": "center", "sortable": True},
                        {"name": "status", "label": "Status", "field": "status", "align": "center", "sortable": True},
                        {"name": "dest", "label": "Planned Destination", "field": "dest", "align": "left", "sortable": True},
                    ]
                    ui.add_css(
                        ".sticky-header-table thead tr th {position: sticky; z-index: 1; background: #f1f5f9;}"
                        ".sticky-header-table thead tr:first-child th {top: 0;}"
                    )
                    refs["table"] = ui.table(
                        columns=review_cols, rows=[], row_key="id", selection="multiple",
                        pagination={"rowsPerPage": 25},
                    ).classes("w-full sticky-header-table").props(
                        'dense flat bordered virtual-scroll :rows-per-page-options="[25, 50, 100, 0]"'
                    ).style("height:450px")
                    refs["table"].add_slot(
                        "body-cell-status",
                        '<q-td :props="props">'
                        '<q-badge v-if="props.value === \'Needs review\'" color="orange" :label="props.value" />'
                        '<q-badge v-else-if="props.value === \'Edited\'" color="green" :label="props.value" />'
                        '<q-badge v-else-if="props.value === \'Duplicate\'" color="purple" :label="props.value" />'
                        '</q-td>',
                    )

                    ui.label("Bulk edit selected rows (destinations refresh when the plan is applied):").classes(
                        "text-xs text-slate-500"
                    )
                    with ui.row().classes("w-full items-center gap-2"):
                        refs["edit_cat"] = ui.select(
                            ["(keep)"] + list(sds.VALID_CATEGORIES), value="(keep)", label="Category"
                        ).props("outlined dense").classes("min-w-[180px]")
                        refs["edit_person"] = ui.select(
                            ["(keep)", "General"], value="(keep)", label="Person",
                            with_input=True, new_value_mode="add-unique",
                        ).props("outlined dense").classes("min-w-[200px]")
                        ui.button("Apply to selected", icon="edit", on_click=lambda: apply_edits()).props("no-caps")

                with card():
                    ui.label("Folder decisions - custom folders").classes("text-base font-bold text-slate-800")
                    ui.label(
                        "Keep as is = copy the folder untouched. Sort its files = sort into category folders. "
                        f"After saving a folder decision, {BUILD_PLAN_INSTRUCTION[0].lower() + BUILD_PLAN_INSTRUCTION[1:]} "
                        "Do this before editing individual rows, because building a new plan can replace those edits."
                    ).classes("text-xs text-slate-500")
                    folder_cols = [
                        {"name": "folder", "label": "Source Folder", "field": "folder", "align": "left"},
                        {"name": "files", "label": "Files", "field": "files", "align": "center"},
                        {"name": "choice", "label": "Current Choice", "field": "choice", "align": "left"},
                        {"name": "why", "label": "Why", "field": "why", "align": "left"},
                    ]
                    refs["ftable"] = ui.table(
                        columns=folder_cols, rows=[], row_key="id", selection="single",
                        pagination={"rowsPerPage": 6},
                    ).classes("w-full").props("dense flat")
                    with ui.row().classes("w-full gap-2 items-center"):
                        ui.button("Keep as is", on_click=lambda: set_folder_choice("keep")).props("no-caps dense")
                        ui.button("Sort its files", on_click=lambda: set_folder_choice("sort")).props(
                            "no-caps dense color=grey-7"
                        )
                        ui.button("+ Add Folder", on_click=lambda: add_folder_rule()).props("no-caps dense outline")
                        ui.button("Delete Rule", on_click=lambda: delete_folder_rule()).props(
                            "no-caps dense color=negative outline"
                        )
                        refs["folder_hint"] = ui.label("").classes("text-xs text-slate-500")

            # Family Members
            pages["family"] = ui.column().classes("w-full gap-4")
            with pages["family"]:
                with card():
                    ui.label("Household & Family Members").classes("text-base font-bold text-slate-800")
                    ui.label(
                        "The primary owner's files go to the category folders; everyone else goes to Family/<Name>/."
                    ).classes("text-xs text-slate-500")
                    with ui.row().classes("w-full gap-2"):
                        ui.button("+ Add Member", on_click=lambda: member_dialog(None)).props("no-caps dense")
                        ui.button("Edit Selected", on_click=lambda: edit_member()).props("no-caps dense outline")
                        ui.button("Make Primary", on_click=lambda: make_primary()).props("no-caps dense outline")
                        ui.button("Remove", on_click=lambda: delete_member()).props(
                            "no-caps dense color=negative outline"
                        )
                    fam_cols = [
                        {"name": "name", "label": "Canonical Full Name (Output Folder)", "field": "name", "align": "left"},
                        {"name": "role", "label": "Role", "field": "role", "align": "center"},
                        {"name": "aliases", "label": "Recognized Aliases, Initials & Nicknames", "field": "aliases", "align": "left"},
                    ]
                    refs["ftab"] = ui.table(
                        columns=fam_cols, rows=[], row_key="id", selection="single",
                        pagination={"rowsPerPage": 10},
                    ).classes("w-full").props("dense flat")

            # ML Vocabulary
            pages["ml_vocab"] = ui.column().classes("w-full gap-4")
            with pages["ml_vocab"]:
                with card():
                    ui.label("Machine Learning Custom Vocabulary & Learned Rules").classes(
                        "text-base font-bold text-slate-800"
                    )
                    ui.label(
                        "Enter a word or phrase such as oci, ppf, or form-16. "
                        "Matching manual terms take priority over ordinary classification. "
                        "Build a new plan after adding a term. Do not enter personal information."
                    ).classes("text-xs text-slate-500")

                    with ui.row().classes("w-full gap-2 items-center pt-2"):
                        vocab_kw_in = ui.input("Custom word or phrase").props("outlined dense").classes("flex-1")
                        vocab_cat_sel = ui.select(
                            list(sds.VALID_CATEGORIES), value=list(sds.VALID_CATEGORIES)[0], label="Category"
                        ).props("outlined dense").classes("w-48")

                        def add_custom_vocab():
                            if STATE["running"] or STATE["finished"] is not None:
                                ui.notify("Wait for the current run to finish before adding vocabulary.", type="warning")
                                return
                            kw = (vocab_kw_in.value or "").strip()
                            cat = vocab_cat_sel.value
                            try:
                                sds.save_manual_keyword(kw, cat, sds.LEARNING_CSV_PATH)
                            except ValueError as exc:
                                ui.notify(str(exc), type="warning")
                                return
                            except (OSError, csv.Error) as exc:
                                ui.notify(f"Could not save vocabulary: {exc}", type="negative")
                                return
                            vocab_kw_in.set_value("")
                            refresh_vocab_table()
                            set_banner("warning", f"Vocabulary saved. {BUILD_PLAN_INSTRUCTION}")
                            ui.notify(f"Added '{kw}' to {cat}. {BUILD_PLAN_INSTRUCTION}", type="positive")

                        ui.button("Insert Custom Word", icon="add", on_click=add_custom_vocab).props(
                            "no-caps color=primary"
                        )

                    vocab_cols = [
                        {"name": "keyword", "label": "Keyword Pattern", "field": "keyword", "align": "left", "sortable": True},
                        {"name": "category", "label": "Mapped Category", "field": "category", "align": "left", "sortable": True},
                        {"name": "origin", "label": "Source", "field": "origin", "align": "left", "sortable": True},
                    ]
                    vocab_table = ui.table(
                        columns=vocab_cols, rows=[], row_key="id", pagination={"rowsPerPage": 15}
                    ).classes("w-full mt-2").props("dense flat")

                    def refresh_vocab_table():
                        latest_path = sds.INTERNAL_DATA_DIR / "latest_learned_vocabulary.json"
                        try:
                            data = json.loads(latest_path.read_text(encoding="utf-8"))
                            latest = {term for term in data if isinstance(term, str)} if isinstance(data, list) else set()
                        except (OSError, ValueError):
                            latest = set()

                        rows = []
                        for entry in sds.load_vocabulary_entries(sds.LEARNING_CSV_PATH):
                            kind = entry["kind"]
                            if kind == "manual":
                                origin = "Manual"
                            elif kind == "auto":
                                origin = "New - latest run" if entry["key"] in latest else "Automatic"
                            else:
                                origin = "Existing - source unknown"
                            rows.append({
                                "id": f"{kind}:{entry['key']}",
                                "keyword": entry["keyword"], "category": entry["category"], "origin": origin,
                            })
                        rows.sort(key=lambda row: (row["origin"] != "New - latest run", row["keyword"].lower()))
                        vocab_table.rows[:] = rows
                        vocab_table.update()

                    refresh_vocab_table()

            # Settings & Rules
            pages["settings"] = ui.column().classes("w-full gap-4")
            with pages["settings"]:
                with card():
                    ui.label("Folder Configuration").classes("text-base font-bold text-slate-800")
                    ui.label("Configure your input source archive and organized output destination.").classes(
                        "text-xs text-slate-500"
                    )
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        refs["in"] = ui.input("Input Archive Folder", value=SETTINGS.get("input_dir", "")).props(
                            "outlined dense"
                        ).classes("flex-1 min-w-0")
                        ui.button("Browse...", icon="folder_open", on_click=lambda: browse("in")).props(
                            "outline no-caps"
                        )
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        refs["out"] = ui.input("Output Archive Folder", value=SETTINGS.get("output_dir", "")).props(
                            "outlined dense"
                        ).classes("flex-1 min-w-0")
                        ui.button("Browse...", icon="folder_open", on_click=lambda: browse("out")).props(
                            "outline no-caps"
                        )
                    refs["in"].on_value_change(lambda _: save_paths())
                    refs["out"].on_value_change(lambda _: save_paths())

                with card():
                    ui.label("General Settings & Preset Thresholds").classes("text-base font-bold text-slate-800")

                    def on_auto_rename(e):
                        SETTINGS["auto_rename"] = bool(e.value)
                        persist()

                    ui.switch(
                        "Auto-rename scanned images (clean name + category + date)",
                        value=bool(SETTINGS.get("auto_rename", True)), on_change=on_auto_rename,
                    )

                    ui.separator().classes("my-2")
                    ui.label("Customizable Preset Confidence Thresholds (%)").classes("text-xs font-bold text-slate-600")

                    def update_thresh(preset_key, field, val):
                        try:
                            f_val = float(val) / 100.0
                            PRESETS[preset_key][field] = f_val
                            SETTINGS[f"{preset_key}_{field}"] = f_val
                            persist()
                        except Exception:
                            pass

                    with ui.row().classes("w-full gap-4 flex-wrap"):
                        for pk in ("Balanced", "Strict", "Fast & Relaxed"):
                            with ui.column().classes("flex-1 min-w-[180px] gap-1"):
                                ui.label(pk).classes("text-xs font-bold text-slate-700")
                                ui.number(
                                    "Python Conf %", value=int(PRESETS[pk]["py_conf"] * 100), min=40, max=99,
                                    on_change=lambda e, k=pk: update_thresh(k, "py_conf", e.value),
                                ).props("outlined dense").classes("w-full")
                                ui.number(
                                    "Review Conf %", value=int(PRESETS[pk]["rev_conf"] * 100), min=30, max=99,
                                    on_change=lambda e, k=pk: update_thresh(k, "rev_conf", e.value),
                                ).props("outlined dense").classes("w-full")

                    def on_min_files(e):
                        try:
                            SETTINGS["folder_keep_min_files"] = max(1, int(e.value))
                            persist()
                        except Exception:
                            pass

                    ui.number(
                        "Files needed in a folder before asking to keep it (default 3)",
                        value=int(SETTINGS.get("folder_keep_min_files", 3)), min=1, step=1, format="%d",
                        on_change=on_min_files,
                    ).props("outlined dense").classes("w-full max-w-md")

                with card():
                    ui.label("Exception Rules (evaluated BEFORE classification)").classes(
                        "text-base font-bold text-slate-800"
                    )
                    with ui.expansion("💡 View Exception Rule Guidance & Example", icon="help_outline").classes(
                        "w-full bg-slate-50 rounded-lg text-xs"
                    ):
                        ui.label(
                            "Exception rules override standard classification. For example, if you want all files with "
                            "'GymMembership' in their filename to automatically route to 'Medical' under your primary owner, "
                            "set Pattern to 'GymMembership', Match Scope to 'filename', Action to 'force_route', and Category to 'Medical'."
                        ).classes("p-2 text-slate-600 leading-relaxed")
                    with ui.row().classes("w-full gap-2 pt-2"):
                        ui.button("+ Add Exception Rule", on_click=lambda: rule_dialog(None)).props("no-caps dense")
                        ui.button("Edit Selected", on_click=lambda: edit_rule()).props("no-caps dense outline")
                        ui.button("Remove", on_click=lambda: delete_rule()).props(
                            "no-caps dense color=negative outline"
                        )
                    rule_cols = [
                        {"name": "pattern", "label": "Keyword / Regex", "field": "pattern", "align": "left"},
                        {"name": "scope", "label": "Match Scope", "field": "scope", "align": "center"},
                        {"name": "action", "label": "Action", "field": "action", "align": "center"},
                        {"name": "category", "label": "Forced Category", "field": "category", "align": "left"},
                        {"name": "person", "label": "Forced Family Member", "field": "person", "align": "left"},
                    ]
                    refs["rtab"] = ui.table(
                        columns=rule_cols, rows=[], row_key="id", selection="single",
                        pagination={"rowsPerPage": 10},
                    ).classes("w-full").props("dense flat")

    # Widgets above are created before these handlers are called.
    async def browse(which: str):
        chosen = await pick_folder(refs[which].value or "")
        if chosen:
            refs[which].set_value(chosen)

    async def start_sorting(dry_run: bool):
        if STATE["running"]:
            ui.notify("A run is already in progress.", type="warning")
            return
        in_str = (refs["in"].value or "").strip()
        out_str = (refs["out"].value or "").strip()
        if not in_str or not Path(in_str).exists():
            ui.notify("Please select a valid Input Archive folder.", type="negative")
            return
        if not out_str:
            ui.notify("Please select an Output Archive folder.", type="negative")
            return
        in_path, out_path = Path(in_str), Path(out_str)
        if in_path.resolve() == out_path.resolve():
            ui.notify("Input and Output folders must be different.", type="negative")
            return

        if dry_run:
            try:
                has_existing_plan = bool(sds.load_plan_for_review())
            except Exception:
                has_existing_plan = False
            if has_existing_plan and STATE["has_completed"]:
                if not await confirm(
                    "⚠️ A sorting run has already completed.\n\n"
                    "1. Building a new plan updates the preview with current rules and folder decisions. "
                    "It can replace edits made in Review Plan.\n"
                    "2. To classify files again instead of reusing cached results, "
                    "select 'Ignore cache' before building the plan.\n\n"
                    "Do you wish to proceed with this dry run?"
                ):
                    return
        else:
            try:
                has_plan = bool(sds.load_plan_for_review())
            except Exception:
                has_plan = False
            if not has_plan:
                if not await confirm(
                    "No saved plan found.\nApply will scan, classify and copy in one step (slower). Continue?"
                ):
                    return
            if refs["clean"].value:
                if not await confirm(
                    "'Clean output' is ticked.\n\n"
                    f"This deletes previously sorted files inside:\n{out_path}\n"
                    "(Your original files are untouched.)\n\nContinue?"
                ):
                    return

        problems = await asyncio.get_running_loop().run_in_executor(None, preflight_check)
        if problems:
            for p in problems:
                log(f"[PREFLIGHT] {p}")
            msg = ("Preflight check found problems:\n\n- " + "\n- ".join(problems)
                   + "\n\nResults will be fast but unreliable. Run anyway?")
            if not await confirm(msg, "Run anyway"):
                set_banner("error", "Run cancelled by preflight: " + "; ".join(problems))
                return
        save_paths()
        preset = PRESETS[current_preset()]
        mode = "STAGE 1: BUILD PLAN (--plan)" if dry_run else "STAGE 2: APPLY PLAN & COPY FILES (--apply)"

        STATE["stop_event"].clear()
        STATE["log"].clear()
        STATE["log_gen"] += 1
        STATE["progress"] = 0.0
        STATE["kpi_total"], STATE["kpi_code"], STATE["kpi_ollama"] = "0 / 0", "0 (0%)", "0 (0%)"
        start_str = datetime.now().strftime("%H:%M:%S")
        STATE["kpi_start"] = start_str
        STATE["kpi_end"] = "Running..."
        STATE["status"] = f"Started at {start_str} - {mode}"
        set_banner("info", f"Running {mode} ...")
        log(f"[*] Start {start_str} | {mode}")
        log(
            f"[*] Preset: {current_preset()} (Python {int(preset['py_conf'] * 100)}% | "
            f"Review {int(preset['rev_conf'] * 100)}%)"
        )
        STATE["running"] = True
        threading.Thread(
            target=worker,
            args=(
                in_path, out_path, dry_run, 25 if refs["limit"].value else None,
                preset["py_conf"], preset["rev_conf"],
                bool(refs["clean"].value) and not dry_run, bool(refs["ignore"].value),
            ),
            daemon=True,
        ).start()
        refs["ignore"].set_value(False)

    def stop_run():
        if STATE["running"]:
            STATE["stop_event"].set()
            set_banner("warning", "Stopping after the current file and saving progress...")
            log("[!] Stop requested. Finishing current file and saving state...")

    async def resort():
        out = (refs["out"].value or "").strip()
        if not out or not Path(out).exists():
            ui.notify("Please specify a valid output directory first.", type="warning")
            return
        if await confirm("Re-apply the saved plan to your output archive?"):
            await start_sorting(False)

    def open_output():
        out = (refs["out"].value or "").strip()
        if out and Path(out).exists():
            open_path(out)
        else:
            ui.notify("Output folder does not exist yet.", type="warning")

    # Review table
    review = {"records": []}

    def rev_threshold() -> float:
        return PRESETS[current_preset()]["rev_conf"]

    def load_plan():
        try:
            review["records"] = sds.load_plan_for_review() or []
        except Exception as e:
            review["records"] = []
            log(f"[!] Could not load plan for review: {e}")
        names = ["General"] + list(SETTINGS.get("family_config", {}).keys())
        for record in review["records"]:
            person = record.get("person_name")
            if person and person not in names:
                names.append(person)
        people_cache["list"] = names
        refs["edit_person"].set_options(["(keep)"] + names, value="(keep)")
        refresh_rows()

    def refresh_rows():
        recs = review["records"]
        table = refs["table"]
        table.selected.clear()
        if not recs:
            table.rows[:] = []
            table.update()
            refs["review_info"].set_text("No plan loaded yet. Go to Run & Status → 1. Build Plan.")
            return
        q = (refs["search"].value or "").strip().lower()
        low_only = bool(refs["low_only"].value)
        threshold = rev_threshold()
        rows = []
        for i, record in enumerate(recs):
            conf = record.get("confidence", 0.0)
            conf = float(conf) if isinstance(conf, (int, float)) else 0.0
            edited = bool(record.get("manual_override"))
            is_low = conf < threshold and not edited
            if low_only and not is_low:
                continue
            is_dupe = bool(record.get("possible_duplicate_of")) or record.get("resolved_by") == "Skipped (Exact Dup)"
            status = "Edited" if edited else ("Duplicate" if is_dupe else ("Needs review" if is_low else "Normal"))
            hay = " ".join(str(x) for x in (
                record.get("filename", ""), record.get("category", ""), record.get("person_name", ""),
                int(conf * 100), record.get("resolved_by") or "Code", status, record.get("destination", ""),
            )).lower()
            if q and q not in hay:
                continue
            rows.append({
                "id": i,
                "file": record.get("filename", "Unknown"),
                "category": record.get("category", ""),
                "person": record.get("person_name", ""),
                "conf": int(conf * 100),
                "source": (record.get("resolved_by") or "Code")[:20],
                "status": status,
                "dest": record.get("destination", ""),
            })
        table.rows[:] = rows
        table.update()
        refs["review_info"].set_text(
            f"Showing {len(rows)} of {len(recs)} planned documents "
            "(orange = below review threshold, green = edited)"
        )

    def apply_edits():
        sel = refs["table"].selected
        if not sel:
            ui.notify("Select one or more rows first.", type="warning")
            return
        new_cat = refs["edit_cat"].value
        new_person = refs["edit_person"].value
        new_cat = "" if new_cat in (None, "", "(keep)") else new_cat
        new_person = "" if new_person in (None, "", "(keep)") else str(new_person).strip()
        if not new_cat and not new_person:
            ui.notify("Pick a Category and/or a Person to apply.", type="warning")
            return
        edits = {}
        for row in sel:
            record = review["records"][row["id"]]
            edits[record.get("source_path", "")] = {
                "category": new_cat or record.get("category", ""),
                "person_name": new_person or record.get("person_name", ""),
            }
        try:
            changed = sds.save_plan_edits(edits)
            log(f"[+] Review: {len(sel)} row(s) updated ({changed} field change(s) saved).")
            ui.notify(f"{len(sel)} row(s) updated.", type="positive")
        except Exception as e:
            log(f"[!] Could not save review edits: {e}")
            ui.notify(f"Could not save edits: {e}", type="negative")
            return
        load_plan()

    # Folder decisions
    def load_folders():
        try:
            data = sds.load_folder_decisions() or {}
        except Exception:
            data = {}
        saved_raw = SETTINGS.get("folder_decisions", {}) or {}
        saved = {k.lower(): v for k, v in saved_raw.items()}
        labels = {"ask": "Not answered (sorted for now)", "keep": "Keep as is", "sort": "Sort its files"}
        order = {"ask": 0, "keep": 1, "sort": 2}
        rows, seen = [], set()
        for name, decision in sorted(data.items(), key=lambda kv: (order.get(kv[1].get("action"), 3), kv[0].lower())):
            reason = decision.get("reason", "")
            if reason.startswith(("folder name is a category", "family-member", "only ")):
                continue
            last = name.split("/")[-1].lower()
            action = saved.get(name.lower()) or saved.get(last) or decision.get("action", "ask")
            rows.append({
                "id": name, "folder": name, "files": decision.get("files", ""),
                "choice": labels.get(action, action), "why": reason,
            })
            seen.update((name.lower(), last))
        for key, action in saved_raw.items():
            if key.lower() in seen or key.split("/")[-1].lower() in seen:
                continue
            rows.append({
                "id": key, "folder": key, "files": "-",
                "choice": labels.get(action, action), "why": "Custom rule added by you",
            })
        refs["ftable"].selected.clear()
        refs["ftable"].rows[:] = rows
        refs["ftable"].update()
        refs["folder_hint"].set_text("" if rows else "No custom folders need a decision.")

    def _selected_folder():
        sel = refs["ftable"].selected
        if not sel:
            ui.notify("Select a folder in the list first.", type="warning")
            return None
        return sel[0]["id"]

    def folder_change_saved(message: str):
        load_folders()
        refs["folder_hint"].set_text(f"{message} {BUILD_PLAN_INSTRUCTION}")
        set_banner("warning", f"{message} The saved plan has not changed. {BUILD_PLAN_INSTRUCTION}")

    def set_folder_choice(choice: str):
        folder = _selected_folder()
        if folder is None:
            return
        SETTINGS.setdefault("folder_decisions", {})[folder] = choice
        persist()
        folder_change_saved("Folder decision saved.")

    async def add_folder_rule():
        name = await prompt_text("Add Custom Folder Rule", "Folder name or path (e.g. Tax_2025)")
        if not name:
            return
        SETTINGS.setdefault("folder_decisions", {})[name] = "keep"
        persist()
        log(f"[+] Added custom folder rule: '{name}' -> keep")
        folder_change_saved("Rule added (keep).")

    async def delete_folder_rule():
        folder = _selected_folder()
        if folder is None:
            return
        if not await confirm(f"Remove the folder decision rule for '{folder}'?", "Remove"):
            return
        fd = SETTINGS.setdefault("folder_decisions", {})
        last = folder.split("/")[-1].lower()
        for key in [k for k in fd if k.lower() == folder.lower() or k.lower() == last]:
            fd.pop(key, None)
        persist()
        STATE["dismissed"].discard(folder)
        log(f"[-] Removed folder rule for: '{folder}'")
        folder_change_saved("Rule removed.")

    def ask_folder_questions():
        try:
            data = sds.load_folder_decisions() or {}
        except Exception:
            return
        asks = [(k, d) for k, d in data.items() if d.get("action") == "ask" and k not in STATE["dismissed"]]
        if not asks:
            return
        choices = {k: "sort" for k, _ in asks}

        with ui.dialog().props("persistent") as dlg, ui.card().style("max-width:95vw; width:720px"):
            ui.label("Keep these folders as they are?").classes("text-base font-bold")
            ui.label(
                "These look like custom topic folders. YES = copy the whole folder untouched. "
                "NO = sort its files into category folders. "
                "Saving choices does not start a new run."
            ).classes("text-xs text-slate-500")
            with ui.column().classes("w-full gap-2 overflow-y-auto").style("max-height:55vh"):
                for key, decision in asks:
                    with ui.card().classes("w-full p-3 gap-1").props("flat bordered"):
                        ui.label(key).classes("font-bold text-sm")
                        ui.label(f"{decision.get('files', 0)} files - {decision.get('detail', '')}").classes(
                            "text-xs text-slate-500"
                        )
                        ui.toggle(
                            {"keep": "Yes, keep as-is", "sort": "No, sort its files"}, value="sort",
                            on_change=lambda e, k=key: choices.__setitem__(k, e.value),
                        ).props("no-caps dense")

            def later():
                STATE["dismissed"].update(choices.keys())
                log("[i] Folder questions postponed - those folders are sorted normally.")
                dlg.close()
                dlg.delete()

            def save():
                SETTINGS.setdefault("folder_decisions", {}).update(choices)
                persist()
                kept_count = sum(choice == "keep" for choice in choices.values())
                log(f"[+] Folder answers saved ({kept_count} kept as-is).")
                dlg.close()
                dlg.delete()
                folder_change_saved("Folder answers saved.")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Decide later (sort for now)", on_click=later).props("flat no-caps")
                ui.button("Save choices", on_click=save).props("no-caps")
        dlg.open()

    def ask_review_questions():
        try:
            fdata = sds.load_folder_decisions() or {}
        except Exception:
            fdata = {}
        if any(d.get("action") == "ask" and k not in STATE["dismissed"] for k, d in fdata.items()):
            return
        recs = [
            r for r in (sds.load_plan_for_review() or [])
            if "_NEEDS_REVIEW" in str(r.get("destination", "")) and not r.get("manual_override")
        ]
        if not recs:
            return
        names = ["General"] + list(SETTINGS.get("family_config", {}).keys())
        picks = {}
        with ui.dialog().props("persistent") as dlg, ui.card().style("max-width:95vw; width:860px"):
            ui.label(f"{len(recs)} file(s) need a decision").classes("text-base font-bold")
            ui.label("Pick the right category, or leave it and decide later.").classes("text-xs text-slate-500")
            with ui.column().classes("w-full gap-2 overflow-y-auto").style("max-height:55vh"):
                for r in recs[:50]:
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(r.get("filename", "")).classes("text-sm font-bold flex-1 min-w-[200px] break-all")
                        cat = ui.select(
                            ["(leave)"] + list(sds.VALID_CATEGORIES), value="(leave)"
                        ).props("outlined dense").classes("min-w-[160px]")
                        cur = r.get("person_name") or names[0]
                        per = ui.select(
                            names if cur in names else names + [cur], value=cur
                        ).props("outlined dense").classes("min-w-[180px]")
                        picks[r.get("source_path", "")] = (cat, per)
            if len(recs) > 50:
                ui.label(f"Showing first 50. Use the Review Plan page for the other {len(recs) - 50}.").classes(
                    "text-xs text-slate-500"
                )

            def later():
                dlg.close()
                dlg.delete()

            def save():
                edits = {
                    src: {"category": c.value, "person_name": p.value}
                    for src, (c, p) in picks.items() if c.value != "(leave)"
                }
                if edits:
                    try:
                        sds.save_plan_edits(edits)
                        log(f"[+] Review: {len(edits)} file(s) assigned.")
                    except Exception as e:
                        ui.notify(f"Could not save: {e}", type="negative")
                dlg.close()
                dlg.delete()
                load_plan()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Decide later", on_click=later).props("flat no-caps")
                ui.button("Save choices", on_click=save).props("no-caps")
        dlg.open()

    def reload_all():
        load_plan()
        load_folders()

    # Family members
    def refresh_family():
        family = SETTINGS.get("family_config", {}) or {}
        primary = SETTINGS.get("primary_person", "")
        rows = [{
            "id": name, "name": name,
            "role": "Primary" if name == primary else "",
            "aliases": ", ".join(aliases),
        } for name, aliases in family.items()]
        refs["ftab"].selected.clear()
        refs["ftab"].rows[:] = rows
        refs["ftab"].update()
        refs["owner_lbl"].set_text(primary or "(not set)")

    def member_dialog(old):
        family = SETTINGS.setdefault("family_config", {})
        edited = {"flag": bool(old)}
        with ui.dialog() as dlg, ui.card().style("max-width:95vw; width:560px"):
            ui.label("Edit Family Member" if old else "Add Family Member").classes("text-base font-bold")
            name_in = ui.input("Canonical Full Name (e.g. First Middle Last)", value=old or "").props(
                "outlined dense autofocus"
            ).classes("w-full")
            alias_in = ui.input(
                "Aliases & initials (comma separated - add nicknames at the end)",
                value=", ".join(family.get(old, [])) if old else "",
            ).props("outlined dense").classes("w-full")
            alias_in.on("keydown", lambda: edited.update(flag=True))

            def on_name(e):
                if not edited["flag"]:
                    alias_in.set_value(", ".join(generate_auto_aliases(e.value or "")))

            name_in.on_value_change(on_name)

            def autofill():
                edited["flag"] = False
                alias_in.set_value(", ".join(generate_auto_aliases((name_in.value or "").strip())))

            def save():
                name = (name_in.value or "").strip()
                if not name:
                    ui.notify("Please enter a canonical full name.", type="warning")
                    return
                aliases = [a.strip() for a in (alias_in.value or "").split(",") if a.strip()]
                if not aliases:
                    aliases = generate_auto_aliases(name)
                if name not in aliases:
                    aliases.insert(0, name)
                if old and old != name:
                    family.pop(old, None)
                    if SETTINGS.get("primary_person") == old:
                        SETTINGS["primary_person"] = name
                family[name] = aliases
                if not SETTINGS.get("primary_person"):
                    SETTINGS["primary_person"] = name
                persist()
                refresh_family()
                dlg.close()
                dlg.delete()
                ui.notify(f"Family member saved. {BUILD_PLAN_INSTRUCTION}", type="positive")

            with ui.row().classes("w-full justify-between gap-2"):
                ui.button("Auto-Fill Aliases", on_click=autofill).props("flat no-caps")
                with ui.row().classes("gap-2"):
                    ui.button("Cancel", on_click=lambda: (dlg.close(), dlg.delete())).props("flat no-caps")
                    ui.button("Save Member", on_click=save).props("no-caps")
        dlg.open()

    def _selected_member():
        sel = refs["ftab"].selected
        if not sel:
            ui.notify("Please select a family member first.", type="warning")
            return None
        return sel[0]["id"]

    def edit_member():
        name = _selected_member()
        if name is not None:
            member_dialog(name)

    def make_primary():
        name = _selected_member()
        if name is None:
            return
        SETTINGS["primary_person"] = name
        persist()
        refresh_family()
        ui.notify(f"Primary owner set to {name}. {BUILD_PLAN_INSTRUCTION}", type="positive")

    async def delete_member():
        name = _selected_member()
        if name is None:
            return
        extra = "\n\nThis is the primary owner." if name == SETTINGS.get("primary_person") else ""
        if not await confirm(f"Remove '{name}' from the family configuration?{extra}", "Remove"):
            return
        SETTINGS.get("family_config", {}).pop(name, None)
        if SETTINGS.get("primary_person") == name:
            SETTINGS["primary_person"] = next(iter(SETTINGS.get("family_config", {})), "")
        persist()
        refresh_family()

    # Exception rules
    def refresh_rules():
        rows = []
        for i, rule in enumerate(SETTINGS.get("exception_rules", []) or []):
            rows.append({
                "id": i, "pattern": rule.get("pattern", ""),
                "scope": rule.get("match_scope", "filename"), "action": rule.get("action", "force_route"),
                "category": rule.get("category") or "(Auto-Detect)",
                "person": rule.get("person_name") or "(Auto-Detect)",
            })
        refs["rtab"].selected.clear()
        refs["rtab"].rows[:] = rows
        refs["rtab"].update()

    def rule_dialog(idx):
        rules = SETTINGS.setdefault("exception_rules", [])
        rule = rules[idx] if idx is not None and 0 <= idx < len(rules) else {}
        people = list(SETTINGS.get("family_config", {}).keys())
        with ui.dialog() as dlg, ui.card().style("max-width:95vw; width:560px"):
            ui.label("Configure Exception Rule").classes("text-base font-bold")
            pat = ui.input("Match keyword or regex pattern", value=rule.get("pattern", "")).props(
                "outlined dense autofocus"
            ).classes("w-full")
            with ui.row().classes("w-full gap-2 no-wrap"):
                scope = ui.select(
                    ["filename", "folder", "text", "any"], value=rule.get("match_scope", "filename"),
                    label="Search in",
                ).props("outlined dense").classes("flex-1")
                action = ui.select(
                    ["force_route", "skip_file"], value=rule.get("action", "force_route"), label="Rule action"
                ).props("outlined dense").classes("flex-1")
            with ui.row().classes("w-full gap-2 no-wrap"):
                cat = ui.select(
                    ["(Auto-Detect)"] + list(sds.VALID_CATEGORIES),
                    value=rule.get("category") or "(Auto-Detect)", label="Assign category",
                ).props("outlined dense").classes("flex-1")
                person = ui.select(
                    ["(Auto-Detect)", "General"] + people,
                    value=rule.get("person_name") or "(Auto-Detect)", label="Assign person",
                ).props("outlined dense").classes("flex-1")

            def save():
                pattern = (pat.value or "").strip()
                if not pattern:
                    ui.notify("Please enter a keyword or regex pattern.", type="warning")
                    return
                new = {
                    "pattern": pattern, "match_scope": scope.value, "action": action.value,
                    "category": "" if cat.value == "(Auto-Detect)" else cat.value,
                    "person_name": "" if person.value == "(Auto-Detect)" else person.value,
                }
                if idx is None:
                    rules.append(new)
                else:
                    rules[idx] = new
                persist()
                refresh_rules()
                dlg.close()
                dlg.delete()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=lambda: (dlg.close(), dlg.delete())).props("flat no-caps")
                ui.button("Save Rule", on_click=save).props("no-caps")
        dlg.open()

    def edit_rule():
        sel = refs["rtab"].selected
        if not sel:
            ui.notify("Please select an exception rule first.", type="warning")
            return
        rule_dialog(sel[0]["id"])

    async def delete_rule():
        sel = refs["rtab"].selected
        if not sel:
            ui.notify("Please select an exception rule first.", type="warning")
            return
        idx = sel[0]["id"]
        rules = SETTINGS.get("exception_rules", [])
        if 0 <= idx < len(rules) and await confirm("Remove this exception rule?", "Remove"):
            rules.pop(idx)
            persist()
            refresh_rules()

    # Setup Wizard
    wiz_vals = {}
    with ui.dialog().props("persistent") as wiz, ui.card().style("max-width:95vw; width:680px"):
        ui.label("Welcome to Smart Document Sorter - Setup Wizard").classes("text-base font-bold")
        with ui.stepper().props("flat animated").classes("w-full") as stepper:
            with ui.step("Input"):
                ui.label("Where are your unsorted personal documents currently stored?").classes("text-sm")
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    wiz_vals["in"] = ui.input("Input folder").props("outlined dense").classes("flex-1 min-w-0")

                    async def wiz_browse_in():
                        chosen = await pick_folder(wiz_vals["in"].value or "")
                        if chosen:
                            wiz_vals["in"].set_value(chosen)

                    ui.button("Browse...", on_click=wiz_browse_in).props("outline no-caps")
                ui.label("Your original files are never moved, modified or deleted.").classes("text-xs text-slate-500")

                def w_next1():
                    value = (wiz_vals["in"].value or "").strip()
                    if not value or not Path(value).exists():
                        ui.notify("Please select a valid Input directory.", type="warning")
                        return
                    stepper.next()

                with ui.stepper_navigation():
                    ui.button("Cancel", on_click=lambda: wiz.close()).props("flat no-caps")
                    ui.button("Next", on_click=w_next1).props("no-caps")

            with ui.step("Output"):
                ui.label("Where should your organized archive be created?").classes("text-sm")
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    wiz_vals["out"] = ui.input("Output folder").props("outlined dense").classes("flex-1 min-w-0")

                    async def wiz_browse_out():
                        chosen = await pick_folder(wiz_vals["out"].value or "")
                        if chosen:
                            wiz_vals["out"].set_value(chosen)

                    ui.button("Browse...", on_click=wiz_browse_out).props("outline no-caps")

                def w_next2():
                    input_value = (wiz_vals["in"].value or "").strip()
                    output_value = (wiz_vals["out"].value or "").strip()
                    if not output_value:
                        ui.notify("Please select an Output directory.", type="warning")
                        return
                    if input_value and Path(input_value).resolve() == Path(output_value).resolve():
                        ui.notify("Input and Output cannot be the same folder.", type="negative")
                        return
                    stepper.next()

                with ui.stepper_navigation():
                    ui.button("Back", on_click=lambda: stepper.previous()).props("flat no-caps")
                    ui.button("Next", on_click=w_next2).props("no-caps")

            with ui.step("Owner"):
                ui.label("Who is the primary archive owner?").classes("text-sm")
                wiz_vals["owner"] = ui.input("Primary owner full name", placeholder="e.g. Alex Smith").props(
                    "outlined dense"
                ).classes("w-full")
                ui.label(
                    "Primary owner documents go directly under the category folders. "
                    "Other household members go to Family/<Name>/."
                ).classes("text-xs text-slate-500")
                with ui.stepper_navigation():
                    ui.button("Back", on_click=lambda: stepper.previous()).props("flat no-caps")
                    ui.button("Next", on_click=lambda: stepper.next()).props("no-caps")

            with ui.step("Preset"):
                ui.label("Choose your sorting preset and preferences:").classes("text-sm")
                wiz_vals["preset"] = ui.radio(list(PRESETS.keys()), value="Balanced").props("inline")
                wiz_vals["rename"] = ui.checkbox("Auto-rename scanned images with clean dates and categories", value=True)

                def w_finish():
                    owner = (wiz_vals["owner"].value or "").strip()
                    SETTINGS["input_dir"] = (wiz_vals["in"].value or "").strip()
                    SETTINGS["output_dir"] = (wiz_vals["out"].value or "").strip()
                    SETTINGS["sorting_preset"] = wiz_vals["preset"].value
                    SETTINGS["auto_rename"] = bool(wiz_vals["rename"].value)
                    SETTINGS["onboarding_completed"] = True
                    if owner:
                        SETTINGS["primary_person"] = owner
                        family = SETTINGS.setdefault("family_config", {})
                        if owner not in family:
                            family[owner] = generate_auto_aliases(owner)
                    persist()
                    refs["in"].set_value(SETTINGS["input_dir"])
                    refs["out"].set_value(SETTINGS["output_dir"])
                    refs["preset"].set_value(SETTINGS["sorting_preset"])
                    refresh_family()
                    log("[*] Initial setup completed successfully.")
                    set_banner("info", "Setup complete. Click '1. Build Plan' to start.")
                    wiz.close()
                    show("run")

                with ui.stepper_navigation():
                    ui.button("Back", on_click=lambda: stepper.previous()).props("flat no-caps")
                    ui.button("Complete Setup", on_click=w_finish).props("no-caps color=positive")

    def open_wizard():
        wiz_vals["in"].set_value(SETTINGS.get("input_dir", ""))
        wiz_vals["out"].set_value(SETTINGS.get("output_dir", ""))
        wiz_vals["owner"].set_value(SETTINGS.get("primary_person", ""))
        preset = SETTINGS.get("sorting_preset", "Balanced")
        wiz_vals["preset"].set_value(preset if preset in PRESETS else "Balanced")
        wiz_vals["rename"].set_value(bool(SETTINGS.get("auto_rename", True)))
        stepper.set_value("Input")
        wiz.open()

    def maybe_first_run():
        done = SETTINGS.get("onboarding_completed", False)
        has_paths = bool(SETTINGS.get("input_dir") and SETTINGS.get("output_dir"))
        if not done or not has_paths:
            open_wizard()

    # Finish handling
    def handle_finish(fin: dict):
        dry = fin["dry_run"]
        if not fin["ok"]:
            set_banner("error", f"Run failed: {fin['error']}")
            ui.notify(f"Error: {fin['error']}", type="negative")
            return
        refresh_vocab_table()
        summary = fin["summary"] or {}
        total = summary.get("total", 0)
        elapsed = summary.get("elapsed", 0.0)
        code_cnt = summary.get("code", 0)
        ollama_cnt = summary.get("ollama", 0)
        start_t = fin.get("start", "N/A")
        end_t = fin.get("end", "N/A")

        if fin["stopped"]:
            set_banner("warning", f"Stopped by user after {total} document(s). Progress was saved.")
            ui.notify("Run stopped. Progress saved.", type="warning")
        elif dry:
            telemetry_msg = (
                f"Dry Run Complete | Processed: {total} | Start: {start_t} | End: {end_t} | "
                f"Python: {code_cnt} docs | Rule checks: {summary.get('python_classification_seconds', 0.0):.2f}s | "
                f"Ollama: {ollama_cnt} docs | Ollama calls: {summary.get('ollama_classification_seconds', 0.0):.2f}s | "
                f"Run elapsed: {elapsed:.2f}s"
            )
            log(f"[Telemetry] {telemetry_msg}")
            set_banner("success", telemetry_msg + ". Check Review Plan, then apply.")
            ui.notify("Dry Run telemetry generated successfully.", type="positive")
        else:
            set_banner("success", f"Apply complete: {total} document(s) processed in {elapsed:.1f}s. Output folder is ready.")
            ui.notify("Files copied successfully.", type="positive")

        if not fin["stopped"]:
            STATE["progress"] = 1.0
        load_plan()
        fb = sum(1 for r in review["records"] if _is_ai_fallback(r))
        STATE["kpi_ollama"] = f"{max(0, ollama_cnt - fb)} real / {fb} fallback"
        if fb:
            log(f"[WARNING] {fb} file(s) counted as Ollama were fallbacks: the AI did not answer.")
            set_banner("warning", f"{fb} file(s) got a fallback instead of an AI answer. "
                       "Check Ollama (ollama list), then rebuild the plan with Ignore cache.")
        load_folders()
        if total == 0 and dry and not fin["stopped"]:
            set_banner("warning", "No supported files were found in the input folder.")
        if dry and not fin["stopped"]:
            ask_folder_questions()
            ask_review_questions()

    # UI refresh from shared run state
    def tick():
        if log_idx["gen"] != STATE["log_gen"]:
            refs["log"].clear()
            log_idx["i"], log_idx["gen"] = 0, STATE["log_gen"]
        lines = STATE["log"][log_idx["i"]:]
        for line in lines:
            refs["log"].push(line)
        log_idx["i"] += len(lines)

        refs["progress"].set_value(STATE["progress"])
        refs["status"].set_text(STATE["status"])
        for key in ("kpi_total", "kpi_code", "kpi_ollama", "kpi_start", "kpi_end"):
            refs[key].set_text(STATE[key])

        kind, text = STATE["banner"]
        refs["banner_lbl"].set_text(text)
        refs["banner"].classes(
            replace=f"w-full items-center p-2 rounded-lg border {BANNER_STYLES.get(kind, BANNER_STYLES['info'])}"
        )

        running = STATE["running"]
        for key in ("btn_plan", "btn_apply", "btn_resort"):
            refs[key].set_enabled(not running)
        refs["btn_stop"].set_enabled(running and not STATE["stop_event"].is_set())

        fin = STATE["finished"]
        if fin is not None:
            STATE["finished"] = None
            handle_finish(fin)

    for line in STATE["log"]:
        refs["log"].push(line)
    log_idx["i"] = len(STATE["log"])
    refresh_family()
    refresh_rules()
    load_plan()
    load_folders()
    show("run")
    ui.timer(0.5, tick)
    ui.timer(0.4, maybe_first_run, once=True)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="Smart Document Sorter", port=8080, reload=False, show=True, favicon="📁")