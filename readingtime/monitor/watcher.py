"""
Filesystem monitor for the ReadingTime bookshelf.

Uses ``watchdog`` to watch the shelf directory for file deletions and moves.
The core challenge is distinguishing **user actions** (which signal "I liked
this book") from **system actions** (the agent auto-expiring a stale book).

How it works:
    1. Before the agent deletes a file, it writes ``agent_is_deleting = filename``
       to the ``system_state`` table.
    2. When the watcher receives a delete/move event, it checks that flag.
    3. If the filename matches → system action → clear flag, do nothing.
    4. Otherwise → user action → record a liked signal + trigger refill.

Usage::

    from readingtime.monitor.watcher import ShelfWatcher
    watcher = ShelfWatcher()
    watcher.start()
    # ... agent runs ...
    watcher.stop()
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from readingtime.config import config
from readingtime.database import db

logger = logging.getLogger(__name__)


class ShelfHandler(FileSystemEventHandler):
    """EventHandler that reacts to files leaving the shelf."""

    def __init__(self) -> None:
        super().__init__()
        self._shelf_path = config.shelf_path

    # -- event callbacks ------------------------------------------------------

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Called when a file is deleted from the shelf directory.

        Only acts on ``.epub`` files.  Checks the ``system_state`` table to
        decide if this is a user action or a system action.
        """
        self._handle_removal(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Called when a file is moved/renamed.

        If the file was moved **out of** the shelf directory (or renamed to a
        non-EPUB extension), treat it as a user removal.
        """
        # Only care if it was an EPUB that left the shelf
        src_path = Path(event.src_path)
        if src_path.suffix.lower() != ".epub":
            return

        dest_path = Path(event.dest_path)
        # If destination is still inside the shelf and still .epub, it's a rename
        if dest_path.suffix.lower() == ".epub" and self._is_in_shelf(dest_path):
            logger.debug("EPUB renamed within shelf: %s → %s", src_path.name, dest_path.name)
            return

        # Otherwise, it's effectively a removal
        self._handle_removal(event)

    # -- internal -------------------------------------------------------------

    def _handle_removal(self, event: FileSystemEvent) -> None:
        """Decide whether a file event is user or system action, and respond."""
        file_path = Path(event.src_path)
        filename = file_path.name

        # Only care about EPUB files
        if not filename.lower().endswith(".epub"):
            return

        # The "book identity" is the parent folder name
        dirname = file_path.parent.name if file_path.parent != self._shelf_path else filename

        # Check system_state: is the agent deleting this book?
        agent_deleting = db.get_state("agent_is_deleting")
        if agent_deleting == dirname:
            logger.debug("System deletion detected — ignoring: %s", dirname)
            db.clear_state("agent_is_deleting")
            return

        # User action → record as liked
        logger.info("User removal detected: %s (book: %s)", filename, dirname)

        from readingtime.shelf.manager import shelf_manager

        try:
            shelf_manager.handle_user_removal(dirname)
        except Exception as exc:
            logger.error("Error handling user removal of %s: %s", dirname, exc)

    def _is_in_shelf(self, path: Path) -> bool:
        """Check if *path* is inside the configured shelf directory."""
        try:
            path.resolve().relative_to(self._shelf_path.resolve())
            return True
        except ValueError:
            return False


class ShelfWatcher:
    """Manages the watchdog Observer lifecycle.

    Thin wrapper around :class:`Observer` and :class:`ShelfHandler`.
    """

    def __init__(self) -> None:
        self._observer: Observer | None = None
        self._handler = ShelfHandler()

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def start(self) -> None:
        """Start watching the shelf directory in a background thread."""
        if self.is_running:
            logger.warning("Watcher is already running")
            return

        shelf_path = str(config.shelf_path)
        if not os.path.isdir(shelf_path):
            os.makedirs(shelf_path, exist_ok=True)
            logger.info("Created shelf directory: %s", shelf_path)

        self._observer = Observer()
        self._observer.schedule(self._handler, shelf_path, recursive=True)
        self._observer.start()
        logger.info("Watcher started on %s", shelf_path)

    def stop(self) -> None:
        """Stop the watcher gracefully."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("Watcher stopped")
