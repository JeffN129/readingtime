"""
Book summarizer — generates reading notes and personalized recommendations.

Uses LLM to create:
    1. A ~300 character summary of the book (in the book's language or Chinese).
    2. A one-line "why you'll like this" recommendation (in Chinese).

Both are written to ``{book.epub}.readingnote.md`` by the shelf manager.
On LLM failure, falls back to using the book's source description.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from readingtime.agent.llm import llm_call, parse_json_response
from readingtime.agent.prompts import SUMMARY_PROMPT
from readingtime.database import db

logger = logging.getLogger(__name__)


class Summarizer:
    """LLM-powered book summarizer and recommendation writer."""

    # -- summary --------------------------------------------------------------

    def generate(self, book_result, epub_path: str) -> str:
        """Generate a book summary.

        Extracts the first ~2000 characters from the EPUB for context,
        then asks the LLM to write a summary.

        Args:
            book_result: :class:`BookResult` with title, author, tags, etc.
            epub_path: Path to the downloaded EPUB file.

        Returns:
            A summary string (~300 chars), or a fallback description.
        """
        title = book_result.title or "Unknown"
        author = book_result.author or "Unknown"
        tags = ", ".join(book_result.tags) if book_result.tags else "fiction"
        language = book_result.language or "en"

        # Get profile for personalization
        profile = db.get_profile()
        liked_tags = ", ".join(profile.get("liked_tags", [])[:5]) if profile else "classic literature"

        # Extract text from EPUB
        excerpt = self._get_excerpt(epub_path)

        # Determine summary language
        summary_lang = "Chinese" if "zh" in language else "English"

        prompt = SUMMARY_PROMPT.format(
            title=title,
            author=author,
            language=language,
            tags=tags,
            liked_tags=liked_tags,
            summary_lang=summary_lang,
            excerpt=excerpt,
        )

        try:
            response = llm_call(prompt, temperature=0.6, max_tokens=500)
            result = parse_json_response(response)

            if isinstance(result, dict) and result.get("summary"):
                logger.debug("LLM summary generated for '%s'", title)
                return result.get("summary", "")

        except Exception as exc:
            logger.warning("LLM summary failed for '%s': %s", title, exc)

        # Fallback
        if book_result.description:
            return book_result.description[:500]
        return f"{title} — 作者 {author}。暂无详细摘要。"

    # -- recommendation reason ------------------------------------------------

    def generate_reason(self, book_result) -> str:
        """Generate a one-line personalized recommendation (in Chinese).

        Args:
            book_result: :class:`BookResult` to recommend.

        Returns:
            A one-line recommendation string.
        """
        tags_str = "、".join(book_result.tags[:3]) if book_result.tags else "经典"

        profile = db.get_profile()
        if profile:
            liked = profile.get("liked_tags", [])[:3]
            if liked:
                tags_str = "、".join(liked)

        # Quick prompt for a single reason line
        prompt = f"""Based on the book info below, write ONE sentence in Chinese explaining why someone who likes {tags_str} would enjoy this book. Be specific and warm.

Title: {book_result.title}
Author: {book_result.author}
Tags: {', '.join(book_result.tags) if book_result.tags else 'fiction'}

Return ONLY the recommendation sentence, no quotes, no extra text."""

        try:
            response = llm_call(prompt, temperature=0.7, max_tokens=150, expect_json=False)
            return response.strip().strip('"').strip("'")
        except Exception as exc:
            logger.warning("LLM reason generation failed: %s", exc)

        return f"如果你喜欢{tags_str}类作品，这本书值得一读。"

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _get_excerpt(epub_path: str) -> str:
        """Get the first ~2000 characters of body text from an EPUB."""
        try:
            from readingtime.shelf.epub_utils import read_first_n_chars
            text = read_first_n_chars(epub_path, 2000)
            return text if text else "(excerpt unavailable)"
        except Exception as exc:
            logger.debug("Failed to read EPUB excerpt: %s", exc)
            return "(excerpt unavailable)"
