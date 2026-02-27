"""
Utility functions: logging setup, filename sanitization, extension validation.
"""
import logging
import re
import sys
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging to stdout with a clean format."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "httpx", "openai", "pdfminer", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for safe use as a filename component.

    - Removes null bytes and path separators (prevents path traversal)
    - Replaces shell-unsafe characters with underscores
    - Collapses whitespace/underscore runs
    - Limits to 100 characters
    """
    if not name:
        return "unknown"
    name = name.replace("\x00", "")
    # Strip path separators — most critical for traversal prevention
    name = re.sub(r"[/\\]", "_", name)
    # Remove characters unsafe on Windows/Linux filesystems
    name = re.sub(r'[<>:"|?*]', "_", name)
    # Collapse runs of whitespace and underscores
    name = re.sub(r"[\s_]+", "_", name)
    name = name.strip("._")
    return name[:100] if name else "unknown"


def sanitize_vendor_name(vendor: str) -> str:
    """
    Sanitize a vendor name for safe use as a directory component.

    Returns a lowercase, path-safe directory name.
    Handles German umlauts and common special characters.
    """
    if not vendor or vendor.strip().lower() in ("unknown", ""):
        return "unknown_vendor"

    clean = vendor.lower()
    # Allow German umlauts, alphanumerics, spaces, hyphens
    clean = re.sub(r"[^a-z0-9äöüß\s\-]", "", clean)
    clean = re.sub(r"[\s\-]+", "_", clean)
    clean = clean.strip("_")
    return clean[:80] if clean else "unknown_vendor"


def is_allowed_extension(filename: str, allowed: set[str]) -> bool:
    """Return True if the filename's extension is in the allowed set."""
    return Path(filename).suffix.lower() in allowed
