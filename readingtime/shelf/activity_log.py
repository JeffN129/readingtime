"""
Activity log — monthly Markdown journal for the shelf.

Each month gets its own file (``activity-2026-06.md``) in the shelf
directory.  Entries are inserted at the top so the newest events appear
first (reverse chronological order).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def log_activity(
    shelf_path: Path,
    action: str,
    title: str,
    author: str = "",
    note: str = "",
) -> None:
    """Append an entry to this month's activity log file.

    Args:
        shelf_path: Path to the shelf directory.
        action: Emoji + label, e.g. ``"➕ 补充"``, ``"❤️ 删除"``.
        title: Book title.
        author: Book author (displayed as ``-`` when empty).
        note: Optional extra note (displayed as ``-`` when empty).
    """
    now = datetime.now(timezone.utc)
    bj_time = now + timedelta(hours=8)  # Beijing time
    month_file = shelf_path / f"activity-{bj_time.strftime('%Y-%m')}.md"
    time_str = bj_time.strftime('%m-%d %H:%M')

    if not month_file.exists():
        header = (
            f"# 书架活动日志 — {bj_time.strftime('%Y年%m月')}\n\n"
            "| 时间 | 操作 | 书名 | 作者 | 备注 |\n"
            "|------|------|------|------|------|\n"
        )
        month_file.parent.mkdir(parents=True, exist_ok=True)
        month_file.write_text(header, encoding="utf-8")

    author_display = author or "-"
    note_display = note or "-"
    row = f"| {time_str} | {action} | {title} | {author_display} | {note_display} |\n"

    # Insert at the top (reverse chronological)
    content = month_file.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    # Table header is lines 0-3; insert new row at line 4
    insert_pos = 4
    if len(lines) >= 4:
        lines.insert(insert_pos, row)
    else:
        lines.append(row)
    month_file.write_text("".join(lines), encoding="utf-8")
