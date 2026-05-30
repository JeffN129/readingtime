"""
Configuration loading and validation module.

Loads settings from two sources:
  1. config.yaml  — user-editable YAML (paths, preferences, source priority)
  2. .env         — secrets (API keys, tokens), never committed to git

Provides a global Config singleton — import `config` and use it anywhere.
All parameters are read from config; nothing is hardcoded.

Usage:
    from readingtime.config import config
    shelf_path = config.shelf_path
"""

import os
import sys
import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default configuration values (used when config.yaml doesn't exist yet)
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = {
    "shelf": {
        "path": "~/Books/ReadingTime",
        "size": 10,
        "book_lifetime_days": 30,
        "language": "en",
    },
    "llm": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "max_tokens": 1000,
    },
    "sources": {
        "priority": ["kgbook"],
        "kgbook": {
            "enabled": True,
        },
    },
    "logging": {
        "level": "INFO",
        "file": "~/.readingtime/logs/agent.log",
    },
}

# Path to the project root (where CLAUDE.md lives)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Global configuration singleton.

    Loads config.yaml + .env on first access.  All modules import the
    module-level ``config`` instance — no need to create your own.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._loaded = False

    # -- public API ----------------------------------------------------------

    @property
    def shelf_path(self) -> Path:
        """Expanded, absolute path to the bookshelf directory."""
        return Path(self.get("shelf", "path")).expanduser().resolve()

    @property
    def shelf_size(self) -> int:
        return int(self.get("shelf", "size"))

    @property
    def book_lifetime_days(self) -> int:
        return int(self.get("shelf", "book_lifetime_days"))

    @property
    def language(self) -> str:
        return str(self.get("shelf", "language"))

    @property
    def llm_provider(self) -> str:
        return str(self.get("llm", "provider"))

    @property
    def llm_model(self) -> str:
        return str(self.get("llm", "model"))

    @property
    def llm_base_url(self) -> str:
        return str(self.get("llm", "base_url"))

    @property
    def llm_max_tokens(self) -> int:
        return int(self.get("llm", "max_tokens"))

    @property
    def source_priority(self) -> list[str]:
        return list(self.get("sources", "priority"))

    @property
    def log_level(self) -> str:
        return str(self.get("logging", "level"))

    @property
    def log_file(self) -> Path:
        return Path(self.get("logging", "file")).expanduser().resolve()

    # -- low-level access ----------------------------------------------------

    def get(self, *keys: str) -> Any:
        """Deep read from the config dict: ``config.get("shelf", "size")``."""
        self._ensure_loaded()
        node: Any = self._data
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k, {})
            else:
                return None
        return node

    def to_dict(self) -> dict[str, Any]:
        """Return the full merged config as a plain dict (for debugging)."""
        self._ensure_loaded()
        return dict(self._data)

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, force: bool = False) -> None:
        """Generate config.yaml if it does not exist, then load everything.

        Called by ``readingtime init``.  Idempotent — safe to call multiple
        times.
        """
        config_path = self._config_path()
        if not config_path.exists() or force:
            self._write_default_config(config_path)

        self._load_dotenv()
        self._load_yaml(config_path)
        self._validate()
        self._loaded = True
        logger.info("Configuration loaded successfully")

    def reload(self) -> None:
        """Force re-read of config.yaml + .env (useful after manual edits)."""
        self._loaded = False
        self.initialize()

    # -- internal helpers ----------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.initialize()

    def _config_path(self) -> Path:
        """Return the path to config.yaml.

        Uses READINGTIME_CONFIG env-var override if set; otherwise looks in
        the project root.
        """
        if env_path := os.getenv("READINGTIME_CONFIG"):
            return Path(env_path)
        return _PROJECT_ROOT / "config.yaml"

    def _load_dotenv(self) -> None:
        """Load .env from the project root."""
        env_path = _PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.debug("Loaded .env from %s", env_path)
        else:
            logger.warning(".env not found at %s — API calls will fail", env_path)

    def _load_yaml(self, path: Path) -> None:
        """Merge YAML file into self._data, filling gaps with defaults."""
        with open(path, "r", encoding="utf-8") as fh:
            user_config = yaml.safe_load(fh) or {}

        # Deep-merge user config over defaults
        self._data = _deep_merge(_DEFAULT_CONFIG, user_config)

    def _write_default_config(self, path: Path) -> None:
        """Write a fresh config.yaml with default values."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(_DEFAULT_CONFIG, fh, default_flow_style=False, allow_unicode=True)
        logger.info("Default config.yaml written to %s", path)

    def _validate(self) -> None:
        """Validate required configuration and abort early if something is
        missing or misconfigured.

        IMPORTANT: Access ``self._data`` directly here — do NOT use properties,
        because property access triggers ``_ensure_loaded()``, which calls
        ``_validate()``, causing infinite recursion.
        """
        errors: list[str] = []

        # --- API key --------------------------------------------------------
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            errors.append(
                "DEEPSEEK_API_KEY is not set in .env — "
                "get yours at https://platform.deepseek.com/api_keys"
            )

        # --- Shelf path -----------------------------------------------------
        raw_path = self._data.get("shelf", {}).get("path", "")
        try:
            sp = Path(raw_path).expanduser().resolve()
        except Exception as exc:
            errors.append(f"Cannot resolve shelf.path: {exc}")
        else:
            try:
                sp.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                errors.append(
                    f"No write permission for shelf path: {sp}\n"
                    "Please update config.yaml → shelf.path to a writable location."
                )

        if errors:
            msg = "\n\n".join(f"  • {e}" for e in errors)
            sys.exit(f"Configuration error(s):\n{msg}")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    merged = base.copy()
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


# ---------------------------------------------------------------------------
# Module-level singleton — the one place everyone reads config from
# ---------------------------------------------------------------------------
config = Config()
