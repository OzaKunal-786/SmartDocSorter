"""
core/renamer.py - Standardized Document Auto-Renaming Engine.
Generates uniform, descriptive filenames:
Format: Context_Category_DDMMYYYY_UniqueHash.[ext] (e.g., ThungaHospitalBill_Medical_11062025_a1b2c3.jpeg)

Guards:
- Context / Descriptive Subject first, followed by Category, ending with 8-character date in DDMMYYYY format and a 6-char SHA-256 hash suffix.
- Strips out WhatsApp timestamps, camera codes, and metadata noise.
- True personal photos (Photos & People) preserve their original filenames.
- Filesystem sanitization (strips illegal characters on Windows/POSIX).
"""

import re
from pathlib import Path
from typing import Optional

from core.config import IMAGE_EXTENSIONS, normalize_for_matching


# Comprehensive regex to strip camera prefixes, WhatsApp timestamps, dates, and time tokens
NOISE_PREFIXES_RE = re.compile(
    r"^(?:scan(?:ned)?|img|pxl|photo|pic|document|doc|whatsapp\s*image|file|dscn|camscanner|"
    r"\d{4}[-_]\d{1,2}[-_]\d{1,2}[-_]?at[-_]?\d{1,2}[_.]\d{2}[_.]\d{2}[-_]?[ap]m?|"
    r"\d{4}[-_]\d{2}[-_]\d{2}|[0-9]{8}_[0-9]{6}|"
    r"\d{1,2}[_.]\d{2}[_.]\d{2})[_\-\s]*",
    re.IGNORECASE
)


# Renaming is limited to these image types for now (PDF/other formats get their own renamer later)
RENAMEABLE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def sanitize_filename_token(token: str) -> str:
    """
    Cleans a string to be safe inside a filename, replacing illegal characters with underscores.
    """
    if not token:
        return ""
    # Strip illegal filename characters: < > : " / \ | ? *
    clean = re.sub(r'[<>:"/\\|?*]+', '_', token.strip())
    # Replace whitespace sequences with underscores
    clean = re.sub(r'[\s\-]+', '_', clean)
    # Strip leading/trailing underscores
    return clean.strip('_')


def _clean_context(stem: str) -> str:
    """Original filename without camera/WhatsApp/date noise, as a safe token (max 60 chars)."""
    prev = None
    while prev != stem:
        prev = stem
        stem = NOISE_PREFIXES_RE.sub("", stem).strip(" _-.")
    # WhatsApp "at 10.15.30 AM", "WA0001" codes and bare dates (the date is re-added at the end in DDMMYYYY)
    stem = re.sub(r"(?i)(?<![a-z0-9])at[_\-\s]*\d{1,2}[.:_]\d{2}(?:[.:_]\d{2})?[_\-\s]*(?:[ap]m)?(?![a-z0-9])", " ", stem)
    stem = re.sub(r"(?i)(?<![a-z0-9])WA\d+(?![a-z0-9])", " ", stem)
    stem = re.sub(r"(?<!\d)20\d{2}[-_]?\d{2}[-_]?\d{2}(?!\d)", " ", stem)
    stem = re.sub(r"[\s_\-.]+", " ", stem).strip(" _-.")
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)            # trailing "(1)" copy markers
    stem = re.sub(r"(?i)[_\-\s]*copy[_\-\s]*\d*$", "", stem)  # trailing "copy"
    token = sanitize_filename_token(stem)
    return token[:60].strip("_")


def generate_standardized_filename(
    original_filename: str,
    category: str,
    person_name: str = "",
    document_date: Optional[str] = None,
    vendor_group: Optional[str] = None,
    primary_person: str = "",
    file_hash: Optional[str] = None,      # kept for caller compatibility, not used
    time_trailer: Optional[str] = None    # kept for caller compatibility, not used
) -> str:
    """
    Image-only renamer. Format: Context_Category_DDMMYYYY.ext
    Example: 'ThungaHospitalBill_Medical_11062025.jpeg'

    - Only .jpg / .jpeg / .png are renamed; every other format keeps its original name.
    - 'Photos & People' images keep their original name.
    - Context = cleaned original filename; if nothing meaningful is left: vendor, then person name.
    - Date = document date, else a date found in the original filename; omitted if none.
    - Name clashes (_2, _3) are handled at copy time in storage.resolve_collision.
    """
    path = Path(original_filename)
    stem = path.stem
    ext = path.suffix.lower()

    if ext not in RENAMEABLE_IMAGE_EXTENSIONS:
        return original_filename
    if category == "Photos & People":
        return original_filename

    clean_cat = sanitize_filename_token(category) if category else "Miscellaneous"

    # 1. CONTEXT
    context = _clean_context(stem)
    if not re.search(r"[A-Za-z]{3}", context or ""):  # nothing meaningful left (only digits/codes)
        if vendor_group:
            context = sanitize_filename_token(vendor_group)
        elif person_name and person_name.lower() not in {"general", "none", "unknown", ""}:
            context = sanitize_filename_token(person_name)
        else:
            context = ""

    # 2. DATE (DDMMYYYY)
    clean_date = ""
    if document_date:
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", document_date)
        if m:
            clean_date = f"{int(m.group(3)):02d}{int(m.group(2)):02d}{m.group(1)}"
    if not clean_date:
        m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", stem)
        if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
            clean_date = f"{m.group(3)}{m.group(2)}{m.group(1)}"

    tokens = []
    if context:
        tokens.append(context)
    if not context or context.lower() != clean_cat.lower():
        tokens.append(clean_cat)
    if clean_date:
        tokens.append(clean_date)

    base_name = "_".join(tokens)[:180]
    return f"{base_name}{ext}" if base_name else original_filename
