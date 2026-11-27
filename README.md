# Smart Document Sorter (100% Offline AI Organization)

An easy-to-use, **100% offline** desktop application that automatically reads, renames, deduplicates, and organizes messy PDFs, scanned documents, receipts, invoices, Word/Excel files, and photos into clean personal and family folders.

Built with native support for **Dutch (Netherlands)**, **Indian**, and **Global English** documents.

---

## ✨ Key Features

* 🔒 **100% Offline & Private:** Nothing ever leaves your computer. All OCR and AI runs locally using PyMuPDF, RapidOCR, and Ollama (`qwen2.5:1.5b`).
* 🛡️ **Non-Destructive Safe Copy:** Your original files are **never modified, moved, or deleted** — the sorter only copies them to your organized output folder.
* ⚡ **Fast Hybrid Engine:** Over 85% of documents are sorted instantly using fast Python rules (~1 ms per file); local AI (Ollama) is only called for ambiguous files.
* 🖥️ **Modern Browser Interface:** Powered by NiceGUI — includes a First-Time Setup Wizard, live progress tracking, an in-app Plan Review & Edit table, Folder Preservation prompts, Family Management, and Exception Rules.
* 👨‍👩‍👧‍👦 **Smart Family Matching:** Automatically separates documents by family member with built-in middle-name protection so parent/child names don't get confused.
* 🗂️ **Smart Auto-Rename:** Automatically renames messy scanner files (like `IMG_0023.pdf`) into clean readable names: `[Name]_[Date]_[Category_Vendor].[ext]` (while keeping `Photos & People` filenames untouched).
* 📁 **Folder Preservation Policy:** Automatically detects topic folders (e.g., `GYM/`, `Trip/`) and asks whether you want to **Keep** them intact or **Sort** their contents.
* 🏢 **Automatic Vendor Grouping:** Groups multiple bills from the same provider into clean subfolders (e.g., `Banking/ING/`, `Invoices/Ziggo/`).
* ⏰ **Expiration Tracking:** Detects expiry dates on Passports, IDs, Driving Licenses, and Insurance policies and flags them as **Valid**, **Expiring Soon (<90 days)**, or **Expired**.
* 🔍 **Smart Deduplication:** Skips exact duplicates (SHA-256) and flags near-duplicates without falsely merging monthly bills.
* 📋 **Structured Logging:** Saves timestamped run logs to `runtime/logs/sorter.log` (rotating at 5 MB) plus a detailed `debug_run_report.txt`.

---

## 🚀 Easy Step-by-Step Setup & Usage

### Step 1: Install Python (One-Time)
Make sure **Python 3.11 or 3.12** is installed from [python.org/downloads](https://www.python.org/downloads/).
> ⚠️ During installation, check **"Add Python to PATH"**.

### Step 2: Download Smart Document Sorter
Download or clone this repository to your computer:
```bash
git clone https://github.com/OzaKunal-786/SmartDocSorter.git
cd SmartDocSorter
```
*(Or click **Code → Download ZIP** on GitHub and extract the folder.)*

### Step 3: Double-Click `start.bat` (Automatic Setup & Launch)
Simply double-click **`start.bat`**. It automatically does everything for you:
1. Creates an isolated Python environment (`.venv`).
2. Installs all required libraries from `requirements.txt` (`nicegui`, `pymupdf`, `rapidocr-onnxruntime`, `ollama`, etc.).
3. Checks for **Ollama** (and tries to install it via `winget` / download the lightweight `qwen2.5:1.5b` model if available).
4. Opens the **SmartDocSorter Browser Interface** automatically!

> 💡 *Note: Even without Ollama installed, SmartDocSorter works in fast rule-based mode and sorts 85%+ of documents automatically.*

### Step 4: Sort Your Documents in the App
1. **First-Time Wizard:** Pick your **Input Folder** (messy files), **Output Folder** (where organized copies go), and optionally add family member names.
2. **Stage 1 — Safe Dry-Run (Plan):** Click **Run Dry-Run Plan**. Nothing is copied yet! You can preview every file's destination, change categories/people directly in the **Review Plan** table, and answer any **Folder Decisions** (`Keep` vs `Sort`).
3. **Stage 2 — Apply:** Once happy with the preview, click **Apply Plan** to copy and organize your files safely.

---

## 💻 Optional: Command-Line Usage (CLI)

If you prefer running from a terminal instead of the browser UI:

```bash
# Stage 1: Build a safe dry-run plan and review_dashboard.html (no files copied)
python smart_doc_sorter.py --plan

# Stage 2: Apply the reviewed plan and copy files
python smart_doc_sorter.py --apply

# Test only the first 25 files
python smart_doc_sorter.py --plan --limit 25

# Re-evaluate all files ignoring cached results
python smart_doc_sorter.py --plan --ignore-cache
```

---

## 📁 Project Structure

```text
SmartDocSorter/
├── start.bat                    # One-click auto-installer & launcher (Windows)
├── run_browser_gui.py           # Modern NiceGUI browser desktop interface
├── smart_doc_sorter.py          # Thin CLI entry point & backward-compatible facade
├── requirements.txt             # All required Python libraries (auto-installed by start.bat)
├── user_settings_template.json  # Clean example configuration (no personal data)
├── .gitignore                   # Blocks personal settings, caches, logs & venv from Git
├── README.md                    # Quick-start guide (this file)
├── TECHNICAL_DOCUMENTATION.md   # Easy-language guide to every file & dependency
└── core/                        # Modular sorting engine
    ├── __init__.py              # Public exports for the core package
    ├── config.py                # Paths, categories, constants & text normalizers
    ├── extractor.py             # Reads PDFs, Word, Excel, text & runs RapidOCR on images
    ├── rules.py                 # Keyword/regex dictionaries & learning.csv loader
    ├── person_matcher.py        # Family member matching & middle-name protection
    ├── classifier.py            # 3-tier classification (Exceptions -> Rules -> Ollama AI)
    ├── deduplicator.py          # Exact (SHA-256) & near-duplicate (pHash/text) checks
    ├── folder_policy.py         # Smart Keep-vs-Sort decisions for topic folders
    ├── renamer.py               # Standardized [Name]_[Date]_[Category_Vendor] renaming
    ├── storage.py               # Safe file copying, atomic JSON writes & collision handling
    ├── reporter.py              # Generates offline review_dashboard.html & debug reports
    ├── run_audit.py             # Per-file audit trail for transparency & debugging
    ├── logger.py                # Rotating structured logger (runtime/logs/sorter.log)
    └── pipeline.py              # Main orchestrator connecting all stages together
```

---

## ⚙️ Configuration (`user_settings.json`)

Your personal settings (folder paths, family names, presets) are stored locally in `user_settings.json` (which is **gitignored** so your personal data is never uploaded). See [`user_settings_template.json`](user_settings_template.json) for an example structure:

* **`auto_rename`** (`true` / `false`): Renames organized documents to `[Name]_[Date]_[Category_Vendor].[ext]`. Photos in `Photos & People` are **never** renamed.
* **`sorting_preset`**: Choose `"balanced"` (default), `"strict"` (sends more edge cases to AI), or `"fast"` (relies more on instant Python rules).

---

## 📖 Detailed Documentation

For a beginner-friendly explanation of **every file**, **every dependency**, and **how the sorting engine works under the hood**, see [`TECHNICAL_DOCUMENTATION.md`](TECHNICAL_DOCUMENTATION.md).
