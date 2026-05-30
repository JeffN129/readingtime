"""
Format conversion — convert non-EPUB ebooks to EPUB.

Uses the pure-Python ``mobi`` package for AZW3/MOBI → EPUB conversion.
Calibre's ``ebook-convert`` is an optional fallback for PDF files and
other formats that the pure-Python path cannot handle.

AZW3 (KF8) files are essentially EPUBs wrapped in a PalmDB container.
MOBI (KF7) files contain HTML/OPF/NCX that can be repackaged as EPUB.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ebooklib import epub

logger = logging.getLogger(__name__)

# Formats we recognise as ebooks (not reading notes or other files)
_EBOOK_EXTENSIONS = [".epub", ".azw3", ".mobi", ".pdf", ".azw", ".prc", ".txt"]

# Formats that the pure-Python path can handle
_PURE_PYTHON_FORMATS = {".azw3", ".mobi", ".azw", ".prc"}


def convert_to_epub(filepath: Path) -> Path | None:
    """Convert a non-EPUB ebook to EPUB.

    Tries pure-Python conversion first (AZW3/MOBI), then falls back to
    Calibre if available (PDF, other formats).

    Also detects mislabeled files (EPUBs saved with wrong extension).

    Args:
        filepath: Path to the ebook file.

    Returns:
        The path to the new EPUB on success, or ``None`` on failure.
        On success the *original* file is deleted.
    """
    if filepath.suffix.lower() == ".epub":
        return filepath

    # -- Check if file is actually an EPUB mislabeled as something else -----
    actual_fmt = _detect_format(filepath)
    if actual_fmt == "epub":
        epub_path = filepath.with_suffix(".epub")
        filepath.rename(epub_path)
        logger.info("File was actually EPUB — renamed: %s → %s", filepath.name, epub_path.name)
        return epub_path

    ext = filepath.suffix.lower()

    # -- Pure-Python path (AZW3 / MOBI) -----------------------------------
    if ext in _PURE_PYTHON_FORMATS:
        logger.info("Converting %s → EPUB (pure Python) …", filepath.name)
        epub_path = filepath.with_suffix(".epub")
        try:
            _kindle_to_epub(filepath, epub_path)
            if epub_path.exists() and epub_path.stat().st_size > 0:
                logger.info("Converted %s → EPUB (%.1f KB)", filepath.name, epub_path.stat().st_size / 1024)
                filepath.unlink()
                return epub_path
        except Exception as exc:
            logger.warning("Pure-Python conversion failed: %s", exc)
            # Clean up partial output
            if epub_path.exists():
                epub_path.unlink()

    # -- Calibre fallback (PDF, or anything the pure-Python path didn't handle)
    if _has_calibre():
        logger.info("Converting %s → EPUB (Calibre) …", filepath.name)
        epub_path = filepath.with_suffix(".epub")
        try:
            result = subprocess.run(
                ["ebook-convert", str(filepath), str(epub_path)],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0 and epub_path.exists() and epub_path.stat().st_size > 0:
                logger.info("Converted %s → EPUB (%.1f KB)", filepath.name, epub_path.stat().st_size / 1024)
                filepath.unlink()
                return epub_path
            else:
                logger.warning("Calibre conversion failed: %s", result.stderr[:300])
                if epub_path.exists():
                    epub_path.unlink()
                return None
        except subprocess.TimeoutExpired:
            logger.warning("Calibre conversion timed out for %s", filepath.name)
            if epub_path.exists():
                epub_path.unlink()
            return None
        except Exception as exc:
            logger.warning("Calibre conversion error: %s", exc)
            if epub_path.exists():
                epub_path.unlink()
            return None
    else:
        if ext not in _PURE_PYTHON_FORMATS:
            logger.warning(
                "Cannot convert %s — Calibre not installed for PDF support. "
                "Install from https://calibre-ebook.com/download",
                ext,
            )
        return None


# ---------------------------------------------------------------------------
# Pure-Python Kindle → EPUB
# ---------------------------------------------------------------------------

def _kindle_to_epub(src_path: Path, dst_path: Path) -> None:
    """Convert an AZW3/MOBI file to EPUB using the ``mobi`` package.

    Workflow:
        1. Extract the Kindle file to a temp directory.
        2. Repackage the extracted content as a valid EPUB.
    """
    from mobi.kindleunpack import unpackBook

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        extract_dir = tmpd / "extracted"
        extract_dir.mkdir()

        # Unpack the Kindle file
        unpackBook(str(src_path), str(extract_dir), epubver="3")

        # Find the content directory (mobi8 for KF8/AZW3, mobi7 for older MOBI)
        content_dir = None
        for candidate in ["mobi8", "mobi7", "azw3"]:
            cd = extract_dir / candidate
            if cd.exists() and (cd / "content.opf").exists():
                content_dir = cd
                break

        if content_dir is None:
            raise RuntimeError(f"No content.opf found in extracted {src_path.suffix}")

        # Build EPUB from extracted content
        _build_epub_from_extracted(content_dir, dst_path)


def _build_epub_from_extracted(content_dir: Path, dst_path: Path) -> None:
    """Package extracted Kindle content as a valid EPUB.

    Reads ``content.opf`` for metadata and manifest, then repackages
    everything into a proper EPUB ZIP with correct mimetype handling.
    """
    opf_path = content_dir / "content.opf"
    if not opf_path.exists():
        raise RuntimeError(f"content.opf not found in {content_dir}")

    # -- Parse OPF for metadata --------------------------------------------
    import re
    opf_text = opf_path.read_text("utf-8", errors="replace")

    title = _extract_opf_tag(opf_text, "title")
    author = _extract_opf_tag(opf_text, "creator")
    language = _extract_opf_tag(opf_text, "language") or "zh"

    # -- Build EPUB with ebooklib -----------------------------------------
    book = epub.EpubBook()
    book.set_identifier(f"readingtime-{dst_path.stem}")
    book.set_title(title or dst_path.stem)
    if author:
        book.add_author(author)
    book.set_language(language)

    # Collect all referenced files from OPF manifest
    manifest_items = re.findall(
        r'<item[^>]+href="([^"]+)"[^>]+media-type="([^"]+)"',
        opf_text,
    )

    spine_order: list[str] = []
    chapters: list[epub.EpubHtml] = []

    for href, media_type in manifest_items:
        item_path = content_dir / href
        if not item_path.exists():
            continue

        if "html" in media_type:
            # HTML content item — add as chapter
            raw = item_path.read_bytes()
            html_text = raw.decode("utf-8", errors="replace")

            # Deduplicate file names
            fname = Path(href).name
            counter = 1
            while any(c.file_name == fname for c in chapters):
                stem, ext = os.path.splitext(fname)
                fname = f"{stem}_{counter}{ext}"
                counter += 1

            chapter = epub.EpubHtml(
                title=fname,
                file_name=fname,
                lang=language,
            )
            chapter.content = html_text
            book.add_item(chapter)
            chapters.append(chapter)
            spine_order.append(fname)

        elif "image" in media_type or href.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg")):
            # Image — add to EPUB
            fname = Path(href).name
            img_content = item_path.read_bytes()
            img_item = epub.EpubItem(
                uid=f"img_{fname}",
                file_name=f"images/{fname}",
                media_type=media_type,
                content=img_content,
            )
            book.add_item(img_item)

        elif "ncx" in media_type or href.endswith(".ncx"):
            # NCX (TOC) — add to book
            book.add_item(epub.EpubItem(
                uid="ncx",
                file_name="toc.ncx",
                media_type="application/x-dtbncx+xml",
                content=item_path.read_bytes(),
            ))

    # If no chapters found, try any HTML file in the directory
    if not chapters:
        for f in sorted(content_dir.iterdir()):
            if f.suffix.lower() in (".html", ".htm", ".xhtml"):
                raw = f.read_bytes()
                html_text = raw.decode("utf-8", errors="replace")
                chapter = epub.EpubHtml(
                    title=f.name,
                    file_name=f.name,
                    lang=language,
                )
                chapter.content = html_text
                book.add_item(chapter)
                chapters.append(chapter)
                spine_order.append(f.name)

    if not chapters:
        raise RuntimeError(f"No HTML content found in {content_dir}")

    # -- Set up spine and TOC ----------------------------------------------
    book.spine = chapters
    book.toc = chapters  # ebooklib expects EpubHtml objects directly

    # Add default NCX and nav if not present
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # -- Write EPUB --------------------------------------------------------
    epub.write_epub(str(dst_path), book)
    logger.debug("EPUB written: %s (%.1f KB)", dst_path.name, dst_path.stat().st_size / 1024)


def _extract_opf_tag(opf_text: str, tag: str) -> str:
    """Extract a Dublin Core tag value from OPF XML text."""
    import re
    # Match <dc:tag>value</dc:tag>
    pattern = rf'<dc:{tag}[^>]*>\s*(.+?)\s*</dc:{tag}>'
    m = re.search(pattern, opf_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# Utility: find the actual ebook file in a directory
# ---------------------------------------------------------------------------

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


def _detect_format(filepath: Path) -> str | None:
    """Detect actual ebook format from file magic bytes.

    Returns one of: 'epub', 'pdf', 'mobi', 'azw3', or None if unknown.
    """
    try:
        with open(filepath, "rb") as fh:
            head = fh.read(68)
    except OSError:
        return None

    # EPUB / ZIP: PK\x03\x04 with mimetype
    if head[:4] == b"PK\x03\x04":
        if b"mimetypeapplication/epub+zip" in head[:68]:
            return "epub"
        return None

    # PDF
    if head[:5] == b"%PDF-":
        return "pdf"

    # MOBI / AZW3: PalmDB header
    if head[:32].startswith(b"BOOKMOBI"):
        return "mobi"

    # PalmDB without BOOKMOBI marker (could be AZW3)
    # Check for PalmDB header at offset 0
    # (database name, attributes, version, etc.)
    try:
        name = head[:32].rstrip(b"\x00").decode("ascii", errors="replace")
        if len(name) > 0 and all(c.isprintable() or c in "\x00" for c in name):
            # Could be a PalmDB file (AZW3)
            if len(head) >= 68:
                # Check for MOBI header at offset 16+ (after PalmDB header)
                mobi_magic = head[60:68]
                if mobi_magic == b"BOOKMOBI":
                    return "azw3"
    except Exception:
        pass

    return None
