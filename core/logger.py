"""
core/logger.py - Central Structured Logging for Smart Document Sorter.
Provides standardized logging (INFO, WARNING, ERROR, DEBUG) to both console and log files.
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from core.config import RUNTIME_ROOT_DIR

_logger: Optional[logging.Logger] = None


def setup_logger(
    log_dir: Optional[Path] = None,
    level: int = logging.INFO,
    log_to_console: bool = True
) -> logging.Logger:
    """
    Initializes and configures the central application logger.
    Logs are written to runtime/logs/sorter.log with automatic rotation (max 5MB, 3 backups).
    """
    global _logger

    target_log_dir = log_dir or (RUNTIME_ROOT_DIR / "logs")
    target_log_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = target_log_dir / "sorter.log"

    logger = logging.getLogger("SmartDocSorter")
    logger.setLevel(level)

    # Avoid duplicate handlers if already configured
    if logger.handlers:
        return logger

    # Log format: [2026-11-21 18:25:00] [INFO] [pipeline] Scanned 120 documents
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating file handler (5 MB per file, up to 3 archives)
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console handler
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    _logger = logger
    return logger


def get_logger(module_name: Optional[str] = None) -> logging.Logger:
    """
    Returns a child logger scoped to a specific module (e.g., 'SmartDocSorter.extractor').
    """
    global _logger
    if _logger is None:
        setup_logger()

    base = _logger or logging.getLogger("SmartDocSorter")
    if module_name:
        return base.getChild(module_name)
    return base
