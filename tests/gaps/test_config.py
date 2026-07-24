"""Tests for idea_forge.config.Settings: Gap Detection (Anthropic) fields.

Covers spec acceptance criterion 16 (specs/2026-07-24-gap-detection.md §5).
"""

from pathlib import Path

from idea_forge.config import Settings

REQUIRED_REDDIT_ENV = {
    "REDDIT_CLIENT_ID": "abc123",
    "REDDIT_CLIENT_SECRET": "shh-secret",
    "REDDIT_USER_AGENT": "idea-forge/0.1 by u/someone",
}


def _write_env(tmp_path: Path, extra: str = "") -> Path:
    env_file = tmp_path / ".env"
    lines = "\n".join(f"{k}={v}" for k, v in REQUIRED_REDDIT_ENV.items())
    env_file.write_text(lines + "\n" + extra, encoding="utf-8")
    return env_file


def _delenv_gap_vars(monkeypatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "GAP_MODEL",
        "GAP_MAX_DOCS_PER_CALL",
        "GAP_REQUEST_TIMEOUT_SECONDS",
        "GAP_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(var, raising=False)


# --- Criterion 16: loads from env + working defaults --------------------------


def test_loads_all_four_gap_fields_from_env_file(tmp_path, monkeypatch):
    _delenv_gap_vars(monkeypatch)
    env_file = _write_env(
        tmp_path,
        "ANTHROPIC_API_KEY=sk-ant-test\n"
        "GAP_MODEL=claude-opus-4-8-custom\n"
        "GAP_MAX_DOCS_PER_CALL=25\n"
        "GAP_REQUEST_TIMEOUT_SECONDS=60.0\n",
    )
    settings = Settings(_env_file=env_file)

    assert settings.anthropic_api_key == "sk-ant-test"
    assert settings.gap_model == "claude-opus-4-8-custom"
    assert settings.gap_max_docs_per_call == 25
    assert settings.gap_request_timeout_seconds == 60.0


def test_gap_fields_default_when_absent(tmp_path, monkeypatch):
    _delenv_gap_vars(monkeypatch)
    env_file = _write_env(tmp_path)
    settings = Settings(_env_file=env_file)

    assert settings.anthropic_api_key == ""
    assert settings.gap_model == "claude-opus-4-8"
    assert settings.gap_max_docs_per_call == 50
    assert settings.gap_request_timeout_seconds == 120.0
    assert settings.gap_max_output_tokens == 16000


def test_gap_max_output_tokens_loads_from_env(tmp_path, monkeypatch):
    _delenv_gap_vars(monkeypatch)
    env_file = _write_env(tmp_path, "GAP_MAX_OUTPUT_TOKENS=8000\n")
    settings = Settings(_env_file=env_file)

    assert settings.gap_max_output_tokens == 8000
