"""Tests for idea_forge.config.Settings.

Covers spec acceptance criterion 16 (specs/2026-07-24-reddit-ingestion-adapter.md §5).
"""

from pathlib import Path

import pytest
from idea_forge.config import Settings
from pydantic import ValidationError

REQUIRED_ENV = {
    "REDDIT_CLIENT_ID": "abc123",
    "REDDIT_CLIENT_SECRET": "shh-secret",
    "REDDIT_USER_AGENT": "idea-forge/0.1 by u/someone",
}


def _write_env(tmp_path: Path, content: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


def test_loads_all_three_required_reddit_creds_from_env_file(tmp_path):
    env_file = _write_env(
        tmp_path,
        "REDDIT_CLIENT_ID=abc123\n"
        "REDDIT_CLIENT_SECRET=shh-secret\n"
        "REDDIT_USER_AGENT=idea-forge/0.1 by u/someone\n",
    )
    settings = Settings(_env_file=env_file)

    assert settings.reddit_client_id == "abc123"
    assert settings.reddit_client_secret == "shh-secret"
    assert settings.reddit_user_agent == "idea-forge/0.1 by u/someone"


@pytest.mark.parametrize(
    "missing_key",
    ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"],
)
def test_missing_required_cred_raises_validation_error(tmp_path, monkeypatch, missing_key):
    env_vars = {k: v for k, v in REQUIRED_ENV.items() if k != missing_key}
    lines = "\n".join(f"{k}={v}" for k, v in env_vars.items())
    env_file = _write_env(tmp_path, lines + "\n")

    # Ensure ambient environment variables (e.g. from the dev shell/CI) don't
    # accidentally supply the missing key and mask the failure.
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_reddit_subreddits_parses_comma_separated_string(tmp_path):
    env_file = _write_env(
        tmp_path,
        "REDDIT_CLIENT_ID=abc123\n"
        "REDDIT_CLIENT_SECRET=shh-secret\n"
        "REDDIT_USER_AGENT=idea-forge/0.1 by u/someone\n"
        'REDDIT_SUBREDDITS="a, b,c"\n',
    )
    settings = Settings(_env_file=env_file)

    assert settings.reddit_subreddits == ["a", "b", "c"]


def test_reddit_subreddits_defaults_when_not_set(tmp_path):
    env_file = _write_env(tmp_path, "\n".join(f"{k}={v}" for k, v in REQUIRED_ENV.items()) + "\n")
    settings = Settings(_env_file=env_file)

    assert settings.reddit_subreddits == ["SomebodyMakeThis"]


def test_reddit_subreddits_strips_whitespace_and_drops_empty_entries(tmp_path):
    env_file = _write_env(
        tmp_path,
        "\n".join(f"{k}={v}" for k, v in REQUIRED_ENV.items())
        + "\n"
        + 'REDDIT_SUBREDDITS=" a ,, b ,c"\n',
    )
    settings = Settings(_env_file=env_file)

    assert settings.reddit_subreddits == ["a", "b", "c"]


def test_default_values_for_optional_settings(tmp_path):
    env_file = _write_env(tmp_path, "\n".join(f"{k}={v}" for k, v in REQUIRED_ENV.items()) + "\n")
    settings = Settings(_env_file=env_file)

    assert settings.reddit_listing == "new"
    assert settings.reddit_limit_per_subreddit == 100
    assert settings.reddit_page_size == 100
    assert settings.reddit_top_time_filter == "week"
    assert settings.request_timeout_seconds == 10.0
    assert settings.max_retries == 3


def test_get_settings_returns_cached_instance(tmp_path, monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    from idea_forge.config import get_settings

    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()

    assert first is second

    get_settings.cache_clear()
