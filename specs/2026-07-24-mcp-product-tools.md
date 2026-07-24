# Spec: MCP tools `check_novelty` + `detect_gaps` — v1

Date: 2026-07-24 · Status: approved for implementation
Context: product pivot per `research/2026-07-24-dogfood-findings.md` (MCP server = the product)

## 1. Problem

Per the approved pivot, the MCP server is now the product: a "demand-signal terminal for Claude Code." Today `src/idea_forge/mcp_server.py` exposes only raw fetch tools. Calling agents can pull documents but must hand-roll gap analysis and novelty checks. This spec adds two analytical tools to the **same file, same conventions** (`_impl` split, always return `str`, `"Error: "` prefix, stderr-only logging, limit clamps):

- **`check_novelty`** — a *keyless* prior-art lookup. Given an idea string, searches Hacker News stories via the existing `HackerNewsAdapter` and returns structured evidence (titles, urls, engagement) for the calling agent to judge. No LLM call, no similarity scoring — evidence, not verdicts (aligned with the anti-"AI says your idea is good" backlash).
- **`detect_gaps`** — wraps HN fetch + `GapDetector.detect()` into one call, returning structured `GapCandidate`s. Requires `ANTHROPIC_API_KEY`; when absent it returns a friendly guidance message explaining both how to set the key and the keyless alternative (fetch + analyze yourself).

Both reuse already-tested layers verbatim. No changes to `gaps/` or `ingestion/`, no new dependencies.

## 2. Files touched

| File | Reason |
|---|---|
| `src/idea_forge/mcp_server.py` | Add imports (`os`, `GapDetector`, `GapDetectionError`), constants, helpers (`_anthropic_key_present`, `_hn_prior_art_entry`), two `_impl` functions, two `@mcp.tool()` wrappers with full docstrings. No changes to existing functions. |
| `tests/test_mcp_server.py` | Extend: monkeypatch `HackerNewsAdapter` (reuse `FakeAdapter`) and `GapDetector`; 15 new acceptance-criteria tests. |

Not touched: `gaps/*.py`, `ingestion/*.py`, `config.py`, `pyproject.toml`, `.mcp.json`. FastMCP server name stays `"idea-forge-ingestion"`.

## 3. Interfaces

New/added in `src/idea_forge/mcp_server.py`:

```python
import os

from idea_forge.gaps.detector import GapDetector
from idea_forge.gaps.errors import GapDetectionError

MAX_NOVELTY_RESULTS = 20       # ceiling for check_novelty max_results
NOVELTY_QUERY_MAX_CHARS = 300  # idea string truncated to this before hitting Algolia

_ANTHROPIC_MISSING_MSG = (
    "ANTHROPIC_API_KEY is not set, so detect_gaps cannot run its LLM analysis. "
    "Two options:\n"
    "  1. Set it: add ANTHROPIC_API_KEY=sk-ant-... to your .env (or export it in the "
    "environment) and call detect_gaps again.\n"
    "  2. Keyless alternative: call fetch_hackernews to pull the same posts, then "
    "analyze them for demand-supply gaps yourself — you are an LLM."
)


def _anthropic_key_present(settings: Settings) -> bool:
    """True if settings.anthropic_api_key is non-blank OR ANTHROPIC_API_KEY env is set."""
    ...


def _hn_prior_art_entry(doc: RawDocument) -> dict[str, Any]:
    """Compact prior-art record: unique_key, title, url, points, num_comments,
    created_at (ISO string). points/num_comments from doc.metadata; absent → None."""
    ...


async def _check_novelty_impl(idea: str, max_results: int = 10) -> str:
    """Return JSON {"query", "prior_art": [...], "count", "note"} or 'Error: ...'."""
    ...


async def _detect_gaps_impl(
    source: str = "hackernews",
    tags: str = "ask_hn",
    query: str = "",
    limit: int = 25,
) -> str:
    """Return JSON {"gaps": [...], "doc_count", "gap_count"}, the key-missing guidance
    message, or an 'Error: ...' string."""
    ...


@mcp.tool()
async def check_novelty(idea: str, max_results: int = 10) -> str:
    """Search Hacker News for prior art related to an idea (keyless, no API key needed).

    Use this to sanity-check whether an idea already exists or has been discussed
    before you invest in it. It searches Hacker News stories (via Algolia HN Search)
    for posts matching your idea and returns them as EVIDENCE for you to judge — it
    does NOT score similarity or decide whether your idea is novel. That judgment is
    yours: read the returned titles, engagement, and links, then reason about overlap.

    Args:
        idea: a short description of the idea to check (a sentence or a few keywords).
            Long strings are truncated before searching; keep it focused for best matches.
        max_results: how many prior-art posts to return (default 10, clamped to 1..20).

    Returns:
        A JSON string:
        {
          "query": "<the search text actually sent to Hacker News>",
          "prior_art": [
            {"unique_key", "title", "url", "points", "num_comments", "created_at"}, ...
          ],
          "count": <number of results>,
          "note": "Judge similarity yourself — this tool returns evidence, not verdicts."
        }
        An empty "prior_art" list (count 0) means no matching HN stories were found — that
        is a signal, not an error. On failure, a plain string beginning "Error: ".
    """
    return await _check_novelty_impl(idea, max_results)


@mcp.tool()
async def detect_gaps(
    source: str = "hackernews",
    tags: str = "ask_hn",
    query: str = "",
    limit: int = 25,
) -> str:
    """Fetch demand-signal posts and analyze them for demand-supply GAPS (needs ANTHROPIC_API_KEY).

    Use this when you want structured, evidence-cited gap candidates — problems where
    demand looks strong but existing supply looks weak — rather than raw posts. It fetches
    Hacker News posts and runs them through the gap-detection LLM analysis in one call.

    This tool requires ANTHROPIC_API_KEY (it makes an LLM call). If the key is not set, it
    returns a friendly message explaining how to set it AND the keyless alternative: call
    fetch_hackernews and analyze the posts for gaps yourself.

    Args:
        source: data source; only "hackernews" is supported in v1 (anything else returns a
            friendly error naming the supported source).
        tags: comma-separated Algolia HN tags to fetch (e.g. "ask_hn", "ask_hn,show_hn").
        query: optional free-text query to narrow the fetched posts; "" fetches all.
        limit: max posts to fetch per tag before analysis (default 25, clamped to 1..100).

    Returns:
        A JSON string:
        {
          "gaps": [ <GapCandidate>, ... ],   # problem, evidence[{unique_key, quote}],
                                             # demand_signal, supply_signal, confidence, metadata
          "doc_count": <posts analyzed>,
          "gap_count": <gaps found>
        }
        A GapDetectionError (LLM/API failure, invalid output) yields a string beginning
        "Error: ". Missing ANTHROPIC_API_KEY yields the friendly guidance message.
    """
    return await _detect_gaps_impl(source, tags, query, limit)
```

## 4. Behavior & edge cases

**Shared**
1. Both `_impl` functions always return `str` (JSON or plain human-readable string); never raise out to the MCP runtime. JSON via `json.dumps(..., ensure_ascii=False)`.
2. Both build `Settings` through the existing `_base_settings(require_reddit=False, **overrides)` — missing `reddit_*` creds never break these HN-only tools.
3. Exceptions logged with `logger.exception(...)` → stderr; short `f"Error: {type(exc).__name__}: {exc}"` returned, matching existing tools.

**check_novelty**
4. Query derivation — **DECIDED: pass the raw idea string through to Algolia**, transformed only by `idea.strip()[:NOVELTY_QUERY_MAX_CHARS]`. Algolia already tokenizes/ranks full-text relevance; no stopword list to maintain, trivially testable. The exact text sent is echoed in the response `"query"` field.
5. Overrides: `hn_tags="story"`, `hn_query=<derived query>`, `hn_limit_per_tag=<clamped max_results>`.
6. `max_results` clamp: `max(1, min(max_results, MAX_NOVELTY_RESULTS))` (ceiling 20).
7. Blank idea (empty after strip) → `"Error: idea must not be empty — pass a short description or keywords to check for prior art."` before any network call.
8. Each doc maps via `_hn_prior_art_entry`: `unique_key`, `title`, `url`, `points`/`num_comments` (`None` when absent), `created_at` ISO string. Body NOT included (compact records).
9. Zero results → `{"query": ..., "prior_art": [], "count": 0, "note": ...}` — valid, not an error.
10. `note` is the fixed string `"Judge similarity yourself — this tool returns evidence, not verdicts."`
11. Adapter errors (`IngestionError`/`RateLimitError`, broad `Exception`) → `"Error: ..."` string.

**detect_gaps**
12. `source != "hackernews"` → `"Error: source '<x>' is not supported in v1. Only 'hackernews' is available."` before any work.
13. `limit` clamped via existing `_clamp_limit` (1..100) → `hn_limit_per_tag`. `tags` → `hn_tags` override only when not `_is_blank_csv(tags)`; `query` → `hn_query`.
14. Key check after building settings, before fetching: `not _anthropic_key_present(settings)` → return `_ANTHROPIC_MISSING_MSG` (**guidance-style, no "Error:" prefix — DECIDED**: it is a recoverable, expected keyless path, not a failure). No fetch, no adapter, no GapDetector constructed.
15. Key present: fetch HN docs (`HackerNewsAdapter(settings)` drained via existing `_collect`), then `GapDetector(settings).detect(docs)` — GapDetector self-constructs its client (no injection in production).
16. Result: `{"gaps": [g.model_dump(mode="json") for g in gaps], "doc_count": len(docs), "gap_count": len(gaps)}`.
17. `GapDetectionError` → `f"Error: {exc}"`. `IngestionError` during fetch → `"Error: ..."`. Broad `Exception` fallback → `"Error: ..."`.
18. Zero fetched docs → `detect([])` returns `[]` (no API call) → `{"gaps": [], "doc_count": 0, "gap_count": 0}`. Valid, not an error.
19. Async throughout; no event-loop blocking, no `asyncio.run`.

## 5. Acceptance criteria

Each testable by calling the `_impl` functions directly with monkeypatched `HackerNewsAdapter` (reuse `FakeAdapter`) and monkeypatched `GapDetector`; no live network, no real key.

1. `_check_novelty_impl("realtime collab editor", max_results=3)` with `FakeAdapter` yielding 3 docs → JSON `{"query": "realtime collab editor", "prior_art": [...3...], "count": 3, "note": <fixed note>}`; each entry has exactly `unique_key, title, url, points, num_comments, created_at`, no `body`.
2. `check_novelty` sets overrides on constructed `Settings`: `hn_tags == ["story"]`, `hn_query == <stripped truncated idea>`, `hn_limit_per_tag == clamped max_results`.
3. `max_results` clamps: `1000` → 20; `0` → 1.
4. Idea longer than `NOVELTY_QUERY_MAX_CHARS` is truncated to exactly that length in both `hn_query` and the echoed `"query"`.
5. `_check_novelty_impl("   ")` → string starting `"Error:"` mentioning `idea`; no adapter constructed.
6. Doc with `points`/`num_comments` in metadata surfaces the integers; doc without them yields `null` for both.
7. Zero docs → valid JSON with `"prior_art": []`, `"count": 0`.
8. Adapter raising `RateLimitError` → `"Error:"` string containing `RateLimitError`; nothing propagates.
9. `_detect_gaps_impl(source="reddit")` → `"Error:"` string naming `hackernews`; no adapter, no GapDetector constructed.
10. `_detect_gaps_impl()` with no key anywhere (clean env + blank settings key) returns `_ANTHROPIC_MISSING_MSG` — mentions BOTH `ANTHROPIC_API_KEY` and `fetch_hackernews`, does NOT start with `"Error:"`; no GapDetector constructed, no HTTP call.
11. With key present, `FakeAdapter` yielding 4 docs, monkeypatched `GapDetector.detect` returning 2 `GapCandidate`s → JSON `{"gaps": [...2...], "doc_count": 4, "gap_count": 2}` with gap dicts carrying `problem, evidence, demand_signal, supply_signal, confidence`; `FakeAdapter.last_settings` shows `hn_tags == ["ask_hn"]`, `hn_query == "agents"`, `hn_limit_per_tag == 5` for the call `(tags="ask_hn", query="agents", limit=5)`.
12. `limit=10000` in `detect_gaps` → `hn_limit_per_tag == 100`.
13. Monkeypatched `GapDetector.detect` raising `GapDetectionError("boom")` → return value starts `"Error:"` and contains `boom`; nothing propagates.
14. Zero docs with key present → `{"gaps": [], "doc_count": 0, "gap_count": 0}`.
15. Importing `idea_forge.mcp_server` exposes `check_novelty`, `detect_gaps`, `_check_novelty_impl`, `_detect_gaps_impl` alongside existing symbols; both new tools have non-empty `__doc__`.

## 6. Non-goals

- No Reddit (or any non-HN) source for `detect_gaps` in v1.
- No similarity scoring, embeddings, or novelty verdicts in `check_novelty` — evidence only.
- No Google Patents / Product Hunt / arXiv sources (v2).
- No stopword stripping / keyword-extraction NLP.
- No pricing/monetization mechanics (separate track).
- No MCP resources or prompts — tools only.
- No changes to `gaps/`, `ingestion/`, `config.py`, `pyproject.toml`, `.mcp.json`; no new dependencies.
- ~~No client injection into `GapDetector` in production~~ **AMENDED post-review:** `_detect_gaps_impl` injects a context-managed `AsyncAnthropic` (built with the settings key/timeout) into `GapDetector` so the httpx pool is closed per call — a self-constructed detector client would leak on a long-lived MCP server. Tests still monkeypatch the class.
- No cross-call caching, dedup, or persistence.

## 7. Implementation order

1. `src/idea_forge/mcp_server.py` — imports + constants (`MAX_NOVELTY_RESULTS`, `NOVELTY_QUERY_MAX_CHARS`, `_ANTHROPIC_MISSING_MSG`).
2. Helpers: `_anthropic_key_present`, `_hn_prior_art_entry`, novelty clamp.
3. `_check_novelty_impl` (blank guard → settings → fetch via `_collect` → map → JSON/error).
4. `_detect_gaps_impl` (source validation → settings → key check → fetch → detect → JSON/guidance/error).
5. `@mcp.tool()` wrappers with the full §3 docstrings.
6. `tests/test_mcp_server.py` — extend with all 15 criteria; add a fake `GapDetector`.
7. `uv run pytest`, `uv run mypy src/`, `ruff check --fix . && ruff format .`.
