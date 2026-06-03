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
