"""
Book source abstract interface.

Defines the protocol that every book source must implement:
    - search(query, language, limit) -> List[BookResult]
    - download(result, save_path) -> bool

All downstream modules (shelf manager, recommender) only import BookSource
and BookResult — they never care which concrete source is being used.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class BookResult:
    """Normalised representation of a book returned by any source."""

    source_id: str
    title: str
    author: str
    language: str = "en"
    tags: List[str] = field(default_factory=list)
    formats: List[str] = field(default_factory=list)  # e.g. ['epub', 'pdf']
    epub_download_url: Optional[str] = None
    cover_url: Optional[str] = None
    page_count: Optional[int] = None
    description: Optional[str] = None  # raw blurb, fed to the LLM summarizer
    download_count: int = 0  # popularity indicator (higher = more popular)


class BookSource(ABC):
    """Abstract book source — implement for Gutenberg, Open Library, etc."""

    # Subclasses should override this for logging / display.
    name: str = "base"

    @abstractmethod
    def search(
        self,
        query: str,
        language: str = "en",
        limit: int = 10,
    ) -> List[BookResult]:
        """Search for books matching *query*.

        Must only return results that have a downloadable EPUB format.
        """
        ...

    @abstractmethod
    def download(self, result: BookResult, save_path: str) -> bool:
        """Download the EPUB file for *result* to *save_path*.

        Returns True on success.
        """
        ...
