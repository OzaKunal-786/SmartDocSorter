"""
core/folder_policy.py - Source-folder preservation decisions (tree-aware).

The dry run walks EVERY folder at every depth. For each folder it decides:
  SORT : normal sorting (files classified and routed to category / family / Work folders)
  KEEP : preserve the folder exactly as it is (nothing inside is sorted or renamed)
  ASK  : a specifically-named topic folder (e.g. GYM, OCI) - the user answers Yes/No.
         Until answered it is SORTED normally (nothing is hidden inside an unanswered folder).

Walk rules, top-down (first match wins). A decision on a folder applies to its whole subtree:
  1. Generic wrapper (Documents, Personal, Downloads...)           -> no decision, look inside
  2. Name is a category (Tax, Banking, Identity ...)               -> SORT
  3. Name belongs to a family member                               -> SORT
  4. Fewer than `min_files` files inside (recursive)              -> SORT
  5. User already answered (settings 'folder_decisions')           -> that answer
  6. Name mentions a category AND every file is that category      -> SORT into that category
  7. Otherwise (topic folder such as GYM / OCI)                    -> ASK
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.config import normalize_for_matching, strip_scanner_tool_folders
from core.person_matcher import _match_person_in_text

DEFAULT_MIN_FILES_FOR_FOLDER_DECISION = 3

# Words that say nothing about content (ignored when deciding if a name "is" a category / a wrapper)
FILLER_WORDS = {
    "doc", "docs", "document", "documents", "file", "files", "folder", "folders", "my", "our",
    "misc", "miscellaneous", "other", "others", "scan", "scans", "scanned", "new", "all", "and", "of", "the",
    "de", "van", "personal", "archive", "archives", "download", "downloads", "desktop", "backup",
    "backups", "old", "stuff", "general", "data",
}


@dataclass
class FolderDecision:
    folder: str                       # relative path of the deciding folder, e.g. 'Documents/GYM'
    file_count: int
    action: str                       # 'sort' | 'keep' | 'ask'
    reason: str
    sort_category: Optional[str] = None
    detail: str = ""                  # e.g. 'Invoices 8, Banking 3' (shown in the question dialog)

    @property
    def preserved(self) -> bool:
        """Only an explicit KEEP preserves a folder. Unanswered (ASK) folders are sorted."""
        return self.action == "keep"


def _parts(rel_folder: str) -> List[str]:
    cleaned = strip_scanner_tool_folders(rel_folder or "")
    return [p for p in re.split(r"[\\/]+", cleaned) if p and p != "."]


def top_level_folder(rel_folder: str) -> str:
    parts = _parts(rel_folder)
    return parts[0] if parts else ""


# Words that mean "this folder IS that category" (deliberately narrow: topic names such as OCI, GYM,
# Embassy... are NOT here, so those folders get the keep-as-is question instead of being auto-sorted)
CATEGORY_NAME_WORDS: Dict[str, set] = {
    "Invoices": {"invoice", "invoices", "bill", "bills", "receipt", "receipts", "factuur", "facturen", "bonnen"},
    "Taxes": {"tax", "taxes", "belasting", "itr"},
    "Medical": {"medical", "health", "medisch", "doctor", "hospital"},
    "Contracts": {"contract", "contracts", "agreement", "agreements"},
    "Employment": {"employment", "work", "job", "jobs", "salary", "payslip", "payslips", "hr", "cv", "resume"},
    "Banking": {"bank", "banking", "finance", "financial", "statements", "afschriften", "investment", "investments"},
    "Identity": {"identity", "id", "ids", "passport", "passports", "paspoort", "visa", "visas", "permit", "permits", "kyc"},
    "Insurance": {"insurance", "verzekering", "polis"},
    "Real Estate": {"house", "home", "woning", "property", "mortgage", "hypotheek", "rent", "huur", "real", "estate"},
    "Vehicles": {"car", "cars", "auto", "vehicle", "vehicles", "bike"},
    "Education": {"education", "study", "school", "university", "college", "degree"},
    "Photos & People": {"photo", "photos", "pictures"},
}


def _category_words_in_name(folder: str) -> Tuple[List[str], str]:
    """Returns (categories named in the folder name, leftover words that are NOT category/filler words)."""
    words = normalize_for_matching(folder).split()
    cats = [c for c, ws in CATEGORY_NAME_WORDS.items() if any(w in ws for w in words)]
    known = set().union(*CATEGORY_NAME_WORDS.values()) | FILLER_WORDS
    leftover = [w for w in words if w not in known]
    return cats, " ".join(leftover)


def _is_wrapper(folder: str) -> bool:
    words = normalize_for_matching(folder).split()
    return bool(words) and all(w in FILLER_WORDS for w in words)


def decide_folders(
    files: List[Tuple[str, str, bool]],
    family_config: Dict[str, List[str]],
    saved_choices: Optional[Dict[str, str]] = None,
    min_files: int = DEFAULT_MIN_FILES_FOR_FOLDER_DECISION,
) -> Dict[str, FolderDecision]:
    """
    files = [(relative_folder, classified_category, is_confident), ...] for every real (non-duplicate) file.
    saved_choices = {'GYM': 'keep' | 'sort'} - key is the folder path OR just its name (case-insensitive).
    Returns {relative_path: FolderDecision} ONLY for deciding folders (wrappers and subfolders of a
    decided folder have no entry; use decision_for()).
    """
    saved = {k.strip().lower().replace("\\", "/"): str(v).strip().lower() for k, v in (saved_choices or {}).items()}

    agg: Dict[Tuple[str, ...], List[Tuple[str, bool]]] = {}
    children: Dict[Tuple[str, ...], set] = {}
    for rel_folder, category, confident in files:
        parts = tuple(_parts(rel_folder))
        for i in range(1, len(parts) + 1):
            agg.setdefault(parts[:i], []).append((category, confident))
            children.setdefault(parts[:i - 1], set()).add(parts[i - 1])

    decisions: Dict[str, FolderDecision] = {}

    def visit(prefix: Tuple[str, ...]) -> None:
        for name in sorted(children.get(prefix, ()), key=str.lower):
            path = prefix + (name,)
            key = "/".join(path)
            items = agg.get(path, [])
            n = len(items)
            if _is_wrapper(name):
                visit(path)
                continue
            cats_in_name, leftover = _category_words_in_name(name)
            counts: Dict[str, int] = {}
            for c, _ in items:
                counts[c] = counts.get(c, 0) + 1
            detail = ", ".join(f"{c} {k}" for c, k in sorted(counts.items(), key=lambda kv: -kv[1])[:4])

            if cats_in_name and not leftover:
                decisions[key] = FolderDecision(key, n, "sort", "folder name is a category", detail=detail)
            elif family_config and _match_person_in_text(name, family_config, allow_single_word_relative=True):
                decisions[key] = FolderDecision(key, n, "sort", "family-member folder (family routing)", detail=detail)
            elif n < min_files:
                decisions[key] = FolderDecision(key, n, "sort", f"only {n} file(s) (< {min_files})", detail=detail)
            elif (saved.get(key.lower()) or saved.get(name.lower())) in ("keep", "sort"):
                choice = saved.get(key.lower()) or saved.get(name.lower())
                decisions[key] = FolderDecision(key, n, choice, "user decision", detail=detail)
            else:
                file_cats = set(counts)
                if cats_in_name and all(ok for _, ok in items) and len(file_cats) == 1 and next(iter(file_cats)) in cats_in_name:
                    cat = next(iter(file_cats))
                    decisions[key] = FolderDecision(
                        key, n, "sort", f"all {n} files are '{cat}' and the name mentions it", sort_category=cat, detail=detail
                    )
                else:
                    decisions[key] = FolderDecision(key, n, "ask", "topic folder - keep as-is? (answer Yes/No)", detail=detail)

    visit(())
    return decisions


def decision_for(rel_folder: str, decisions: Dict[str, FolderDecision]) -> Optional[FolderDecision]:
    """The decision that governs a file: the first (outermost) deciding folder on its path."""
    parts = _parts(rel_folder)
    for i in range(1, len(parts) + 1):
        d = decisions.get("/".join(parts[:i]))
        if d is not None:
            return d
    return None


def kept_root_for(rel_folder: str, decisions: Dict[str, FolderDecision]) -> Optional[str]:
    """Key of the KEPT folder that governs this file's folder, or None (file is sorted normally)."""
    d = decision_for(rel_folder, decisions)
    return d.folder if d is not None and d.preserved else None


def preserved_destination(rel_folder: str, root: str, filename: str) -> str:
    """Output path (relative) inside a kept folder: <kept folder name>/<sub folders>/<filename>."""
    parts = _parts(rel_folder)
    kept_len = len(root.split("/"))
    kept_name = parts[kept_len - 1] if len(parts) >= kept_len else root.split("/")[-1]
    return "/".join([kept_name] + parts[kept_len:] + [filename])
