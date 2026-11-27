"""
core/rules.py - Regex pattern dictionaries and non-sensitive learning.csv engine.
Keeps regex logic completely separate from OCR, extraction, and sorting pipelines.
"""

import re
import csv
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.config import (
    VALID_CATEGORIES,
    LEARNING_CSV_PATH,
    LEARNING_STOPWORDS,
    normalize_for_matching
)

# ==============================================================================
# DETERMINISTIC REGEX DICTIONARIES (NETHERLANDS + INDIA + GLOBAL)
# ==============================================================================
CATEGORY_PATTERNS: Dict[str, re.Pattern] = {
    "Taxes": re.compile(
        r"(?i)\b(belasting|inkomstenbelasting|inkomensverklaring|belastingaangifte|jaaropgave|"
        r"jaaropgaaf|form\s*(?:no\.?\s*)?16a?|tax\s+return|dutch\s+tax|inland\s+revenue|"
        r"belastingdienst|aanslag|voorschotbeschikking|omzetbelasting|toeslagen|zorgtoeslag|"
        r"huurtoeslag|kinderopvangtoeslag|kindgebonden\s+budget|waterschapsbelasting|"
        r"gemeentelijke\s+belastingen|loonheffing|30%\s*ruling|30%-regeling|"
        r"assessment\s+year|tax\s+year|itr-?[0-9v]?|form\s*(?:no\.?\s*)?26as|tds\s+certificate|"
        r"income\s+tax\s+department|challan\s*280|advance\s+tax|annual\s+information\s+statement)\b"
    ),
    "Vehicles": re.compile(
        r"(?i)\b(kenteken|kentekenbewijs|rdw\b|apk\b|motorrijtuigenbelasting|wegenbelasting|"
        r"anwb\b|autoverzekering|groene\s*kaart|groenekaart|tenaamstellingscode|voertuig|"
        r"vehicle\s+registration|registration\s+certificate|rc\s+book|\brc\b|car\s+rental|"
        r"puc\b|pollution\s+under\s+control|chassis\s+no|chassis\s+number|engine\s+no|"
        r"engine\s+number|motor\s+vehicle|rto\b|fastag|car\s+service|car\s+accident|bike\s+insurance)\b"
    ),
    "Banking": re.compile(
        r"(?i)\b(bank\s+statement|account\s+statement|consolidated\s+statement|iban|swift|bic|"
        r"credit\s+card|debit\s*card|abn\s*debit|\bdebit\b|closing\s+balance|opening\s+balance|rekeningafschrift|bankrekening|"
        r"spaargeld|spaarrekening|betaalrekening|financieel\s+jaaroverzicht|deposito|"
        r"transaction\s+history|interest\s+certificate|mutatieoverzicht|"
        r"fixed\s+deposit|fd\s+|term\s+deposit|savings\s+account|current\s+account|"
        r"nre\b|nro\b|demat|client\s+master\s+report|cmr\b|dp\s+id|mutual\s+fund|cibil|"
        r"bkr\s+report|credit\s*score|cancelled\s+cheque|bank\s+account|public\s+provident\s+fund|"
        r"axis\s+bank|ing\s+bank|abn\s+amro|abna|rabobank|sns\s+bank|bunq|knab|"
        r"hdfc\s+bank|icici\s+bank|state\s+bank\s+of\s+india|kotak|fire\b|financial\s+independence|budget|net\s*worth)\b"
    ),
    "Invoices": re.compile(
        r"(?i)\b(invoice|factuur|bill\s+with\s+receipt|bill\b|rechnung|btw|vat\b|receipt|"
        r"total\s+amount|due\s+date|payment\s+due|remittance|subtotal|te\s+betalen|"
        r"factuurnummer|factuurdatum|betalingsbewijs|kassabon|pakbon|besteloverzicht|"
        r"kwitantie|order\s+confirmation|purchase\s+order|tax\s+invoice|proforma\s+invoice|"
        r"creditnota|cash\s+memo|payment\s+receipt|gstin|cgst|sgst|igst|automatische\s+incasso|"
        r"energierapport|energy\s+report|energienota|jaarafrekening|electricity|utility\s+bill|"
        r"society\s+maintenance|maintenance\s+bill|maintenance\s+invoice|"
        r"eticket|e-ticket|heenvlucht|terugvlucht|ticket)\b"
    ),
    "Employment": re.compile(
        r"(?i)\b(salary\s+slip|payslip|employment\s+contract|arbeidsovereenkomst|"
        r"salarisspecificatie|loonstrook|werkgever|salary|wage|employer|joining\s+letter|"
        r"appointment\s+letter|appraisal\s+letter|relieving\s+letter|offer\s+letter|"
        r"deputation\s+letter|deputtion\s+letter|experience\s+certificate|werkgeversverklaring|"
        r"pensioenoverzicht|vaststellingsovereenkomst|vakantiegeld|epfo|provident\s+fund|"
        r"gratuity|full\s+and\s+final\s+settlement|increment|resume\b|curriculum\s+vitae|\bcv\b|"
        r"cover\s+letter|job\s+application|reference\s+letter|performance\s+review|timesheet|"
        r"expense\s+claim|employment\s+verification|business\s+trip)\b"
    ),
    "Insurance": re.compile(
        r"(?i)\b(polis\b|policy\s+number|policy\s+schedule|insurance|verzekering|zorgverzekering|"
        r"eigen\s+risico|inboedelverzekering|opstalverzekering|aansprakelijkheidsverzekering|"
        r"reisverzekering|rechtsbijstandverzekering|zilveren\s+kruis|menzis|vgz|cz\s+insurance|"
        r"new\s+india\s+insurance|new\s+india\s+assurance|star\s+health|premium\s+receipt|premie|"
        r"coverage|insured|polisblad|schade|claim|polisnummer|dekking|health\s+insurance|"
        r"life\s+insurance|mediclaim|sum\s+assured|zorgpas|ehic)\b"
    ),
    "Identity": re.compile(
        r"(?i)\b(passport\b|pan\s*card|pancard|permanent\s+account\s+number|aadhar|aadhaar|eaadhar|"
        r"uidai|voter\s*id|election\s+commission|election\s+card|driving\s+licen[sc]e|\blicen[sc]e\b|"
        r"national\s+identity|residence\s+permit|residence\s+card|\brp\b|ov\s*chipkaart|ov-chipkaart|verblijfsvergunning|"
        r"verblijfstitel|rijbewijs|paspoort|identiteitskaart|id\s+card|nl\s+id|bl\s+id|bsn\b|"
        r"burgerservicenummer|uittreksel|brp\b|basisregistratie\s+personen|geboorteakte|"
        r"huwelijksakte|digid|oci\b|overseas\s+citizen|surrender\s+certificate|"
        r"surrender\s+online|naturalization|naturalisatie|birth\s+certificate|"
        r"marriage\s+certificate|declaration\s+for\s+non-resident|re-kyc|ration\s*card|visum|visa\b)\b"
    ),
    "Medical": re.compile(
        r"(?i)\b(hospital|clinic|doctor|physician|prescription|patient|diagnosis|"
        r"pharmacy|pathology|medical|health\s+center|ziekenhuis|apotheek|huisarts|tandarts|"
        r"fysiotherapie|verwijsbrief|bloedonderzoek|radiologie|medisch|recept|treatment|"
        r"consultation|lab\s+report|test\s+asked|discharge\s+summary|vaccination|vaccine|"
        r"covid|blood\s+test|hematology|biochemistry|zorgkosten|declaratieoverzicht|"
        r"ot\s+referral|diet\s+questions)\b"
    ),
    "Real Estate": re.compile(
        r"(?i)\b(mortgage|hypotheek|hypotheekakte|tenancy|rental\s+agreement|land\s+registry|deed|"
        r"property\s+tax|huurovereenkomst|kadaster|notaris|notariële\s+akte|koopakte|koopovereenkomst|"
        r"leveringsakte|taxatierapport|woz-beschikking|woz-waarde|vereniging\s+van\s+eigenaars|"
        r"eigendomsbewijs|huurcontract|energielabel|sale\s+deed|conveyance\s+deed|"
        r"index\s+ii|7/12\s+extract|encumbrance\s+certificate|khata\s+certificate|"
        r"leave\s+and\s+license)\b"
    ),
    "Contracts": re.compile(
        r"(?i)\b(agreement|contract|terms\s+and\s+conditions|lease|non-disclosure|"
        r"nda\b|overeenkomst|algemene\s+voorwaarden|machtiging|opzegging|handtekening|"
        r"memorandum\s+of\s+understanding|affidavit|power\s+of\s+attorney|indemnity\s+bond|"
        r"letter\s+of\s+consent|letter\s+of\s+content|police\s+report)\b"
    ),
    "Education": re.compile(
        r"(?i)\b(diploma|degree|be\s+degree|bachelor|master\s+of|transcript|grade\s+sheet|"
        r"university|college|school|international\s+school|community\s+school|"
        r"term\s+[0-9]\s+report|mark\s*sheet|statement\s+of\s+marks|tuition|academic|"
        r"curriculum|alumni|convocation|career\s+guidance|cijferlijst|getuigschrift|"
        r"propedeuse|studiefinanciering|collegegeld|provisional\s+certificate|"
        r"leaving\s+certificate|bonafide\s+certificate|transfer\s+certificate|"
        r"worksheet[s]?|drill\s*sheet[s]?|grade\s*[0-9]|fraction[s]?|geometry|place\s*value|"
        r"rhyming|spelling|reading\s*&\s*math|k-5)\b"
    ),
    "Miscellaneous": re.compile(
        r"(?i)\b(family\s*tree|stamboom|genealog[a-z]*)\b"
    )
}

FOLDER_PATH_HINTS: Dict[str, re.Pattern] = {
    "Vehicles": re.compile(r"(?i)\b(car|auto|vehicle|bike|scooter|rdw|rc|kenteken|motor|parking)\b"),
    "Taxes": re.compile(r"(?i)\b(tax|taxes|belasting|itr|form16|jaaropgave|toeslag)\b"),
    "Identity": re.compile(r"(?i)\b(id|identity|passport|paspoort|visa|ind|permit|aadhaar|pan|rijbewijs|kyc|oci)\b"),
    "Employment": re.compile(r"(?i)\b(work|job|salary|payslip|payslips|loonstrook|employment|hr|offer|employer|cv|resume)\b"),
    "Real Estate": re.compile(r"(?i)\b(house|home|woning|hypotheek|mortgage|property|rent|huur|flat|apartment|kadaster)\b"),
    "Medical": re.compile(r"(?i)\b(medical|health|hospital|doctor|medisch|ziekenhuis|apotheek|lab|prescription)\b"),
    "Insurance": re.compile(r"(?i)\b(insurance|verzekering|polis|zorg|mediclaim|lic)\b"),
    "Banking": re.compile(r"(?i)\b(bank|banking|statements|afschriften|finance|investment|fd|mutual\s*fund|ppf|iban)\b"),
    "Education": re.compile(r"(?i)\b(education|study|school|university|college|degree|diploma|marksheet)\b"),
    "Invoices": re.compile(r"(?i)\b(invoice|invoices|bills|receipts|facturen|factuur|bonnen|purchases|warranty|tickets)\b"),
    "Photos & People": re.compile(r"(?i)\b(photo|photos|pictures|portraits|family\s*photos|camera|selfie|headshot)\b"),
}

# ==============================================================================
# NON-SENSITIVE LEARNING.CSV ENGINE
# ==============================================================================
_VOCAB_FIELDS = ["keyword_pattern", "category", "match_type", "learned_at"]

# SDS-FIX-LEARNING: filler words that must never become learned keywords
_JUNK_LEARN_WORDS = {
    "whats", "whatsapp", "image", "images", "scan", "scanned", "scans", "photo", "photos",
    "document", "documents", "copy", "final", "signed", "version", "screenshot", "file",
    "files", "page", "pages", "front", "back", "new", "old", "draft", "untitled", "img",
    "personal", "other", "others", "misc", "updated", "latest",
}


def _clean_vocab_rows(rows):
    """SDS-FIX-LEARNING: repair glued rows and skip bad rows instead of rejecting the whole file."""
    out, skipped, repaired = [], [], 0
    for row in rows:
        extra = row.pop(None, None)
        parts = [row]
        if extra:
            la = (row.get("learned_at") or "").strip()
            ok = False
            if len(extra) == 3 and len(la) > 19:
                try:
                    datetime.fromisoformat(la[:19])
                    row["learned_at"] = la[:19]
                    parts = [row, {
                        "keyword_pattern": la[19:], "category": extra[0],
                        "match_type": extra[1], "learned_at": extra[2],
                    }]
                    ok = True
                    repaired += 1
                except ValueError:
                    pass
            if not ok:
                skipped.append(row.get("keyword_pattern"))
                continue
        for r in parts:
            r = {k: (v or "").strip() for k, v in r.items()}
            kw, cat = r.get("keyword_pattern", ""), r.get("category", "")
            if not kw or not cat:
                skipped.append(kw or "(empty)")
                continue
            if r.get("match_type", "").lower() != "manual" and kw.lower() in _JUNK_LEARN_WORDS:
                skipped.append(kw)
                continue
            out.append(r)
    if repaired or skipped:
        print(f"[Warning] learning.csv: repaired {repaired} glued row(s), skipped {len(skipped)} bad/junk row(s): {skipped[:10]}")
    return out


def _read_vocabulary_csv(csv_path: Path):
    """Read current CSV or the older keyword_pattern:/category: format."""
    if not csv_path.exists():
        return _VOCAB_FIELDS.copy(), []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        first = next((line for line in f if line.strip()), "")
        if not first:
            return _VOCAB_FIELDS.copy(), []
        header = next(csv.reader([first]))
        if {"keyword_pattern", "category"}.issubset(header):
            f.seek(0)
            reader = csv.DictReader(line for line in f if line.strip())
            fields = list(reader.fieldnames or [])
            if len(fields) != len(set(fields)):
                raise ValueError("Duplicate learning.csv columns; file was not changed.")
            rows = _clean_vocab_rows(list(reader))  # SDS-FIX-LEARNING
            return fields + [name for name in _VOCAB_FIELDS if name not in fields], rows

        if not first.strip().startswith("keyword_pattern:"):
            raise ValueError("Unrecognized learning.csv format; file was not changed.")
        f.seek(0)
        rows = []
        keyword = ""
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("keyword_pattern:") and not keyword:
                keyword = line.split(":", 1)[1].strip()
                if not keyword:
                    raise ValueError("An old vocabulary entry has no keyword.")
            elif line.startswith("category:") and keyword:
                category = line.split(":", 1)[1].strip()
                if not category:
                    raise ValueError("An old vocabulary entry has no category.")
                rows.append({"keyword_pattern": keyword, "category": category})
                keyword = ""
            else:
                raise ValueError("Unexpected line in old vocabulary format; file was not changed.")
        if keyword:
            raise ValueError("An old vocabulary entry has no category.")
        return _VOCAB_FIELDS.copy(), rows


def _vocabulary_entries(rows):
    entries = {}
    for row in rows:
        keyword = (row.get("keyword_pattern") or "").strip()
        category = (row.get("category") or "").strip()
        kind = (row.get("match_type") or "").strip().lower()
        if not keyword or keyword.lower() == "keyword_pattern" or category not in VALID_CATEGORIES:
            continue
        if kind != "manual":
            keyword = keyword.lower()
            if keyword == "certificate" and category == "Education":
                continue
        key = normalize_for_matching(keyword) if kind == "manual" else keyword
        if key:
            entries[(kind == "manual", key)] = {
                "keyword": keyword, "category": category, "kind": kind, "key": key,
            }
    return list(entries.values())


def load_vocabulary_entries(csv_path: Path = LEARNING_CSV_PATH):
    try:
        _, rows = _read_vocabulary_csv(csv_path)
        return _vocabulary_entries(rows)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"[Warning] Failed reading learning.csv ({exc}).")
        return []


def load_learning_csv(csv_path: Path = LEARNING_CSV_PATH) -> Dict[str, str]:
    """Keep the existing keyword-to-category API for older callers."""
    entries = load_vocabulary_entries(csv_path)
    rules = {row["keyword"]: row["category"] for row in entries if row["kind"] != "manual"}
    rules.update({row["keyword"].lower(): row["category"] for row in entries if row["kind"] == "manual"})
    return rules


def _write_vocabulary_csv(csv_path, fields, rows):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=csv_path.parent,
            prefix=".vocabulary-", suffix=".tmp", delete=False,
        ) as f:
            temp = Path(f.name)
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temp.replace(csv_path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def save_manual_keyword(keyword: str, category: str, csv_path: Path = LEARNING_CSV_PATH) -> None:
    """Save one intentional literal term; raise ValueError if it cannot be added."""
    keyword = re.sub(r"\s+", " ", (keyword or "").strip())
    if category not in VALID_CATEGORIES:
        raise ValueError("Select a valid category.")
    if (
        not 3 <= len(keyword) <= 80
        or sum(ch.isalnum() for ch in keyword) < 3
        or not re.fullmatch(r"[^\W_]+(?:[ _-][^\W_]+)*", keyword)
        or keyword.lower() == "keyword_pattern"
    ):
        raise ValueError("Enter a word or phrase with at least 3 letters or digits, up to 80 characters. Spaces, hyphens and underscores are allowed.")

    fields, rows = _read_vocabulary_csv(csv_path)
    key = normalize_for_matching(keyword)
    if any(row["kind"] == "manual" and row["key"] == key for row in _vocabulary_entries(rows)):
        raise ValueError("An equivalent manual term already exists.")
    rows.append({
        "keyword_pattern": keyword, "category": category,
        "match_type": "manual", "learned_at": datetime.now().isoformat(),
    })
    _write_vocabulary_csv(csv_path, fields, rows)


def save_learned_keyword(
    keyword: Optional[str],
    category: str,
    family_config: Dict[str, List[str]],
    csv_path: Path = LEARNING_CSV_PATH
) -> bool:
    """
    Appends a generic domain keyword to learning.csv if verified non-personal and not in stop words.
    """
    if not keyword or category not in VALID_CATEGORIES or category in {"Miscellaneous", "Photos & People"}:
        return False

    kw_clean = re.sub(r"\s+", " ", keyword.strip().lower())
    # Allow 1-word or 2-word generic domain phrases (e.g. 'police report', 'surrender certificate')
    if not re.match(r"^[a-z]{4,18}(\s[a-z]{4,18})?$", kw_clean):
        return False

    words = kw_clean.split()
    if all(w in LEARNING_STOPWORDS for w in words):
        return False
    if any(w in _JUNK_LEARN_WORDS for w in words):  # SDS-FIX-LEARNING
        return False

    for canonical, aliases in family_config.items():
        if kw_clean in canonical.lower():
            return False
        for a in aliases:
            if kw_clean in a.lower() or a.lower() in kw_clean:
                return False

    try:
        fields, rows = _read_vocabulary_csv(csv_path)
        if any(
            row["key"] == kw_clean
            or (row["kind"] == "manual" and row["key"] == normalize_for_matching(kw_clean))
            for row in _vocabulary_entries(rows)
        ):
            return False
        rows.append({
            "keyword_pattern": kw_clean, "category": category,
            "match_type": "auto", "learned_at": datetime.now().isoformat(),
        })
        _write_vocabulary_csv(csv_path, fields, rows)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"[Warning] Could not save an automatic vocabulary entry ({exc}).")
        return False
    return True
