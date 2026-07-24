"""Tests for idea_forge.gaps.detector: GapDetector.

Covers spec acceptance criteria 5-15, 17 (specs/2026-07-24-gap-detection.md §5),
against the AMENDED structured-output call path: `client.messages.create(...,
output_config=...)`, `stop_reason` checked before parsing, first text block's
JSON validated ourselves via `GapAnalysis.model_validate_json`.

No live network — a fake AsyncAnthropic-like client (exposing `.messages.create`
as an AsyncMock) is injected or patched in place of the real SDK client.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import pytest

from idea_forge.config import Settings
from idea_forge.gaps import GapAnalysis, GapCandidate, GapDetectionError, GapDetector
from idea_forge.gaps.detector import BODY_TRUNCATE_CHARS, SYSTEM_PROMPT
from idea_forge.ingestion.base import RawDocument

REQUIRED_REDDIT_ENV = {
    "reddit_client_id": "abc123",
    "reddit_client_secret": "shh-secret",
    "reddit_user_agent": "idea-forge/0.1 by u/someone",
}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """No real .env / ANTHROPIC_API_KEY leaks into these tests."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "ANTHROPIC_API_KEY",
        "GAP_MODEL",
        "GAP_MAX_DOCS_PER_CALL",
        "GAP_REQUEST_TIMEOUT_SECONDS",
        "GAP_MAX_OUTPUT_TOKENS",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
    ):
        monkeypatch.delenv(var, raising=False)


def make_settings(**overrides: object) -> Settings:
    kwargs = dict(REQUIRED_REDDIT_ENV)
    kwargs.update(overrides)
    return Settings(**kwargs)


def make_doc(
    *,
    source: str = "reddit",
    source_id: str = "abc",
    title: str = "t",
    body: str = "b",
    metadata: dict | None = None,
) -> RawDocument:
    now = datetime.now(UTC)
    return RawDocument(
        source=source,
        source_id=source_id,
        title=title,
        body=body,
        url="https://example.com",
        author="someone",
        created_at=now,
        fetched_at=now,
        metadata=metadata or {},
    )


class FakeMessages:
    """Stand-in for AsyncAnthropic().messages — `.create` is an AsyncMock."""

    def __init__(self, responses: list[object] | None = None) -> None:
        self.create = AsyncMock(side_effect=responses if responses else None)


class FakeAsyncAnthropic:
    """Stand-in for anthropic.AsyncAnthropic: exposes `.messages.create`."""

    def __init__(self, responses: list[object] | None = None) -> None:
        self.messages = FakeMessages(responses)


def make_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def make_response(gaps: list[GapCandidate], stop_reason: str = "end_turn") -> SimpleNamespace:
    """A `messages.create`-shaped response carrying a valid GapAnalysis JSON text block."""
    body = GapAnalysis(gaps=gaps).model_dump_json()
    return SimpleNamespace(stop_reason=stop_reason, content=[make_text_block(body)])


def make_candidate(**overrides: object) -> GapCandidate:
    base = dict(
        problem="p",
        demand_signal="d",
        supply_signal="none observed",
        confidence=0.5,
    )
    base.update(overrides)
    return GapCandidate(**base)


# --- Criterion 5: no key anywhere -> GapDetectionError at construction --------


def test_construction_raises_when_no_key_and_no_client_injected(monkeypatch):
    settings = make_settings(anthropic_api_key="")

    with pytest.raises(GapDetectionError):
        GapDetector(settings)


# --- Criterion 6: injected client skips key checks ----------------------------


def test_construction_succeeds_with_injected_client_and_no_key():
    settings = make_settings(anthropic_api_key="")
    fake = FakeAsyncAnthropic()

    detector = GapDetector(settings, client=fake)

    assert detector is not None


# --- Criterion 7: self-constructed client gets configured timeout ------------


def test_self_constructed_client_receives_configured_timeout(monkeypatch):
    settings = make_settings(anthropic_api_key="sk-ant-test", gap_request_timeout_seconds=42.0)
    captured: dict = {}

    def fake_ctor(*args, **kwargs):
        captured.update(kwargs)
        return FakeAsyncAnthropic()

    monkeypatch.setattr("idea_forge.gaps.detector.AsyncAnthropic", fake_ctor)

    GapDetector(settings)

    assert captured.get("timeout") == 42.0


# --- Criterion 8: empty input -> no API calls ---------------------------------


async def test_detect_empty_docs_returns_empty_list_without_calling_client():
    settings = make_settings()
    fake = FakeAsyncAnthropic()
    detector = GapDetector(settings, client=fake)

    result = await detector.detect([])

    assert result == []
    fake.messages.create.assert_not_called()


# --- Criterion 9: batching -----------------------------------------------------


async def test_detect_batches_five_docs_with_max_two_into_three_calls():
    settings = make_settings(gap_max_docs_per_call=2)
    docs = [make_doc(source_id=str(i)) for i in range(5)]

    batch_results = [
        [make_candidate(problem="batch0")],
        [make_candidate(problem="batch1")],
        [make_candidate(problem="batch2")],
    ]
    responses = [make_response(gaps) for gaps in batch_results]
    fake = FakeAsyncAnthropic(responses)
    detector = GapDetector(settings, client=fake)

    result = await detector.detect(docs)

    assert fake.messages.create.call_count == 3
    assert [c.problem for c in result] == ["batch0", "batch1", "batch2"]


# --- Criterion 10: model, adaptive thinking, max_tokens, output_config --------


async def test_each_batch_call_carries_model_thinking_max_tokens_and_output_config():
    settings = make_settings(
        gap_model="claude-opus-4-8",
        gap_max_docs_per_call=50,
        gap_max_output_tokens=16000,
    )
    docs = [make_doc()]
    fake = FakeAsyncAnthropic([make_response([])])
    detector = GapDetector(settings, client=fake)

    await detector.detect(docs)

    _, kwargs = fake.messages.create.call_args
    assert kwargs.get("model") == "claude-opus-4-8"
    assert kwargs.get("thinking") == {"type": "adaptive"}
    assert kwargs.get("max_tokens") == settings.gap_max_output_tokens

    output_config = kwargs.get("output_config")
    assert output_config is not None
    assert output_config["format"]["type"] == "json_schema"
    assert "schema" in output_config["format"]


# --- Criterion 11: doc serialization -------------------------------------------


def test_serialize_docs_includes_unique_key_title_metadata_and_truncates_body():
    long_body = "x" * (BODY_TRUNCATE_CHARS + 500)
    doc_with_metadata = make_doc(
        source="reddit",
        source_id="abc",
        title="My Title",
        body=long_body,
        metadata={"points": 42, "num_comments": 7},
    )

    serialized = GapDetector._serialize_docs([doc_with_metadata])

    assert doc_with_metadata.unique_key in serialized
    assert "My Title" in serialized
    assert "42" in serialized
    assert "7" in serialized
    # truncated: the full long body must not appear verbatim
    assert long_body not in serialized


def test_serialize_docs_preserves_input_order():
    docs = [make_doc(source_id=str(i), title=f"title-{i}") for i in range(3)]

    serialized = GapDetector._serialize_docs(docs)

    positions = [serialized.index(f"title-{i}") for i in range(3)]
    assert positions == sorted(positions)


def test_serialize_docs_omits_metadata_when_absent():
    doc = make_doc(metadata={})

    serialized = GapDetector._serialize_docs([doc])

    assert "points" not in serialized
    assert "num_comments" not in serialized


# --- Criterion 12: refusal skips batch, subsequent batches still run ---------


async def test_refusal_batch_contributes_zero_gaps_and_logs_warning_but_continues(caplog):
    settings = make_settings(gap_max_docs_per_call=1)
    docs = [make_doc(source_id="0"), make_doc(source_id="1")]

    refusal_response = SimpleNamespace(
        stop_reason="refusal",
        content=[make_text_block("I can't help with that")],
    )
    responses = [
        refusal_response,
        make_response([make_candidate(problem="ok")]),
    ]
    fake = FakeAsyncAnthropic(responses)
    detector = GapDetector(settings, client=fake)

    with caplog.at_level("WARNING"):
        result = await detector.detect(docs)

    assert [c.problem for c in result] == ["ok"]
    assert any("refus" in record.message.lower() for record in caplog.records)


# --- NEW: max_tokens truncation -> GapDetectionError --------------------------


async def test_max_tokens_stop_reason_raises_gap_detection_error_mentioning_truncation():
    settings = make_settings(gap_max_docs_per_call=50)
    docs = [make_doc()]

    truncated_response = SimpleNamespace(
        stop_reason="max_tokens",
        content=[make_text_block('{"gaps": [')],  # truncated / incomplete JSON
    )
    fake = FakeAsyncAnthropic([truncated_response])
    detector = GapDetector(settings, client=fake)

    with pytest.raises(GapDetectionError) as exc_info:
        await detector.detect(docs)

    message = str(exc_info.value).lower()
    assert "truncat" in message or "gap_max_output_tokens" in message


# --- Criterion 13: SDK error wrapping ------------------------------------------


async def test_batch_api_status_error_wrapped_as_gap_detection_error_with_cause():
    settings = make_settings(gap_max_docs_per_call=50)
    docs = [make_doc()]

    api_error = anthropic.APIStatusError(
        "boom",
        response=SimpleNamespace(status_code=500, headers={}, request=SimpleNamespace()),
        body=None,
    )
    fake = FakeAsyncAnthropic()
    fake.messages.create = AsyncMock(side_effect=api_error)
    detector = GapDetector(settings, client=fake)

    with pytest.raises(GapDetectionError) as exc_info:
        await detector.detect(docs)

    assert exc_info.value.__cause__ is api_error


# --- Criterion 14: invalid parsed output -> GapDetectionError -----------------


async def test_invalid_json_text_block_raises_gap_detection_error():
    settings = make_settings(gap_max_docs_per_call=50)
    docs = [make_doc()]

    bad_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[make_text_block("not valid json at all")],
    )
    fake = FakeAsyncAnthropic([bad_response])
    detector = GapDetector(settings, client=fake)

    with pytest.raises(GapDetectionError):
        await detector.detect(docs)


async def test_out_of_range_confidence_in_json_raises_gap_detection_error():
    settings = make_settings(gap_max_docs_per_call=50)
    docs = [make_doc()]

    invalid_gap_json = (
        '{"gaps": [{"problem": "p", "evidence": [], "demand_signal": "d", '
        '"supply_signal": "s", "confidence": 2, "metadata": {}}]}'
    )
    bad_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[make_text_block(invalid_gap_json)],
    )
    fake = FakeAsyncAnthropic([bad_response])
    detector = GapDetector(settings, client=fake)

    with pytest.raises(GapDetectionError):
        await detector.detect(docs)


# --- Criterion 15: well-formed response fields intact --------------------------


async def test_well_formed_response_returns_candidates_with_fields_intact():
    settings = make_settings(gap_max_docs_per_call=50)
    docs = [make_doc()]

    candidate = make_candidate(
        problem="unmet need",
        demand_signal="lots of complaints",
        supply_signal="none observed",
        confidence=0.87,
    )
    fake = FakeAsyncAnthropic([make_response([candidate])])
    detector = GapDetector(settings, client=fake)

    result = await detector.detect(docs)

    assert len(result) == 1
    got = result[0]
    assert got.problem == "unmet need"
    assert got.demand_signal == "lots of complaints"
    assert got.supply_signal == "none observed"
    assert got.confidence == 0.87


# --- Criterion 17: SYSTEM_PROMPT is a module-level non-empty constant ---------


def test_system_prompt_is_nonempty_string_constant():
    assert isinstance(SYSTEM_PROMPT, str)
    assert len(SYSTEM_PROMPT.strip()) > 0
