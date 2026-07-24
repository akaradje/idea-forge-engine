# Spec: Gap Detection Layer (Pipeline Stage 3) — v1

Date: 2026-07-24 · Status: approved for implementation

## 1. Problem

Pipeline stage 3 turns raw demand-signal documents (from any ingestion adapter — Reddit, Hacker News, future sources) into structured **gap candidates**: unmet needs where demand looks high but existing supply looks low. Today the pipeline stops at ingestion (`list[RawDocument]`); nothing consumes those docs analytically. This layer introduces `src/idea_forge/gaps/` with a `GapDetector` that sends compactly-serialized documents to Claude (`claude-opus-4-8`) via the Anthropic SDK's **structured output** (guaranteed JSON schema), and returns validated `GapCandidate` pydantic models. It is a pure analysis stage: no persistence, no embeddings, no idea synthesis. It reuses codebase conventions from the ingestion adapters (Pydantic v2 boundaries, injectable client for tests, explicit timeout, stdlib logging, specific exception wrapping) but talks to the Anthropic SDK instead of `httpx`.

## 2. Files touched

| File | Reason |
|---|---|
| `src/idea_forge/config.py` | Extend `Settings` with `anthropic_api_key`, `gap_model`, `gap_max_docs_per_call`, `gap_request_timeout_seconds`. |
| `src/idea_forge/gaps/__init__.py` | **New**: re-export `GapDetector`, `GapCandidate`, `GapEvidence`, `GapAnalysis`, `GapDetectionError`. |
| `src/idea_forge/gaps/models.py` | **New**: `GapEvidence`, `GapCandidate`, `GapAnalysis` (the structured-output schema). |
| `src/idea_forge/gaps/errors.py` | **New**: `GapDetectionError(Exception)`. |
| `src/idea_forge/gaps/detector.py` | **New**: `GapDetector` (client construction, batching, SDK call, error/refusal handling) + module-level `SYSTEM_PROMPT`, `_serialize_docs`. |
| `pyproject.toml` | Add dependency `anthropic>=0.60` (uv resolves latest). |
| `.env.example` | Document `ANTHROPIC_API_KEY`, `GAP_MODEL`, `GAP_MAX_DOCS_PER_CALL`, `GAP_REQUEST_TIMEOUT_SECONDS` (all optional). |
| `tests/gaps/__init__.py`, `tests/gaps/test_models.py`, `tests/gaps/test_detector.py`, `tests/gaps/test_config.py` | **New**: mirrored tests, fake/injected `AsyncAnthropic`, no live calls. |

**Structured-output call path (AMENDED post-review):** use `client.messages.create(..., output_config={"format": {"type": "json_schema", "schema": GapAnalysis.model_json_schema()}})` and validate the returned text with `GapAnalysis.model_validate_json(...)` ourselves. Rationale: `messages.parse` validates eagerly *inside* the SDK call, so a real refusal (natural-language text block) or a `max_tokens` truncation surfaces as `ValidationError` from the call itself — making the spec-required "check `stop_reason` before parsing" unreachable and turning a skippable refusal into a run-aborting error. With `create` we control the order: check `stop_reason` first (`"refusal"` → skip batch with warning; `"max_tokens"` → `GapDetectionError` explaining the output budget was exceeded and suggesting lowering `gap_max_docs_per_call` or raising `gap_max_output_tokens`), then parse the first text block's JSON (`ValidationError`/`json` failure → `GapDetectionError`). The output budget is `Settings.gap_max_output_tokens: int = 16000` (env `GAP_MAX_OUTPUT_TOKENS`), not a hardcoded constant. Tests mock `client.messages.create` returning an object with `.stop_reason` and `.content` (list of blocks with `.type == "text"` / `.text` JSON string).

## 3. Interfaces

`src/idea_forge/config.py` — add to `Settings` (existing fields unchanged):

```python
class Settings(BaseSettings):
    # ... existing reddit_* / hn_* / shared fields ...

    # Gap Detection (Anthropic)
    anthropic_api_key: str = ""            # optional; SDK also reads ANTHROPIC_API_KEY env
    gap_model: str = "claude-opus-4-8"
    gap_max_docs_per_call: int = 50        # docs per Anthropic request; larger inputs batched
    gap_request_timeout_seconds: float = 120.0
```

`src/idea_forge/gaps/errors.py`:

```python
class GapDetectionError(Exception):
    """Raised when gap detection fails irrecoverably (missing key, SDK/API error, invalid output)."""
```

`src/idea_forge/gaps/models.py`:

```python
from typing import Any

from pydantic import BaseModel, Field


class GapEvidence(BaseModel):
    unique_key: str                                    # references RawDocument.unique_key
    quote: str                                         # short verbatim snippet supporting the gap


class GapCandidate(BaseModel):
    problem: str                                       # concise unmet-need statement
    evidence: list[GapEvidence] = Field(default_factory=list)
    demand_signal: str                                 # why demand looks high
    supply_signal: str                                 # existing solutions found; "none observed" if absent
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GapAnalysis(BaseModel):
    """Structured-output wrapper: the top-level schema the model must return."""
    gaps: list[GapCandidate] = Field(default_factory=list)
```

`src/idea_forge/gaps/detector.py`:

```python
import logging

from anthropic import AsyncAnthropic

from idea_forge.config import Settings
from idea_forge.gaps.models import GapAnalysis, GapCandidate
from idea_forge.ingestion.base import RawDocument

logger = logging.getLogger(__name__)

SYSTEM_PROMPT: str = "..."          # module-level, testable/tunable (see Behavior 11/14)
BODY_TRUNCATE_CHARS = 2000          # per-doc body budget in the prompt


class GapDetector:
    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None: ...
        # If client is None: build AsyncAnthropic(
        #     api_key=settings.anthropic_api_key or None,   # None → SDK reads ANTHROPIC_API_KEY env
        #     timeout=settings.gap_request_timeout_seconds)
        # Raise GapDetectionError if settings key is blank AND ANTHROPIC_API_KEY env unset
        # AND no client injected. Injected client skips all key checks.

    async def detect(self, docs: list[RawDocument]) -> list[GapCandidate]: ...
        # Batch by gap_max_docs_per_call, analyze each batch sequentially, concat (no dedup).

    async def _detect_batch(self, docs: list[RawDocument]) -> list[GapCandidate]: ...
        # One Anthropic structured call; returns that batch's gaps (possibly empty).

    @staticmethod
    def _batch(docs: list[RawDocument], size: int) -> list[list[RawDocument]]: ...

    @staticmethod
    def _serialize_docs(docs: list[RawDocument]) -> str: ...
        # Compact per-doc block: unique_key, title, body truncated to BODY_TRUNCATE_CHARS,
        # allow-listed metadata (points, num_comments) when present. Input order preserved.
```

## 4. Behavior & edge cases

1. **Key resolution.** With `client=None`: use `settings.anthropic_api_key` if non-empty, else let the SDK read `ANTHROPIC_API_KEY` env. Both empty/absent → `GapDetectionError` with a clear message at construction. Injected client → no key checks (tests need no key).
2. **Timeout.** Self-created client is built with `timeout=settings.gap_request_timeout_seconds`. Injected client used as-is.
3. **Retry policy.** No hand-rolled retry loop. Rely on the SDK's built-in retries (default `max_retries=2`, retries 429/5xx/connection errors with backoff). Documented in the detector docstring. This is the deliberate exception to the tenacity rule — the SDK owns retry for its own error taxonomy.
4. **Batching.** `detect` splits docs into consecutive chunks of at most `gap_max_docs_per_call`, calls `_detect_batch` per chunk **sequentially** (avoid burst/rate pressure in v1), concatenates results in batch order. No cross-batch dedup (v2 / caller concern).
5. **Empty input.** `detect([])` returns `[]` immediately — zero API calls.
6. **Structured output.** Request forces the `GapAnalysis` schema (`messages.parse` with `output_format=GapAnalysis`, or the json_schema fallback). Response validated into `GapAnalysis`; `.gaps` returned. Out-of-range `confidence` fails Pydantic validation → wrapped in `GapDetectionError`.
7. **Adaptive thinking.** Pass `thinking={"type": "adaptive"}` on each request.
8. **Refusal handling.** If `stop_reason == "refusal"`, log a warning with context (layer, batch index, doc count) and **skip that batch** (zero gaps), continuing with remaining batches. A refusal never aborts `detect`. Check `stop_reason` before reading parsed output.
9. **SDK error wrapping.** Catch `anthropic.APIError` subclasses (`APIStatusError`, `APIConnectionError`, `APITimeoutError`) from a batch call and re-raise as `GapDetectionError` with the original as `__cause__` plus log context. Do not catch bare `Exception`. Hard errors propagate (fail the whole `detect`) in v1 — per-batch isolation of hard errors is a v2 refinement.
10. **Malformed/invalid parsed output.** Content failing `GapAnalysis` validation → `GapDetectionError`.
11. **Doc serialization.** `_serialize_docs` emits one compact block per doc: `unique_key`, `title`, body truncated to `BODY_TRUNCATE_CHARS`, and allow-listed metadata (`points`, `num_comments`) only when present. Input order preserved (deterministic, testable). The prompt instructs the model to cite evidence by `unique_key`.
12. **No event-loop blocking.** `AsyncAnthropic` only; no sync SDK, no `time.sleep`.
13. **Logging (stdlib `logging`).** Info on completed `detect` (total docs, batch count, total gaps); warning on skipped refusal batches. Never log keys/secrets.
14. **`supply_signal` semantics.** `SYSTEM_PROMPT` instructs the model to emit the literal `"none observed"` when no existing solution appears in the docs; the field is schema-required, non-null.

## 5. Acceptance criteria

Each testable with an injected fake `AsyncAnthropic` (or monkeypatched `client.messages`); zero live network.

1. `GapCandidate(problem="p", demand_signal="d", supply_signal="none observed", confidence=0.5)` validates; `evidence`/`metadata` default to `[]`/`{}`.
2. `confidence=1.5` raises `pydantic.ValidationError`; `confidence=-0.1` also raises.
3. `GapEvidence(unique_key="reddit:abc", quote="...")` validates; missing `unique_key` raises.
4. `GapAnalysis.model_validate({"gaps": []})` yields `.gaps == []`.
5. `GapDetector(settings)` with blank `anthropic_api_key`, no `ANTHROPIC_API_KEY` env, no injected client → `GapDetectionError`.
6. `GapDetector(settings, client=fake)` constructs without any key present.
7. Self-constructed client (patched `AsyncAnthropic`) receives `timeout=settings.gap_request_timeout_seconds`.
8. `await detect([])` returns `[]`; the fake client's messages method is never called.
9. `detect` over 5 docs with `gap_max_docs_per_call=2` makes exactly 3 batch calls; result equals the concatenation of the mocked batch results in order.
10. Each batch call carries the configured `gap_model` and `thinking={"type": "adaptive"}` (asserted on captured kwargs).
11. The serialized batch prompt contains each doc's `unique_key` and title, includes `points`/`num_comments` when present in metadata, and truncates a long body to `BODY_TRUNCATE_CHARS`.
12. A batch whose mocked response has `stop_reason == "refusal"` contributes zero gaps, logs a warning, and does not prevent a following non-refusal batch's gaps from being returned.
13. A batch call raising `anthropic.APIStatusError` (mocked) causes `detect` to raise `GapDetectionError` with the SDK error as `__cause__`.
14. A mocked response failing `GapAnalysis` validation (e.g. `confidence=2`) raises `GapDetectionError`.
15. Parsed `GapCandidate`s from a well-formed mocked response are returned with fields intact.
16. `Settings` loads `ANTHROPIC_API_KEY`, `GAP_MODEL`, `GAP_MAX_DOCS_PER_CALL`, `GAP_REQUEST_TIMEOUT_SECONDS` from a temp `.env`; all four have working defaults when absent (`""`, `"claude-opus-4-8"`, `50`, `120.0`).
17. `SYSTEM_PROMPT` is a module-level, importable, non-empty constant.

## 6. Non-goals

- No idea synthesis (stage 4), novelty validation (stage 5), or scoring (stage 6).
- No persistence, DB, embeddings, or vector/pgvector search.
- No cross-batch or per-doc dedup — callers dedup via `unique_key`; v2.
- No hand-rolled retry/backoff (`tenacity`) — SDK owns retries.
- No parallel/concurrent batch execution (sequential only in v1).
- No per-batch isolation of hard SDK errors (only refusals are skipped).
- No MCP exposure, FastAPI route, CLI, scheduling, or streaming.
- No changes to ingestion adapters, `base.py`, or existing tests.
- No structlog migration (project-wide logging alignment is a separate refactor).

## 7. Implementation order

1. `pyproject.toml` — add `anthropic>=0.60`; `uv sync`; confirm the installed SDK exposes `messages.parse` (pin the chosen structured-output path).
2. `config.py` — add the four fields; `tests/gaps/test_config.py`.
3. `.env.example` — document the new vars.
4. `gaps/errors.py` → `gaps/models.py`; `tests/gaps/test_models.py`.
5. `gaps/detector.py` — `SYSTEM_PROMPT` + `_serialize_docs` + `_batch` → `__init__` (key resolution, client build) → `_detect_batch` (SDK call, refusal + error handling, validation) → `detect`.
6. `gaps/__init__.py` — re-exports.
7. `tests/gaps/test_detector.py` — all acceptance criteria with injected fake client.
8. `uv run pytest`, `uv run mypy src/`, `ruff check --fix . && ruff format .`.
