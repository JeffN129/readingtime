"""
Recommendation engine — turns user profile into book recommendations.

Two main responsibilities:
    1. **Query generation** — Produce 3-5 search terms from the profile using LLM.
    2. **Candidate scoring** — Rank a batch of :class:`BookResult` objects against
       the profile using LLM (with heuristic fallback).

Both paths fail gracefully: if the LLM is unavailable, the shelf manager's
built-in heuristics take over.
"""

from __future__ import annotations

import json
import logging
from typing import List

from readingtime.agent.llm import llm_call, parse_json_response
from readingtime.agent.prompts import QUERY_GENERATION_PROMPT, SCORING_PROMPT

logger = logging.getLogger(__name__)


class Recommender:
    """LLM-powered book recommendation engine."""

    # -- query generation -----------------------------------------------------

    def generate_queries(self, profile: dict) -> list[str]:
        """Generate 3-5 search queries based on the user profile.

        Args:
            profile: Dict with liked_tags, liked_authors, neutral_tags, lang_pref.

        Returns:
            A list of search query strings.  Empty list on failure.
        """
        liked_tags = json.dumps(profile.get("liked_tags", [])[:10])
        liked_authors = json.dumps(profile.get("liked_authors", [])[:5])
        neutral_tags = json.dumps(profile.get("neutral_tags", [])[:10])
        lang_pref = profile.get("lang_pref", "en")

        prompt = QUERY_GENERATION_PROMPT.format(
            liked_tags=liked_tags,
            liked_authors=liked_authors,
            neutral_tags=neutral_tags,
            lang_pref=lang_pref,
        )

        try:
            response = llm_call(prompt, temperature=0.8)
            queries = parse_json_response(response)

            if isinstance(queries, list) and len(queries) > 0:
                # Ensure all items are strings
                valid = [str(q) for q in queries if q]
                logger.info("LLM generated %d search queries", len(valid))
                return valid[:5]

        except Exception as exc:
            logger.warning("Query generation failed: %s", exc)

        return []

    # -- candidate scoring ----------------------------------------------------

    def score_candidates(self, candidates: list, profile: dict) -> list[tuple]:
        """Score a list of BookResult candidates using LLM.

        Args:
            candidates: List of :class:`BookResult` objects (at least 3).
            profile: User profile dict.

        Returns:
            List of ``(BookResult, score)`` tuples sorted by score descending.
            Falls back to heuristic scoring on LLM failure.
        """
        if len(candidates) < 3:
            # Too few to bother with LLM; use simple scoring
            return self._heuristic_score(candidates, profile)

        # Build candidate text for the prompt
        candidate_lines = []
        for i, c in enumerate(candidates):
            tags_str = ", ".join(c.tags) if c.tags else "unknown"
            candidate_lines.append(
                f"{i+1}. [{c.source_id}] {c.title} by {c.author} — tags: {tags_str}"
            )
        candidates_text = "\n".join(candidate_lines)

        prompt = SCORING_PROMPT.format(
            liked_tags=json.dumps(profile.get("liked_tags", [])[:10]),
            liked_authors=json.dumps(profile.get("liked_authors", [])[:5]),
            neutral_tags=json.dumps(profile.get("neutral_tags", [])[:10]),
            lang_pref=profile.get("lang_pref", "en"),
            candidates_text=candidates_text,
        )

        try:
            response = llm_call(prompt, temperature=0.3)
            scores = parse_json_response(response)

            if isinstance(scores, dict) and scores:
                return self._apply_llm_scores(candidates, scores)

        except Exception as exc:
            logger.warning("LLM scoring failed, falling back to heuristic: %s", exc)

        return self._heuristic_score(candidates, profile)

    # -- private helpers ------------------------------------------------------

    @staticmethod
    def _apply_llm_scores(candidates: list, scores: dict) -> list[tuple]:
        """Merge LLM scores back onto candidate objects."""
        scored: list[tuple] = []
        for c in candidates:
            score = scores.get(c.source_id, 5)  # default: neutral
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 5.0
            # Clamp to 1-10 range
            score = max(1.0, min(10.0, score))
            scored.append((c, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        logger.debug(
            "LLM scored %d candidates (top: %.1f, bottom: %.1f)",
            len(scored),
            scored[0][1] if scored else 0,
            scored[-1][1] if scored else 0,
        )
        return scored

    @staticmethod
    def _heuristic_score(candidates: list, profile: dict) -> list[tuple]:
        """Simple tag-overlap scoring (no LLM required).

        This mirrors the heuristic in :mod:`readingtime.shelf.manager`.
        """
        liked_tags = set(t.lower() for t in profile.get("liked_tags", []))
        neutral_tags = set(t.lower() for t in profile.get("neutral_tags", []))
        liked_authors = set(a.lower() for a in profile.get("liked_authors", []))

        scored: list[tuple] = []
        for c in candidates:
            score = 5.0
            c_tags = set(t.lower() for t in (c.tags or []))
            c_author = (c.author or "").lower()

            tag_overlap = c_tags & liked_tags
            score += len(tag_overlap) * 1.5

            if c_author in liked_authors:
                score += 3.0

            tag_penalty = c_tags & neutral_tags
            score -= len(tag_penalty) * 1.0

            if c.description:
                score += 0.5

            scored.append((c, max(0.0, score)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored
