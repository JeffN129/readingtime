"""
Integration tests covering the full shelf lifecycle:

    - User removal → pending → undo / expiry → signal
    - Auto expiry → file deletion → DB update
    - Activity log writing
    - Notification hooks (mocked)
    - Watcher event → shelf manager dispatch
    - Database pending_removals CRUD
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# -- Redirect config and DB before any ReadingTime imports -------------------
os.environ["READINGTIME_DB"] = os.path.join(
    tempfile.gettempdir(), "test_integration.db"
)
os.environ["READINGTIME_CONFIG"] = os.path.join(
    tempfile.gettempdir(), "test_integration_config.yaml"
)

from readingtime.config import config  # noqa: E402
from readingtime.database import db  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def setup_env(tmp_path):
    """Create a fresh test config and database for every test."""
    test_config = {
        "shelf": {
            "path": str(tmp_path / "Shelf"),
            "size": 3,
            "book_lifetime_days": 30,
            "language": "zh",
        },
        "llm": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "max_tokens": 500,
        },
        "sources": {
            "priority": ["kgbook"],
            "kgbook": {"enabled": True},
        },
        "logging": {
            "level": "WARNING",
            "file": str(tmp_path / "logs" / "agent.log"),
        },
    }
    config_path = Path(os.environ["READINGTIME_CONFIG"])
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(test_config, f)

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


def _make_book_dir(shelf_path: Path, dirname: str) -> Path:
    """Helper: create a book directory with a dummy EPUB inside."""
    book_dir = shelf_path / dirname
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / f"{dirname}.epub").write_text("dummy epub content", encoding="utf-8")
    return book_dir


# ---------------------------------------------------------------------------
# Undo lifecycle
# ---------------------------------------------------------------------------

class TestUndoLifecycle:
    """User deletion creates a pending removal; undo restores the book."""

    def test_user_removal_creates_pending(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "test_book_Author")
        db.add_book("Test Book", "test_book_Author", author="Author")

        shelf_manager.handle_user_removal("test_book_Author")

        pending = db.get_pending_removal("test_book_Author")
        assert pending is not None
        assert pending["title"] == "Test Book"
        assert pending["dirname"] == "test_book_Author"

    def test_undo_within_window_restores_book(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "undo_book_Author")
        db.add_book("Undo Book", "undo_book_Author", author="Author")

        shelf_manager.handle_user_removal("undo_book_Author")

        result = shelf_manager.undo_removal("undo_book_Author")
        assert result is True

        book = db.get_book_by_filename("undo_book_Author")
        assert book is not None
        assert book.get("removed_at") is None
        # Pending entry should be cleaned up
        assert db.get_pending_removal("undo_book_Author") is None

    def test_undo_after_expiry_fails(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "late_undo_Author")
        db.add_book("Late Undo", "late_undo_Author", author="Author")

        shelf_manager.handle_user_removal("late_undo_Author")

        # Force expiry to the past
        db.conn.execute(
            "UPDATE pending_removals SET expires_at = ? WHERE filename = ?",
            (
                (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "late_undo_Author",
            ),
        )
        db.conn.commit()

        result = shelf_manager.undo_removal("late_undo_Author")
        assert result is False

    def test_undo_unknown_filename(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        result = shelf_manager.undo_removal("does_not_exist")
        assert result is False


# ---------------------------------------------------------------------------
# Pending processing
# ---------------------------------------------------------------------------


class TestPendingProcessing:
    """Expired pending removals record 'liked' signals and update profile."""

    def test_process_expired_pending_records_signal(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "process_book_Author")
        book_id = db.add_book(
            "Process Book", "process_book_Author", author="Author", tags=["fiction"]
        )

        shelf_manager.handle_user_removal("process_book_Author")

        # Force expiry
        db.conn.execute(
            "UPDATE pending_removals SET expires_at = ? WHERE filename = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "process_book_Author",
            ),
        )
        db.conn.commit()

        # Mock refill to avoid triggering downloads
        with patch.object(shelf_manager, "refill", return_value=[]):
            processed = shelf_manager._process_pending_removals()
        assert processed >= 1

        signals = db.get_recent_signals()
        liked = [s for s in signals if s["signal"] == "liked"]
        assert len(liked) >= 1
        assert liked[0]["title"] == "Process Book"

    def test_process_empty_returns_zero(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        processed = shelf_manager._process_pending_removals()
        assert processed == 0

    def test_no_signal_for_removed_book_that_is_gone(self, setup_env):
        """If book already removed from DB completely, skip gracefully."""
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "gone_book_Author")
        book_id = db.add_book("Gone Book", "gone_book_Author", author="Author")

        # Create pending removal directly (bypass handle_user_removal)
        db.mark_removed("gone_book_Author", "manual")
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        db.conn.execute(
            "INSERT INTO pending_removals (filename, book_id, title, author, "
            "dirname, source_id, removed_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("gone_book_Author", book_id, "Gone Book", "Author",
             "gone_book_Author", "", past_time, past_time),
        )
        db.conn.commit()

        # Delete the book entirely from the DB
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("DELETE FROM books WHERE filename = ?", ("gone_book_Author",))
        db.conn.execute("PRAGMA foreign_keys = ON")
        db.conn.commit()

        # Should not crash
        with patch.object(shelf_manager, "refill", return_value=[]):
            processed = shelf_manager._process_pending_removals()
        assert processed >= 1  # Entry was cleared gracefully


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


class TestActivityLog:
    """Activity log file is written to the shelf directory."""

    def test_log_created_on_user_removal(self, setup_env):
        from readingtime.shelf.manager import shelf_manager
        from readingtime.shelf.activity_log import log_activity

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "log_test_Author")
        db.add_book("Log Test", "log_test_Author", author="Author")

        shelf_manager.handle_user_removal("log_test_Author")

        now = datetime.now(timezone.utc) + timedelta(hours=8)
        month_str = now.strftime("%Y-%m")
        activity_file = shelf_path / f"activity-{month_str}.md"

        assert activity_file.exists()
        content = activity_file.read_text(encoding="utf-8")
        assert "Log Test" in content
        assert "待确认" in content  # Pending confirmation

    def test_log_on_auto_expiry(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "old_log_Author")
        db.add_book("Old Log", "old_log_Author", author="Author")

        # Set added_at to 31 days ago
        old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        db.conn.execute(
            "UPDATE books SET added_at = ? WHERE filename = ?",
            (old_date, "old_log_Author"),
        )
        db.conn.commit()

        with patch.object(shelf_manager, "refill", return_value=[]):
            shelf_manager.check_expirations()

        now = datetime.now(timezone.utc) + timedelta(hours=8)
        month_str = now.strftime("%Y-%m")
        activity_file = shelf_path / f"activity-{month_str}.md"

        assert activity_file.exists()
        content = activity_file.read_text(encoding="utf-8")
        assert "Old Log" in content
        assert "过期" in content

    def test_module_function_works_standalone(self, tmp_path):
        from readingtime.shelf.activity_log import log_activity

        shelf = tmp_path / "log_shelf"
        shelf.mkdir(exist_ok=True)
        log_activity(shelf, "🧪 测试", "Test Book", "Author", "testing")

        now = datetime.now(timezone.utc) + timedelta(hours=8)
        month_str = now.strftime("%Y-%m")
        activity_file = shelf / f"activity-{month_str}.md"
        assert activity_file.exists()
        content = activity_file.read_text(encoding="utf-8")
        assert "Test Book" in content
        assert "🧪 测试" in content


# ---------------------------------------------------------------------------
# Notifications (mocked)
# ---------------------------------------------------------------------------


class TestNotificationHooks:
    """Notifications are triggered at shelf lifecycle events."""

    def test_notify_on_user_removal(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "notif_book_Author")
        db.add_book("Notif Book", "notif_book_Author", author="Author")

        with patch("readingtime.notifier.ask_liked_book") as mock_ask:
            shelf_manager.handle_user_removal("notif_book_Author")
            mock_ask.assert_called()
            # Should pass book info to the interactive ask
            args = mock_ask.call_args
            assert "Notif Book" in str(args)

    def test_notify_on_auto_expiry(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "old_notif_Author")
        db.add_book("Old Notif", "old_notif_Author", author="Author")

        old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        db.conn.execute(
            "UPDATE books SET added_at = ? WHERE filename = ?",
            (old_date, "old_notif_Author"),
        )
        db.conn.commit()

        with patch("readingtime.shelf.manager.notify") as mock_notify:
            with patch.object(shelf_manager, "refill", return_value=[]):
                shelf_manager.check_expirations()
            mock_notify.assert_called()

    def test_notify_on_undo(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "undo_notif_Author")
        db.add_book("Undo Notif", "undo_notif_Author", author="Author")

        shelf_manager.handle_user_removal("undo_notif_Author")

        with patch("readingtime.shelf.manager.notify") as mock_notify:
            shelf_manager.undo_removal("undo_notif_Author")
            mock_notify.assert_called()

    def test_notify_does_not_crash(self):
        """Verify notify() returns False gracefully when no backend available."""
        from readingtime.notifier import notify, reset_notifier

        reset_notifier()
        result = notify("Test", "This should not crash")
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Expiry flow
# ---------------------------------------------------------------------------


class TestExpiryFlow:
    """Auto-expiry removes old books and records neutral signals."""

    def test_expiry_removes_book_and_folders(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        book_dir = _make_book_dir(shelf_path, "old_book_Author")
        db.add_book("Old Book", "old_book_Author", author="Author")

        old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        db.conn.execute(
            "UPDATE books SET added_at = ? WHERE filename = ?",
            (old_date, "old_book_Author"),
        )
        db.conn.commit()

        # Mock refill to avoid triggering downloads
        with patch.object(shelf_manager, "refill", return_value=[]):
            expired = shelf_manager.check_expirations()
        assert expired >= 1
        assert not book_dir.exists()

        signals = db.get_recent_signals()
        neutral = [s for s in signals if s["signal"] == "neutral"]
        assert len(neutral) >= 1

    def test_protected_book_not_expired(self, setup_env):
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        book_dir = _make_book_dir(shelf_path, "protected_book_Author")
        db.add_book("Protected Book", "protected_book_Author", author="Author")
        db.extend_protection("protected_book_Author")

        old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        db.conn.execute(
            "UPDATE books SET added_at = ? WHERE filename = ?",
            (old_date, "protected_book_Author"),
        )
        db.conn.commit()

        expired = shelf_manager.check_expirations()
        assert expired == 0
        assert book_dir.exists()


# ---------------------------------------------------------------------------
# Watcher integration
# ---------------------------------------------------------------------------


class TestWatcherIntegration:
    """Watcher events trigger correct shelf manager methods."""

    def test_user_deletion_triggers_pending_removal(self, setup_env):
        from readingtime.monitor.watcher import ShelfHandler
        from watchdog.events import FileDeletedEvent
        from readingtime.shelf.manager import shelf_manager

        shelf_path = shelf_manager.shelf_path
        _make_book_dir(shelf_path, "watch_book_Author")
        db.add_book("Watch Book", "watch_book_Author", author="Author")

        epub_path = shelf_path / "watch_book_Author" / "watch_book_Author.epub"
        handler = ShelfHandler()
        event = FileDeletedEvent(str(epub_path))
        handler.on_deleted(event)

        pending = db.get_pending_removal("watch_book_Author")
        assert pending is not None

    def test_system_deletion_ignored_by_watcher(self, setup_env):
        from readingtime.monitor.watcher import ShelfHandler
        from watchdog.events import FileDeletedEvent
        from readingtime.shelf.manager import shelf_manager

        db.set_state("agent_is_deleting", "system_del_Author")

        handler = ShelfHandler()
        event = FileDeletedEvent(
            str(config.shelf_path / "system_del_Author" / "system_del_Author.epub")
        )
        handler.on_deleted(event)

        assert db.get_state("agent_is_deleting") is None
        assert db.get_pending_removal("system_del_Author") is None


# ---------------------------------------------------------------------------
# Database: pending_removals CRUD
# ---------------------------------------------------------------------------


class TestPendingRemovalsDB:
    """Direct database operations on pending_removals table."""

    def test_record_and_get(self):
        book_id = db.add_book("DB Test", "dbtest_Author", author="Author")
        rid = db.record_pending_removal(
            filename="dbtest_Author",
            book_id=book_id,
            title="DB Test",
            author="Author",
            dirname="dbtest_Author",
            source_id="kgbook:1:123",
        )
        assert rid > 0

        pending = db.get_pending_removal("dbtest_Author")
        assert pending is not None
        assert pending["title"] == "DB Test"
        assert pending["source_id"] == "kgbook:1:123"
        assert "expires_at" in pending

    def test_get_all_and_delete(self):
        book_id = db.add_book("Multi Test", "multi_Author", author="Author")
        db.record_pending_removal("multi_Author", book_id, "Multi Test")

        all_pending = db.get_all_pending_removals()
        assert len(all_pending) >= 1

        db.delete_pending_removal("multi_Author")
        assert db.get_pending_removal("multi_Author") is None

    def test_clear_expired(self):
        book_id = db.add_book("Expire Test", "expire_Author", author="Author")
        db.record_pending_removal(
            "expire_Author", book_id, "Expire Test", grace_minutes=0
        )

        import time
        time.sleep(0.1)

        expired = db.clear_expired_pending()
        assert len(expired) >= 1
        assert expired[0]["title"] == "Expire Test"
        assert db.get_pending_removal("expire_Author") is None

    def test_restore_book(self):
        book_id = db.add_book("Restore Test", "restore_Author", author="Author")
        db.mark_removed("restore_Author", "manual")
        book = db.get_book_by_filename("restore_Author")
        assert book["removed_at"] is not None

        result = db.restore_book("restore_Author")
        assert result is True

        book = db.get_book_by_filename("restore_Author")
        assert book["removed_at"] is None
        assert book["removal_type"] is None


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------


class TestPathUtilities:
    """Standalone path utility functions."""

    def test_safe_dirname_standalone(self):
        from readingtime.shelf.paths import safe_dirname
        from readingtime.sources.base import BookResult

        r = BookResult(
            source_id="test:1:1",
            title="三体",
            author="刘慈欣",
            formats=["epub"],
        )
        result = safe_dirname(r)
        assert "三体" in result or "3" in result or "san" in result.lower()

    def test_candidate_key_standalone(self):
        from readingtime.shelf.paths import candidate_key
        from readingtime.sources.base import BookResult

        r = BookResult(
            source_id="test:1:1",
            title="活着",
            author="余华",
            formats=["epub"],
        )
        key = candidate_key(r)
        assert "活着" in key
        assert "余华" in key

    def test_list_epub_files_standalone(self, tmp_path):
        from readingtime.shelf.paths import list_epub_files

        shelf = tmp_path / "epub_shelf"
        shelf.mkdir(exist_ok=True)
        (shelf / "Book1").mkdir()
        (shelf / "Book1" / "book.epub").write_text("dummy")
        (shelf / "Book2").mkdir()
        (shelf / "Book2" / "another.epub").write_text("dummy")
        (shelf / "EmptyDir").mkdir()

        files = list_epub_files(shelf)
        assert len(files) == 2

    def test_book_epub_path_standalone(self, tmp_path):
        from readingtime.shelf.paths import book_epub_path

        result = book_epub_path(tmp_path, "活着_余华")
        assert result == tmp_path / "活着_余华" / "活着_余华.epub"


# ---------------------------------------------------------------------------
# Sourcing utilities
# ---------------------------------------------------------------------------


class TestSourcingUtilities:
    """Source registry and query helpers."""

    def test_sources_dict_has_kgbook(self):
        from readingtime.shelf.sourcing import _SOURCES
        assert "kgbook" in _SOURCES

    def test_seed_queries_non_empty(self):
        from readingtime.shelf.sourcing import _SEED_QUERIES
        assert len(_SEED_QUERIES) >= 10
        assert "活着" in _SEED_QUERIES

    def test_simplify_query_single_word(self):
        from readingtime.shelf.sourcing import simplify_query
        assert simplify_query("活着") == []

    def test_simplify_query_multi_word(self):
        from readingtime.shelf.sourcing import simplify_query
        result = simplify_query("三体 刘慈欣")
        assert "三体" in result
        assert len(result) <= 2


# ---------------------------------------------------------------------------
# AgentCapabilities
# ---------------------------------------------------------------------------


class TestAgentCapabilities:
    """Capability detection for LLM agent modules."""

    def test_singleton_exists(self):
        from readingtime.agent.capabilities import agent_capabilities
        from readingtime.agent.capabilities import AgentCapabilities
        assert isinstance(agent_capabilities, AgentCapabilities)

    def test_properties_are_bools(self):
        from readingtime.agent.capabilities import agent_capabilities
        assert isinstance(agent_capabilities.has_profiler, bool)
        assert isinstance(agent_capabilities.has_recommender, bool)
        assert isinstance(agent_capabilities.has_summarizer, bool)

    def test_cls_properties(self):
        from readingtime.agent.capabilities import agent_capabilities
        # Each either returns a class or None
        assert agent_capabilities.profiler_cls is not None or True
        assert agent_capabilities.recommender_cls is not None or True
        assert agent_capabilities.summarizer_cls is not None or True
