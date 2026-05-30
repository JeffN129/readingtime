"""
Scheduled tasks for the ReadingTime agent.

Runs in a background daemon thread alongside the filesystem watcher.
Uses the ``schedule`` library for cron-like scheduling.

Tasks:
    - Daily at 02:00  — Check for expired books (on shelf > 30 days)
    - Daily at 02:10  — Regenerate READING_TIME.md
    - Every 30 minutes — Verify shelf count == 10, trigger refill if needed
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule

from readingtime.config import config

logger = logging.getLogger(__name__)

# Global stop flag — set by the CLI on shutdown
_running = False


# ---------------------------------------------------------------------------
# Task implementations
# ---------------------------------------------------------------------------

def check_expirations() -> None:
    """Check for books that have exceeded ``book_lifetime_days``."""
    logger.debug("Scheduled: check_expirations")
    try:
        from readingtime.shelf.manager import shelf_manager
        expired = shelf_manager.check_expirations()
        if expired > 0:
            logger.info("Scheduled expiry: %d book(s) removed", expired)
    except Exception as exc:
        logger.error("check_expirations failed: %s", exc)


def verify_shelf_count() -> None:
    """Ensure the shelf is at capacity."""
    logger.debug("Scheduled: verify_shelf_count")
    try:
        from readingtime.shelf.manager import shelf_manager
        current = shelf_manager.current_count()
        if current < config.shelf_size:
            logger.info(
                "Shelf count %d < %d — triggering refill",
                current,
                config.shelf_size,
            )
            shelf_manager.refill()
    except Exception as exc:
        logger.error("verify_shelf_count failed: %s", exc)


def regenerate_reading_time_md() -> None:
    """(Re)Generate the READING_TIME.md file in the shelf root."""
    logger.debug("Scheduled: regenerate_reading_time_md")
    try:
        _write_reading_time_md()
    except Exception as exc:
        logger.error("regenerate_reading_time_md failed: %s", exc)


# ---------------------------------------------------------------------------
# READING_TIME.md generation
# ---------------------------------------------------------------------------

def _write_reading_time_md() -> None:
    """Write the daily READING_TIME.md overview to the shelf root."""
    from readingtime.database import db
    from readingtime.shelf.epub_utils import estimate_reading_time

    books = db.get_current_books()
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    lines: list[str] = [
        f"# 📚 ReadingTime 书架 · {today_str} 更新",
        "",
        "| # | 书名 | 作者 | 语言 | 预计阅读时长 | 入架天数 | 剩余天数 |",
        "|---|------|------|------|------------|---------|---------|",
    ]

    for i, book in enumerate(books, 1):
        title = book.get("title", "Unknown")
        author = book.get("author", "Unknown")
        lang = book.get("language", "en").upper()
        page_count = book.get("page_count")
        est_min = estimate_reading_time(page_count)
        est_str = f"约 {est_min / 60:.1f} 小时" if est_min else "未知"

        # Calculate days on shelf and days remaining
        added_str = book.get("added_at", "")
        days_on_shelf = "?"
        days_left = "?"
        if added_str:
            try:
                added_at = datetime.fromisoformat(added_str)
                days_on_shelf = str((now - added_at).days)
                days_left = str(max(0, config.book_lifetime_days - (now - added_at).days))
            except (ValueError, TypeError):
                pass

        lines.append(
            f"| {i} | {title} | {author} | {lang} | {est_str} | "
            f"{days_on_shelf} 天 | {days_left} 天 |"
        )

    lines.extend([
        "",
        "---",
        "*手动移走一本书 = 告诉系统你喜欢它，会推荐更多类似书籍*  ",
        "*30 天内未移走的书会被自动替换*",
    ])

    md_path = config.shelf_path / "READING_TIME.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("READING_TIME.md updated at %s", md_path)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def setup_schedule() -> None:
    """Register all recurring jobs with the ``schedule`` library."""
    schedule.clear()

    # Daily at 02:00 — expiry check
    schedule.every().day.at("02:00").do(check_expirations)

    # Daily at 02:10 — regenerate READING_TIME.md
    schedule.every().day.at("02:10").do(regenerate_reading_time_md)

    # Every 30 minutes — shelf count integrity check
    schedule.every(30).minutes.do(verify_shelf_count)

    logger.info("Scheduler jobs registered")


def run_scheduler() -> None:
    """Run the scheduler loop (blocking — call in a daemon thread).

    Checks pending jobs every second.  Exits when :func:`stop_scheduler` is
    called from another thread.
    """
    global _running
    _running = True
    setup_schedule()

    logger.info("Scheduler loop started")
    while _running:
        schedule.run_pending()
        time.sleep(1)

    logger.info("Scheduler loop stopped")


def stop_scheduler() -> None:
    """Signal the scheduler loop to exit gracefully."""
    global _running
    _running = False
