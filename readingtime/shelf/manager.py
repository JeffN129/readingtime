"""
Shelf manager — core business logic for the ReadingTime agent.

Responsibilities:
    - Maintain exactly ``shelf.size`` books on the shelf at all times
    - Distinguish user-initiated removals (liked) from auto-expiry (neutral)
    - Trigger refill when the shelf count drops below the configured size
    - Track when books were added and expire them after ``book_lifetime_days``
    - Coordinate across database, book sources, and (eventually) LLM agent modules

During bootstrapping (Steps 1-9), LLM-dependent features are stubbed.
Once agent/ modules are built (Steps 10-11), the full recommendation pipeline
kicks in automatically.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from readingtime.config import config
from readingtime.database import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Imports from extracted shelf sub-modules
# ---------------------------------------------------------------------------
from readingtime.agent.capabilities import agent_capabilities
from readingtime.notifier import notify
from readingtime.shelf.activity_log import log_activity
from readingtime.shelf.paths import (
    safe_dirname, candidate_key, book_epub_path, book_note_path, list_epub_files,
)
from readingtime.shelf.sourcing import _SOURCES, _SEED_QUERIES, simplify_query


class ShelfManager:
    """Core orchestrator for the bookshelf lifecycle.

    Create one instance and call its methods.  All state lives in the database
    and filesystem — ShelfManager itself is stateless beyond cached config.
    """

    # ------------------------------------------------------------------
    # Shelf queries
    # ------------------------------------------------------------------

    @property
    def shelf_path(self) -> Path:
        """Path to the bookshelf directory (read from live config)."""
        return config.shelf_path

    def get_current_books(self) -> list[dict]:
        """Return all books currently on the shelf (removed_at IS NULL)."""
        return db.get_current_books()

    def current_count(self) -> int:
        """How many EPUB files are currently on the shelf."""
        return len(self._list_epub_files())

    # ------------------------------------------------------------------
    # User-initiated removal (liked signal)
    # ------------------------------------------------------------------

    def handle_user_removal(self, filename: str) -> None:
        """Called by the watcher when the user manually deletes/moves an EPUB.

        1. Mark the book as removed with 'manual' reason
        2. Record a pending removal (5-min grace period for undo)
        3. After grace period expires → record 'liked' signal + refill
        """
        # Process any expired pending removals first
        self._process_pending_removals()

        book = db.get_book_by_filename(filename)
        if book is None:
            logger.warning(
                "User removed unknown file: %s — skipping signal", filename
            )
            self._refill_if_needed()
            return

        db.mark_removed(filename, "manual")
        logger.info("User removed: %s — pending undo (5-min grace)", book.get("title"))
        log_activity(self.shelf_path, "⏳ 待确认", book.get("title", "?"),
                      book.get("author", ""), "5分钟内可恢复 (readingtime undo)")

        # Record as pending — signal is NOT recorded yet
        db.record_pending_removal(
            filename=filename,
            book_id=book["id"],
            title=book.get("title", "?"),
            author=book.get("author", ""),
            dirname=filename,
            source_id=book.get("source_id") or "",
        )

        # Ask the user if they liked this book (interactive notification with buttons)
        from readingtime.notifier import ask_liked_book
        ask_liked_book(
            book_id=book["id"],
            filename=filename,
            title=book.get("title", "?"),
            author=book.get("author", ""),
        )

        # Don't refill yet — wait for the grace period to expire

    def undo_removal(self, filename: str) -> bool:
        """Undo a book removal within the 5-minute grace period.

        Restores the book entry (clears ``removed_at``), removes the
        pending record, and logs the restoration.  No signal is recorded
        — the undo is treated as if the deletion never happened.

        Returns ``True`` if restored, ``False`` if no pending entry or
        the grace period has already expired.
        """
        pending = db.get_pending_removal(filename)
        if pending is None:
            logger.warning("No pending removal found for %s", filename)
            return False

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(pending["expires_at"])
        if now > expires_at:
            logger.warning("Undo window expired for %s", filename)
            db.delete_pending_removal(filename)
            return False

        # Restore the book in the database
        db.restore_book(filename)
        db.delete_pending_removal(filename)
        log_activity(self.shelf_path, "↩️ 恢复", pending["title"],
                      pending.get("author", ""), "用户撤销删除")
        notify("↩️ 已恢复", f"《{pending['title']}》已重新加入书架")
        logger.info("Undo: restored %s", pending["title"])

        # Try to re-download (user already deleted the files)
        source_id = pending.get("source_id", "")
        if source_id:
            try:
                from readingtime.sources.base import BookResult
                # Construct a minimal BookResult for re-download
                book_result = BookResult(
                    source_id=source_id,
                    title=pending["title"],
                    author=pending.get("author", ""),
                    formats=["epub"],
                    epub_download_url="",
                )
                dirname = pending.get("dirname", filename)
                save_path = self._book_epub_path(self.shelf_path, dirname)
                save_path.parent.mkdir(parents=True, exist_ok=True)
                success = self._download_book(book_result, save_path)
                if success:
                    logger.info("Undo: re-downloaded %s", pending["title"])
                else:
                    logger.warning("Undo: could not re-download %s — DB entry restored anyway",
                                   pending["title"])
            except Exception as exc:
                logger.warning("Undo: re-download error for %s: %s", pending["title"], exc)
        return True

    def _process_pending_removals(self) -> int:
        """Finalise any expired pending removals.

        For each entry whose grace period has passed:
        1. Record a 'liked' signal
        2. Update the user profile
        3. Log the confirmation

        Call this at the top of every lifecycle entry point so that
        stale pending entries are always cleaned up promptly.

        Returns the number of entries processed.
        """
        expired = db.clear_expired_pending()
        if not expired:
            return 0

        for entry in expired:
            book = db.get_book_by_id(entry["book_id"])
            if book is None:
                logger.debug("Pending removal for deleted book %d, skipping",
                             entry["book_id"])
                continue

            features = self._extract_book_features(book)
            db.record_signal(book["id"], "liked", features)
            self._update_profile_from_signal("liked", features)
            log_activity(self.shelf_path, "❤️ 已确认", entry["title"],
                          entry.get("author", ""), "用户喜欢（已确认）")

        logger.info("Processed %d expired pending removal(s)", len(expired))

        # Now that signals are recorded, refill if needed
        if self.current_count() < config.shelf_size:
            self.refill()

        return len(expired)

    # ------------------------------------------------------------------
    # System-initiated removal (auto-expiry / neutral signal)
    # ------------------------------------------------------------------

    def handle_auto_expiry(self, filename: str) -> None:
        """Called when a book exceeds ``book_lifetime_days`` without being
        manually removed.

        1. Mark as 'auto_expired'
        2. Record a 'neutral' signal
        3. Delete the file (with system_state flag so watcher ignores it)
        4. Trigger refill
        """
        self._process_pending_removals()
        book = db.get_book_by_filename(filename)
        if book is None:
            logger.warning("Auto-expiry on unknown file: %s", filename)
            self._refill_if_needed()
            return

        db.mark_removed(filename, "auto_expired")
        logger.info("Auto-expired: %s — recording as NEUTRAL", book.get("title"))
        # Compute days on shelf for the log
        days_on = "?"
        added_str = book.get("added_at", "")
        if added_str:
            try:
                added_at = datetime.fromisoformat(added_str)
                days_on = str((datetime.now(timezone.utc) - added_at).days)
            except (ValueError, TypeError):
                pass
        log_activity(self.shelf_path, "⏰ 过期", book.get("title", "?"),
                      book.get("author", ""), f"在架{days_on}天")

        features = self._extract_book_features(book)
        db.record_signal(book["id"], "neutral", features)
        self._update_profile_from_signal("neutral", features)

        # Notify
        notify("⏰ 自动过期", f"《{book.get('title', '?')}》在架{days_on}天，已自动移除")

        # Delete the file (set flag so watcher ignores it)
        self._system_delete_file(filename)

        # Refill
        self._refill_if_needed()

    # ------------------------------------------------------------------
    # System delete (with flag to prevent watcher false-positive)
    # ------------------------------------------------------------------

    def _system_delete_file(self, dirname: str) -> bool:
        """Delete a book folder from the shelf, setting the system_state flag
        so the watcher knows this is NOT a user action."""
        import shutil
        db.set_state("agent_is_deleting", dirname)
        folder_path = self.shelf_path / dirname
        try:
            if folder_path.exists():
                shutil.rmtree(folder_path)
                logger.debug("System deleted folder: %s", dirname)
                return True
            return False
        except OSError as exc:
            logger.error("Failed to delete %s: %s", dirname, exc)
            db.clear_state("agent_is_deleting")
            return False

    # ------------------------------------------------------------------
    # Refill — fill shelf back up to ``shelf.size``
    # ------------------------------------------------------------------

    def refill(self, n: int = 1) -> list[str]:
        """Add *n* books to the shelf (or enough to reach ``shelf.size``).

        Returns a list of newly-added filenames.

        Refill pipeline:
            1. Get user profile from DB (or default if no profile yet)
            2. Generate search queries (LLM if available, else heuristic)
            3. Search sources in priority order, collect candidates
            4. Score candidates (LLM if available, else heuristic)
            5. Download the top-scored book → generate note → repeat
        """
        self._process_pending_removals()
        added: list[str] = []
        target = config.shelf_size
        current_count = self.current_count()
        needed = max(n, target - current_count)

        if needed <= 0:
            logger.debug("Shelf is full (%d books), no refill needed", current_count)
            return added

        logger.info("Refill: need %d book(s) to reach %d", needed, target)

        # Build candidate pool once, then pick top-N (avoid duplicate work)
        profile = self._get_or_default_profile()
        queries = self._generate_queries(profile)
        candidates = self._search_all_sources(queries)

        # Exclude books already on shelf or in history
        on_shelf = {b["filename"] for b in db.get_current_books()}
        history = {b["filename"] for b in db.get_book_history(limit=200)}
        seen = on_shelf | history

        fresh = [c for c in candidates if self._candidate_key(c) not in seen]
        using_fallback = False
        if not fresh:
            logger.warning("No fresh candidates found after dedup — using all candidates")
            fresh = candidates
            using_fallback = True

        # Score and sort
        scored = self._score_candidates(fresh, profile)
        scored.sort(key=lambda x: x[1], reverse=True)

        for book_result, score in scored:
            if len(added) >= needed:
                break

            dirname = self._safe_dirname(book_result)
            if not using_fallback and dirname in seen:
                continue

            save_path = self._book_epub_path(self.shelf_path, dirname)
            logger.info(
                "Downloading: %s by %s (score=%.1f)",
                book_result.title,
                book_result.author,
                score,
            )

            success = self._download_book(book_result, save_path)
            if not success:
                continue

            # Extract metadata from the downloaded EPUB
            metadata = self._extract_epub_metadata(save_path)

            # Register in database (store dirname as the filename)
            book_id = db.add_book(
                title=metadata.get("title") or book_result.title,
                filename=dirname,
                author=metadata.get("author") or book_result.author,
                source=book_result.source_id.split(":")[0] if ":" in book_result.source_id else "unknown",
                source_id=book_result.source_id,
                language=metadata.get("language") or book_result.language,
                tags=metadata.get("tags") or book_result.tags,
                page_count=metadata.get("page_count") or book_result.page_count,
            )

            # Generate reading note
            self._generate_reading_note(book_result, save_path, metadata, book_id, dirname)

            seen.add(dirname)
            added.append(dirname)
            logger.info("Added to shelf: %s", book_result.title)
            source_name = book_result.source_id.split(":")[0] if ":" in book_result.source_id else "?"
            log_activity(self.shelf_path, "➕ 补充", book_result.title,
                          book_result.author, source_name)
            notify("📚 新书上架", f"《{book_result.title}》已加入书架 — 来自 {source_name}")

        if len(added) < needed:
            logger.warning(
                "Refill only added %d/%d books — sources may be exhausted",
                len(added),
                needed,
            )

        return added

    def _refill_if_needed(self) -> None:
        """Check if shelf is below target and refill if so."""
        if self.current_count() < config.shelf_size:
            self.refill()
        else:
            logger.debug("Shelf count OK (%d/%d)", self.current_count(), config.shelf_size)

    # ------------------------------------------------------------------
    # Initialization — first-run shelf seeding
    # ------------------------------------------------------------------

    def initialize_shelf(self) -> int:
        """Fill the shelf with 10 books for the first time.

        Uses popular / classic search terms instead of a user profile
        (which doesn't exist yet).  Returns the number of books added.
        """
        self._process_pending_removals()
        logger.info("Initializing shelf at %s", self.shelf_path)
        self.shelf_path.mkdir(parents=True, exist_ok=True)

        current = self.current_count()
        needed = config.shelf_size - current
        if needed <= 0:
            logger.info("Shelf already has %d books — skipping init", current)
            return 0

        # Shuffle seed queries to get variety
        queries = list(_SEED_QUERIES)
        random.shuffle(queries)

        added = 0
        for query in queries:
            if added >= needed:
                break
            results = self._search_all_sources([query])
            if not results:
                continue

            # Pick a random result from top candidates (no profile to score against)
            book = random.choice(results[:5])
            dirname = self._safe_dirname(book)
            save_path = self._book_epub_path(self.shelf_path, dirname)

            if save_path.exists():
                continue

            success = self._download_book(book, save_path)
            if not success:
                continue

            metadata = self._extract_epub_metadata(save_path)
            db.add_book(
                title=metadata.get("title") or book.title,
                filename=dirname,
                author=metadata.get("author") or book.author,
                source=book.source_id.split(":")[0] if ":" in book.source_id else "unknown",
                source_id=book.source_id,
                language=metadata.get("language") or book.language,
                tags=metadata.get("tags") or book.tags,
                page_count=metadata.get("page_count") or book.page_count,
            )
            added += 1
            logger.info("Seeded: %s by %s", book.title, book.author)
            source_name = book.source_id.split(":")[0] if ":" in book.source_id else "?"
            log_activity(self.shelf_path, "➕ 补充", book.title,
                          book.author, source_name)

        logger.info("Shelf initialized with %d books", added)
        if added > 0:
            notify("🎉 书架就绪", f"已为你准备了 {added} 本书，开始阅读吧！")
        return added

    # ------------------------------------------------------------------
    # Add single book (public API — used by CLI ``add`` command)
    # ------------------------------------------------------------------

    def add_single_book(self, query: str) -> str | None:
        """Search across all sources and download the first matching book.

        This is a public convenience method for the CLI.  It searches sources
        in priority order, downloads the first EPUB found, registers it in the
        database, and generates the reading note.

        Args:
            query: A search term (title, author, or keyword).

        Returns:
            The filename of the added book, or None if nothing was found.
        """
        results = self._search_all_sources([query])
        if not results:
            logger.info("add_single_book: no results for '%s'", query)
            return None

        # Try each result until one downloads successfully
        for book_result in results[:10]:
            dirname = self._safe_dirname(book_result)
            save_path = self._book_epub_path(self.shelf_path, dirname)

            if save_path.exists():
                continue

            success = self._download_book(book_result, save_path)
            if not success:
                continue

            metadata = self._extract_epub_metadata(save_path)
            source_name = book_result.source_id.split(":")[0] if ":" in book_result.source_id else "unknown"

            book_id = db.add_book(
                title=metadata.get("title") or book_result.title,
                filename=dirname,
                author=metadata.get("author") or book_result.author,
                source=source_name,
                source_id=book_result.source_id,
                language=metadata.get("language") or book_result.language,
                tags=metadata.get("tags") or book_result.tags,
                page_count=metadata.get("page_count") or book_result.page_count,
            )
            self._generate_reading_note(book_result, save_path, metadata, book_id, dirname)
            logger.info("add_single_book: added %s", book_result.title)
            log_activity(self.shelf_path, "➕ 补充", book_result.title,
                          book_result.author, source_name)
            notify("📚 新书上架", f"《{book_result.title}》已加入书架 — 来自 {source_name}")
            return dirname

        return None

    # ------------------------------------------------------------------
    # Expiry check
    # ------------------------------------------------------------------

    def check_expirations(self) -> int:
        """Check all books on shelf for expiry.

        A book is expired if:
            - It has been on the shelf longer than ``book_lifetime_days``
            - AND it is NOT protected (``is_protected = 1``)

        Returns the number of books expired.
        """
        self._process_pending_removals()
        books = db.get_current_books()
        lifetime = config.book_lifetime_days
        now = datetime.now(timezone.utc)
        expired = 0

        for book in books:
            if book.get("is_protected"):
                logger.debug("Skipping protected book: %s", book.get("title"))
                continue

            added_str = book.get("added_at", "")
            if not added_str:
                continue

            try:
                added_at = datetime.fromisoformat(added_str)
            except (ValueError, TypeError):
                logger.warning("Invalid added_at for %s: %s", book.get("filename"), added_str)
                continue

            age_days = (now - added_at).days
            if age_days >= lifetime:
                logger.info(
                    "Expiring '%s' — %d days on shelf (limit: %d)",
                    book.get("title"),
                    age_days,
                    lifetime,
                )
                self.handle_auto_expiry(book["filename"])
                expired += 1

        if expired == 0:
            logger.debug("No books expired (checked %d on shelf)", len(books))

        # Warn about books expiring soon
        self._warn_expiring_soon(books, lifetime, now)

        return expired

    def _warn_expiring_soon(self, books: list[dict], lifetime: int, now: datetime) -> None:
        """Send notifications for books that will expire within 3 days."""
        warn_threshold = 3
        expiring = []
        for book in books:
            if book.get("is_protected"):
                continue
            added_str = book.get("added_at", "")
            if not added_str:
                continue
            try:
                added_at = datetime.fromisoformat(added_str)
            except (ValueError, TypeError):
                continue
            remaining = lifetime - (now - added_at).days
            if 0 < remaining <= warn_threshold:
                expiring.append((book, remaining))

        if not expiring:
            return

        if len(expiring) == 1:
            b, days = expiring[0]
            notify("⚠️ 即将过期", f"《{b.get('title', '?')}》还剩 {days} 天将从书架移除")
        else:
            names = "、".join(b.get("title", "?") for b, _ in expiring[:3])
            if len(expiring) > 3:
                names += f" 等{len(expiring)}本"
            notify("⚠️ 即将过期", f"{names}\n将在 {warn_threshold} 天内从书架移除")

    # ------------------------------------------------------------------
    # Private: book download & metadata helpers
    # ------------------------------------------------------------------

    def _download_book(self, book_result, save_path: Path) -> bool:
        """Download a book from its source and convert to EPUB if needed.

        After download the actual file may have a non-EPUB extension
        (e.g. .azw3, .mobi, .pdf).  We detect the real format, convert
        to EPUB via Calibre if available, and ensure the final file is
        always at ``save_path`` (the .epub path callers expect).

        Sets ``agent_is_deleting`` flag during download to prevent the
        filesystem watcher from misinterpreting format-rename operations
        as user deletions.
        """
        dirname = save_path.parent.name

        # Block watcher from seeing our file operations as "user removals"
        db.set_state("agent_is_deleting", dirname)
        try:
            source_name = book_result.source_id.split(":")[0] if ":" in book_result.source_id else ""
            source = _SOURCES.get(source_name)
            if source is None:
                # Fallback: try all sources
                for src in _SOURCES.values():
                    if hasattr(src, "download"):
                        try:
                            if src.download(book_result, str(save_path)):
                                break
                        except Exception as exc:
                            logger.debug("Fallback download via %s failed: %s", getattr(src, "name", "?"), exc)
                else:
                    return False
            else:
                try:
                    if not source.download(book_result, str(save_path)):
                        return False
                except Exception as exc:
                    logger.error("Download error for %s: %s", book_result.title, exc)
                    return False

            # -- Ensure the file is EPUB -------------------------------------------
            # The source may have renamed the file to its real format (.azw3 etc.)
            from readingtime.shelf.converter import convert_to_epub, find_book_file

            stem = save_path.stem
            actual = find_book_file(save_path.parent, stem)
            if actual is None:
                logger.error("Downloaded file not found for %s", save_path.name)
                return False

            if actual.suffix.lower() == ".epub":
                # Already EPUB — rename back to save_path if the source renamed it
                if actual != save_path:
                    actual.rename(save_path)
                return True

            # Non-EPUB → convert
            epub_result = convert_to_epub(actual)
            if epub_result is None:
                # Conversion failed or Calibre not installed — keep the original
                # but rename it to .epub so callers can find it
                if actual != save_path:
                    actual.rename(save_path)
                logger.warning("Kept %s in original format (not EPUB)", book_result.title)
                return True  # Book is still usable

            # Conversion succeeded — ensure it's at save_path
            if epub_result != save_path:
                epub_result.rename(save_path)
            return True
        finally:
            db.clear_state("agent_is_deleting")

    def _extract_epub_metadata(self, path: Path) -> dict:
        """Extract metadata from a downloaded EPUB file."""
        try:
            from readingtime.shelf.epub_utils import extract_metadata
            return extract_metadata(str(path))
        except Exception as exc:
            logger.warning("Metadata extraction failed for %s: %s", path.name, exc)
            return {}

    def _generate_reading_note(self, book_result, epub_path: Path, metadata: dict, book_id: int, dirname: str = "") -> None:
        """Generate a .readingnote.md alongside the EPUB."""
        try:
            from readingtime.shelf.epub_utils import estimate_reading_time

            title = metadata.get("title") or book_result.title
            author = metadata.get("author") or book_result.author
            lang = metadata.get("language") or book_result.language
            page_count = metadata.get("page_count") or book_result.page_count
            est_minutes = estimate_reading_time(page_count)
            est_hours = f"{est_minutes / 60:.1f}" if est_minutes else "未知"

            source = book_result.source_id.split(":")[0] if ":" in book_result.source_id else "unknown"

            # Generate summary (LLM if available, else template)
            summary_text = self._generate_summary(book_result, epub_path)

            note_content = f"""# {title}

**作者**：{author}
**语言**：{lang}
**预计阅读时间**：约 {est_hours} 小时
**加入书架**：{datetime.now(timezone.utc).strftime('%Y-%m-%d')}
**书源**：{source}

---

## 摘要

{summary_text}

---

## 为什么你会喜欢这本书

> {self._generate_recommendation_reason(book_result)}
"""
            note_path = self._book_note_path(self.shelf_path, dirname) if dirname else Path(str(epub_path) + ".readingnote.md")
            note_path.write_text(note_content, encoding="utf-8")
            logger.debug("Reading note written: %s", note_path.name)

        except Exception as exc:
            logger.warning("Failed to generate reading note: %s", exc)

    def _generate_summary(self, book_result, epub_path: Path) -> str:
        """Generate a book summary — LLM if available, else description fallback."""
        # Try LLM summarizer first
        if agent_capabilities.has_summarizer:
            try:
                summarizer = agent_capabilities.summarizer_cls()
                return summarizer.generate(book_result, str(epub_path))
            except Exception as exc:
                logger.warning("LLM summarizer failed, using fallback: %s", exc)

        # Fallback: use the description from the source, or a template
        if book_result.description:
            return book_result.description[:500]
        return f"{book_result.title} — 作者 {book_result.author}。暂无详细摘要。"

    def _generate_recommendation_reason(self, book_result) -> str:
        """Generate a one-line 'why you'll like this' — LLM or template."""
        if agent_capabilities.has_summarizer:
            try:
                summarizer = agent_capabilities.summarizer_cls()
                return summarizer.generate_reason(book_result)
            except Exception:
                pass

        # Template fallback
        tags_str = "、".join(book_result.tags[:3]) if book_result.tags else "经典"
        return f"如果你喜欢{tags_str}类作品，这本书值得一读。"

    # ------------------------------------------------------------------
    # Private: profile helpers
    # ------------------------------------------------------------------

    def _get_or_default_profile(self) -> dict:
        """Return the current user profile or a sensible default."""
        profile = db.get_profile()
        if profile is None:
            return {
                "liked_tags": [],
                "liked_authors": [],
                "neutral_tags": [],
                "lang_pref": config.language,
            }
        return profile

    def _extract_book_features(self, book: dict) -> dict:
        """Extract features from a book dict for profiling."""
        tags = book.get("tags", [])
        if isinstance(tags, str):
            try:
                import json
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []

        return {
            "tags": tags if isinstance(tags, list) else [],
            "author": book.get("author", ""),
            "language": book.get("language", "en"),
        }

    def _update_profile_from_signal(self, signal: str, features: dict) -> None:
        """Update the profile table based on a new signal."""
        if agent_capabilities.has_profiler:
            try:
                profiler = agent_capabilities.profiler_cls()
                profiler.update_profile(signal, features)
                return
            except Exception as exc:
                logger.warning("Agent profiler failed, using heuristic: %s", exc)

        # Heuristic profile update (no LLM)
        profile = self._get_or_default_profile()
        tags = features.get("tags", [])
        author = features.get("author", "")

        liked_tags = list(profile.get("liked_tags", []))
        neutral_tags = list(profile.get("neutral_tags", []))
        liked_authors = list(profile.get("liked_authors", []))

        if signal == "liked":
            for tag in tags:
                if tag not in liked_tags:
                    liked_tags.append(tag)
                if tag in neutral_tags:
                    neutral_tags.remove(tag)
            if author and author not in liked_authors:
                liked_authors.append(author)
        elif signal == "neutral":
            for tag in tags:
                if tag not in liked_tags and tag not in neutral_tags:
                    neutral_tags.append(tag)

        db.upsert_profile(
            liked_tags=liked_tags,
            liked_authors=liked_authors,
            neutral_tags=neutral_tags,
            lang_pref=profile.get("lang_pref", "en"),
        )

    # ------------------------------------------------------------------
    # Private: query generation (heuristic fallback)
    # ------------------------------------------------------------------

    def _generate_queries(self, profile: dict) -> list[str]:
        """Generate search queries from the user profile.

        Uses LLM if available AND the profile has meaningful data;
        otherwise falls back to heuristic / seed queries.
        """
        liked_tags = profile.get("liked_tags", [])
        liked_authors = profile.get("liked_authors", [])

        # Only use LLM when we have real preference data
        has_profile = bool(liked_tags or liked_authors)

        if has_profile and agent_capabilities.has_recommender:
            try:
                recommender = agent_capabilities.recommender_cls()
                queries = recommender.generate_queries(profile)
                if queries:
                    return queries[:5]
            except Exception as exc:
                logger.warning("LLM query generation failed, using heuristic: %s", exc)

        # Heuristic: combine liked tags + liked authors into queries
        queries: list[str] = []

        # Author-based queries
        for author in liked_authors[:3]:
            queries.append(author)

        # Tag-based queries
        for tag in liked_tags[:3]:
            queries.append(f"{tag} books")

        # Combination queries
        if liked_tags and liked_authors:
            queries.append(f"{liked_tags[0]} by authors like {liked_authors[0]}")

        # Fall back to seed queries if no profile data
        if not queries:
            queries = random.sample(_SEED_QUERIES, min(5, len(_SEED_QUERIES)))

        return queries[:5]

    # ------------------------------------------------------------------
    # Private: source search orchestration
    # ------------------------------------------------------------------

    def _search_all_sources(self, queries: list[str]) -> list:
        """Run queries across all sources in priority order, deduplicating
        by (title, author) key.  Returns a flat list of BookResult objects."""
        from readingtime.sources.base import BookResult

        results: list[BookResult] = []
        seen_keys: set[str] = set()

        for source_name in config.source_priority:
            source = _SOURCES.get(source_name)
            if source is None:
                logger.debug("Unknown source '%s' — skipping", source_name)
                continue

            # Z-Library returns empty list when not configured
            for query in queries:
                # Try the original query, then fall back to a simplified version
                # (kgbook returns 0 results for "Title Author" queries)
                for attempt, q in enumerate([query] + simplify_query(query)):
                    try:
                        # Don't filter by language — Chinese books often have varied language tags
                        batch = source.search(q, language="", limit=5)
                    except NotImplementedError:
                        logger.debug("%s.search not implemented — skipping", source_name)
                        continue
                    except Exception as exc:
                        logger.error("Search error in %s for '%s': %s", source_name, q, exc)
                        continue
                    if batch:
                        if attempt > 0:
                            logger.debug("Query '%s' → simplified to '%s' → %d results", query, q, len(batch))
                        break

                for r in batch:
                    key = self._candidate_key(r)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append(r)

        logger.debug("Total unique candidates across all sources: %d", len(results))
        return results

    def _score_candidates(self, candidates: list, profile: dict) -> list[tuple]:
        """Score a list of BookResult objects against the user profile.

        Uses LLM if available; otherwise heuristic scoring based on tag overlap.
        Returns [(BookResult, score), ...].
        """
        if agent_capabilities.has_recommender and len(candidates) >= 3:
            try:
                recommender = agent_capabilities.recommender_cls()
                return recommender.score_candidates(candidates, profile)
            except Exception as exc:
                logger.warning("LLM scoring failed, using heuristic: %s", exc)

        # Heuristic scoring
        liked_tags = set(profile.get("liked_tags", []))
        neutral_tags = set(profile.get("neutral_tags", []))
        liked_authors = set(profile.get("liked_authors", []))

        scored: list[tuple] = []
        for c in candidates:
            score = 5.0  # neutral baseline

            c_tags = set(c.tags) if c.tags else set()
            c_author = c.author or ""

            # Bonus for matched liked tags
            tag_overlap = c_tags & liked_tags
            score += len(tag_overlap) * 1.5

            # Bonus for liked author
            if c_author in liked_authors:
                score += 3.0

            # Penalty for neutral tags
            tag_penalty = c_tags & neutral_tags
            score -= len(tag_penalty) * 1.0

            # Bonus for popularity (higher downloads = likely better quality)
            if c.download_count > 0:
                score += min(3.0, c.download_count / 1000)  # cap at +3

            # Small bonus for having a description (helps LLM later)
            if c.description:
                score += 0.5

            scored.append((c, max(0.0, score)))

        return scored

    # ------------------------------------------------------------------
    # Private: utilities (thin wrappers — real logic in shelf/paths.py)
    # ------------------------------------------------------------------

    def _list_epub_files(self) -> list[str]:
        """Return list of .epub filenames currently on disk in the shelf."""
        return list_epub_files(self.shelf_path)

    @staticmethod
    def _candidate_key(book_result) -> str:
        """Generate a stable dedup key for a BookResult."""
        return candidate_key(book_result)

    @staticmethod
    def _safe_dirname(book_result) -> str:
        """Generate a safe directory name for a BookResult."""
        return safe_dirname(book_result)

    @staticmethod
    def _book_epub_path(shelf_path, dirname: str) -> Path:
        """Full path to the EPUB file inside its book folder."""
        return book_epub_path(shelf_path, dirname)

    @staticmethod
    def _book_note_path(shelf_path, dirname: str) -> Path:
        """Full path to the reading note inside its book folder."""
        return book_note_path(shelf_path, dirname)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
shelf_manager = ShelfManager()
