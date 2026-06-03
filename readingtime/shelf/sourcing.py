"""
Source registry and seed queries for the ReadingTime agent.

Holds the singleton ``_SOURCES`` dict that maps source names to
instantiated ``BookSource`` objects, as well as the seed query
list used for first-time shelf initialization.
"""

from __future__ import annotations

from readingtime.sources.kgbook import KgbookSource

# ---------------------------------------------------------------------------
# Concrete source instances — keyed by the source name used in
# ``config.source_priority`` and ``BookResult.source_id`` prefixes.
# ---------------------------------------------------------------------------

_SOURCES: dict[str, object] = {
    "kgbook": KgbookSource(),
}

# ---------------------------------------------------------------------------
# Seed queries — used by ``initialize_shelf()`` when there is no user
# profile yet.  These are popular / classic Chinese book titles that are
# likely to be found on most ebook sites.
# ---------------------------------------------------------------------------

_SEED_QUERIES: list[str] = [
    "活着",
    "三体",
    "红楼梦",
    "百年孤独",
    "围城",
    "平凡的世界",
    "人类简史",
    "明朝那些事儿",
    "小王子",
    "局外人",
    "1984",
    "骆驼祥子",
    "呐喊",
    "边城",
    "白鹿原",
    "哈利波特",
    "牧羊少年奇幻之旅",
    "追风筝的人",
    "挪威的森林",
    "解忧杂货店",
]

# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def simplify_query(query: str) -> list[str]:
    """Generate progressively simpler fallback queries.

    Some search engines (kgbook) return 0 results for "Title Author"
    queries.  This returns shorter / alternative versions to retry with.

    Returns at most 2 alternatives — the first word, and half the query.
    """
    parts = query.split()
    if len(parts) <= 1:
        return []  # Already a single word, nothing to simplify

    alternatives: list[str] = []

    # Try just the first word (usually the most distinctive)
    first = parts[0]
    if first != query:
        alternatives.append(first)

    # If the original has 4+ words, try first half
    if len(parts) >= 4:
        half = " ".join(parts[: len(parts) // 2])
        if half != first and half != query:
            alternatives.append(half)

    return alternatives
