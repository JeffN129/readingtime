"""Tests for readingtime.agent.profiler — user preference learning."""

import os
import tempfile

import pytest

os.environ["READINGTIME_DB"] = os.path.join(tempfile.gettempdir(), "test_profiler.db")
os.environ["READINGTIME_CONFIG"] = os.path.join(tempfile.gettempdir(), "test_profiler_config.yaml")

import yaml

from readingtime.config import config
from readingtime.database import db


@pytest.fixture(autouse=True)
def setup_env(tmp_path):
    """Fresh config + DB for each test."""
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
    config_path = tmp_path / "test_profiler_config.yaml"
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


class TestProfiler:
    def test_extract_features_fallback(self, setup_env):
        """extract_features should return a dict even without LLM."""
        from readingtime.agent.profiler import Profiler

        p = Profiler()
        book = {
            "title": "Test Book",
            "author": "Test Author",
            "tags": ["fiction", "mystery"],
            "description": "A test book description.",
        }
        features = p.extract_features(book)
        assert isinstance(features, dict)
        assert "tags" in features
        assert "era" in features

    def test_update_profile_liked(self, setup_env):
        """update_profile with 'liked' should add tags and authors."""
        from readingtime.agent.profiler import Profiler

        p = Profiler()
        features = {
            "tags": ["fiction", "mystery"],
            "author": "Test Author",
            "era": "modern",
            "style": "literary",
            "themes": ["justice"],
            "audience": "adult",
        }
        p.update_profile("liked", features)

        profile = db.get_profile()
        assert profile is not None
        assert "fiction" in [t.lower() for t in profile["liked_tags"]]
        assert "mystery" in [t.lower() for t in profile["liked_tags"]]
        assert "Test Author" in profile["liked_authors"]

    def test_update_profile_neutral(self, setup_env):
        """update_profile with 'neutral' should add to neutral_tags."""
        from readingtime.agent.profiler import Profiler

        p = Profiler()
        p.update_profile("neutral", {"tags": ["romance"], "author": "Romance Author"})

        profile = db.get_profile()
        assert profile is not None
        assert "romance" in [t.lower() for t in profile["neutral_tags"]]

    def test_update_profile_liked_removes_neutral(self, setup_env):
        """Liking a tag that was neutral should remove it from neutral."""
        from readingtime.agent.profiler import Profiler

        p = Profiler()
        p.update_profile("neutral", {"tags": ["romance"], "author": ""})
        p.update_profile("liked", {"tags": ["romance"], "author": ""})

        profile = db.get_profile()
        assert "romance" in [t.lower() for t in profile["liked_tags"]]
        assert "romance" not in [t.lower() for t in profile["neutral_tags"]]

    def test_get_profile(self, setup_env):
        from readingtime.agent.profiler import Profiler

        # No profile initially
        assert Profiler.get_profile() is None

        # Create one
        db.upsert_profile(liked_tags=["fiction"], liked_authors=["Kafka"])
        profile = Profiler.get_profile()
        assert profile is not None
        assert "fiction" in profile["liked_tags"]

    def test_update_profile_duplicate_tags(self, setup_env):
        """Duplicate tags should not appear multiple times."""
        from readingtime.agent.profiler import Profiler

        p = Profiler()
        p.update_profile("liked", {"tags": ["fiction"], "author": ""})
        p.update_profile("liked", {"tags": ["fiction"], "author": ""})

        profile = db.get_profile()
        # Count occurrences (case-insensitive)
        fiction_count = sum(1 for t in profile["liked_tags"] if t.lower() == "fiction")
        assert fiction_count == 1


class TestPrompts:
    def test_prompts_are_non_empty(self):
        from readingtime.agent import prompts

        assert len(prompts.QUERY_GENERATION_PROMPT) > 0
        assert len(prompts.SCORING_PROMPT) > 0
        assert len(prompts.SUMMARY_PROMPT) > 0
        assert len(prompts.FEATURE_EXTRACTION_PROMPT) > 0

    def test_prompts_have_format_placeholders(self):
        from readingtime.agent import prompts

        # All prompts should have format placeholders
        assert "{" in prompts.QUERY_GENERATION_PROMPT
        assert "{" in prompts.SCORING_PROMPT
        assert "{" in prompts.SUMMARY_PROMPT
        assert "{" in prompts.FEATURE_EXTRACTION_PROMPT

    def test_query_prompt_formats(self):
        from readingtime.agent import prompts

        result = prompts.QUERY_GENERATION_PROMPT.format(
            liked_tags="fiction",
            liked_authors="Kafka",
            neutral_tags="romance",
            lang_pref="en",
        )
        assert "fiction" in result
        assert "Kafka" in result

    def test_scoring_prompt_formats(self):
        from readingtime.agent import prompts

        result = prompts.SCORING_PROMPT.format(
            liked_tags="fiction",
            liked_authors="Kafka",
            neutral_tags="romance",
            lang_pref="en",
            candidates_text="[g:1] Test Book by Author — tags: fiction",
        )
        assert "Test Book" in result

    def test_summary_prompt_formats(self):
        from readingtime.agent import prompts

        result = prompts.SUMMARY_PROMPT.format(
            title="Test Book",
            author="Author",
            language="en",
            tags="fiction",
            liked_tags="fiction",
            summary_lang="English",
            excerpt="Once upon a time...",
        )
        assert "Test Book" in result
        assert "Once upon a time" in result
