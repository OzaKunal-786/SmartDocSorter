"""
core/classifier.py - Deterministic scoring, Ollama AI fallback, exception rule engine.
Clear decision tree: Exception Rules -> Python Rules -> Ollama Fallback.
"""

import json
import re
from core.config import SCANNER_WATERMARK_RE
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Literal

from pydantic import BaseModel, Field
import ollama

from core.logger import get_logger

log = get_logger("classifier")

from core.config import (
    VALID_CATEGORIES,
    EXPIRABLE_CATEGORIES,
    SPECIFIC_DOMAIN_CATEGORIES,
    IMAGE_EXTENSIONS,
    DEFAULT_PYTHON_CONFIDENCE_THRESHOLD,
    MODEL_NAME,
    EXPIRING_SOON_DAYS,
    PHOTO_FILENAME_RE,
    DATE_PATTERNS,
    EXPIRATION_PATTERNS,
    normalize_for_matching
)
from core.rules import CATEGORY_PATTERNS, FOLDER_PATH_HINTS
from core.person_matcher import canonicalize_person, detect_vendor_or_series, canonicalize_vendor

ollama_client = ollama.Client(host="http://127.0.0.1:11434", trust_env=False)


# ==============================================================================
# PYDANTIC SCHEMA
# ==============================================================================
class DocumentClassification(BaseModel):
    category: Literal[
        "Invoices", "Taxes", "Medical", "Contracts", "Employment",
        "Banking", "Identity", "Insurance", "Real Estate", "Vehicles",
        "Education", "Photos & People", "Miscellaneous"
    ]
    person_name: str
    document_date: Optional[str] = None
    expiration_date: Optional[str] = None
    expiration_status: Literal["valid", "expiring_soon", "expired", "none"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    tags: List[str] = Field(default_factory=list)
    reasoning: str
    suggested_keyword: Optional[str] = None
    vendor_group: Optional[str] = None
    resolved_by: str = "Code"


# ==============================================================================
# DATES & EXPIRATION HELPERS
# ==============================================================================
def parse_date_safe(date_str: Optional[str]) -> Optional[date]:
    """
    Safely parses ISO (YYYY-MM-DD) or European (DD-MM-YYYY) date strings into date objects.
    """
    if not date_str:
        return None
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", date_str)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    match_dmy = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", date_str)
    if match_dmy:
        try:
            return date(int(match_dmy.group(3)), int(match_dmy.group(2)), int(match_dmy.group(1)))
        except ValueError:
            pass
    return None


def calculate_expiration_status(
    expiration_date_str: Optional[str],
    category: str,
    current_date: date
) -> Tuple[Optional[str], str]:
    """
    Computes expiration status ('expired', 'expiring_soon', 'valid', or 'none')
    for expirable categories (Identity, Vehicles, Insurance).
    """
    if category not in EXPIRABLE_CATEGORIES or not expiration_date_str:
        return None, "none"

    parsed = parse_date_safe(expiration_date_str)
    if not parsed or parsed.year < current_date.year - 15:
        return None, "none"

    if parsed < current_date:
        return parsed.isoformat(), "expired"
    elif current_date <= parsed <= (current_date + timedelta(days=EXPIRING_SOON_DAYS)):
        return parsed.isoformat(), "expiring_soon"
    else:
        return parsed.isoformat(), "valid"


def extract_dates_deterministically(text: str, fallback_creation_date: str = "") -> Tuple[Optional[str], Optional[str]]:
    """
    Deterministic regex scan for document date and expiration date in extracted text.
    """
    doc_date, exp_date = None, None
    for p in DATE_PATTERNS:
        m = p.search(text)
        if m:
            doc_date = m.group(1)
            break
    if not doc_date and fallback_creation_date:
        doc_date = fallback_creation_date

    for p in EXPIRATION_PATTERNS:
        m = p.search(text)
        if m:
            exp_date = m.group(1)
            break
    return doc_date, exp_date


# ==============================================================================
# DYNAMIC VENDOR & GENERIC BANKING EXTRACTORS (ZERO HARDCODING)
# ==============================================================================
def is_generic_bank_statement(text: str) -> bool:
    """
    Universally detects bank statements and financial accounts using a whitespace-resilient 
    IBAN/Account pattern and ledger terminology, without relying on hardcoded bank names.
    """
    if not text:
        return False
        
    # Normalize text by removing excessive internal spacing/newlines for structural matching
    clean_text = re.sub(r'\s+', '', text)
    
    # Whitespace-agnostic IBAN pattern (2 letters followed by 10-34 alphanumeric characters)
    has_iban = bool(re.search(r'[a-zA-Z]{2}\d{2}[a-zA-Z0-9]{10,32}', clean_text))
    
    # Alternative account number patterns (e.g., BBAN, Bank Account, Account Number)
    has_account_pattern = bool(re.search(r'(?:account|rekening|iban|bic|swift|sortcode|nre|nro)[\s\d\w-]{3,20}\d{4,}', text, re.IGNORECASE))

    has_ledger_terms = any(term in text.lower() for term in [
        "statement", "rekeningafschrift", "extracto", "relevé", 
        "balance", "saldo", "opening balance", "closing balance", "transactions", 
        "bedrag", "bij", "af", "account no", "rekeningnummer", "saldo per"
    ])
    
    return (has_iban or has_account_pattern) and has_ledger_terms


def extract_dynamic_vendor(text: str, filename: str) -> Optional[str]:
    """
    Dynamically extracts prominent merchant or institution names from top header lines
    when not already resolved by detect_vendor_or_series.
    """
    if not text:
        return None
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for line in lines[:4]:
        if SCANNER_WATERMARK_RE.search(line):
            continue
        if 3 <= len(line) <= 25 and not any(w in line.lower() for w in [
            "invoice", "bill", "statement", "page", "date", "http", "www", "tel",
            "rekeningafschrift", "iban", "bic", "saldo", "bedrag", "overzicht"
        ]):
            clean_line = re.sub(r'[^a-zA-Z0-9\s]', '', line).strip()
            if clean_line and not clean_line.isdigit():
                words = [w.capitalize() for w in clean_line.split() if len(w) > 2]
                if 1 <= len(words) <= 2:
                    return "_".join(words)
    return None


# ==============================================================================
# EXCEPTION RULES ENGINE
# ==============================================================================
def evaluate_exception_rules(
    doc_data: Dict[str, Any],
    exception_rules: List[Dict[str, Any]],
    family_config: Dict[str, List[str]],
    primary_person: str,
    current_date: date
) -> Tuple[bool, Optional[DocumentClassification]]:
    """
    User-configured rules that supersede deterministic and AI classification.
    """
    if not exception_rules:
        return False, None

    fn = doc_data["filename"]
    folder = doc_data.get("relative_folder") or doc_data["parent_folder"]
    text = f"{doc_data.get('metadata_text', '')}\n{doc_data['first_text']}\n{doc_data['tail_text']}"

    for rule in exception_rules:
        pattern_str = (rule.get("pattern") or "").strip()
        if not pattern_str:
            continue

        scope = rule.get("match_scope", "filename")
        action = rule.get("action", "force_route")

        if scope == "filename":
            target = fn
        elif scope == "folder":
            target = folder
        elif scope == "text":
            target = text
        else:
            target = f"{fn}\n{folder}\n{text}"

        try:
            matched = bool(re.search(pattern_str, target, flags=re.IGNORECASE))
        except re.error:
            matched = pattern_str.lower() in target.lower()

        if not matched:
            continue

        if action == "skip_file":
            return True, None

        forced_cat = rule.get("category") or "Miscellaneous"
        if forced_cat not in VALID_CATEGORIES:
            forced_cat = "Miscellaneous"

        forced_person = rule.get("person_name")
        if not forced_person:
            forced_person = canonicalize_person(
                None, family_config, primary_person=primary_person,
                filename=fn, relative_folder_path=folder,
                metadata_text=doc_data.get("metadata_text", ""), body_text=doc_data["first_text"]
            )

        doc_date, raw_exp = extract_dates_deterministically(
            doc_data["full_text"], doc_data.get("pdf_creation_date", "")
        )
        exp_date, exp_status = calculate_expiration_status(raw_exp, forced_cat, current_date)
        vendor = canonicalize_vendor(detect_vendor_or_series(doc_data, forced_cat) or extract_dynamic_vendor(doc_data["first_text"], fn))

        return False, DocumentClassification(
            category=forced_cat,
            person_name=forced_person,
            document_date=doc_date,
            expiration_date=exp_date,
            expiration_status=exp_status,
            confidence=1.0,
            summary=f"Routed by Custom Exception Rule matching '{pattern_str}'.",
            tags=[forced_cat.lower(), "exception-rule"],
            reasoning=f"User exception rule matched '{pattern_str}' in {scope}.",
            vendor_group=vendor,
            resolved_by="Code"
        )

    return False, None


# ==============================================================================
# DETERMINISTIC CLASSIFICATION
# ==============================================================================
def _folder_levels(doc_data: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Source-folder path as [(normalized_folder_name, weight)]: nearest folder = 1.0, each parent = 0.5."""
    raw = doc_data.get("relative_folder") or doc_data.get("parent_folder") or ""
    parts = [p for p in re.split(r"[\\/]+", raw) if p and p != "."]
    levels = []
    for i, part in enumerate(parts):
        norm = normalize_for_matching(part)
        if norm:
            levels.append((norm, 1.0 if i == len(parts) - 1 else 0.5))
    return levels


_LEARNED_RE_CACHE: Dict[str, "re.Pattern"] = {}


def classify_deterministically(
    doc_data: Dict[str, Any],
    learned_rules: Dict[str, str],
    family_config: Dict[str, List[str]],
    primary_person: str,
    current_date: date,
    manual_rules: Optional[Any] = None,
    min_python_confidence: float = DEFAULT_PYTHON_CONFIDENCE_THRESHOLD
) -> Optional[DocumentClassification]:
    """
    Ultra-fast heuristic and regex scoring classifier with universal structural detection.
    """
    fn = normalize_for_matching(Path(doc_data["filename"]).stem)
    rel_folder = normalize_for_matching(doc_data.get("relative_folder") or doc_data["parent_folder"])
    meta_lower = normalize_for_matching(doc_data.get("metadata_text", ""))
    full_ocr_text = f"{meta_lower} {normalize_for_matching(doc_data['first_text'])} {normalize_for_matching(doc_data['tail_text'])}"

    person_name = canonicalize_person(
        None,
        family_config=family_config,
        primary_person=primary_person,
        filename=doc_data["filename"],
        relative_folder_path=doc_data.get("relative_folder", ""),
        metadata_text=doc_data.get("metadata_text", ""),
        body_text=doc_data["first_text"]
    )

    is_other_family_member = (
        person_name not in {primary_person, "General", ""}
        and person_name in family_config
    )

    # 1. Image Photo Rule
    if doc_data.get("extension") in IMAGE_EXTENSIONS:
        has_doc_kw_in_path = any(p.search(f"{fn} {rel_folder}") for p in CATEGORY_PATTERNS.values())
        ocr_char_len = len(doc_data.get("first_text", "").strip())
        if PHOTO_FILENAME_RE.search(fn) or (ocr_char_len < 15 and not has_doc_kw_in_path):
            cat_for_photo = "Photos & People"
            return DocumentClassification(
                category=cat_for_photo,
                person_name=person_name,
                document_date=None,
                expiration_date=None,
                expiration_status="none",
                confidence=0.94,
                summary=f"Personal/portrait image for {person_name}.",
                tags=["photo", "image"],
                reasoning=f"Image file with photo filename or minimal text ({ocr_char_len} chars).",
                resolved_by="Code"
            )

    category_scores: Dict[str, int] = {}
    matched_keywords: Dict[str, List[str]] = {}

    # 2. Universal Generic Banking Detection (IBAN + Ledger terminology - Zero Hardcoding)
    if is_generic_bank_statement(full_ocr_text) and not CATEGORY_PATTERNS["Invoices"].search(fn):
        category_scores["Banking"] = category_scores.get("Banking", 0) + 12
        matched_keywords.setdefault("Banking", []).append("universal-banking-signature")

    # 3. Score Source Folder Path Hints + Filename Hints
    folder_levels = _folder_levels(doc_data)
    for cat, hint_re in FOLDER_PATH_HINTS.items():
        # Source folder is a STRONG hint (nearest folder weighs most); OCR can still outvote it
        folder_hits = int(round(sum(len(hint_re.findall(name)) * 10 * w for name, w in folder_levels)))
        fn_hint_hits = len(hint_re.findall(fn)) * 4
        if folder_hits + fn_hint_hits > 0:
            category_scores[cat] = category_scores.get(cat, 0) + folder_hits + fn_hint_hits
            matched_keywords.setdefault(cat, []).append("folder/filename-hint")

    # 4. Score learning.csv rules
    for kw, cat in learned_rules.items():
        kw_norm = normalize_for_matching(kw)
        if not kw_norm:
            continue
        kw_re = _LEARNED_RE_CACHE.get(kw_norm)
        if kw_re is None:   # compile once per keyword (python's own cache holds only 512 patterns)
            kw_re = _LEARNED_RE_CACHE[kw_norm] = re.compile(r'\b' + re.escape(kw_norm) + r'\b')
        fn_hit = bool(kw_re.search(fn))
        folder_hit = bool(kw_re.search(rel_folder))
        meta_hit = bool(kw_re.search(meta_lower)) if meta_lower else False
        text_hit = bool(kw_re.search(full_ocr_text[:2000]))
        if fn_hit or folder_hit or meta_hit or text_hit:
            pts = (6 if fn_hit else 0) + (5 if folder_hit else 0) + (4 if meta_hit else 0) + (4 if text_hit else 0)
            category_scores[cat] = category_scores.get(cat, 0) + pts
            matched_keywords.setdefault(cat, []).append(kw)

    # 5. Score Category Regex Patterns
    for cat, pattern in CATEGORY_PATTERNS.items():
        fn_hit_count = len(pattern.findall(fn))
        fn_matches = fn_hit_count * 5 + (10 if fn_hit_count > 0 else 0)
        folder_matches = int(round(sum(len(pattern.findall(name)) * 8 * w for name, w in folder_levels)))
        meta_matches = len(pattern.findall(meta_lower)) * 3 if meta_lower else 0
        text_matches = len(pattern.findall(full_ocr_text)) * 2
        total_score = fn_matches + folder_matches + meta_matches + text_matches
        if total_score > 0:
            category_scores[cat] = category_scores.get(cat, 0) + total_score

    # 6. DOMAIN PRIORITY RULE
    for generic_cat in ["Invoices", "Contracts"]:
        if generic_cat in category_scores:
            best_domain_score = max(
                (score for cat, score in category_scores.items() if cat in SPECIFIC_DOMAIN_CATEGORIES),
                default=0
            )
            if best_domain_score >= 3:
                category_scores[generic_cat] = min(category_scores[generic_cat], best_domain_score - 2)

    if not category_scores:
        if is_other_family_member:
            return DocumentClassification(
                category="Identity",
                person_name=person_name,
                document_date=doc_data.get("pdf_creation_date") or None,
                expiration_date=None,
                expiration_status="none",
                confidence=0.90,
                summary=f"Personal document belonging to {person_name}.",
                tags=["family-member", person_name.lower().split()[0]],
                reasoning=f"Matched family member '{person_name}' from folder/filename/text.",
                resolved_by="Code"
            )
        return None

    sorted_cats = sorted(category_scores.items(), key=lambda x: x[1], reverse=True)
    top_cat, top_score = sorted_cats[0]
    second_score = sorted_cats[1][1] if len(sorted_cats) > 1 else 0

    margin_ratio = (top_score - second_score) / float(max(1, top_score))
    if top_score >= 6 and margin_ratio >= 0.35:
        conf = 0.96
    elif top_score >= 4 and margin_ratio >= 0.25:
        conf = 0.90
    elif top_score >= 3:
        conf = 0.85
    elif top_score >= 2:
        conf = 0.80
    else:
        conf = 0.70

    if is_other_family_member and conf < 0.88:
        conf = 0.88

    if conf < min_python_confidence:
        return None

    doc_date, raw_exp = extract_dates_deterministically(
        doc_data["full_text"], doc_data.get("pdf_creation_date", "")
    )
    exp_date, exp_status = calculate_expiration_status(raw_exp, top_cat, current_date)
    vendor = canonicalize_vendor(detect_vendor_or_series(doc_data, top_cat) or extract_dynamic_vendor(doc_data["first_text"], doc_data["filename"]))

    kw_list = matched_keywords.get(top_cat, [])
    kw_note = f" (matched '{kw_list[0]}')" if kw_list else ""

    return DocumentClassification(
        category=top_cat,
        person_name=person_name,
        document_date=doc_date,
        expiration_date=exp_date,
        expiration_status=exp_status,
        confidence=conf,
        summary=f"{top_cat} document for {person_name}{kw_note}.",
        tags=[top_cat.lower(), "python-rules", person_name.lower().split()[0]],
        reasoning=f"Deterministic score ({top_score} pts, {int(conf*100)}% confidence){kw_note}.",
        vendor_group=vendor,
        resolved_by="Code"
    )


# ==============================================================================
# DISTILLED OLLAMA CLASSIFIER
# ==============================================================================
def create_distilled_payload(doc_data: Dict[str, Any], max_chars: int = 2000) -> str:
    lines = [line.strip() for line in doc_data["first_text"].split("\n") if line.strip()]
    top_lines = lines[:10]
    interesting = []
    date_currency_re = re.compile(r"(?i)(\$|€|£|₹|rs\b|inr\b|usd\b|eur\b|\d{4}[-/]\d{2}|\d{2}[-/]\d{2}|due|expires?|amount|kenteken|bsn|pan|aadhaar)")
    for line in lines[10:30]:
        if date_currency_re.search(line):
            interesting.append(line)
            if len(interesting) >= 4:
                break
    distilled = "\n".join(top_lines + interesting)
    meta_line = f"\nPDF Metadata: {doc_data['metadata_text']}" if doc_data.get("metadata_text") else ""
    rel_folder = doc_data.get("relative_folder") or doc_data["parent_folder"]
    return f"Filename: {doc_data['filename']}\nSource Folder Path: {rel_folder}{meta_line}\nContent:\n{distilled}"[:max_chars]


def classify_with_ollama(
    doc_data: Dict[str, Any],
    family_config: Dict[str, List[str]],
    primary_person: str,
    current_date: date,
    model_name: str = MODEL_NAME,
    max_chars: int = 2000
) -> DocumentClassification:
    distilled_snippet = create_distilled_payload(doc_data, max_chars=max_chars)
    canonical_people = list(family_config.keys())
    default_owner = primary_person if primary_person else "General"
    family_members_list = ", ".join([f'"{p}"' for p in canonical_people]) if canonical_people else f'"{default_owner}"'

    prompt = f"""Classify document for {default_owner}'s personal archive.
Today: {current_date.isoformat()}
Known family members: [{family_members_list}]
Rules:
1. If a medical/hospital/pharmacy bill, choose "Medical" (not Invoices).
2. If a car/vehicle document or parking/rental bill, choose "Vehicles".
3. If a flight/train ticket, subscription, or merchant invoice/receipt, choose "Invoices".
4. If a bank statement, account summary, or financial ledger, choose "Banking".
5. If no other family member is named, assign person_name to "{default_owner}".

{distilled_snippet}

Output JSON schema:
{{
  "category": "Invoices|Taxes|Medical|Contracts|Employment|Banking|Identity|Insurance|Real Estate|Vehicles|Education|Photos & People|Miscellaneous",
  "person_name": "One of Known family members, or {default_owner}",
  "document_date": "YYYY-MM-DD or null",
  "expiration_date": "YYYY-MM-DD or null",
  "expiration_status": "valid|expiring_soon|expired|none",
  "confidence": float 0.0 to 1.0,
  "summary": "1 factual sentence",
  "tags": ["tag1", "tag2"],
  "reasoning": "1 sentence why",
  "suggested_keyword": "1 generic institutional or document-type noun from the text (no names/numbers) or null",
  "vendor_group": "Detected merchant, bank, or vendor name (e.g. SportCity, ABN_AMRO) or null"
}}
"""

    try:
        response = ollama_client.chat(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a fast document classifier. Extract vendor/merchant or bank name into vendor_group when applicable. Output only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            format=DocumentClassification.model_json_schema(),
            options={"num_ctx": 1024, "num_predict": 300, "temperature": 0.0},
            keep_alive="15m"
        )
        data = json.loads(response.message.content)
        classification = DocumentClassification(**data)
        classification.resolved_by = "Ollama"

        if not classification.document_date and doc_data.get("pdf_creation_date"):
            classification.document_date = doc_data["pdf_creation_date"]

        classification.person_name = canonicalize_person(
            classification.person_name,
            family_config=family_config,
            primary_person=primary_person,
            filename=doc_data["filename"],
            relative_folder_path=doc_data.get("relative_folder", ""),
            metadata_text=doc_data.get("metadata_text", ""),
            body_text=doc_data["first_text"]
        )

        exp_date, exp_status = calculate_expiration_status(
            classification.expiration_date, classification.category, current_date
        )
        classification.expiration_date = exp_date
        classification.expiration_status = exp_status
        
        if not classification.vendor_group:
            classification.vendor_group = detect_vendor_or_series(doc_data, classification.category) or extract_dynamic_vendor(doc_data["first_text"], doc_data["filename"])
        classification.vendor_group = canonicalize_vendor(classification.vendor_group)
            
        return classification

    except Exception as e:
        fallback_person = canonicalize_person(
            None,
            family_config=family_config,
            primary_person=primary_person,
            filename=doc_data["filename"],
            relative_folder_path=doc_data.get("relative_folder", ""),
            metadata_text=doc_data.get("metadata_text", ""),
            body_text=doc_data["first_text"]
        )
        return DocumentClassification(
            category="Miscellaneous",
            person_name=fallback_person,
            document_date=doc_data.get("pdf_creation_date") or None,
            expiration_date=None,
            expiration_status="none",
            confidence=0.45,
            summary=f"Document ({doc_data['filename']}) fallback triage.",
            tags=["needs-review"],
            reasoning=f"Inference fallback: {str(e)[:80]}",
            resolved_by="Ollama"
        )