"""
core/deduplicator.py - SHA-256 exact-duplicate & SequenceMatcher near-duplicate guards.
Pure deduplication math; completely decoupled from classification.
"""

import hashlib
import difflib
import re
from pathlib import Path
from typing import Optional

from core.config import normalize_for_matching


def compute_sha256(file_path: Path) -> str:
    """
    Computes standard SHA-256 hex digest for exact byte-for-byte deduplication.
    """
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def hamming_distance_hex(hash1: str, hash2: str) -> int:
    """
    Computes Hamming distance between two hex string perceptual hashes.
    """
    try:
        return bin(int(hash1, 16) ^ int(hash2, 16)).count("1")
    except Exception:
        return 64


def _is_true_near_duplicate(
    cand_fn: str,
    cand_person: str,
    cand_cat: str,
    cand_text: str,
    cand_phash: Optional[str],
    existing_fn: str,
    existing_person: str,
    existing_cat: str,
    existing_text: str,
    existing_phash: Optional[str]
) -> bool:
    """
    Strict Near-Duplicate Verification:
    1. Must belong to the SAME Person and SAME Category.
    2. Never flag different ID types (Aadhar vs Pan vs Passport vs Election Card).
    3. Never flag numbered/monthly series (e.g. Apr26 vs Aug-26).
    4. Strips scanner watermarks and requires >= 88% text similarity on >= 50 non-watermark chars.
    """
    if cand_person != existing_person or cand_cat != existing_cat:
        return False

    # Guard 1: Distinct document types in filenames can NEVER be near-duplicates
    id_types = ["aadhar", "pan", "passport", "election", "voter", "driving", "license", "birth", "marriage"]
    f1 = normalize_for_matching(Path(cand_fn).stem)
    f2 = normalize_for_matching(Path(existing_fn).stem)
    f1_types = {t for t in id_types if t in f1}
    f2_types = {t for t in id_types if t in f2}
    if f1_types and f2_types and f1_types != f2_types:
        return False

    # Guard 2: Do not flag month-named files or series differing only by numbers/letters
    if Path(cand_fn).stem.lower() != Path(existing_fn).stem.lower():
        month_re = re.compile(r"(?i)^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[_\-\s]*\d{2,4}$")
        if month_re.match(Path(cand_fn).stem) or month_re.match(Path(existing_fn).stem):
            return False
        s1_alpha = re.sub(r"[\d_\-\s]+", "", Path(cand_fn).stem.lower())
        s2_alpha = re.sub(r"[\d_\-\s]+", "", Path(existing_fn).stem.lower())
        if s1_alpha == s2_alpha and Path(cand_fn).stem.lower() != Path(existing_fn).stem.lower():
            return False

    # Guard 3: Strip scanner watermarks before measuring similarity
    c_sub = re.sub(r"(?i)(camscanner|adobe\s*scan|scanned\s+by|page\s*break|---)", " ", cand_text[:800])
    e_sub = re.sub(r"(?i)(camscanner|adobe\s*scan|scanned\s+by|page\s*break|---)", " ", existing_text[:800])
    c_clean = re.sub(r"\s+", " ", c_sub).strip().lower()
    e_clean = re.sub(r"\s+", " ", e_sub).strip().lower()

    if len(c_clean) >= 50 and len(e_clean) >= 50:
        ratio = difflib.SequenceMatcher(None, c_clean, e_clean).quick_ratio()
        return ratio >= 0.88

    if cand_cat == "Photos & People" and cand_phash and existing_phash:
        return hamming_distance_hex(cand_phash, existing_phash) <= 1

    return False
