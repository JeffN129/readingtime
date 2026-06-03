"""
Desktop notifications for ReadingTime.

Provides a single ``notify()`` function that works across platforms with
graceful degradation:

1. Try ``winotify`` (Windows-native toast notifications)
2. Try ``plyer`` (cross-platform fallback)
3. Log to console (when no notification library is installed)

**Never raises** — all failures are caught and logged silently.
This is intentional: notifications are cosmetic, not critical.

Usage::

    from readingtime.notifier import notify
    notify("Shelf Refilled", "Added 2 new books.")

Optional dependencies (install separately)::

    pip install winotify       # Windows only, best experience
    pip install plyer          # cross-platform fallback
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-loaded notifier backend
# ---------------------------------------------------------------------------

_NOTIFIER: Optional[Tuple[str, Any]] = None
"""Cached notifier backend: (type_name, implementation).  ``None`` means
   not yet initialised.  ``("none", None)`` means no backend is available."""


def _get_notifier() -> Tuple[str, Any]:
    """Discover the best available notification backend.

    Returns ``(backend_name, implementation)`` where *backend_name* is one of
    ``"winotify"``, ``"plyer"``, or ``"none"``.
    """
    global _NOTIFIER
    if _NOTIFIER is not None:
        return _NOTIFIER

    # -- Windows: prefer winotify for native toast experience ---------------
    if sys.platform == "win32":
        try:
            from winotify import Notification as WinNotification  # type: ignore

            _NOTIFIER = ("winotify", WinNotification)
            logger.debug("Using winotify for desktop notifications")
            return _NOTIFIER
        except ImportError:
            pass

    # -- Cross-platform: plyer ----------------------------------------------
    try:
        from plyer import notification  # type: ignore

        _NOTIFIER = ("plyer", notification)
        logger.debug("Using plyer for desktop notifications")
        return _NOTIFIER
    except ImportError:
        pass

    # -- No backend available -----------------------------------------------
    _NOTIFIER = ("none", None)
    logger.debug("No notification library available — notifications disabled")
    return _NOTIFIER


def reset_notifier() -> None:
    """Reset the cached notifier backend (useful in tests)."""
    global _NOTIFIER
    _NOTIFIER = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify(
    title: str,
    message: str,
    app_name: str = "ReadingTime",
    duration: str = "short",
) -> bool:
    """Show a desktop notification toast.

    Args:
        title: Notification title (1 line recommended).
        message: Notification body (2-3 lines max).
        app_name: Application name shown in the toast header.
        duration: ``"short"`` (default) or ``"long"`` — hint, not a guarantee.

    Returns:
        ``True`` if a notification was actually shown, ``False`` if it
        degraded to a log message.
    """
    backend_type, backend = _get_notifier()

    if backend_type == "none":
        logger.info("[NOTIFICATION] %s: %s", title, message)
        return False

    try:
        if backend_type == "winotify":
            toast = backend(
                app_id=app_name,
                title=title,
                msg=message,
                duration=duration,
            )
            toast.show()
        elif backend_type == "plyer":
            backend.notify(
                title=title,
                message=message,
                app_name=app_name,
                timeout=5 if duration == "short" else 10,
            )
        logger.debug("Notification shown: %s — %s", title, message)
        return True
    except Exception as exc:
        logger.warning("Notification failed (%s): %s", backend_type, exc)
        return False


# ---------------------------------------------------------------------------
# Interactive notification — "Do you like this book?"
# ---------------------------------------------------------------------------

_ASK_MANAGER: "Optional[AskManager]" = None
"""Global interactive-notification manager.  Initialised by the daemon on
startup and torn down on shutdown."""


class AskManager:
    """Manages interactive winotify notifications with callback buttons.

    Uses ``winotify.Notifier`` (named-pipe server) to receive button-click
    callbacks from Windows toast notifications.

    Call :meth:`start` once during daemon initialisation, then use
    :meth:`ask_liked_book` to prompt the user after a shelf deletion.
    """

    def __init__(self) -> None:
        self._notifier: Any = None
        self._pending_asks: dict[str, str] = {}  # filename → book_title

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the winotify Notifier server and register callbacks.

        Must be called from the daemon thread after :func:`notify` has
        confirmed that ``winotify`` is available.
        """
        _, backend = _get_notifier()
        if backend is None:
            logger.debug("AskManager: no winotify backend, skipping")
            return

        try:
            from winotify import Notifier as WinNotifier, Registry  # type: ignore
        except ImportError:
            logger.debug("AskManager: winotify not installed, skipping")
            return

        registry = Registry("ReadingTime")
        self._notifier = WinNotifier(registry)

        @self._notifier.register_callback
        def _on_liked() -> None:  # pragma: no cover
            self._handle_response("liked")

        @self._notifier.register_callback
        def _on_neutral() -> None:  # pragma: no cover
            self._handle_response("neutral")

        self._notifier.start()
        logger.info("AskManager started (interactive notifications ready)")

    def shutdown(self) -> None:
        """Clean up the Notifier (called on daemon shutdown)."""
        if self._notifier is not None:
            try:
                self._notifier.clear()
            except Exception:
                pass
            self._notifier = None
            logger.debug("AskManager shut down")

    # -- public API ----------------------------------------------------------

    def ask_liked_book(self, book_id: int, filename: str, title: str, author: str = "") -> bool:
        """Show an interactive toast asking the user if they liked a book.

        Args:
            book_id: Database ID of the removed book.
            filename: Shelf directory name (used to look up the pending removal).
            title: Book title to display.
            author: Book author (optional).

        Returns:
            ``True`` if the interactive notification was sent, ``False`` if
            it degraded to a log message.
        """
        if self._notifier is None:
            logger.info("[ASK] Liked 《%s》? (no backend — defaulting to liked)", title)
            self._record_signal(book_id, filename, "liked")
            return False

        self._pending_asks[filename] = title

        display_title = f"📖 你读过《{title}》吗？"
        display_msg = "这本书已从书架移除。你喜欢它吗？"
        if author:
            display_msg = f"《{title}》by {author}\n这本书已从书架移除。你喜欢它吗？"

        try:
            from winotify import Notification as WinNotification  # type: ignore
            toast = WinNotification(
                app_id="ReadingTime",
                title=display_title,
                msg=display_msg,
                duration="long",
            )
            toast.add_actions(label="👍 喜欢", launch="readingtime:liked")
            toast.add_actions(label="👎 一般", launch="readingtime:neutral")
            toast.show()
            logger.info("Interactive ask sent for 《%s》 (book_id=%d)", title, book_id)
            return True
        except Exception as exc:
            logger.warning("Interactive ask failed for 《%s》: %s — defaulting to liked", title, exc)
            self._record_signal(book_id, filename, "liked")
            return False

    # -- internals -----------------------------------------------------------

    def _handle_response(self, signal: str) -> None:
        """Called when the user clicks a button in the interactive toast."""
        try:
            from readingtime.database import db
            db.init_db()

            # Find the most recent pending removal
            pending_list = db.get_all_pending_removals()
            if not pending_list:
                logger.debug("AskManager: no pending removal to resolve")
                return

            pending = pending_list[0]  # most recent
            filename = pending.get("filename", "")
            book_id = pending.get("book_id")
            title = pending.get("title", filename)

            self._record_signal(book_id, filename, signal)

            # The pending removal will be finalised by _process_pending_removals
            # when its grace period expires — but since we already recorded the
            # signal, clear the pending entry now.
            db.delete_pending_removal(filename)

            # Log to activity
            from readingtime.shelf.activity_log import log_activity
            from readingtime.config import config
            shelf_path = config.shelf_path
            action = "❤️ 已确认" if signal == "liked" else "👎 一般"
            log_activity(shelf_path, action, title, pending.get("author", ""),
                        "用户喜欢" if signal == "liked" else "用户标记为一般")

            # Notify user of result
            if signal == "liked":
                notify("👍 已记录", f"你喜欢《{title}》— 系统会推荐更多类似书籍")
            else:
                notify("👎 已记录", f"《{title}》标记为一般 — 系统不会推荐类似书籍")

        except Exception as exc:
            logger.error("AskManager._handle_response error: %s", exc)

    def _record_signal(self, book_id: int, filename: str, signal: str) -> None:
        """Record a behavioural signal for the book."""
        try:
            from readingtime.database import db
            db.init_db()
            db.record_signal(book_id, signal)
            logger.info("AskManager: recorded '%s' signal for book_id=%d (%s)", signal, book_id, filename)
        except Exception as exc:
            logger.error("AskManager._record_signal error: %s", exc)


# -- module-level helpers ----------------------------------------------------


def init_ask_manager() -> None:
    """Initialise the global interactive-notification manager.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _ASK_MANAGER
    if _ASK_MANAGER is not None:
        return
    _ASK_MANAGER = AskManager()
    _ASK_MANAGER.start()


def ask_liked_book(book_id: int, filename: str, title: str, author: str = "") -> bool:
    """Ask the user via interactive toast whether they liked a book.

    Falls back to a plain :func:`notify` if the interactive backend is
    unavailable.
    """
    if _ASK_MANAGER is not None:
        return _ASK_MANAGER.ask_liked_book(book_id, filename, title, author)

    # Fallback: plain notification + default to liked
    notify("📖 已读完？", f"《{title}》已移除 — 5分钟内可运行 readingtime undo 恢复")
    from readingtime.database import db
    db.init_db()
    db.record_signal(book_id, "liked")
    return False


def shutdown_ask_manager() -> None:
    """Shut down the interactive-notification manager."""
    global _ASK_MANAGER
    if _ASK_MANAGER is not None:
        _ASK_MANAGER.shutdown()
        _ASK_MANAGER = None
