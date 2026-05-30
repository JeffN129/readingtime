"""Tests for format conversion utilities."""

from pathlib import Path

from readingtime.shelf.converter import convert_to_epub, find_book_file, _has_calibre


class TestFindBookFile:
    def test_finds_epub(self, tmp_path):
        (tmp_path / "book.epub").write_text("epub")
        result = find_book_file(tmp_path, "book")
        assert result is not None
        assert result.suffix == ".epub"

    def test_finds_azw3(self, tmp_path):
        (tmp_path / "book.azw3").write_text("azw3")
        result = find_book_file(tmp_path, "book")
        assert result is not None
        assert result.suffix == ".azw3"

    def test_finds_mobi(self, tmp_path):
        (tmp_path / "book.mobi").write_text("mobi")
        result = find_book_file(tmp_path, "book")
        assert result is not None
        assert result.suffix == ".mobi"

    def test_finds_pdf(self, tmp_path):
        (tmp_path / "book.pdf").write_text("pdf")
        result = find_book_file(tmp_path, "book")
        assert result is not None
        assert result.suffix == ".pdf"

    def test_returns_none_when_no_book(self, tmp_path):
        (tmp_path / "book.readingnote.md").write_text("note")
        result = find_book_file(tmp_path, "book")
        assert result is None

    def test_epub_preferred_over_others(self, tmp_path):
        # When multiple formats exist, epub should be found first
        (tmp_path / "book.epub").write_text("epub")
        (tmp_path / "book.azw3").write_text("azw3")
        result = find_book_file(tmp_path, "book")
        assert result is not None
        assert result.suffix == ".epub"


class TestConvertToEpub:
    def test_already_epub_returns_same_path(self, tmp_path):
        epub = tmp_path / "book.epub"
        epub.write_text("epub content")
        result = convert_to_epub(epub)
        assert result == epub
        assert epub.exists()

    def test_no_calibre_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "readingtime.shelf.converter._has_calibre", lambda: False
        )
        mobi = tmp_path / "book.mobi"
        mobi.write_text("mobi content")
        result = convert_to_epub(mobi)
        assert result is None
        assert mobi.exists()  # original kept


class TestHasCalibre:
    def test_returns_bool(self):
        result = _has_calibre()
        assert isinstance(result, bool)
