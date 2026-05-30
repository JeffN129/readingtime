"""
Format conversion — convert non-EPUB ebooks to EPUB using Calibre.

Uses Calibre's ``ebook-convert`` CLI tool. Falls back gracefully if Calibre
is not installed (original file is kept, a warning is logged).

Supported input formats: AZW3, MOBI, PDF, and anything Calibre can read.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Formats we recognize as ebooks (not reading notes or other files)
_EBOOK_EXTENSIONS = {".epub", ".azw3", ".mobi", ".pdf", ".azw", ".prc", ".txt"}


def convert_to_epub(filepath: Path) -> Path | None:
    """Convert a non-EPUB ebook to EPUB via Calibre's ``ebook-convert``.

    Args:
        filepath: Path to the ebook file (any format Calibre can read).

    Returns:
        The path to the new EPUB file if conversion succeeded, or ``None``
        if conversion failed or Calibre is not installed.
        On success the *original* file is deleted.
    """
    if filepath.suffix.lower() == ".epub":
        return filepath

    # Check for Calibre
    if not _has_calibre():
        logger.warning(
            "Calibre not installed — cannot convert %s to EPUB. "
            "Install from https://calibre-ebook.com/download",
            filepath.suffix,
        )
        return None

    epub_path = filepath.with_suffix(".epub")
    logger.info("Converting %s → EPUB …", filepath.name)

    try:
        result = subprocess.run(
            [
                "ebook-convert",
                str(filepath),
                str(epub_path),
            ],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min — large PDFs can be slow
        )

        if result.returncode == 0 and epub_path.exists() and epub_path.stat().st_size > 0:
            logger.info("Converted %s → EPUB (%.1f KB)", filepath.name, epub_path.stat().st_size / 1024)
            # Remove the original non-EPUB file
            filepath.unlink()
            return epub_path
        else:
            logger.warning("Conversion failed for %s: %s", filepath.name, result.stderr[:300])
            # Clean up partial output
            if epub_path.exists():
                epub_path.unlink()
            return None

    except subprocess.TimeoutExpired:
        logger.warning("Conversion timed out for %s", filepath.name)
        if epub_path.exists():
            epub_path.unlink()
        return None
    except Exception as exc:
        logger.warning("Conversion error for %s: %s", filepath.name, exc)
        if epub_path.exists():
            epub_path.unlink()
        return None


def find_book_file(parent_dir: Path, stem: str) -> Path | None:
    """Find the ebook file in *parent_dir* whose name starts with *stem*.

    After download, the file may have any extension (azw3, mobi, pdf, epub).
    This helper locates it so we can convert it.

    Returns the file path, or None if no ebook file was found.
    """
    for ext in _EBOOK_EXTENSIONS:
        candidate = parent_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    # Fallback: try any file matching the stem
    for f in parent_dir.glob(f"{stem}.*"):
        if f.suffix.lower() in _EBOOK_EXTENSIONS:
            return f
    return None


def _has_calibre() -> bool:
    """Check if Calibre's ebook-convert is available on PATH."""
    return shutil.which("ebook-convert") is not None
