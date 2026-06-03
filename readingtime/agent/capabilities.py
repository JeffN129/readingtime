"""
Agent module capability detection and lazy loading.

Provides a module-level singleton ``agent_capabilities`` that:

1. Independently imports each LLM-powered agent module on init
2. Exposes boolean properties for capability checks
3. Holds the actual class references for instantiation

Usage::

    from readingtime.agent.capabilities import agent_capabilities

    if agent_capabilities.has_summarizer:
        summarizer = agent_capabilities.summarizer_cls()
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AgentCapabilities:
    """Centralised capability detection for LLM-powered agent modules.

    Each module is imported independently — a missing summarizer won't
    prevent the profiler from loading.
    """

    def __init__(self) -> None:
        self._profiler_cls: Optional[type] = None
        self._recommender_cls: Optional[type] = None
        self._summarizer_cls: Optional[type] = None
        self._load_modules()

    # -- capability queries ---------------------------------------------------

    @property
    def has_profiler(self) -> bool:
        """True when the Profiler module was successfully imported."""
        return self._profiler_cls is not None

    @property
    def has_recommender(self) -> bool:
        """True when the Recommender module was successfully imported."""
        return self._recommender_cls is not None

    @property
    def has_summarizer(self) -> bool:
        """True when the Summarizer module was successfully imported."""
        return self._summarizer_cls is not None

    @property
    def profiler_cls(self) -> Optional[type]:
        """The Profiler class, or None if not available."""
        return self._profiler_cls

    @property
    def recommender_cls(self) -> Optional[type]:
        """The Recommender class, or None if not available."""
        return self._recommender_cls

    @property
    def summarizer_cls(self) -> Optional[type]:
        """The Summarizer class, or None if not available."""
        return self._summarizer_cls

    # -- loading --------------------------------------------------------------

    def _load_modules(self) -> None:
        """Attempt to import each agent module independently.

        Each ImportError is caught silently — the corresponding class
        reference stays ``None`` and ``has_*`` returns ``False``.
        """
        try:
            from readingtime.agent.profiler import Profiler

            self._profiler_cls = Profiler
            logger.debug("AgentProfiler loaded")
        except ImportError:
            logger.debug("AgentProfiler not available")

        try:
            from readingtime.agent.recommender import Recommender

            self._recommender_cls = Recommender
            logger.debug("AgentRecommender loaded")
        except ImportError:
            logger.debug("AgentRecommender not available")

        try:
            from readingtime.agent.summarizer import Summarizer

            self._summarizer_cls = Summarizer
            logger.debug("AgentSummarizer loaded")
        except ImportError:
            logger.debug("AgentSummarizer not available")


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------

agent_capabilities = AgentCapabilities()
