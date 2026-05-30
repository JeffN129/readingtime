"""Tests for format conversion utilities."""

from pathlib import Path

from readingtime.shelf.converter import (
    convert_to_epub,
    find_book_file,
    _has_calibre,
    _detect_format,
)


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

    def test_mislabeled_epub_renamed(self, tmp_path):
        # A file with .mobi extension but EPUB magic bytes
        mobi = tmp_path / "book.mobi"
        # Write EPUB magic bytes (ZIP + mimetype)
        epub_magic = (
            b"PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x08\x00\x00\x00mimetypeapplication/epub+zip"
            b"PK\x03\x04\x14\x00\x02\x08\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        mobi.write_bytes(epub_magic)
        result = convert_to_epub(mobi)
        assert result is not None
        assert result.suffix == ".epub"
        assert result.exists()
        assert not mobi.exists()  # original renamed


class TestDetectFormat:
    def test_detects_epub(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(
            b"PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x08\x00\x00\x00"
            b"mimetypeapplication/epub+zip"
        )
        assert _detect_format(f) == "epub"

    def test_detects_pdf(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"%PDF-1.4\n%abc\n")
        assert _detect_format(f) == "pdf"

    def test_detects_mobi(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"BOOKMOBI" + b"\x00" * 25)
        assert _detect_format(f) == "mobi"

    def test_returns_none_for_unknown(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"just some random text not a book")
        assert _detect_format(f) is None


class TestHasCalibre:
    def test_returns_bool(self):
        result = _has_calibre()
        assert isinstance(result, bool)
