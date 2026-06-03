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
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ebooklib import epub

logger = logging.getLogger(__name__)

# Regex patterns for cleaning mobi-specific markup
_MOBI_PAGEBREAK = re.compile(r"<mbp:pagebreak\s*/?>", re.IGNORECASE)
_MOBI_ATTRS = re.compile(r"""\s+(?:height|width)\s*=\s*["'][^"']*["']""", re.IGNORECASE)

# Patterns for detecting chapter/section titles in Chinese ebooks
# NOTE: Uses explicit alternation instead of (?:...)? because of a
# Python 3.14 regex bug where optional non-capturing groups don't
# backtrack correctly when the group content is absent.
_CHAPTER_TITLE_PATTERNS = [
    # <font><b>第X章</b></font> or <b>第X章</b>
    re.compile(
        r"<font[^>]*>\s*<b>\s*"
        r"(第[一二三四五六七八九十百千\d]+[章节回卷部集篇].*?)"
        r"\s*</b>\s*</font>",
        re.IGNORECASE,
    ),
    re.compile(
        r"<b>\s*"
        r"(第[一二三四五六七八九十百千\d]+[章节回卷部集篇].*?)"
        r"\s*</b>",
        re.IGNORECASE,
    ),
    # <font><b>Any Title</b></font> or <b>Any Title</b>
    re.compile(
        r"<font[^>]*>\s*<b>\s*"
        r"([^<]{2,60}?)"
        r"\s*</b>\s*</font>",
        re.IGNORECASE,
    ),
    re.compile(
        r"<b>\s*"
        r"([^<]{2,60}?)"
        r"\s*</b>",
        re.IGNORECASE,
    ),
]

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
        1. Extract the Kindle file to a temp directory via ``unpackBook``.
        2. Check if ``unpackBook`` already created a valid EPUB (KF8/AZW3 path).
        3. Otherwise, build an EPUB from the extracted content files.
    """
    from mobi.kindleunpack import unpackBook

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        extract_dir = tmpd / "extracted"
        extract_dir.mkdir()

        # Unpack the Kindle file
        unpackBook(str(src_path), str(extract_dir), epubver="3")

        # -- Check if unpackBook already created an EPUB (KF8/AZW3 path) ----
        for sub in extract_dir.iterdir():
            if sub.is_dir():
                for epub_file in sub.rglob("*.epub"):
                    if epub_file.stat().st_size > 1000:
                        shutil.copy2(epub_file, dst_path)
                        logger.info(
                            "Using mobi-built EPUB: %s (%.1f KB)",
                            epub_file.name,
                            epub_file.stat().st_size / 1024,
                        )
                        return

        # -- No pre-built EPUB → find content directory and build one --------
        content_dir = _find_content_dir(extract_dir)
        if content_dir is None:
            raise RuntimeError(f"No content.opf found in extracted {src_path.suffix}")

        _build_epub_from_extracted(content_dir, dst_path)


def _find_content_dir(extract_dir: Path) -> Path | None:
    """Find the directory that contains ``content.opf`` in the unpacked tree.

    Supports both MOBI7 (flat) and KF8 (OEBPS subdirectory) layouts.
    """
    # Direct OPF in subdirectory (MOBI7 layout)
    for candidate in ["mobi8", "mobi7", "azw3"]:
        cd = extract_dir / candidate
        opf = cd / "content.opf"
        if cd.exists() and opf.exists():
            return cd

    # OEBPS subdirectory (KF8 layout)
    for candidate in ["mobi8", "mobi7", "azw3"]:
        cd = extract_dir / candidate
        oebps = cd / "OEBPS"
        if oebps.exists() and (oebps / "content.opf").exists():
            return oebps

    # Deep search as last resort
    for opf in sorted(extract_dir.rglob("content.opf")):
        return opf.parent

    return None


def _build_epub_from_extracted(content_dir: Path, dst_path: Path) -> None:
    """Package extracted Kindle content as a valid EPUB.

    Reads ``content.opf`` for metadata and manifest, then repackages
    everything into a proper EPUB ZIP with correct mimetype handling.

    For MOBI7 books that unpack to a single HTML file, the content is
    split at ``<mbp:pagebreak/>`` boundaries so each chapter becomes a
    separate spine item — this ensures compatibility with phone EPUB
    readers that struggle with huge single-file books.
    """
    opf_path = content_dir / "content.opf"
    if not opf_path.exists():
        raise RuntimeError(f"content.opf not found in {content_dir}")

    # -- Parse OPF for metadata --------------------------------------------
    opf_text = opf_path.read_text("utf-8", errors="replace")

    title = _extract_opf_tag(opf_text, "title")
    author = _extract_opf_tag(opf_text, "creator")

    # -- Collect HTML files -------------------------------------------------
    html_files = _find_html_files(content_dir, opf_text)

    # -- Process each HTML file into chapters -------------------------------
    all_chapters: list[epub.EpubHtml] = []
    chapter_counter = 0

    for html_path in html_files:
        raw = html_path.read_bytes()
        html_text = raw.decode("utf-8", errors="replace")

        # Split at page breaks and clean
        segments = _split_and_clean(html_text)

        for seg_title, seg_body in segments:
            chapter_counter += 1

            # Deduplicate file names
            base = _safe_filename(seg_title) or f"chapter{chapter_counter:03d}"
            fname = f"{base}.xhtml"
            counter = 1
            while any(c.file_name == fname for c in all_chapters):
                fname = f"{base}_{counter}.xhtml"
                counter += 1

            chapter = epub.EpubHtml(
                title=seg_title,
                file_name=fname,
                lang="zh",
            )
            chapter.content = seg_body
            all_chapters.append(chapter)

    if not all_chapters:
        # Fallback: grab any HTML in the directory
        for f in sorted(content_dir.rglob("*.html")):
            raw = f.read_bytes()
            html_text = raw.decode("utf-8", errors="replace")
            chapter = epub.EpubHtml(
                title=f.stem,
                file_name=f.name,
                lang="zh",
            )
            chapter.content = html_text
            all_chapters.append(chapter)

    if not all_chapters:
        raise RuntimeError(f"No HTML content found in {content_dir}")

    # -- Detect language from actual content --------------------------------
    sample = "".join(c.content for c in all_chapters[:3] if c.content)[:8000]
    language = _detect_language(sample)

    # -- Build EPUB with ebooklib ------------------------------------------
    book = epub.EpubBook()
    book.set_identifier(f"readingtime-{dst_path.stem}")
    book.set_title(title or dst_path.stem)
    if author:
        book.add_author(author)
    book.set_language(language)

    # Add all chapters as book items
    for chapter in all_chapters:
        book.add_item(chapter)

    # -- Add images and other resources from extracted content --------------
    _add_resources(book, content_dir, opf_text)

    # -- Build proper TOC --------------------------------------------------
    book.toc = _build_toc(all_chapters)
    book.spine = all_chapters

    # Add NCX and NAV for EPUB2/EPUB3 dual compatibility
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # -- Write EPUB --------------------------------------------------------
    epub.write_epub(str(dst_path), book)
    logger.debug(
        "EPUB written: %s (%d chapters, %.1f KB)",
        dst_path.name,
        len(all_chapters),
        dst_path.stat().st_size / 1024,
    )


# ---------------------------------------------------------------------------
# Helpers for _build_epub_from_extracted
# ---------------------------------------------------------------------------


def _find_html_files(content_dir: Path, opf_text: str) -> list[Path]:
    """Find HTML/XHTML files referenced in the OPF manifest.

    Falls back to globbing the directory if the manifest yields nothing.
    """
    manifest_hrefs = re.findall(
        r'<item[^>]+href="([^"]+)"[^>]+media-type="([^"]+)"',
        opf_text,
    )

    html_files: list[Path] = []
    for href, media_type in manifest_hrefs:
        if "html" not in media_type:
            continue
        item_path = content_dir / href
        if item_path.exists():
            html_files.append(item_path)

    # Fallback: any .html/.xhtml/.htm in the directory tree
    if not html_files:
        for ext in (".html", ".htm", ".xhtml"):
            html_files.extend(sorted(content_dir.rglob(f"*{ext}")))

    return html_files


def _split_and_clean(html_text: str) -> list[tuple[str, str]]:
    """Split HTML at page-break boundaries and clean mobi markup.

    Returns a list of ``(title, body_html)`` tuples, where *body_html*
    is a complete XHTML document fragment suitable for ebooklib.
    """
    # Extract body content
    body_m = re.search(
        r"<body[^>]*>(.*?)</body>", html_text, re.DOTALL | re.IGNORECASE
    )
    if not body_m:
        # No body tag — treat as a single segment
        return [(_extract_chapter_title(html_text) or "Book", html_text)]

    body_inner = body_m.group(1)

    # Split at <mbp:pagebreak/>
    raw_parts = _MOBI_PAGEBREAK.split(body_inner)
    segments = [p.strip() for p in raw_parts if p.strip()]

    if not segments:
        return [("Book", body_inner)]

    chapters: list[tuple[str, str]] = []
    for seg in segments:
        # Clean mobi-specific markup
        cleaned = _clean_mobi_html(seg)

        # Detect title from the segment
        seg_title = _extract_chapter_title(cleaned)

        if not seg_title:
            seg_title = f"Section {len(chapters) + 1}"

        # Ensure unique titles for TOC
        base_title = seg_title
        dup = 1
        while any(t == seg_title for t, _ in chapters):
            dup += 1
            seg_title = f"{base_title} ({dup})"

        chapters.append((seg_title, cleaned))

    return chapters


def _clean_mobi_html(html: str) -> str:
    """Remove mobi-specific markup from an HTML fragment.

    - Strips ``<mbp:pagebreak/>`` tags
    - Removes mobi-specific ``height``/``width`` attributes on block elements
    - Removes ``epub:prefix`` (mobi-generated, conflicts with ebooklib)
    """
    html = _MOBI_PAGEBREAK.sub("", html)
    html = _MOBI_ATTRS.sub("", html)
    html = html.replace(
        'epub:prefix="z3998: http://www.daisy.org/z3998/2012/vocab/structure/#"',
        "",
    )
    return html


def _detect_language(text: str) -> str:
    """Return ``"zh"`` if the text is primarily Chinese, otherwise ``"en"``."""
    if not text:
        return "en"
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    total = len(text)
    # If > 5% CJK characters, treat as Chinese
    return "zh" if total > 0 and (cjk / total) > 0.05 else "en"


def _extract_chapter_title(html_segment: str) -> str | None:
    """Try to extract a chapter/section title from an HTML segment.

    Looks for common Chinese ebook chapter heading patterns like
    ``<b>第一章</b>`` or ``<font><b>书名</b></font>``.
    """
    for pattern in _CHAPTER_TITLE_PATTERNS:
        match = pattern.search(html_segment)
        if match:
            title = match.group(1).strip()
            # Filter out obviously-not-title matches
            if len(title) >= 2 and not title.startswith("<"):
                return title
    return None


def _safe_filename(text: str) -> str:
    """Convert a chapter title to a safe filename stem."""
    # Keep Chinese chars, ASCII alphanumerics, hyphens, underscores
    safe = []
    for ch in text:
        if ch.isalnum() or ch in "-_." or "一" <= ch <= "鿿":
            safe.append(ch)
        else:
            safe.append("_")
    name = "".join(safe).strip("_")
    return name[:80] if name else "chapter"


def _build_toc(chapters: list[epub.EpubHtml]) -> list:
    """Build a table-of-contents list for ebooklib from chapter objects.

    Uses ``epub.Link`` objects which ebooklib natively understands for
    generating the NCX and NAV navigation documents.
    """
    toc: list = []
    for ch in chapters:
        toc.append(epub.Link(ch.file_name, ch.title, ch.id))
    return toc


def _add_resources(
    book: epub.EpubBook, content_dir: Path, opf_text: str
) -> None:
    """Add images and other non-HTML resources from the extracted directory."""
    manifest_items = re.findall(
        r'<item[^>]+href="([^"]+)"[^>]+media-type="([^"]+)"',
        opf_text,
    )

    seen_names: set[str] = set()

    for href, media_type in manifest_items:
        if "html" in media_type:
            continue

        item_path = content_dir / href
        if not item_path.exists():
            continue

        fname = Path(href).name

        if "image" in media_type or href.lower().endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".svg")
        ):
            # Deduplicate image file names
            img_fname = f"images/{fname}"
            counter = 1
            while img_fname in seen_names:
                stem, ext = os.path.splitext(fname)
                img_fname = f"images/{stem}_{counter}{ext}"
                counter += 1
            seen_names.add(img_fname)

            book.add_item(
                epub.EpubItem(
                    uid=f"img_{fname}",
                    file_name=img_fname,
                    media_type=media_type,
                    content=item_path.read_bytes(),
                )
            )

        elif "ncx" in media_type or href.endswith(".ncx"):
            book.add_item(
                epub.EpubItem(
                    uid="ncx",
                    file_name="toc.ncx",
                    media_type="application/x-dtbncx+xml",
                    content=item_path.read_bytes(),
                )
            )


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
