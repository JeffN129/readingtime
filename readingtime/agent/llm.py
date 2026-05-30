"""
Lightweight LLM client wrapper for ReadingTime agents.

Uses the OpenAI-compatible API (DeepSeek by default) configured via
config.yaml and .env.  All agent modules import ``llm_call`` from here
instead of creating their own clients.

The wrapper is deliberately minimal — we don't need LangChain for
single-turn prompt → JSON calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from openai import OpenAI

from readingtime.config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level client (lazy init)
# ---------------------------------------------------------------------------
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Return a configured OpenAI client (lazy singleton)."""
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        base_url = config.llm_base_url

        _client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        logger.debug("LLM client initialised — base_url=%s", base_url)
    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def llm_call(
    prompt: str,
    *,
    system: str = "You are a helpful assistant for a book recommendation system.",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    expect_json: bool = True,
) -> str:
    """Send a prompt to the LLM and return the response text.

    Args:
        prompt: The user message / filled prompt template.
        system: System message (sets tone and role).
        model: Model override (default from config).
        max_tokens: Max tokens override (default from config).
        temperature: Sampling temperature.
        expect_json: If True, attempt to extract JSON from the response
                     (the raw text is still returned; use :func:`parse_json_response`
                     to get a dict).

    Returns:
        The LLM's response text, stripped of markdown fences if present.

    Raises:
        RuntimeError: If the LLM call fails after retries.
    """
    client = _get_client()
    model_name = model or config.llm_model
    max_tok = max_tokens or config.llm_max_tokens

    for attempt in range(1, 4):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tok,
                temperature=temperature,
            )
            text = response.choices[0].message.content or ""
            # Strip markdown fences
            text = _strip_fences(text)
            logger.debug("LLM response (%d chars): %s...", len(text), text[:120])
            return text

        except Exception as exc:
            logger.warning("LLM call attempt %d/3 failed: %s", attempt, exc)
            if attempt >= 3:
                raise RuntimeError(f"LLM call failed after 3 attempts: {exc}") from exc

    # Unreachable, but makes type-checkers happy
    raise RuntimeError("LLM call failed")


def parse_json_response(text: str) -> dict[str, Any] | list[Any]:
    """Parse a JSON response from the LLM.

    Tries:
        1. Direct ``json.loads``.
        2. Regex extraction of the first JSON object/array in the text.
        3. Returns empty dict on failure (never raises — caller handles fallback).
    """
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Regex: find first { ... } or [ ... ]
    json_re = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
    match = json_re.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse JSON from LLM response: %s...", text[:200])
    return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ```) from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence line
        text = re.sub(r"^```[^\n]*\n?", "", text)
        # Remove closing fence
        text = re.sub(r"\n?```$", "", text)
    return text.strip()
