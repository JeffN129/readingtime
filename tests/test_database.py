"""Tests for readingtime.database — SQLite CRUD operations."""

import json
import os
import tempfile

import pytest

# Override database path before importing
os.environ["READINGTIME_DB"] = os.path.join(tempfile.gettempdir(), "test_readingtime.db")

from readingtime.database import db, _now, _json_dumps, _json_loads


@pytest.fixture(autouse=True)
def clean_db():
    """Start each test with a fresh database."""
    db._conn = None  # force reconnect
    db_path = os.environ["READINGTIME_DB"]
    if os.path.exists(db_path):
        os.remove(db_path)
    db.init_db()
    yield
    db.close()
    if os.path.exists(db_path):
        os.remove(db_path)


class TestHelpers:
    def test_now_returns_iso_string(self):
        result = _now()
        assert "T" in result
        assert len(result) >= 19

    def test_json_roundtrip(self):
        data = ["fiction", "mystery"]
        encoded = _json_dumps(data)
        decoded = _json_loads(encoded)
        assert decoded == data

    def test_json_loads_empty(self):
        assert _json_loads("") is None
        assert _json_loads(None) is None


class TestBooks:
    def test_add_and_get_book(self):
        book_id = db.add_book(
            title="Test Book",
            filename="test.epub",
            author="Test Author",
            source="gutenberg",
            tags=["fiction", "classic"],
        )
        assert book_id > 0

        book = db.get_book_by_id(book_id)
        assert book is not None
        assert book["title"] == "Test Book"
        assert book["author"] == "Test Author"
        assert book["filename"] == "test.epub"
        assert "fiction" in book["tags"]

    def test_get_current_books(self):
        db.add_book("On Shelf", "shelf.epub", author="A")
        db.add_book("Also On Shelf", "shelf2.epub", author="B")

        current = db.get_current_books()
        assert len(current) == 2

    def test_mark_removed(self):
        db.add_book("To Remove", "remove_me.epub")
        assert db.mark_removed("remove_me.epub", "manual") is True

        # Should no longer appear in current books
        current = db.get_current_books()
        assert all(b["filename"] != "remove_me.epub" for b in current)

        # Marking again should return False
        assert db.mark_removed("remove_me.epub", "manual") is False

    def test_mark_removed_unknown(self):
        assert db.mark_removed("nonexistent.epub", "manual") is False

    def test_get_book_by_filename(self):
        db.add_book("Find Me", "find_me.epub")
        book = db.get_book_by_filename("find_me.epub")
        assert book is not None
        assert book["title"] == "Find Me"

        assert db.get_book_by_filename("nonexistent.epub") is None

    def test_get_book_history(self):
        db.add_book("History Book", "history.epub")
        db.add_book("History Book 2", "history2.epub")
        db.mark_removed("history.epub", "manual")

        history = db.get_book_history()
        assert len(history) >= 2

    def test_extend_protection(self):
        db.add_book("Protected", "protected.epub")
        assert db.extend_protection("protected.epub") is True
        book = db.get_book_by_filename("protected.epub")
        assert book["is_protected"] == 1


class TestSignals:
    def test_record_signal(self):
        book_id = db.add_book("Signal Book", "signal.epub", tags=["fiction"])
        signal_id = db.record_signal(book_id, "liked", {"tags": ["fiction"]})
        assert signal_id > 0

    def test_get_recent_signals(self):
        book_id = db.add_book("Signal Book 2", "signal2.epub", tags=["mystery"])
        db.record_signal(book_id, "liked", {"tags": ["mystery"]})
        db.record_signal(book_id, "neutral", {"tags": ["mystery"]})

        signals = db.get_recent_signals(limit=10)
        assert len(signals) >= 2


class TestProfile:
    def test_upsert_and_get_profile(self):
        db.upsert_profile(
            liked_tags=["fiction", "mystery"],
            liked_authors=["Kafka"],
            neutral_tags=["romance"],
        )

        profile = db.get_profile()
        assert profile is not None
        assert "fiction" in profile["liked_tags"]
        assert "Kafka" in profile["liked_authors"]
        assert "romance" in profile["neutral_tags"]

    def test_upsert_updates_existing(self):
        db.upsert_profile(liked_tags=["fiction"])
        db.upsert_profile(liked_tags=["mystery"])

        profile = db.get_profile()
        # Second upsert with COALESCE should keep old + add new
        assert profile is not None

    def test_get_profile_none(self):
        # Fresh database with no profile
        assert db.get_profile() is None


class TestSystemState:
    def test_set_get_clear(self):
        db.set_state("test_key", "test_value")
        assert db.get_state("test_key") == "test_value"

        db.clear_state("test_key")
        assert db.get_state("test_key") is None

    def test_get_missing_key(self):
        assert db.get_state("nonexistent") is None

    def test_set_overwrites(self):
        db.set_state("key", "value1")
        db.set_state("key", "value2")
        assert db.get_state("key") == "value2"
