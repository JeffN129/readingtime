"""Tests for kgbook book source."""

from readingtime.sources.kgbook import KgbookSource, BASE_URL
from readingtime.sources.base import BookResult


class TestBookResult:
    def test_default_values(self):
        result = BookResult(source_id="test:1", title="Test", author="Author")
        assert result.language == "en"
        assert result.download_count == 0

    def test_full_construction(self):
        result = BookResult(
            source_id="kgbook:15:123",
            title="活着",
            author="余华",
            language="zh",
            tags=["现代文学"],
            formats=["pdf"],
            download_count=0,
        )
        assert result.language == "zh"
        assert result.author == "余华"


class TestKgbookSource:
    def test_source_name(self):
        src = KgbookSource()
        assert src.name == "kgbook"

    def test_parse_search_results_empty(self):
        results = KgbookSource._parse_search_results("<html></html>", 10)
        assert results == []
