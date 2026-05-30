"""Tests for readingtime.shelf.manager — core shelf logic."""

import os
import tempfile

import pytest

# Set temporary paths before imports
os.environ["READINGTIME_DB"] = os.path.join(tempfile.gettempdir(), "test_shelf_manager.db")
os.environ["READINGTIME_CONFIG"] = os.path.join(tempfile.gettempdir(), "test_config.yaml")

import yaml
from pathlib import Path

from readingtime.config import config
from readingtime.database import db


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    """Setup a temporary config and database for each test."""
    # Write a minimal config with tmp_path as shelf
    test_config = {
        "shelf": {
            "path": str(tmp_path / "Books" / "ReadingTime"),
            "size": 3,  # small for testing
            "book_lifetime_days": 30,
            "language": "en",
        },
        "llm": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "max_tokens": 500,
        },
        "sources": {
            "priority": ["gutenberg", "openlibrary", "zlibrary"],
            "zlibrary": {"enabled": False, "domain": ""},
        },
        "logging": {
            "level": "WARNING",
            "file": str(tmp_path / "logs" / "agent.log"),
        },
    }
    config_path = tmp_path / "test_config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(test_config, f)
    os.environ["READINGTIME_CONFIG"] = str(config_path)

    # Reset config and database
    config._loaded = False
    db._conn = None
    db_path = os.environ["READINGTIME_DB"]
    if os.path.exists(db_path):
        os.remove(db_path)

    config.initialize()
    db.init_db()

    yield tmp_path

    db.close()
    if os.path.exists(db_path):
        os.remove(db_path)


class TestShelfManagerBasics:
    def test_current_count_empty(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager
        assert shelf_manager.current_count() == 0

    def test_get_current_books_empty(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager
        books = shelf_manager.get_current_books()
        assert books == []

    def test_shelf_path(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager
        assert shelf_manager.shelf_path.exists()

    def test_candidate_key(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager
        from readingtime.sources.base import BookResult

        r = BookResult(source_id="g:1", title="Test Book", author="John Doe")
        key = shelf_manager._candidate_key(r)
        assert key == "test book||john doe"

    def test_safe_dirname(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager
        from readingtime.sources.base import BookResult

        r = BookResult(source_id="g:1", title="Great Expectations", author="Charles Dickens")
        dirname = shelf_manager._safe_dirname(r)
        # Should be a folder name, no .epub extension
        assert not dirname.endswith(".epub")
        assert "Great_Expectations" in dirname
        assert "Dickens" in dirname

    def test_safe_dirname_special_chars(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager
        from readingtime.sources.base import BookResult

        r = BookResult(source_id="g:1", title="Test: The Book?!", author="J.K. Rowling")
        dirname = shelf_manager._safe_dirname(r)
        # No colons, question marks, or exclamation marks
        assert ":" not in dirname
        assert "?" not in dirname
        assert "!" not in dirname

    def test_book_epub_path(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager

        path = shelf_manager._book_epub_path(shelf_manager._shelf_path, "活着_余华")
        assert path.parent.name == "活着_余华"
        assert path.name == "活着_余华.epub"

    def test_list_epub_files(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager

        # Create a book folder with epub inside
        book_dir = shelf_manager.shelf_path / "TestBook_Author"
        book_dir.mkdir()
        epub = book_dir / "TestBook_Author.epub"
        epub.write_text("dummy content")
        # Create a non-epub file in shelf root
        txt = shelf_manager.shelf_path / "notes.txt"
        txt.write_text("notes")

        files = shelf_manager._list_epub_files()
        assert "TestBook_Author.epub" in files
        assert "notes.txt" not in files


class TestSystemDelete:
    def test_system_delete_file(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager

        # Create a book folder to delete
        book_dir = shelf_manager.shelf_path / "delete_me_Author"
        book_dir.mkdir()
        epub = book_dir / "delete_me_Author.epub"
        epub.write_text("dummy")

        # System delete should remove the entire folder
        result = shelf_manager._system_delete_file("delete_me_Author")
        assert result is True
        assert not book_dir.exists()

        # State flag should still be set (cleared by watcher)
        assert db.get_state("agent_is_deleting") == "delete_me_Author"


class TestRefill:
    def test_refill_no_needed(self, setup_test_env):
        """When shelf is at capacity, refill should do nothing."""
        from readingtime.shelf.manager import shelf_manager

        # Mock: shelf already has 3 books (the test size)
        for i in range(3):
            epub = shelf_manager.shelf_path / f"book{i}.epub"
            epub.write_text("dummy")
            db.add_book(f"Book {i}", f"book{i}.epub")

        added = shelf_manager.refill()
        assert len(added) == 0

    def test_profile_fallback(self, setup_test_env):
        """get_or_default_profile should return default when no profile exists."""
        from readingtime.shelf.manager import shelf_manager

        profile = shelf_manager._get_or_default_profile()
        assert profile["liked_tags"] == []
        assert profile["liked_authors"] == []
        assert profile["neutral_tags"] == []
        assert profile["lang_pref"] == "en"


class TestExpiryCheck:
    def test_check_expirations_none(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager

        expired = shelf_manager.check_expirations()
        assert expired == 0

    def test_check_expirations_no_old_books(self, setup_test_env):
        from readingtime.shelf.manager import shelf_manager

        # Add a "just added" book
        epub = shelf_manager.shelf_path / "new_book.epub"
        epub.write_text("dummy")
        db.add_book("New Book", "new_book.epub")

        expired = shelf_manager.check_expirations()
        assert expired == 0

    def test_handle_user_removal_unknown_file(self, setup_test_env):
        """Removing an unknown file should not crash."""
        from readingtime.shelf.manager import shelf_manager

        # Should not raise
        shelf_manager.handle_user_removal("nonexistent.epub")
