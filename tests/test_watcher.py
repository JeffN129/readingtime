"""Tests for readingtime.monitor.watcher — file system monitoring."""

import os
import tempfile

import pytest

os.environ["READINGTIME_DB"] = os.path.join(tempfile.gettempdir(), "test_watcher.db")
os.environ["READINGTIME_CONFIG"] = os.path.join(tempfile.gettempdir(), "test_watcher_config.yaml")

import yaml
from pathlib import Path

from readingtime.config import config
from readingtime.database import db


@pytest.fixture
def watcher_env(tmp_path):
    """Setup a test shelf directory with config and DB."""
    test_config = {
        "shelf": {
            "path": str(tmp_path / "Shelf"),
            "size": 10,
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
    config_path = tmp_path / "test_watcher_config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(test_config, f)
    os.environ["READINGTIME_CONFIG"] = str(config_path)

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


class TestShelfHandler:
    def test_handler_ignores_non_epub(self, watcher_env):
        from readingtime.monitor.watcher import ShelfHandler
        from watchdog.events import FileDeletedEvent

        handler = ShelfHandler()

        # Should not raise for non-EPUB files
        event = FileDeletedEvent(str(config.shelf_path / "notes.txt"))
        handler.on_deleted(event)
        # No exception = pass

    def test_handler_system_delete_ignored(self, watcher_env):
        """When system_state has agent_is_deleting set, the handler should ignore."""
        from readingtime.monitor.watcher import ShelfHandler
        from watchdog.events import FileDeletedEvent

        # Set the system flag
        db.set_state("agent_is_deleting", "test_book.epub")

        handler = ShelfHandler()
        event = FileDeletedEvent(str(config.shelf_path / "test_book.epub"))
        handler.on_deleted(event)

        # Flag should be cleared
        assert db.get_state("agent_is_deleting") is None

    def test_is_in_shelf(self, watcher_env):
        from readingtime.monitor.watcher import ShelfHandler

        handler = ShelfHandler()
        assert handler._is_in_shelf(config.shelf_path / "book.epub") is True
        assert handler._is_in_shelf(Path("/tmp/somewhere.epub")) is False


class TestShelfWatcher:
    def test_start_stop(self, watcher_env):
        from readingtime.monitor.watcher import ShelfWatcher

        watcher = ShelfWatcher()
        assert watcher.is_running is False

        watcher.start()
        assert watcher.is_running is True

        watcher.stop()
        assert watcher.is_running is False

    def test_double_start(self, watcher_env):
        from readingtime.monitor.watcher import ShelfWatcher

        watcher = ShelfWatcher()
        watcher.start()
        watcher.start()  # should log warning but not crash
        watcher.stop()
