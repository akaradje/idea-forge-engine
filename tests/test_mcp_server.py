"""Tests for idea_forge.mcp_server (MCP stdio server exposing ingestion adapters).

Covers spec acceptance criteria 1-14 (specs/2026-07-24-mcp-server.md §5) and
criteria 1-15 for the product tools check_novelty/detect_gaps
(specs/2026-07-24-mcp-product-tools.md §5).
No real MCP client, no live network — adapters/detector are monkeypatched with
fakes that capture the Settings they were constructed with, or fail via
recorded exceptions where the real classes are exercised end-to-end is not
needed (thin transport layer over already-tested adapters/detector).
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import idea_forge.mcp_server as mcp_server
from idea_forge.gaps.errors import GapDetectionError
from idea_forge.gaps.models import GapCandidate, GapEvidence
from idea_forge.ingestion.base import RawDocument
from idea_forge.ingestion.errors import AuthError, IngestionError, RateLimitError
from idea_forge.mcp_server import (
    _ANTHROPIC_MISSING_MSG,
    MAX_BODY_CHARS,
    MAX_NOVELTY_RESULTS,
    NOVELTY_QUERY_MAX_CHARS,
    _base_settings,
    _check_novelty_impl,
    _detect_gaps_impl,
    _fetch_hackernews_impl,
    _fetch_reddit_impl,
    _serialize,
    check_novelty,
    detect_gaps,
)

# --- helpers ----------------------------------------------------------------


def make_doc(
    *,
    source: str = "hackernews",
    source_id: str = "1",
    title: str = "t",
    body: str = "b",
    url: str = "https://example.com",
    author: str | None = "someone",
    metadata: dict | None = None,
) -> RawDocument:
    now = datetime.now(UTC)
    return RawDocument(
        source=source,
        source_id=source_id,
        title=title,
        body=body,
        url=url,
        author=author,
        created_at=now,
        fetched_at=now,
        metadata=metadata or {},
    )


class FakeAdapter:
    """Stand-in for HackerNewsAdapter/RedditAdapter: captures constructor Settings,
    yields a fixed list of docs (or raises) from fetch(), tracks HTTP-call attempts.
    """

    #: class-level toggle so a test can make construction itself raise (e.g. RedditAdapter's
    #: ValueError for a bad `listing`).
    raise_on_init: type[Exception] | None = None
    #: class-level toggle so a test can make fetch() raise mid-stream.
    raise_on_fetch: type[Exception] | None = None
    docs: tuple[RawDocument, ...] = ()

    last_settings: "Settings | None" = None  # type: ignore[name-defined]  # noqa: F821
    constructed = False
    http_called = False

    def __init__(self, settings, client=None) -> None:
        type(self).last_settings = settings
        type(self).constructed = True
        if self.raise_on_init is not None:
            raise self.raise_on_init("bad init")

    async def __aenter__(self) -> "FakeAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetch(self) -> AsyncIterator[RawDocument]:
        type(self).http_called = True
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch("boom")
        for doc in self.docs:
            yield doc


def reset_fake(fake_cls: type[FakeAdapter], **kwargs) -> None:
    fake_cls.raise_on_init = kwargs.get("raise_on_init")
    fake_cls.raise_on_fetch = kwargs.get("raise_on_fetch")
    fake_cls.docs = kwargs.get("docs", [])
    fake_cls.last_settings = None
    fake_cls.constructed = False
    fake_cls.http_called = False


class FakeGapDetector:
    """Stand-in for gaps.detector.GapDetector: captures constructor Settings,
    `detect()` is an AsyncMock returning a preset GapCandidate list or raising.
    """

    last_settings: "Settings | None" = None  # type: ignore[name-defined]  # noqa: F821
    constructed = False
    #: class-level so a test can set the return value / side effect before construction.
    detect_return: "list[GapCandidate]" = ()  # type: ignore[assignment]
    detect_side_effect: Exception | None = None

    def __init__(self, settings, client=None) -> None:
        type(self).last_settings = settings
        type(self).constructed = True
        if self.detect_side_effect is not None:
            self.detect = AsyncMock(side_effect=self.detect_side_effect)
        else:
            self.detect = AsyncMock(return_value=self.detect_return)


def reset_fake_gap_detector(**kwargs) -> None:
    FakeGapDetector.last_settings = None
    FakeGapDetector.constructed = False
    FakeGapDetector.detect_return = kwargs.get("detect_return", [])
    FakeGapDetector.detect_side_effect = kwargs.get("detect_side_effect")


def make_gap(problem: str = "problem", unique_key: str = "hackernews:1") -> GapCandidate:
    return GapCandidate(
        problem=problem,
        evidence=[GapEvidence(unique_key=unique_key, quote="quote")],
        demand_signal="lots of complaints",
        supply_signal="none observed",
        confidence=0.8,
    )


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """No real .env / REDDIT_* / ANTHROPIC_* env vars leak into these tests."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
        "REDDIT_SUBREDDITS",
        "REDDIT_LISTING",
        "REDDIT_LIMIT_PER_SUBREDDIT",
        "HN_TAGS",
        "HN_QUERY",
        "HN_LIMIT_PER_TAG",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_fakes():
    reset_fake(FakeAdapter)
    reset_fake_gap_detector()
    yield
    reset_fake(FakeAdapter)
    reset_fake_gap_detector()


# --- Criterion 1: HN impl returns {"documents": [...], "count": 3} ----------


async def test_fetch_hackernews_impl_returns_json_with_docs_and_count(monkeypatch):
    docs = [make_doc(source="hackernews", source_id=f"{i}", title=f"Post {i}") for i in range(3)]
    reset_fake(FakeAdapter, docs=docs)
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _fetch_hackernews_impl()

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["count"] == 3
    assert len(payload["documents"]) == 3
    for doc in payload["documents"]:
        assert set(["source", "source_id", "title", "url", "created_at"]).issubset(doc.keys())
        assert isinstance(doc["created_at"], str)


# --- Criterion 2: fetch_hackernews works with no REDDIT_* env, no .env ------


async def test_fetch_hackernews_succeeds_with_no_reddit_creds(monkeypatch):
    reset_fake(FakeAdapter, docs=[make_doc()])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _fetch_hackernews_impl()

    payload = json.loads(result)
    assert payload["count"] == 1
    assert not result.startswith("Error:")


def test_base_settings_require_reddit_false_fills_placeholders():
    settings = _base_settings(require_reddit=False)
    assert settings.reddit_client_id
    assert settings.reddit_client_secret
    assert settings.reddit_user_agent


def test_base_settings_require_reddit_true_raises_without_creds():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _base_settings(require_reddit=True)


# --- Criterion 3: per-call overrides propagate into constructed Settings ----


async def test_hackernews_overrides_propagate_to_constructed_settings(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    await _fetch_hackernews_impl(tags="ask_hn", query="agent", limit_per_tag=5)

    settings = FakeAdapter.last_settings
    assert settings is not None
    assert settings.hn_tags == ["ask_hn"]
    assert settings.hn_query == "agent"
    assert settings.hn_limit_per_tag == 5


# --- Criterion 4: missing reddit creds -> friendly error, no adapter/HTTP ---


async def test_reddit_impl_missing_creds_returns_friendly_error_no_adapter_call(monkeypatch):
    reset_fake(FakeAdapter)
    monkeypatch.setattr(mcp_server, "RedditAdapter", FakeAdapter)

    result = await _fetch_reddit_impl()

    assert result.startswith("Error:")
    assert "REDDIT_CLIENT_ID" in result
    assert FakeAdapter.constructed is False
    assert FakeAdapter.http_called is False


# --- Criterion 5: reddit impl with valid creds + mocked adapter returns docs -


async def test_reddit_impl_with_valid_creds_returns_documents(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "idea-forge-tests/0.1")
    docs = [make_doc(source="reddit", source_id="a", title="hello")]
    reset_fake(FakeAdapter, docs=docs)
    monkeypatch.setattr(mcp_server, "RedditAdapter", FakeAdapter)

    result = await _fetch_reddit_impl()

    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["documents"][0]["title"] == "hello"


# --- Criterion 6: subreddits="" keeps default, "a, b" overrides -------------


async def test_reddit_empty_subreddits_keeps_configured_default(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "idea-forge-tests/0.1")
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "RedditAdapter", FakeAdapter)

    await _fetch_reddit_impl(subreddits="")

    default_settings = _base_settings(require_reddit=True)
    assert FakeAdapter.last_settings.reddit_subreddits == default_settings.reddit_subreddits


async def test_reddit_nonempty_subreddits_overrides_default(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "idea-forge-tests/0.1")
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "RedditAdapter", FakeAdapter)

    await _fetch_reddit_impl(subreddits="a, b")

    assert FakeAdapter.last_settings.reddit_subreddits == ["a", "b"]


# --- Criterion 7: invalid listing -> friendly error, no raise ---------------


async def test_reddit_invalid_listing_returns_friendly_error(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "idea-forge-tests/0.1")

    class RaisingOnInit(FakeAdapter):
        def __init__(self, settings, client=None) -> None:
            type(self).last_settings = settings
            type(self).constructed = True
            raise ValueError(f"unknown reddit_listing: {settings.reddit_listing!r}")

    monkeypatch.setattr(mcp_server, "RedditAdapter", RaisingOnInit)

    result = await _fetch_reddit_impl(listing="hot")

    assert result.startswith("Error:")
    assert "listing" in result
    assert "hot" in result


# --- Criterion 8: RateLimitError during fetch -> "Error: RateLimitError..." -


async def test_hackernews_rate_limit_error_mapped_to_error_string(monkeypatch):
    reset_fake(FakeAdapter, raise_on_fetch=RateLimitError)
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _fetch_hackernews_impl()

    assert result.startswith("Error:")
    assert "RateLimitError" in result


async def test_hackernews_generic_ingestion_error_does_not_raise(monkeypatch):
    reset_fake(FakeAdapter, raise_on_fetch=IngestionError)
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _fetch_hackernews_impl()

    assert result.startswith("Error:")
    assert "IngestionError" in result


# --- Criterion 9: AuthError during reddit fetch -> friendly cred error ------


async def test_reddit_auth_error_during_fetch_maps_to_friendly_credentials_error(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "idea-forge-tests/0.1")
    reset_fake(FakeAdapter, raise_on_fetch=AuthError)
    monkeypatch.setattr(mcp_server, "RedditAdapter", FakeAdapter)

    result = await _fetch_reddit_impl()

    assert result.startswith("Error:")
    assert "REDDIT_CLIENT_ID" in result or "credentials" in result.lower()


# --- Criterion 10: body truncation -------------------------------------------


def test_serialize_truncates_long_body_and_records_metadata():
    long_body = "x" * (MAX_BODY_CHARS + 500)
    doc = make_doc(body=long_body)

    dumped = _serialize([doc])[0]

    assert len(dumped["body"]) == MAX_BODY_CHARS
    assert dumped["metadata"]["body_truncated"] is True
    assert dumped["metadata"]["body_original_length"] == len(long_body)
    # original document is untouched
    assert doc.body == long_body
    assert "body_truncated" not in doc.metadata


def test_serialize_short_body_unchanged_no_truncation_flag():
    short_body = "short body"
    doc = make_doc(body=short_body)

    dumped = _serialize([doc])[0]

    assert dumped["body"] == short_body
    assert "body_truncated" not in dumped["metadata"]
    assert "body_original_length" not in dumped["metadata"]


# --- Criterion 11: limit clamping --------------------------------------------


async def test_hackernews_limit_clamped_above_max(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    await _fetch_hackernews_impl(limit_per_tag=10000)

    assert FakeAdapter.last_settings.hn_limit_per_tag == 100


async def test_hackernews_limit_clamped_below_min_zero(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    await _fetch_hackernews_impl(limit_per_tag=0)

    assert FakeAdapter.last_settings.hn_limit_per_tag == 1


async def test_hackernews_limit_clamped_below_min_negative(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    await _fetch_hackernews_impl(limit_per_tag=-5)

    assert FakeAdapter.last_settings.hn_limit_per_tag == 1


async def test_reddit_limit_clamped_above_max(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "idea-forge-tests/0.1")
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "RedditAdapter", FakeAdapter)

    await _fetch_reddit_impl(limit_per_subreddit=10000)

    assert FakeAdapter.last_settings.reddit_limit_per_subreddit == 100


# --- Criterion 12: zero documents -> valid empty JSON ------------------------


async def test_hackernews_zero_documents_returns_empty_but_valid_json(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _fetch_hackernews_impl()

    payload = json.loads(result)
    assert payload == {"documents": [], "count": 0}


async def test_reddit_zero_documents_returns_empty_but_valid_json(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "idea-forge-tests/0.1")
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "RedditAdapter", FakeAdapter)

    result = await _fetch_reddit_impl()

    payload = json.loads(result)
    assert payload == {"documents": [], "count": 0}


# --- Criterion 13: module exposes the documented public interface -----------


def test_module_exposes_mcp_and_tool_functions_and_main():
    from mcp.server.fastmcp import FastMCP

    assert isinstance(mcp_server.mcp, FastMCP)
    assert callable(mcp_server.fetch_hackernews)
    assert callable(mcp_server.fetch_reddit)
    assert callable(mcp_server.main)


def test_pyproject_scripts_maps_idea_forge_mcp_to_main():
    import pathlib
    import tomllib

    pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    assert scripts.get("idea-forge-mcp") == "idea_forge.mcp_server:main"


# --- Criterion 14: logging to stderr only, nothing to stdout ----------------


def test_main_configures_stderr_logging_and_writes_nothing_to_stdout(monkeypatch, capsys):

    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport=None: None)

    # avoid polluting the real root logger's handler list across test runs
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)

    try:
        mcp_server.main()
    finally:
        root_logger.handlers = original_handlers

    captured = capsys.readouterr()
    assert captured.out == ""


def test_configure_logging_attaches_stderr_stream_handler():
    import sys

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    try:
        mcp_server._configure_logging()
        stream_handlers = [h for h in root_logger.handlers if isinstance(h, logging.StreamHandler)]
        assert any(h.stream is sys.stderr for h in stream_handlers)
    finally:
        root_logger.handlers = original_handlers


# =============================================================================
# Product tools: check_novelty + detect_gaps
# (specs/2026-07-24-mcp-product-tools.md §5, acceptance criteria 1-15)
# =============================================================================


# --- Criterion 1: check_novelty impl returns structured JSON evidence -------


async def test_check_novelty_impl_returns_json_with_prior_art_and_note(monkeypatch):
    docs = [
        make_doc(
            source="hackernews",
            source_id=f"{i}",
            title=f"Collab editor {i}",
            metadata={"points": 10 + i, "num_comments": i},
        )
        for i in range(3)
    ]
    reset_fake(FakeAdapter, docs=docs)
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _check_novelty_impl("realtime collab editor", max_results=3)

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["query"] == "realtime collab editor"
    assert payload["count"] == 3
    assert len(payload["prior_art"]) == 3
    for entry in payload["prior_art"]:
        assert set(entry.keys()) == {
            "unique_key",
            "title",
            "url",
            "points",
            "num_comments",
            "created_at",
        }
    assert payload["note"] == (
        "Judge similarity yourself — this tool returns evidence, not verdicts."
    )


# --- Criterion 2: check_novelty tool sets expected Settings overrides -------


async def test_check_novelty_sets_hn_overrides_on_constructed_settings(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    await check_novelty("realtime collab editor", max_results=3)

    settings = FakeAdapter.last_settings
    assert settings is not None
    assert settings.hn_tags == ["story"]
    assert settings.hn_query == "realtime collab editor"
    assert settings.hn_limit_per_tag == 3


# --- Criterion 3: max_results clamps -----------------------------------------


async def test_check_novelty_max_results_clamped_above_ceiling(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    await _check_novelty_impl("idea", max_results=1000)

    assert FakeAdapter.last_settings.hn_limit_per_tag == MAX_NOVELTY_RESULTS
    assert MAX_NOVELTY_RESULTS == 20


async def test_check_novelty_max_results_clamped_below_min(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    await _check_novelty_impl("idea", max_results=0)

    assert FakeAdapter.last_settings.hn_limit_per_tag == 1


# --- Criterion 4: idea truncated to NOVELTY_QUERY_MAX_CHARS -----------------


async def test_check_novelty_idea_truncated_to_max_chars_in_query_and_hn_query(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    long_idea = "x" * (NOVELTY_QUERY_MAX_CHARS + 100)

    result = await _check_novelty_impl(long_idea)

    payload = json.loads(result)
    assert len(payload["query"]) == NOVELTY_QUERY_MAX_CHARS
    assert FakeAdapter.last_settings.hn_query == "x" * NOVELTY_QUERY_MAX_CHARS


# --- Criterion 5: blank idea -> friendly error, no adapter constructed -----


async def test_check_novelty_blank_idea_returns_error_no_adapter_constructed(monkeypatch):
    reset_fake(FakeAdapter)
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _check_novelty_impl("   ")

    assert result.startswith("Error:")
    assert "idea" in result
    assert FakeAdapter.constructed is False


# --- Criterion 6: points/num_comments present vs absent --------------------


async def test_check_novelty_maps_points_and_comments_when_present(monkeypatch):
    doc = make_doc(metadata={"points": 42, "num_comments": 7})
    reset_fake(FakeAdapter, docs=[doc])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _check_novelty_impl("idea")

    entry = json.loads(result)["prior_art"][0]
    assert entry["points"] == 42
    assert entry["num_comments"] == 7


async def test_check_novelty_maps_null_points_and_comments_when_absent(monkeypatch):
    doc = make_doc(metadata={})
    reset_fake(FakeAdapter, docs=[doc])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _check_novelty_impl("idea")

    entry = json.loads(result)["prior_art"][0]
    assert entry["points"] is None
    assert entry["num_comments"] is None


# --- Criterion 7: zero docs -> valid empty result, not an error ------------


async def test_check_novelty_zero_docs_returns_valid_empty_result(monkeypatch):
    reset_fake(FakeAdapter, docs=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _check_novelty_impl("idea")

    payload = json.loads(result)
    assert payload["prior_art"] == []
    assert payload["count"] == 0


# --- Criterion 8: adapter RateLimitError -> "Error:" string ----------------


async def test_check_novelty_rate_limit_error_mapped_to_error_string(monkeypatch):
    reset_fake(FakeAdapter, raise_on_fetch=RateLimitError)
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)

    result = await _check_novelty_impl("idea")

    assert result.startswith("Error:")
    assert "RateLimitError" in result


# --- Criterion 9: detect_gaps unsupported source -> friendly error ---------


async def test_detect_gaps_unsupported_source_returns_error_no_adapter_no_detector(monkeypatch):
    reset_fake(FakeAdapter)
    reset_fake_gap_detector()
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    monkeypatch.setattr(mcp_server, "GapDetector", FakeGapDetector)

    result = await _detect_gaps_impl(source="reddit")

    assert result.startswith("Error:")
    assert "hackernews" in result
    assert FakeAdapter.constructed is False
    assert FakeGapDetector.constructed is False


# --- Criterion 10: no key anywhere -> guidance message, no side effects ----


async def test_detect_gaps_missing_key_returns_guidance_message_no_side_effects(monkeypatch):
    reset_fake(FakeAdapter)
    reset_fake_gap_detector()
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    monkeypatch.setattr(mcp_server, "GapDetector", FakeGapDetector)

    result = await _detect_gaps_impl()

    assert result == _ANTHROPIC_MISSING_MSG
    assert "ANTHROPIC_API_KEY" in result
    assert "fetch_hackernews" in result
    assert not result.startswith("Error:")
    assert FakeGapDetector.constructed is False
    assert FakeAdapter.constructed is False


# --- Criterion 11: key present -> fetch + detect, overrides propagate ------


async def test_detect_gaps_with_key_returns_gaps_and_propagates_overrides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    docs = [make_doc(source="hackernews", source_id=f"{i}") for i in range(4)]
    reset_fake(FakeAdapter, docs=docs)
    gaps = [make_gap("problem one"), make_gap("problem two")]
    reset_fake_gap_detector(detect_return=gaps)
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    monkeypatch.setattr(mcp_server, "GapDetector", FakeGapDetector)

    result = await _detect_gaps_impl(tags="ask_hn", query="agents", limit=5)

    payload = json.loads(result)
    assert payload["doc_count"] == 4
    assert payload["gap_count"] == 2
    assert len(payload["gaps"]) == 2
    for gap in payload["gaps"]:
        assert set(
            ["problem", "evidence", "demand_signal", "supply_signal", "confidence"]
        ).issubset(gap.keys())

    settings = FakeAdapter.last_settings
    assert settings.hn_tags == ["ask_hn"]
    assert settings.hn_query == "agents"
    assert settings.hn_limit_per_tag == 5


# --- Criterion 12: detect_gaps limit clamped above ceiling -----------------


async def test_detect_gaps_limit_clamped_above_max(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    reset_fake(FakeAdapter, docs=[])
    reset_fake_gap_detector(detect_return=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    monkeypatch.setattr(mcp_server, "GapDetector", FakeGapDetector)

    await _detect_gaps_impl(limit=10000)

    assert FakeAdapter.last_settings.hn_limit_per_tag == 100


# --- Criterion 13: GapDetectionError from detect() -> "Error:" string ------


async def test_detect_gaps_detection_error_mapped_to_error_string(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    reset_fake(FakeAdapter, docs=[make_doc()])
    reset_fake_gap_detector(detect_side_effect=GapDetectionError("boom"))
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    monkeypatch.setattr(mcp_server, "GapDetector", FakeGapDetector)

    result = await _detect_gaps_impl()

    assert result.startswith("Error:")
    assert "boom" in result


# --- Criterion 14: zero fetched docs with key present -----------------------


async def test_detect_gaps_zero_docs_with_key_present_returns_empty_valid_result(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    reset_fake(FakeAdapter, docs=[])
    reset_fake_gap_detector(detect_return=[])
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    monkeypatch.setattr(mcp_server, "GapDetector", FakeGapDetector)

    result = await _detect_gaps_impl()

    payload = json.loads(result)
    assert payload == {"gaps": [], "doc_count": 0, "gap_count": 0}


# --- Criterion 15: module exposes the new product-tool interface -----------


def test_module_exposes_check_novelty_and_detect_gaps_with_docstrings():
    assert callable(mcp_server.check_novelty)
    assert callable(mcp_server.detect_gaps)
    assert callable(mcp_server._check_novelty_impl)
    assert callable(mcp_server._detect_gaps_impl)
    assert check_novelty.__doc__
    assert detect_gaps.__doc__


# --- Additional edge case: adapter fetch failure during detect_gaps --------


async def test_detect_gaps_ingestion_error_during_fetch_returns_error_string(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    reset_fake(FakeAdapter, raise_on_fetch=IngestionError)
    reset_fake_gap_detector()
    monkeypatch.setattr(mcp_server, "HackerNewsAdapter", FakeAdapter)
    monkeypatch.setattr(mcp_server, "GapDetector", FakeGapDetector)

    result = await _detect_gaps_impl()

    assert result.startswith("Error:")
    assert FakeGapDetector.constructed is False
