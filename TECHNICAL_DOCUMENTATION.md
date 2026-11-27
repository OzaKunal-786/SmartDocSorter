# Smart Document Sorter — Technical Documentation

> **An easy-to-read guide explaining what Smart Document Sorter does, how it works under the hood, what every file does, and what every library in `requirements.txt` is used for.**

---

## 1. What Is Smart Document Sorter?

**Smart Document Sorter** is a 100% offline personal document organizer. It scans a messy folder full of PDFs, scanned images, Word documents, Excel sheets, and photos, understands what each file is, and safely copies it into a clean, structured folder tree.

### Key Guarantees
* **100% Offline:** No document text, filename, or personal data is ever sent to the internet.
* **Non-Destructive:** Your original files are **never modified, moved, or deleted**. Everything is copied using `shutil.copy2` (which also keeps original file timestamps).
* **No Personal Data in Code:** All family names, folder paths, and custom rules live in your local `user_settings.json` (which is excluded from Git via `.gitignore`).

---

## 2. How It Works (Step-by-Step)

When you run a **Dry-Run Plan** or **Apply**:

1. **Scan (`storage.py` & `folder_policy.py`):** Walks through your input folder, ignores system/hidden directories, and checks whether any topic subfolders (like `GYM/` or `Vacation/`) should be **Kept intact** or **Sorted**.
2. **Exact Duplicate Check (`deduplicator.py`):** Computes a SHA-256 fingerprint for every file. Exact byte-for-byte duplicates are skipped so you don't store copies twice.
3. **Text & Image Extraction (`extractor.py`):**
   * Digital PDFs → extracted directly with `pymupdf`.
   * Scanned PDFs & images → read offline with `rapidocr-onnxruntime`.
   * Word (`.docx`) & Excel (`.xlsx`) → parsed with `python-docx` and `openpyxl`.
   * Images → also get a perceptual hash (`imagehash`) to spot visually similar photos.
4. **3-Tier Classification (`classifier.py`):**
   * **Tier 1 — Exception Rules:** Checks your custom overrides first (e.g., *"always put files matching `waternet` into `Invoices`"*).
   * **Tier 2 — Fast Python Rules (`rules.py`):** Matches keywords and patterns for Taxes, Banking, Identity, Medical, Vehicles, Insurance, etc. (~1 ms per file).
   * **Tier 3 — Local AI Fallback (`ollama`):** Only if Python rules aren't confident enough, it asks the local `qwen2.5:1.5b` model via Ollama.
5. **Person & Vendor Matching (`person_matcher.py`):** Identifies which family member owns the document (with middle-name protection so a child's document mentioning a parent's name isn't misrouted) and detects vendor names (e.g., `ING`, `Ziggo`, `Belastingdienst`).
6. **Auto-Rename (`renamer.py`):** If `auto_rename` is enabled, messy filenames like `IMG_0023_scan.pdf` are cleaned into `[Name]_[Date]_[Category_Vendor].[ext]`. Files in `Photos & People` are **never** renamed.
7. **Review & Apply (`run_browser_gui.py` / `storage.py` / `reporter.py`):**
   * In **Dry-Run Plan** mode, it builds `sort_plan.json`, `sorting_plan.csv`, `review_dashboard.html`, and `debug_run_report.txt` so you can review and edit everything in the UI before copying a single file.
   * In **Apply** mode, it copies files to their final destinations with collision protection.

---

## 3. Explanation of Every File in the Project

### Root Files
| File | What It Does (In Easy Words) |
| :--- | :--- |
| **`start.bat`** | **One-click launcher for Windows.** Checks Python, creates `.venv`, installs all libraries from `requirements.txt`, checks/sets up Ollama AI, and launches the browser interface (`run_browser_gui.py`). |
| **`run_browser_gui.py`** | **The Modern Browser Interface (NiceGUI).** Gives you a clean sidebar app in your browser with a Setup Wizard, Live Progress, interactive Plan Review table (change Category/Person with dropdowns), Folder Decisions (`Keep` vs `Sort`), Family Manager, Exception Rules, and Settings Presets (`Balanced`, `Strict`, `Fast & Relaxed`). |
| **`smart_doc_sorter.py`** | **CLI Entry Point & Compatibility Facade.** Lets power users run sorting from the command line (`--plan`, `--apply`, `--limit`, `--ignore-cache`) and re-exports `core/` functions for `run_browser_gui.py`. |
| **`requirements.txt`** | **Dependency List.** Tells Python which libraries to install automatically when `start.bat` runs. |
| **`user_settings_template.json`** | **Clean Example Config.** Shows the structure of `user_settings.json` with safe placeholder values (no personal data). |
| **`.gitignore`** | **Git Safety Shield.** Prevents caches (`__pycache__`, `runtime/`), virtual environments (`.venv/`), and your personal files (`user_settings.json`, `learning.csv`) from ever being uploaded to GitHub. |
| **`README.md`** | **Quick-Start Guide.** Simple step-by-step instructions to download, install, and run the app. |
| **`TECHNICAL_DOCUMENTATION.md`** | **This Document.** Plain-English reference for every file, dependency, and workflow. |

### `core/` Package (The Sorting Engine)
| File | What It Does (In Easy Words) |
| :--- | :--- |
| **`core/__init__.py`** | **Package Exports.** Exposes all main functions and constants so `smart_doc_sorter.py` and the GUI can import them cleanly. |
| **`core/config.py`** | **Central Settings & Constants.** Holds default paths, the 13 valid document categories, supported file extensions, confidence thresholds, and text normalization helpers. Contains zero personal data. |
| **`core/extractor.py`** | **Document Reader.** Extracts text from PDFs, Word `.docx`, Excel `.xlsx`, plain `.txt`, and runs offline RapidOCR on scanned PDFs and images. Also computes perceptual image hashes. |
| **`core/rules.py`** | **Pattern & Keyword Engine.** Contains the built-in regex rules for Dutch, Indian, and Global documents (Taxes, Banking, Identity, Vehicles, Insurance, etc.) and loads/saves institutional keywords in `learning.csv`. |
| **`core/person_matcher.py`** | **Family & Vendor Matcher.** Matches documents to family members defined in `user_settings.json`, dynamically shields overlapping/middle names so members aren't confused, and unifies vendor names. |
| **`core/classifier.py`** | **3-Tier Decision Brain.** Runs Exception Rules → Deterministic Python Rules → Local Ollama AI (`qwen2.5:1.5b`). Also checks document expiry dates (`Valid`, `Expiring Soon`, `Expired`). |
| **`core/deduplicator.py`** | **Duplicate Detector.** Computes SHA-256 hashes for exact duplicates and compares text/image similarity for near-duplicates (without falsely merging monthly bills). |
| **`core/folder_policy.py`** | **Folder Preservation Logic.** Walks subfolders and decides whether a folder is a generic wrapper (`Documents/`), a category/family folder (`SORT`), or a specific topic folder (`GYM/`, `Trip/`) to ask the user (`Keep` vs `Sort`). |
| **`core/renamer.py`** | **Auto-Rename Engine.** Strips scanner prefixes (`IMG_`, `SCAN_`, `DSC_`) and builds clean filenames: `[Name]_[Date]_[Category_Vendor].[ext]`. Never renames `Photos & People`. |
| **`core/storage.py`** | **File & State Manager.** Handles safe atomic JSON/CSV writes (OneDrive-friendly), loads/saves `user_settings.json`, computes destination paths, resolves filename collisions, and copies files. |
| **`core/reporter.py`** | **Report & Dashboard Generator.** Creates the offline `review_dashboard.html` and the detailed `debug_run_report.txt` summary. |
| **`core/run_audit.py`** | **Audit Trail.** Records what happened to every single file during scan, plan, and apply so the debug report can explain every decision. |
| **`core/logger.py`** | **Structured Logger.** Writes timestamped logs to `runtime/logs/sorter.log` (automatically rotating at 5 MB, keeping 3 backups) and prints clean status messages to the console. |
| **`core/pipeline.py`** | **Main Orchestrator.** Connects all modules above into `run_sorter()`, managing progress callbacks, caching (`progress_state.json`), multi-step staging, and final reporting. |

---

## 4. Explanation of Every Dependency (`requirements.txt`)

When you run `start.bat`, `pip install -r requirements.txt` automatically installs these libraries:

| Library | Why It Is Needed (In Easy Words) |
| :--- | :--- |
| **`nicegui`** | Powers the modern browser-based graphical interface (`run_browser_gui.py`) with tables, buttons, wizards, and live progress updates. |
| **`pymupdf`** | Fast PDF reader — extracts text and embedded metadata from digital PDFs and renders scanned PDF pages into images for OCR. |
| **`rapidocr-onnxruntime`** | Offline OCR (Optical Character Recognition) engine — reads text out of scanned PDFs, photos of receipts, and ID cards without internet. |
| **`pydantic`** | Validates and structures the JSON output returned by the local Ollama AI so the app never crashes on unexpected AI text. |
| **`ollama`** | Connects Python to your local Ollama service (`127.0.0.1`) to run the `qwen2.5:1.5b` model offline. |
| **`Pillow`** | Python image library — opens, resizes, and rotates photos/scans before OCR or hashing. |
| **`numpy`** | Fast math/array library required by RapidOCR and image processing. |
| **`imagehash`** | Computes perceptual hashes (pHash) of images so visually identical or slightly resized photos are detected as near-duplicates. |
| **`python-docx`** | Reads paragraphs and tables inside Microsoft Word (`.docx`) files. |
| **`openpyxl`** | Reads worksheets and cell values inside Microsoft Excel (`.xlsx`) files. |
| **`tqdm`** | Displays clean progress bars and speed/ETA estimates when running from the terminal CLI. |

---

## 5. Sorting Presets & Auto-Rename

### Settings Presets (in UI & `user_settings.json`)
Instead of tweaking raw numbers, you can choose a preset:
* **🟢 Balanced (Recommended):** `python_confidence=0.78`, `review_confidence=0.60` — Fast rule matching with high accuracy.
* **🟡 Strict:** `python_confidence=0.85`, `review_confidence=0.75` — Sends more ambiguous files to AI and flags edge cases for your review.
* **🔵 Fast & Relaxed:** `python_confidence=0.65`, `review_confidence=0.50` — Relies heavily on instant Python rules for maximum speed.

### Auto-Rename Format
When `"auto_rename": true`:
```text
[Name]_[Date]_[Category_Vendor].[ext]
```
* Example: `IMG_0023_scan.pdf` → `Primary_Owner_2024-03-15_Taxes_Belastingdienst.pdf`
* **Exception:** Files classified under `Photos & People` **always** keep their original filename.

---

## 6. Safety, Logging & Recovery

* **Structured Logs:** Every run logs to `runtime/logs/sorter.log` with timestamps, log levels (`INFO`, `WARNING`, `ERROR`), and module names.
* **Crash Recovery:** Progress is saved periodically in `runtime/_cache/progress_state.json`. If a run is stopped early, re-running skips already-hashed files automatically.
* **Atomic Writes:** Settings and cache files are written to a `.tmp` file first and swapped atomically to prevent file-locking errors with OneDrive or Google Drive.
