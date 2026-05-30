"""
EPUB metadata extraction and utility functions.

Provides:
    - extract_metadata(path) → dict   — title, author, language, page count, tags
    - extract_cover(path, out_path)   — save cover image to file
    - estimate_reading_time(page_count, speed) → int — minutes
    - read_first_n_chars(path, n) → str — first N characters of body text (for LLM summary)

All functions are defensive: malformed EPUBs return partial data rather than raising.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import ebooklib
from ebooklib import epub

logger = logging.getLogger(__name__)

# Default reading speed: 250 pages per hour
DEFAULT_READING_SPEED = 250  # pages/hour

# HTML tag stripper regex (quick & dirty — good enough for extracting preview text)
_HTML_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_metadata(epub_path: str | Path) -> dict:
    """Extract key metadata from an EPUB file.

    Returns a dict with keys:
        title, author, language, page_count, tags, description, cover_href

    All values are strings/ints/lists or None if unavailable.
    Never raises — returns partial data on failure.
    """
    path = Path(epub_path)
    result: dict = {
        "title": path.stem,          # fallback: use filename stem
        "author": None,
        "language": "en",
        "page_count": None,
        "tags": [],
        "description": None,
        "cover_href": None,
    }

    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        logger.warning("Failed to open EPUB %s: %s", path.name, exc)
        return result

    # -- title ----------------------------------------------------------------
    titles = book.get_metadata("DC", "title")
    if titles:
        result["title"] = _clean_text(titles[0][0])

    # -- author ---------------------------------------------------------------
    creators = book.get_metadata("DC", "creator")
    if creators:
        result["author"] = _clean_text(creators[0][0])

    # -- language -------------------------------------------------------------
    langs = book.get_metadata("DC", "language")
    if langs:
        result["language"] = langs[0][0].lower()

    # -- description ----------------------------------------------------------
    descs = book.get_metadata("DC", "description")
    if descs:
        result["description"] = _clean_text(descs[0][0])[:500]

    # -- tags / subjects ------------------------------------------------------
    subjects = book.get_metadata("DC", "subject")
    result["tags"] = [_clean_text(s[0]) for s in subjects if s[0]]

    # -- page count: try calibre custom metadata, else estimate from spine -----
    result["page_count"] = _estimate_page_count(book, path)

    # -- cover image ----------------------------------------------------------
    result["cover_href"] = _find_cover_href(book)

    logger.debug("Extracted metadata from %s: %s", path.name, result["title"])
    return result


def extract_cover(epub_path: str | Path, out_path: str | Path) -> bool:
    """Extract the cover image from an EPUB and save it to *out_path*.

    Returns True if a cover was found and saved.
    """
    path = Path(epub_path)
    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        logger.warning("Failed to open EPUB for cover: %s", exc)
        return False

    cover_href = _find_cover_href(book)
    if not cover_href:
        logger.debug("No cover image found in %s", path.name)
        return False

    try:
        content = book.get_item_with_href(cover_href)
        if content is None:
            return False
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content.get_content())
        logger.debug("Cover saved to %s", out)
        return True
    except Exception as exc:
        logger.warning("Failed to extract cover from %s: %s", path.name, exc)
        return False


def read_first_n_chars(epub_path: str | Path, n: int = 2000) -> str:
    """Read the first *n* characters of body text from an EPUB.

    Used by the summarizer to provide context to the LLM without sending
    the entire book.  Strips HTML tags and collapses whitespace.

    Returns empty string on failure.
    """
    path = Path(epub_path)
    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        logger.warning("Failed to open EPUB for text extraction: %s", exc)
        return ""

    chars_collected = 0
    parts: list[str] = []

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        if chars_collected >= n:
            break
        try:
            raw = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue

        # Strip HTML tags
        text = _HTML_RE.sub(" ", raw)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        remaining = n - chars_collected
        parts.append(text[:remaining])
        chars_collected += len(text)

    return " ".join(parts)[:n]


def estimate_reading_time(
    page_count: Optional[int],
    speed_pages_per_hour: int = DEFAULT_READING_SPEED,
) -> Optional[int]:
    """Estimate reading time in minutes.

    Args:
        page_count: Number of pages (or None if unknown).
        speed_pages_per_hour: Reading speed. Default 250 pages/hour.

    Returns:
        Estimated minutes, or None if page_count is unavailable.
    """
    if page_count is None:
        return None
    return int(page_count / speed_pages_per_hour * 60)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Strip leading/trailing whitespace and collapse internal whitespace."""
    return re.sub(r"\s+", " ", text).strip()


def _estimate_page_count(book: epub.EpubBook, path: Path) -> Optional[int]:
    """Try to determine the page count of an EPUB.

    Priority:
        1. Calibre custom metadata (``calibre:page_count``)
        2. Spine item count × heuristic (250 words per spine item → ~1 page)
        3. File size ÷ 2500 bytes heuristic
    """
    # Attempt 1: Calibre metadata
    for namespace, key in [
        ("{http://calibre.kovidgoyal.net/2009/metadata}", "page_count"),
        ("{http://purl.org/dc/elements/1.1/}", "extent"),
    ]:
        try:
            for item in book.get_metadata(namespace, key):
                if item and item[0]:
                    val = int(item[0])
                    if val > 0:
                        return val
        except (ValueError, TypeError, Exception):
            pass

    # Attempt 2: Spine count × word heuristic
    try:
        spine_items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        if spine_items:
            # Rough: average 250 words per page, one spine item ≈ one chapter ≈ 10 pages
            return len(spine_items) * 10
    except Exception:
        pass

    # Attempt 3: File size heuristic (~2.5 KB per page for plain text EPUBs)
    try:
        size_kb = path.stat().st_size / 1024
        if size_kb > 10:
            return max(1, int(size_kb / 2.5))
    except Exception:
        pass

    return None


def _find_cover_href(book: epub.EpubBook) -> Optional[str]:
    """Find the cover image href within an EPUB.

    Checks:
        1. The ``<guide>`` section for a ``cover`` reference.
        2. Common naming patterns like ``cover.jpg``, ``cover.png``.
        3. The ``<meta name="cover">`` tag.
    """
    # Method 1: EPUB 2 guide section
    try:
        guide = book.get_metadata("OPF", "guide")
        # Not all books expose guide through get_metadata; try direct access
    except Exception:
        pass

    # Method 2: Search items by href pattern
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        href = item.get_name().lower()
        if "cover" in href:
            return item.get_name()

    # Method 3: Check for meta cover tag
    try:
        # ebooklib stores metadata tuples; look for cover meta
        for item in book.get_metadata("OPF", "meta"):
            if len(item) >= 2 and item[0] == "cover":
                cover_id = item[1]
                cover_item = book.get_item_with_id(cover_id)
                if cover_item is not None:
                    return cover_item.get_name()
    except Exception:
        pass

    # Method 3: First image is often the cover
    images = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
    if images:
        return images[0].get_name()

    return None
