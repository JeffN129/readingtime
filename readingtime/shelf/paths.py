"""
Shelf path utilities — file-system helpers for the bookshelf.

These are plain functions (not methods) so they can be reused without
instantiating ``ShelfManager``.  The manager class keeps thin wrappers
for backward compatibility.
"""

from __future__ import annotations

from pathlib import Path


def safe_dirname(book_result) -> str:
    """Generate a safe directory name for a BookResult.

    Each book lives in its own folder: ``{dirname}/{dirname}.epub``.
    """
    title = book_result.title or "unknown"
    author = book_result.author or "unknown"

    # Take last part of author name as short identifier
    surname = author.split()[-1] if author else "unknown"

    # Sanitize: keep letters (incl. CJK), digits, spaces, dashes, underscores
    safe_title = "".join(
        c if c.isalpha() or c.isdigit() or c in " _-" else "" for c in title
    )
    safe_title = safe_title.strip()[:60]
    safe_title = safe_title.replace(" ", "_")

    safe_author = "".join(
        c if c.isalpha() or c.isdigit() else "" for c in surname
    )[:15]

    return f"{safe_title}_{safe_author}" if safe_author else safe_title


def candidate_key(book_result) -> str:
    """Generate a stable dedup key for a BookResult.

    Two books with the same title and author are considered duplicates.
    """
    title = book_result.title.lower().strip() if book_result.title else ""
    author = book_result.author.lower().strip() if book_result.author else ""
    return f"{title}||{author}"


def book_epub_path(shelf_path: Path, dirname: str) -> Path:
    """Full path to the EPUB file inside its book folder."""
    return shelf_path / dirname / f"{dirname}.epub"


def book_note_path(shelf_path: Path, dirname: str) -> Path:
    """Full path to the reading note inside its book folder."""
    return shelf_path / dirname / f"{dirname}.readingnote.md"


def list_epub_files(shelf_path: Path) -> list[str]:
    """Return sorted list of ``.epub`` filenames in shelf subdirectories."""
    if not shelf_path.exists():
        return []
    epub_files: list[str] = []
    for item in sorted(shelf_path.iterdir()):
        if item.is_dir():
            epubs = list(item.glob("*.epub"))
            if epubs:
                epub_files.append(epubs[0].name)
    return sorted(epub_files)
