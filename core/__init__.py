"""
core package - Modular Architecture for Smart Document Sorter.
Exports top-level sorter orchestration, configurations, models, extractors, rules, matchers, classifiers, and storage utilities.
"""

# Config & Constants
from core.config import (
    PROJECT_ROOT,
    RUNTIME_ROOT_DIR,
    INTERNAL_DATA_DIR,
    CURRENT_RUN_DIR,
    LEARNING_CSV_PATH,
    PROGRESS_STATUS_PATH,
    USER_SETTINGS_PATH,
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    DRY_RUN,
    MODEL_NAME,
    DEFAULT_PYTHON_CONFIDENCE_THRESHOLD,
    DEFAULT_REVIEW_CONFIDENCE_THRESHOLD,
    EXPIRING_SOON_DAYS,
    MAX_IMAGE_DIM,
    STATE_SAVE_INTERVAL,
    PHASH_HAMMING_THRESHOLD,
    RECURRING_NAME_MIN_DOCS,
    MIN_DOCS_FOR_VENDOR_CLUBBING,
    DEFAULT_FAMILY_CONFIG,
    VALID_CATEGORIES,
    EXPIRABLE_CATEGORIES,
    SPECIFIC_DOMAIN_CATEGORIES,
    SUPPORTED_EXTENSIONS,
    IMAGE_EXTENSIONS,
    IGNORED_DIRS,
    IGNORED_FILENAMES,
    LEARNING_STOPWORDS,
    NON_HUMAN_NAME_WORDS,
    KNOWN_VENDORS,
    SCANNER_WATERMARK_RE,
    SECONDARY_RELATION_LINE_RE,
    PHOTO_FILENAME_RE,
    DATE_PATTERNS,
    EXPIRATION_PATTERNS,
    CANDIDATE_PERSON_LABEL_RE,
    normalize_for_matching
)

# Extractor
from core.extractor import (
    rapid_ocr,
    compute_perceptual_hash,
    hamming_distance_hex,
    run_ocr_on_image,
    parse_pdf_metadata_date,
    _needs_ocr_for_pdf_page,
    extract_pdf_content,
    extract_image_content,
    extract_docx_content,
    extract_xlsx_content,
    extract_plain_text,
    should_skip_heavy_ocr,
    extract_document_data
)

# Rules
from core.rules import (
    CATEGORY_PATTERNS,
    FOLDER_PATH_HINTS,
    load_learning_csv,
    save_learned_keyword
)

# Deduplicator
from core.deduplicator import (
    compute_sha256,
    _is_true_near_duplicate
)

# Person Matcher
from core.person_matcher import (
    _match_person_in_text,
    canonicalize_person,
    detect_vendor_or_series,
    extract_unlisted_person_candidates
)

# Classifier
from core.classifier import (
    ollama_client,
    DocumentClassification,
    parse_date_safe,
    calculate_expiration_status,
    extract_dates_deterministically,
    evaluate_exception_rules,
    classify_deterministically,
    create_distilled_payload,
    classify_with_ollama
)

# Storage & File Operations
from core.storage import (
    atomic_write_json,
    load_user_settings,
    save_user_settings,
    update_progress_status,
    load_progress_state,
    save_progress_state,
    clean_output_directory,
    sanitize_folder_name,
    determine_destination,
    resolve_collision,
    write_plan_artifacts,
    load_plan_with_manual_edits,
    _compute_vendor_counts,
    apply_existing_plan,
    promote_recurring_names_in_plan,
    scan_and_summarize_files
)

# Reporter
from core.reporter import (
    generate_review_dashboard,
    write_debug_report
)

# Pipeline Orchestration
from core.pipeline import (
    run_sorter
)

# Logger
from core.logger import setup_logger, get_logger

# Renamer
from core.renamer import generate_standardized_filename

__all__ = [
    # Top-level orchestration
    "run_sorter",
    "apply_existing_plan",
    "promote_recurring_names_in_plan",
    "scan_and_summarize_files",

    # Settings and config
    "load_user_settings",
    "save_user_settings",
    "PROJECT_ROOT",
    "RUNTIME_ROOT_DIR",
    "INTERNAL_DATA_DIR",
    "CURRENT_RUN_DIR",
    "LEARNING_CSV_PATH",
    "PROGRESS_STATUS_PATH",
    "USER_SETTINGS_PATH",
    "DEFAULT_INPUT_DIR",
    "DEFAULT_OUTPUT_DIR",
    "DRY_RUN",
    "MODEL_NAME",
    "DEFAULT_PYTHON_CONFIDENCE_THRESHOLD",
    "DEFAULT_REVIEW_CONFIDENCE_THRESHOLD",
    "VALID_CATEGORIES",
    "EXPIRABLE_CATEGORIES",
    "SPECIFIC_DOMAIN_CATEGORIES",
    "SUPPORTED_EXTENSIONS",
    "IMAGE_EXTENSIONS",

    # Models & Classifiers
    "DocumentClassification",
    "classify_deterministically",
    "classify_with_ollama",
    "evaluate_exception_rules",
    "ollama_client",

    # Extractors
    "extract_document_data",
    "extract_pdf_content",
    "extract_image_content",
    "run_ocr_on_image",
    "rapid_ocr",

    # Matchers & Deduplication
    "canonicalize_person",
    "_match_person_in_text",
    "compute_sha256",
    "_is_true_near_duplicate",

    # Rules
    "load_learning_csv",
    "save_learned_keyword",
    "CATEGORY_PATTERNS",
    "FOLDER_PATH_HINTS",

    # Reporters
    "generate_review_dashboard",
    "write_debug_report",

    # Logger & Renamer
    "setup_logger",
    "get_logger",
    "generate_standardized_filename",
]
