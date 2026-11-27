"""
core/config.py - Central configuration constants, patterns, and path settings.
Zero hardcoded personal data or file manipulation logic.
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set

# Bypass corporate proxy for local 127.0.0.1 Ollama connections
os.environ["NO_PROXY"] = "localhost,127.0.0.1,0.0.0.0"
os.environ["no_proxy"] = "localhost,127.0.0.1,0.0.0.0"
for _proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(_proxy_var, None)

# ==============================================================================
# PATH CONFIGURATION
# ==============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT_DIR = PROJECT_ROOT / "runtime"
INTERNAL_DATA_DIR = RUNTIME_ROOT_DIR / "_cache"
CURRENT_RUN_DIR = RUNTIME_ROOT_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
LEARNING_CSV_PATH = PROJECT_ROOT / "learning.csv"
PROGRESS_STATUS_PATH = CURRENT_RUN_DIR / "progress_status.json"
USER_SETTINGS_PATH = PROJECT_ROOT / "user_settings.json"

DEFAULT_INPUT_DIR = str(Path.home() / "Documents" / "Personal")
DEFAULT_OUTPUT_DIR = str(Path.home() / "Documents" / "Organized_Personal")

# ==============================================================================
# PIPELINE DEFAULTS & THRESHOLDS
# ==============================================================================
DRY_RUN = True
MODEL_NAME = "qwen2.5:1.5b"
DEFAULT_PYTHON_CONFIDENCE_THRESHOLD = 0.78
DEFAULT_REVIEW_CONFIDENCE_THRESHOLD = 0.60
EXPIRING_SOON_DAYS = 90
MAX_IMAGE_DIM = 800
STATE_SAVE_INTERVAL = 25
PHASH_HAMMING_THRESHOLD = 2
RECURRING_NAME_MIN_DOCS = 3
MIN_DOCS_FOR_VENDOR_CLUBBING = 3

DEFAULT_FAMILY_CONFIG: Dict[str, List[str]] = {}

# Single source of truth for sorting presets (used by CLI, GUI and pipeline)
SORTING_PRESETS: Dict[str, Dict] = {
    "balanced":     {"label": "Balanced",       "py_conf": 0.78, "rev_conf": 0.60},
    "strict":       {"label": "Strict",         "py_conf": 0.95, "rev_conf": 0.90},
    "fast_relaxed": {"label": "Fast & Relaxed", "py_conf": 0.65, "rev_conf": 0.45},
}


def resolve_preset(name) -> str:
    """Normalises any preset spelling ('Fast & Relaxed', 'fast', 'Strict') to a SORTING_PRESETS key."""
    n = re.sub(r"[^a-z]+", "_", str(name or "").strip().lower()).strip("_")
    if n in SORTING_PRESETS:
        return n
    if n.startswith("fast"):
        return "fast_relaxed"
    return "balanced"

# ==============================================================================
# CATEGORIES & EXTENSIONS
# ==============================================================================
VALID_CATEGORIES = [
    "Invoices", "Taxes", "Medical", "Contracts", "Employment",
    "Banking", "Identity", "Insurance", "Real Estate", "Vehicles",
    "Education", "Photos & People", "Miscellaneous"
]

EXPIRABLE_CATEGORIES = {"Identity", "Vehicles", "Insurance"}

# Categories stored inside the main "Work" folder: Work/<Category>/...
WORK_CATEGORIES = {"Employment", "Contracts"}

SPECIFIC_DOMAIN_CATEGORIES = {
    "Medical", "Vehicles", "Taxes", "Insurance", "Real Estate", "Identity", "Employment", "Education"
}

SUPPORTED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp",
    ".docx", ".xlsx", ".ppt", ".pptx", ".txt"
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp"}

IGNORED_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".gradle", "build", ".idea",
    ".vscode", "bin", "obj", "target", ".venv", "venv", "__pycache__",
    ".cache", "dist", ".sorter_data"
}

IGNORED_FILENAMES = {
    "readme.txt", "readme.md", "license.txt", "debug_run_report.txt",
    "review_dashboard.html", "user_settings.json", "learning.csv", "progress_status.json"
}

LEARNING_STOPWORDS: Set[str] = {
    "document", "documents", "scanned", "scanner", "image", "photo", "whatsapp",
    "download", "downloads", "final", "draft", "version", "copy", "attachment",
    "personal", "general", "folder", "archive", "file", "files", "page", "pages",
    "letter", "number", "total", "amount", "date", "datum", "name", "address",
    "street", "india", "nederland", "netherlands", "amsterdam", "mumbai", "pune",
    "online", "portal", "customer", "client", "service", "services", "print",
    "printed", "signed", "signature", "report", "summary", "details", "information",
    "status", "valid", "expired", "original", "duplicate", "email", "january",
    "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december", "januari", "februari", "maart", "augustus", "oktober",
    "geachte", "mevrouw", "meneer", "heer", "betreft", "bijlage", "statement",
    "certificate", "invoice", "receipt", "agreement", "contract"
}

NON_HUMAN_NAME_WORDS: Set[str] = {
    "sportcity", "sport", "city", "fitness", "gym", "basicfit", "club", "kaart", "groene",
    "insurance", "verzekering", "bank", "axis", "consolidated", "statement", "january",
    "amsterdam", "netherlands", "nederland", "india", "company", "hospital", "clinic",
    "school", "international", "community", "department", "ministry", "applicant", "checklist"
}

# ==============================================================================
# PATTERNS (GENERAL & VENDOR CATALOG)
# ==============================================================================
SCANNER_WATERMARK_RE = re.compile(
    r"(?i)(camscanner|adobe\s*scan|microsoft\s*lens|genius\s*scan|clear\s*scan|tapscanner|scanned\s+by)"
)

SECONDARY_RELATION_LINE_RE = re.compile(
    r"(?im)^.*?\b(?:father'?s?\s*name|mother'?s?\s*name|spouse'?s?\s*name|husband'?s?\s*name|"
    r"wife'?s?\s*name|nominee\s*name|guardian\s*name|son\s+of|daughter\s+of|wife\s+of|s/o|d/o|w/o|c/o)\b.*$"
)

PHOTO_FILENAME_RE = re.compile(
    r"(?i)\b(photo\w*|pic|headshot|selfie|portrait|whitebackground|sign|signature|shoot)\b"
)

DATE_PATTERNS = [
    re.compile(r"(?i)(?:date|datum|dated|statement\s+date|factuurdatum|invoice\s+date|issue\s+date)[\s:]*([0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2})"),
    re.compile(r"(?i)(?:date|datum|dated|statement\s+date|factuurdatum|invoice\s+date|issue\s+date)[\s:]*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})"),
    re.compile(r"\b([0-9]{4}[-/][0-9]{2}[-/][0-9]{2})\b"),
    re.compile(r"\b([0-9]{1,2}[-/][0-9]{2}[-/][0-9]{4})\b"),
]

EXPIRATION_PATTERNS = [
    re.compile(r"(?i)(?:valid\s+until|date\s+of\s+expiry|expiry\s+date|expires\s+on|expiration\s+date|geldig\s+tot|vervaldatum|valid\s+upto|valid\s+thru)[\s:]*([0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2})"),
    re.compile(r"(?i)(?:valid\s+until|date\s+of\s+expiry|expiry\s+date|expires\s+on|expiration\s+date|geldig\s+tot|vervaldatum|valid\s+upto|valid\s+thru)[\s:]*([0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{4})"),
]

CANDIDATE_PERSON_LABEL_RE = re.compile(
    r"(?m)(?:Name|Patient\s*Name|Employee\s*Name|Policyholder|Insured\s*Name|Naam|T\.a\.v\.|De\s+heer|Mevrouw)"
    r"[ \t:.\-]+([A-Z][a-z]{2,15}(?:[ \t]+[A-Z][a-z]{2,15}){1,2})"
)

KNOWN_VENDORS: Dict[str, re.Pattern] = {
    "Ziggo": re.compile(r"(?i)\bziggo\b"),
    "KPN": re.compile(r"(?i)\bkpn\b"),
    "Odido_T-Mobile": re.compile(r"(?i)\b(odido|t-mobile|tele2)\b"),
    "Vodafone": re.compile(r"(?i)\bvodafone\b"),
    "Vattenfall": re.compile(r"(?i)\bvattenfall\b"),
    "Eneco": re.compile(r"(?i)\beneco\b"),
    "Essent": re.compile(r"(?i)\bessent\b"),
    "Waternet_Vitens": re.compile(r"(?i)\b(waternet|vitens|evides|brabant\s+water)\b"),
    "ABN_AMRO": re.compile(r"(?i)\b(abn\s*amro|abna)\b"),
    "ING": re.compile(r"(?i)\bing\s*bank\b"),
    "Rabobank": re.compile(r"(?i)\brabobank\b"),
    "HDFC": re.compile(r"(?i)\bhdfc\b"),
    "ICICI": re.compile(r"(?i)\bicici\b"),
    "SBI": re.compile(r"(?i)\b(state\s+bank\s+of\s+india|sbi)\b"),
    "Axis_Bank": re.compile(r"(?i)\baxis\s*(?:bank|consolidated)?\b"),
    "Kotak": re.compile(r"(?i)\bkotak\b"),
    "Zilveren_Kruis": re.compile(r"(?i)\bzilveren\s*kruis\b"),
    "CZ_Insurance": re.compile(r"(?i)\bcz\s*(?:zorgverzekering|insurance)?\b"),
    "VGZ": re.compile(r"(?i)\bvgz\b"),
    "Payslips": re.compile(r"(?i)\b(payslip|salary\s*slip|loonstrook|salarisspecificatie)\b"),
}

# ==============================================================================
# STRING NORMALIZATION (CAMELCASE + UNDERSCORE SPLITTING)
# ==============================================================================
def normalize_for_matching(s: str) -> str:
    """
    Split CamelCase ('SharmaRohit' -> 'Sharma Rohit', 'ResidenceCard' -> 'Residence Card')
    and letter-digit boundaries ('Card2020' -> 'Card 2020'), lowercased.
    """
    if not s:
        return ""
    # Split lower->Upper ('SharmaRohit' -> 'Sharma Rohit') and Upper->UpperLower ('IndianPassport' -> 'NL Passport')
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    # Split letter-digit boundaries ('Card2020' -> 'Card 2020', '2024Form' -> '2024 Form')
    s = re.sub(r"([a-zA-Z])([0-9])", r"\1 \2", s)
    s = re.sub(r"([0-9])([a-zA-Z])", r"\1 \2", s)
    s = re.sub(r"[_\-./\\()\[\]]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# ==============================================================================
# SCANNER / CAPTURE-TOOL FOLDERS (neutral: they say HOW a file was scanned, never WHAT it is)
# ==============================================================================
SCANNER_TOOL_FOLDER_RE = re.compile(
    r"^(?:scanned\s+|scans?\s+)?(?:cam\s*scanner|adobe\s*scan|microsoft\s*lens|genius\s*scan|clear\s*scan|"
    r"tap\s*scanner|office\s*lens|scanner\s*pro|scanbot|scanned|scans?|scanner)"
    r"(?:\s+(?:docs?|documents?|files?|images?|pdfs?|photos?))?$"
)

# Filename 'old' marker (whole word) -> hard rule: file goes to <Category>/Archive
OLD_FILENAME_RE = re.compile(r"\bold\b")


def strip_scanner_tool_folders(rel_path: str) -> str:
    """Removes scanner-tool folder segments (CamScanner, Scanned_CamScanner, Adobe Scan...) from a relative folder path."""
    if not rel_path:
        return ""
    keep = []
    for seg in re.split(r"[\\/]+", rel_path):
        if not seg or seg == ".":
            continue
        if SCANNER_TOOL_FOLDER_RE.match(normalize_for_matching(seg)):
            continue
        keep.append(seg)
    return "/".join(keep)
