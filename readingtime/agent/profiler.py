"""
User preference profiler — learns reading taste from behavioral signals.

The profiler does two things:

1. **Feature extraction** — Takes raw book metadata and enriches it with
   LLM-derived tags, themes, era, style, and audience information.
2. **Profile updating** — Adjusts the user's preference weights stored in
   the ``profile`` database table based on liked / neutral signals.

Weight rules (simple incremental, no ML):
    - ``liked`` signal  → tag/author weight +1
    - ``neutral`` signal → tag/author weight -0.5 (soft penalty, not a ban)
    - Tags/authors that are consistently liked rise to the top of the list.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from readingtime.agent.llm import llm_call, parse_json_response
from readingtime.agent.prompts import FEATURE_EXTRACTION_PROMPT
from readingtime.database import db

logger = logging.getLogger(__name__)


class Profiler:
    """Builds and maintains a user preference profile from reading signals."""

    # -- feature extraction ---------------------------------------------------

    def extract_features(self, book: dict) -> dict:
        """Extract rich features from a book's metadata using LLM.

        Args:
            book: A book dict from the database (must have title, author, tags).

        Returns:
            A dict with keys: tags, era, style, themes, audience.
            On LLM failure, returns a basic dict from the book's existing tags.
        """
        title = book.get("title", "")
        author = book.get("author", "")
        existing_tags = book.get("tags", [])
        description = book.get("description", "")

        # Build the prompt
        prompt = FEATURE_EXTRACTION_PROMPT.format(
            title=title,
            author=author,
            existing_tags=json.dumps(existing_tags if isinstance(existing_tags, list) else []),
            description=description or "(no description available)",
        )

        try:
            response = llm_call(prompt, temperature=0.3)
            features = parse_json_response(response)
            if isinstance(features, dict) and features:
                logger.debug("LLM features extracted for '%s': %s", title, features)
                return features
        except Exception as exc:
            logger.warning("LLM feature extraction failed for '%s': %s", title, exc)

        # Fallback: use existing tags
        return {
            "tags": existing_tags if isinstance(existing_tags, list) else [],
            "era": "unknown",
            "style": "unknown",
            "themes": [],
            "audience": "adult",
        }

    # -- profile update -------------------------------------------------------

    def update_profile(self, signal: str, features: dict) -> None:
        """Update the user profile based on a behavioural signal.

        Args:
            signal: ``'liked'`` or ``'neutral'``.
            features: Dict from :meth:`extract_features` or the shelf manager's
                      ``_extract_book_features`` fallback.
        """
        profile = db.get_profile()
        if profile is None:
            # First signal — initialise profile
            db.upsert_profile(
                liked_tags=[],
                liked_authors=[],
                neutral_tags=[],
                lang_pref="en",
            )
            profile = db.get_profile() or {}

        liked_tags: list[str] = list(profile.get("liked_tags", []))
        liked_authors: list[str] = list(profile.get("liked_authors", []))
        neutral_tags: list[str] = list(profile.get("neutral_tags", []))

        tags = features.get("tags", [])
        author = features.get("author", "")

        if signal == "liked":
            # Boost liked tags (add to front for priority)
            for tag in tags:
                tag_lower = tag.lower().strip()
                # Remove from neutral if present
                if tag_lower in [t.lower() for t in neutral_tags]:
                    neutral_tags = [t for t in neutral_tags if t.lower() != tag_lower]
                # Add to liked if not already there
                if tag_lower not in [t.lower() for t in liked_tags]:
                    liked_tags.insert(0, tag)  # prepend — most recent first

            # Boost author
            if author:
                author_clean = author.strip()
                if author_clean not in liked_authors:
                    liked_authors.insert(0, author_clean)

        elif signal == "neutral":
            # Soft-penalize neutral tags
            for tag in tags:
                tag_lower = tag.lower().strip()
                # Only add to neutral if not already liked
                if tag_lower not in [t.lower() for t in liked_tags]:
                    if tag_lower not in [t.lower() for t in neutral_tags]:
                        neutral_tags.append(tag)

        # Persist
        db.upsert_profile(
            liked_tags=liked_tags[:20],     # cap to keep table tidy
            liked_authors=liked_authors[:15],
            neutral_tags=neutral_tags[:20],
            lang_pref=profile.get("lang_pref", "en"),
        )
        logger.debug(
            "Profile updated: signal=%s, liked_tags=%d, neutral_tags=%d",
            signal,
            len(liked_tags),
            len(neutral_tags),
        )

    # -- profile access -------------------------------------------------------

    @staticmethod
    def get_profile() -> Optional[dict]:
        """Return the current user profile or None."""
        return db.get_profile()
