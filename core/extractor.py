"""
core/extractor.py - PDF text, RapidOCR, Office extractors, perceptual hash.
Isolates all low-level external format dependencies (pymupdf, rapidocr, openpyxl, python-docx).
"""

import os
import re
from pathlib import Path

# Optimize ONNX Runtime CPU threads for RapidOCR
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("ONNXRUNTIME_CPU_NUM_THREADS", "4")
from typing import Dict, List, Optional, Tuple, Any

from core.logger import get_logger

log = get_logger("extractor")

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    import docx
except ImportError:
    docx = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

from PIL import Image, ImageOps
import numpy as np

try:
    import imagehash
except ImportError:
    imagehash = None

try:
    from rapidocr_onnxruntime import RapidOCR
    rapid_ocr = RapidOCR(use_det=True, use_cls=False, use_rec=True)
except Exception:
    rapid_ocr = None

from core.config import (
    MAX_IMAGE_DIM,
    SCANNER_WATERMARK_RE,
    IMAGE_EXTENSIONS,
    PHOTO_FILENAME_RE,
    KNOWN_VENDORS,
    normalize_for_matching,
    strip_scanner_tool_folders
)
from core.person_matcher import _match_person_in_text
from core.rules import CATEGORY_PATTERNS


def compute_perceptual_hash(img: Image.Image) -> Optional[str]:
    """
    Computes perceptual image hash (pHash) for image comparison.
    """
    try:
        if imagehash is not None:
            return str(imagehash.phash(img))
        small = ImageOps.grayscale(img).resize((8, 8), Image.Resampling.LANCZOS)
        arr = np.asarray(small, dtype=np.float32)
        avg = arr.mean()
        bits = (arr > avg).flatten()
        val = 0
        for b in bits:
            val = (val << 1) | int(b)
        return f"{val:016x}"
    except Exception:
        return None


def hamming_distance_hex(hash1: str, hash2: str) -> int:
    """
    Calculates hamming distance between two hex hashes.
    """
    try:
        return bin(int(hash1, 16) ^ int(hash2, 16)).count("1")
    except Exception:
        return 64


def run_ocr_on_image(img: Image.Image) -> str:
    """
    Fast Single-Pass RGB OCR at max 800px (~0.3s per image).
    """
    if rapid_ocr is None:
        return ""
    try:
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        w, h = img.size
        if max(w, h) > MAX_IMAGE_DIM:
            scale = MAX_IMAGE_DIM / float(max(w, h))
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
        if h > int(w * 1.6):                      # tall screenshots: read only the top part
            img = img.crop((0, 0, w, int(w * 1.6)))
            
        rgb_img = img.convert("RGB")
        result, _ = rapid_ocr(np.array(rgb_img))
        lines = [str(item[1]).strip() for item in (result or []) if item and len(item) > 1 and item[1]]
        return "\n".join(lines).strip()
    except Exception:
        return ""


def parse_pdf_metadata_date(pdf_date_str: str) -> Optional[str]:
    """
    Extracts YYYY-MM-DD from PDF creation date string (e.g. D:20240115...).
    """
    if not pdf_date_str:
        return None
    m = re.search(r"D:(\d{4})(\d{2})(\d{2})", pdf_date_str)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _needs_ocr_for_pdf_page(native_text: str) -> bool:
    """
    Determines if native PDF text is insufficient or just scanner watermarks.
    """
    cleaned = native_text.strip()
    if len(cleaned) < 120:
        return True
    if SCANNER_WATERMARK_RE.search(cleaned) and len(cleaned) < 350:
        return True
    return False


def _header_ocr_useful(native_text: str) -> bool:
    """Header-logo OCR costs time: only worth it for bank/invoice/insurance/employment-like text with no vendor visible."""
    head = native_text[:2500]
    if any(p.search(head) for p in KNOWN_VENDORS.values()):
        return False
    return any(CATEGORY_PATTERNS[c].search(head)
               for c in ("Banking", "Invoices", "Insurance", "Employment", "Real Estate") if c in CATEGORY_PATTERNS)


def _ocr_header_images(page, top_fraction: float = 0.25, dpi: int = 150) -> str:
    """
    Logos/letterheads are often embedded IMAGES on top of a text PDF, so native text never contains them.
    If page 0 has an image in its top part, OCR just that top strip (cheap) and return the text.
    """
    try:
        rect = page.rect
        limit = rect.y0 + rect.height * top_fraction
        has_header_image = False
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if not bbox:
                continue
            r = fitz.Rect(bbox)
            if r.y0 < limit and r.width >= 20 and r.height >= 10:
                has_header_image = True
                break
        if not has_header_image:
            return ""
        clip = fitz.Rect(rect.x0, rect.y0, rect.x1, limit)
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        return run_ocr_on_image(img)
    except Exception:
        return ""


def extract_pdf_content(file_path: Path, skip_ocr: bool = False, ocr_max_pages: int = 2) -> Tuple[str, str, Dict[str, str], Optional[str]]:
    """
    Always reads native PDF text across all pages. Runs single-pass OCR on Page 0 if scanned.
    """
    if fitz is None:
        return "", "", {}, None

    pages_text: List[str] = []
    pdf_meta: Dict[str, str] = {}
    first_page_phash: Optional[str] = None

    try:
        with fitz.open(file_path) as doc:
            raw_meta = doc.metadata or {}
            title = (raw_meta.get("title") or "").strip()
            author = (raw_meta.get("author") or "").strip()
            subject = (raw_meta.get("subject") or "").strip()
            keywords = (raw_meta.get("keywords") or "").strip()
            creation_date = parse_pdf_metadata_date(raw_meta.get("creationDate") or "")

            meta_combined = " | ".join(filter(None, [title, author, subject, keywords]))
            pdf_meta = {
                "meta_text": meta_combined,
                "author": author,
                "creation_date": creation_date or ""
            }

            total_pages = len(doc)
            for page in doc:
                pages_text.append(page.get_text().strip())

            full_page_ocr_done = False
            if not skip_ocr and total_pages > 0 and _needs_ocr_for_pdf_page(pages_text[0]):
                full_page_ocr_done = True
                for i in range(min(total_pages, ocr_max_pages)):
                    page = doc[i]
                    pix = page.get_pixmap(dpi=120)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    if i == 0:
                        first_page_phash = compute_perceptual_hash(img)
                    ocr_text = run_ocr_on_image(img)
                    if ocr_text:
                        pages_text[i] = f"{pages_text[i]}\n{ocr_text}".strip()

            # Text PDF with an image letterhead/logo on top: read the header strip and put it FIRST
            if (total_pages > 0 and not full_page_ocr_done and not _needs_ocr_for_pdf_page(pages_text[0])
                    and _header_ocr_useful(pages_text[0])):
                header_ocr = _ocr_header_images(doc[0])
                if header_ocr:
                    pages_text[0] = f"{header_ocr}\n{pages_text[0]}".strip()

    except Exception:
        return "", "", {}, None

    last_page = pages_text[-1] if len(pages_text) > 1 else ""
    full_text = "\n\n--- PAGE BREAK ---\n\n".join(pages_text)
    return full_text, last_page, pdf_meta, first_page_phash


def extract_image_content(file_path: Path, skip_ocr: bool = False) -> Tuple[str, Optional[str], bool]:
    try:
        with Image.open(file_path) as img:
            try:
                img_upright = ImageOps.exif_transpose(img)
            except Exception:
                img_upright = img
            w, h = img_upright.size
            is_landscape = w > h
            phash = compute_perceptual_hash(img_upright)
            if skip_ocr:
                return "", phash, is_landscape
            text = run_ocr_on_image(img_upright)
            return text, phash, is_landscape
    except Exception:
        return "", None, False


def extract_docx_content(file_path: Path) -> Tuple[str, str]:
    if docx is None:
        return "", ""
    try:
        doc = docx.Document(file_path)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_texts:
                    paragraphs.append(" | ".join(row_texts))
        return "\n".join(paragraphs), ""
    except Exception:
        return "", ""


def extract_xlsx_content(file_path: Path) -> Tuple[str, str]:
    if openpyxl is None:
        return "", ""
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        lines = []
        for sheet_name in wb.sheetnames[:3]:
            ws = wb[sheet_name]
            lines.append(f"=== Sheet: {sheet_name} ===")
            row_count = 0
            for row in ws.iter_rows(values_only=True):
                non_empty = [str(val).strip() for val in row if val is not None and str(val).strip()]
                if non_empty:
                    lines.append(" | ".join(non_empty))
                    row_count += 1
                if row_count >= 50:
                    break
        return "\n".join(lines), ""
    except Exception:
        return "", ""


def extract_plain_text(file_path: Path) -> Tuple[str, str]:
    for enc in ["utf-8", "latin-1", "cp1252"]:
        try:
            with open(file_path, "r", encoding=enc) as f:
                return f.read(), ""
        except UnicodeDecodeError:
            continue
        except Exception:
            break
    return "", ""


def should_skip_heavy_ocr(
    file_path: Path,
    rel_folder_str: str,
    family_config: Dict[str, List[str]]
) -> bool:
    fn_norm = normalize_for_matching(file_path.stem)
    if file_path.suffix.lower() in IMAGE_EXTENSIONS and PHOTO_FILENAME_RE.search(fn_norm):
        return True

    fn_person = _match_person_in_text(fn_norm, family_config, allow_single_word_relative=True)
    if fn_person:
        for pattern in CATEGORY_PATTERNS.values():
            if pattern.search(fn_norm):
                return True
    for cat in ("Taxes", "Education", "Employment", "Medical"):
        if cat in CATEGORY_PATTERNS and CATEGORY_PATTERNS[cat].search(fn_norm):
            return True
    return False


def extract_document_data(file_path: Path, input_root: Optional[Path] = None, skip_ocr: bool = False, ocr_max_pages: int = 2) -> Dict[str, Any]:
    ext = file_path.suffix.lower()
    full_text, last_page_text = "", ""
    pdf_meta: Dict[str, str] = {}
    phash: Optional[str] = None
    is_landscape: bool = False

    if ext == ".pdf":
        full_text, last_page_text, pdf_meta, phash = extract_pdf_content(file_path, skip_ocr=skip_ocr, ocr_max_pages=ocr_max_pages)
    elif ext in IMAGE_EXTENSIONS:
        full_text, phash, is_landscape = extract_image_content(file_path, skip_ocr=skip_ocr)
    elif ext == ".docx":
        full_text, last_page_text = extract_docx_content(file_path)
    elif ext == ".xlsx":
        full_text, last_page_text = extract_xlsx_content(file_path)
    elif ext == ".txt":
        full_text, last_page_text = extract_plain_text(file_path)

    first_2500 = full_text[:2500].strip()
    tail_text = last_page_text[:1500].strip() if last_page_text else full_text[-1500:].strip()
    if tail_text == first_2500 or len(full_text) <= 2500:
        tail_text = ""

    rel_folder = file_path.parent.name
    if input_root is not None:
        try:
            rel_parent = file_path.parent.resolve().relative_to(input_root.resolve())
            rel_folder = "" if str(rel_parent) == "." else str(rel_parent)
        except Exception:
            pass

    # Scanner-tool folders (CamScanner, Scanned_CamScanner...) carry no meaning about the document
    rel_folder = strip_scanner_tool_folders(rel_folder)
    parent_name = strip_scanner_tool_folders(file_path.parent.name)

    return {
        "filename": file_path.name,
        "extension": ext,
        "parent_folder": parent_name,
        "relative_folder": rel_folder,
        "metadata_text": pdf_meta.get("meta_text", ""),
        "pdf_author": pdf_meta.get("author", ""),
        "pdf_creation_date": pdf_meta.get("creation_date", ""),
        "phash": phash,
        "is_landscape": is_landscape,
        "first_text": first_2500,
        "tail_text": tail_text,
        "full_text": full_text
    }