"""
core/person_matcher.py - Dynamic Family/Entity Member Matching & Disambiguation Engine.
100% Generic & Data-Agnostic:
- Automatically calculates middle-name and patronymic overlaps between ANY configured members at runtime.
- Automatically isolates multi-relative family trees and government application receipts without hardcoded personal names.
- Works as a plug-and-play module for any family or enterprise identity hierarchy.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Any

from core.config import (
    SECONDARY_RELATION_LINE_RE,
    CANDIDATE_PERSON_LABEL_RE,
    KNOWN_VENDORS,
    LEARNING_STOPWORDS,
    NON_HUMAN_NAME_WORDS,
    SCANNER_WATERMARK_RE,
    SCANNER_TOOL_FOLDER_RE,
    normalize_for_matching
)

# Generic governmental / consular / visa applicant form clues
GENERIC_APPLICATION_RECEIPT_RE = re.compile(
    r"(?i)\b(application\s+receipt|application\s+status|surrender\s+certificate|"
    r"online\s+application|consular\s+services|passport\s+seva|visa\s+application|"
    r"registration\s+certificate|acknowledgement\s+receipt)\b"
)

# Multi-person family tree records spanning multiple generations
GENERIC_FAMILY_TREE_RE = re.compile(
    r"(?i)\b(family\s*tree|stamboom|genealog[a-z]*|pedigree|ancest[a-z]*)\b"
)


def _compute_dynamic_name_shields(
    family_config: Dict[str, List[str]],
    primary_person: str
) -> Tuple[List[Tuple[re.Pattern, str]], Set[str]]:
    """
    Analyzes all configured names at runtime to dynamically prevent middle-name collisions.
    For ANY configured person whose name has middle tokens (e.g. Person A = 'First Mid Last'):
      1. Builds compound shields for 'First Mid' so Person B ('Mid') cannot steal it.
      2. If Member B's first name appears as a middle name in Member A, Member B is a
         relative who should NOT match body text by a partial alias ('Mid' or 'Mid Last')
         when only full authoritative names should match.
    Zero names are hardcoded. Works for any names or patronymic/matronymic cultures.
    """
    shields: List[Tuple[re.Pattern, str]] = []
    restricted_relative_aliases: Set[str] = set()

    member_name_tokens: Dict[str, List[str]] = {}
    member_first_names: Dict[str, str] = {}

    for canonical in family_config.keys():
        words = normalize_for_matching(canonical).split()
        if words:
            member_name_tokens[canonical] = words
            member_first_names[canonical] = words[0]

    for person_a, tokens_a in member_name_tokens.items():
        if len(tokens_a) >= 2:
            first_a = tokens_a[0]
            # Check all middle tokens of Person A
            middle_tokens = tokens_a[1:-1] if len(tokens_a) > 2 else [tokens_a[1]]
            for mid in middle_tokens:
                for person_b, first_b in member_first_names.items():
                    if person_a != person_b and mid == first_b:
                        # 1. Shield "First_A Mid_B" in text so it attributes to Person A
                        pat = re.compile(r"\b" + re.escape(first_a) + r"\s+" + re.escape(mid) + r"(?:\s+\w+)?\b", re.IGNORECASE)
                        tag = f" _compound_shield_{hash(person_a) & 0xffff}_ "
                        shields.append((pat, tag))

                        # 2. Add Person B's shorter aliases (< 3 words) to restricted set for body text
                        for b_alias in family_config.get(person_b, []):
                            b_clean = normalize_for_matching(b_alias)
                            if len(b_clean.split()) < len(member_name_tokens[person_b]):
                                restricted_relative_aliases.add(b_clean)

    return shields, restricted_relative_aliases


# Scoring thresholds for _match_person_in_text
MIN_MARGIN = 4            # winner must beat the runner-up by this many points (full-name evidence)
MIN_PARTIAL_HITS = 2      # body text: single-word-only evidence must repeat this often


def _match_person_in_text(
    target_text: str,
    family_config: Dict[str, List[str]],
    primary_person: str = "",
    allow_single_word_relative: bool = False
) -> Optional[str]:
    """
    Dynamically disambiguates entity/family ownership from text.

    1. Normalizes CamelCase & punctuation, strips relation lines ('Father's Name', ...).
    2. Applies dynamic compound shields (child carrying a parent's middle name).
    3. Aliases shared by several members (e.g. a surname) never score on their own.
    4. Longest aliases are matched first and CLAIM their character span (text is never
       overwritten), so a full name can't be re-matched by a shorter alias inside it.
    5. Full-name (2+ word) matches are the real evidence. Single-word matches
       (e.g. a shared surname like 'Smith') can't win over a full name and get no top-of-document boost.
    6. Returns None when the evidence is weak or ambiguous; callers then fall through
       to the next rule (and finally to the default owner).

    allow_single_word_relative=True means a trusted, short context (filename / folder name),
    where one single-word hit such as 'John_ID_Image' is enough.
    """
    if not target_text or not target_text.strip() or not family_config:
        return None

    lenient = allow_single_word_relative
    top_window = 250

    cleaned_text = SECONDARY_RELATION_LINE_RE.sub(" ", target_text)
    working_text = normalize_for_matching(cleaned_text)

    # 1. Compound shields
    shields, restricted_tokens = _compute_dynamic_name_shields(family_config, primary_person)
    for shield_pat, token_tag in shields:
        if shield_pat.search(working_text):
            working_text = shield_pat.sub(token_tag, working_text)

    full_scores: Dict[str, int] = {}
    partial_hits: Dict[str, int] = {}
    top_hit: Set[str] = set()

    # Credit shielded matches directly to their parent member
    for canonical in family_config:
        tag = f" _compound_shield_{hash(canonical) & 0xffff}_ "
        if tag in working_text:
            full_scores[canonical] = full_scores.get(canonical, 0) + 14

    # 2. Collect aliases and who owns them
    alias_owners: Dict[str, Set[str]] = {}
    for canonical, aliases in family_config.items():
        for alias in aliases:
            a_clean = normalize_for_matching(alias)
            if not a_clean:
                continue
            if not lenient and a_clean in restricted_tokens:
                continue
            alias_owners.setdefault(a_clean, set()).add(canonical)

    # 3. Resolve shared aliases
    entries: List[Tuple[str, str, int]] = []
    for a_clean, owners in alias_owners.items():
        wc = len(a_clean.split())
        if len(owners) > 1:
            if wc == 1 or primary_person not in owners:
                continue                      # shared surname / ambiguous relatives: never scores
            owners = {primary_person}         # identical multi-word alias: primary wins
        entries.append((a_clean, next(iter(owners)), wc))

    # 4. Most specific first: more words, longer, primary person, then alphabetical (deterministic)
    entries.sort(key=lambda e: (-e[2], -len(e[0]), e[1] != primary_person, e[0]))

    # 5. Match, claiming spans so shorter aliases can't reuse the same characters
    claimed: List[Tuple[int, int]] = []
    for alias, canonical, wc in entries:
        body = r"\s+".join(re.escape(w) for w in alias.split())
        pattern = re.compile(rf"(?<!\w){body}(?!\w)")
        weight = (8 if wc >= 3 else 5) + (2 if canonical != primary_person else 0)

        for m in pattern.finditer(working_text):
            s, e = m.span()
            if any(s < ce and cs < e for cs, ce in claimed):
                continue
            claimed.append((s, e))
            if wc >= 2:
                full_scores[canonical] = full_scores.get(canonical, 0) + weight
                if s < top_window:
                    top_hit.add(canonical)    # top-of-document boost: full names only, once per person
            else:
                partial_hits[canonical] = partial_hits.get(canonical, 0) + 1

    for canonical in top_hit:
        full_scores[canonical] += 10

    # 6a. Full-name evidence decides; partial hits only break exact ties
    if full_scores:
        ranked = sorted(
            full_scores,
            key=lambda c: (full_scores[c], c == primary_person, min(partial_hits.get(c, 0), 2), c),
            reverse=True,
        )
        best = ranked[0]
        if len(ranked) > 1 and full_scores[best] - full_scores[ranked[1]] < MIN_MARGIN:
            return None                       # ambiguous between people
        return best

    # 6b. Single-word-only evidence
    need = 1 if lenient else MIN_PARTIAL_HITS
    margin = 1 if lenient else 2
    ranked_p = sorted(partial_hits.items(), key=lambda kv: (-kv[1], kv[0]))
    if ranked_p and ranked_p[0][1] >= need:
        if len(ranked_p) == 1 or ranked_p[0][1] - ranked_p[1][1] >= margin:
            return ranked_p[0][0]
    return None


def canonicalize_person(
    detected_name: Optional[str],
    family_config: Dict[str, List[str]],
    primary_person: str = "",
    filename: str = "",
    relative_folder_path: str = "",
    metadata_text: str = "",
    body_text: str = ""
) -> str:
    """
    Determines document ownership using a strict enterprise priority hierarchy:
    1. Family trees span all relatives -> always routes to default root owner.
    2. Parent/Relative dedicated source subfolder (100% authoritative).
    3. Filename matching.
    4. Generic Government/Identity application receipt guard -> defaults to primary owner.
    5. Embedded PDF metadata.
    6. Document body text (with dynamic middle-name collision protection).
    """
    default_owner = primary_person if primary_person else "General"
    if not family_config:
        return default_owner

    fn_norm = normalize_for_matching(Path(filename).stem)

    # Rule 1: Family trees / genealogy charts encompass everyone -> assign to root archive owner
    if GENERIC_FAMILY_TREE_RE.search(fn_norm):
        return default_owner

    # Rule 2: A family member named in the FILENAME wins (e.g. 'Alex_Passport.jpeg' sitting in 'Partner docs')
    matched_fn = _match_person_in_text(
        Path(filename).stem, family_config, primary_person, allow_single_word_relative=True
    )
    if matched_fn:
        return matched_fn

    # Rule 3: Dedicated person folder (e.g. 'Relative docs', 'John personal') when the filename names nobody
    if relative_folder_path:
        matched_folder = _match_person_in_text(
            relative_folder_path, family_config, primary_person, allow_single_word_relative=True
        )
        if matched_folder:
            return matched_folder

    # Rule 4: Generic Governmental / Consular Application Receipt Guard:
    # If file is at root level and represents an application/status form listing parent fields,
    # it belongs to the primary applicant (default root owner) rather than parent names inside.
    is_root_level = (not relative_folder_path) or relative_folder_path.lower() in {".", "documents", "personal"}
    if is_root_level:
        snippet_header = normalize_for_matching(body_text[:350])
        if GENERIC_APPLICATION_RECEIPT_RE.search(fn_norm) or GENERIC_APPLICATION_RECEIPT_RE.search(snippet_header):
            return default_owner

    # Rule 5: PDF Metadata
    if metadata_text:
        matched_meta = _match_person_in_text(
            metadata_text, family_config, primary_person, allow_single_word_relative=False
        )
        if matched_meta:
            return matched_meta

    # Rule 6: Document Body Text (top 2000 chars)
    matched_body = _match_person_in_text(
        body_text[:2000], family_config, primary_person, allow_single_word_relative=False
    )
    if matched_body:
        return matched_body

    return default_owner


VENDOR_CATEGORIES = {"Invoices", "Banking", "Employment", "Insurance", "Real Estate"}


def _vendor_key(label: str) -> str:
    """Letters+digits only, lowercase ('ABN_AMRO' / 'Abn Amro' / 'abn-amro' -> 'abnamro')."""
    return re.sub(r"[^a-z0-9]", "", (label or "").lower())


def canonicalize_vendor(label: Optional[str]) -> Optional[str]:
    """
    Maps ANY vendor spelling to ONE canonical folder name.
    1. If it matches a KNOWN_VENDORS pattern (or has the same letters as a known label) -> that label.
    2. Otherwise a tidy Title_Case form (short all-caps codes such as bank codes are kept as-is).
    """
    if not label or not str(label).strip():
        return None
    raw = str(label).strip()
    spaced = re.sub(r"[_\-]+", " ", raw)
    if SCANNER_WATERMARK_RE.search(spaced) or SCANNER_TOOL_FOLDER_RE.match(normalize_for_matching(raw)):
        return None  # scanner apps are never a vendor
    key = _vendor_key(raw)
    for vendor_label, pattern in KNOWN_VENDORS.items():
        if _vendor_key(vendor_label) == key or pattern.search(raw) or pattern.search(spaced):
            return vendor_label
    words = [w for w in re.split(r"[\s_\-]+", raw) if w]
    tidy = [w if (w.isupper() and len(w) <= 5) else w.capitalize() for w in words]
    return "_".join(tidy) or None


def _loose_key(key: str) -> str:
    """Ignores joining words so 'embassyofindia' == 'embassyindia'."""
    return re.sub(r"(of|the|and|de|van|von|der|het)", "", key)


def unify_vendor_names(pairs: List[Tuple[str, Optional[str]]]) -> Dict[Tuple[str, str], str]:
    """
    Cross-document vendor merging (generic, no names hardcoded).
    pairs = [(category, vendor_as_found), ...] -> {(category, vendor_as_found): final_vendor}
    Within one category, variants are merged when:
      - they canonicalize to the same name (e.g. 'ABNA', 'ABN AMRO', 'Abn_Amro'), or
      - one is a prefix of another (min 3 chars), e.g. a 4-letter bank code and the full bank name.
    A KNOWN_VENDORS label wins, otherwise the most descriptive (longest) name.
    """
    mapping: Dict[Tuple[str, str], str] = {}
    by_cat: Dict[str, Dict[str, str]] = {}  # category -> {original: canonical}
    for cat, v in pairs:
        if not v:
            continue
        canon = canonicalize_vendor(v)
        if canon is None:            # e.g. scanner apps: never a vendor -> dropped
            mapping[(cat, v)] = ""
            continue
        by_cat.setdefault(cat, {})[v] = canon

    known = set(KNOWN_VENDORS.keys())
    for cat, orig_to_canon in by_cat.items():
        names = sorted(set(orig_to_canon.values()), key=lambda n: len(_vendor_key(n)))
        group = {n: n for n in names}  # union-find

        def find(n: str) -> str:
            while group[n] != n:
                group[n] = group[group[n]]
                n = group[n]
            return n

        for i, short in enumerate(names):
            sk = _vendor_key(short)
            if len(sk) < 3:
                continue
            for other in names[i + 1:]:
                ok = _vendor_key(other)
                if ok != sk and (ok.startswith(sk) or _loose_key(ok) == _loose_key(sk)):
                    group[find(other)] = find(short)

        members: Dict[str, List[str]] = {}
        for n in names:
            members.setdefault(find(n), []).append(n)
        winner: Dict[str, str] = {}
        for root_name, mem in members.items():
            known_mem = [m for m in mem if m in known]
            best = known_mem[0] if known_mem else max(mem, key=lambda m: (m.count("_"), len(_vendor_key(m))))
            for m in mem:
                winner[m] = best

        for orig, canon in orig_to_canon.items():
            mapping[(cat, orig)] = winner.get(canon, canon)
    return mapping


def _match_known_vendor(chunk: str, category: str) -> Optional[str]:
    raw = chunk or ""
    squashed = re.sub(r"\s+", "", raw).lower()
    for vendor_label, pattern in KNOWN_VENDORS.items():
        if vendor_label == "Payslips" and category != "Employment":
            continue
        if pattern.search(raw) or pattern.search(squashed):
            return vendor_label
    return None


# Lines that name the OTHER party of a payment (never the document's own bank/vendor)
COUNTERPARTY_CUE_RE = re.compile(
    r"(?i)\b(to\s+account|naar\s+rekening|tegenrekening|beneficiary|begunstigde|counterparty|payee|"
    r"creditor|debtor|ten\s+gunste\s+van|transfer\s+to|overboeking\s+naar|betaald\s+aan|paid\s+to|sent\s+to)\b"
)


def _vendor_from_text(text: str, category: str) -> Optional[str]:
    """
    Earliest vendor mention in the top of the document (logo/header first), skipping lines that
    describe the other party of a payment (e.g. 'To account ...').
    """
    for line in text[:2500].split("\n"):
        if not line.strip() or COUNTERPARTY_CUE_RE.search(line):
            continue
        v = _match_known_vendor(line, category)
        if v:
            return v
    return None


def _iban_bank_code(text: str) -> Optional[str]:
    """
    Bank code (4 letters after country+check digits, ISO 13616) of the FIRST IBAN near the top that is
    not marked as a counterparty / 'to account' line. Own account normally appears first (header).
    """
    for line in (text or "")[:2500].split("\n"):
        for m in re.finditer(r"[a-zA-Z]{2}\d{2}\s?([a-zA-Z]{4})(?:\s?\d){4,}", line):
            if COUNTERPARTY_CUE_RE.search(line[:m.start()]):
                continue
            return m.group(1).upper()
    return None


def detect_vendor_or_series(doc_data: Dict[str, Any], category: str) -> Optional[str]:
    """
    Detects the vendor / financial institution. Evidence order (strongest first):
      1. Source folder path   2. Filename   3. OCR/body text (header first, then whole text)
      4. Banking only: IBAN bank code anywhere in the document (ISO 13616, works for any bank).
    Always returns ONE canonical spelling (see canonicalize_vendor).
    """
    if category not in VENDOR_CATEGORIES:
        return None

    folder = doc_data.get("relative_folder") or doc_data.get("parent_folder", "")
    text = f"{doc_data.get('first_text', '')}\n{doc_data.get('tail_text', '')}"

    for chunk in (folder, doc_data.get("filename", ""), text):
        v = _match_known_vendor(chunk, category) if chunk is not text else _vendor_from_text(chunk, category)
        if v:
            return canonicalize_vendor(v)

    if category == "Banking":
        code = _iban_bank_code(doc_data.get("first_text") or text)
        if code:
            return canonicalize_vendor(code)

    return None


def extract_unlisted_person_candidates(
    doc_data: Dict[str, Any],
    family_config: Dict[str, List[str]]
) -> Set[str]:
    """
    Dynamically discovers potential human names appearing after labels like 'Name:', 'Patient Name:', 'De heer', etc.
    """
    candidates: Set[str] = set()
    known_tokens: Set[str] = set()
    for canonical, aliases in family_config.items():
        for tok in normalize_for_matching(canonical).split():
            known_tokens.add(tok)
        for a in aliases:
            for tok in normalize_for_matching(a).split():
                known_tokens.add(tok)

    for match in CANDIDATE_PERSON_LABEL_RE.finditer(doc_data.get("first_text", "")[:1000]):
        name_cand = re.sub(r"[\r\n\t]+", " ", match.group(1)).strip()
        words = [w.lower() for w in name_cand.split()]
        if len(words) < 2:
            continue
        if any(w in LEARNING_STOPWORDS or w in NON_HUMAN_NAME_WORDS or w in known_tokens for w in words):
            continue
        candidates.add(name_cand)

    return candidates