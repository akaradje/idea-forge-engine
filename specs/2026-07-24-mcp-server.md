# Spec: MCP Server exposing Ingestion Adapters as Tools — v1

Date: 2026-07-24 · Status: approved for implementation

## 1. Problem

Claude Code sessions cannot currently pull live demand-signal data (Hacker News, Reddit) without leaving the session to run pipeline code by hand. We want a **thin MCP server** (Model Context Protocol, stdio transport) that exposes the existing ingestion adapters (`HackerNewsAdapter`, `RedditAdapter`) as MCP **tools**, so Claude Code can fetch normalized `RawDocument`s directly. This is strictly a presentation/transport layer over the async adapters as they exist — no business logic, no adapter changes. Because stdio transport uses **stdout for the MCP protocol**, all logging must go to **stderr only**, and adapter errors (`IngestionError`, `RateLimitError`, `AuthError`, …) must be caught and returned as readable strings instead of crashing the server. Reddit needs `REDDIT_*` creds in `.env`; when absent, the tool must return a friendly error, not a traceback.

## 2. Files touched

| File | Reason |
|---|---|
| `pyproject.toml` | Add runtime dep `mcp>=1.2`; add `[project.scripts]` entry `idea-forge-mcp = "idea_forge.mcp_server:main"`. |
| `src/idea_forge/mcp_server.py` | **New**: single-file `FastMCP` server. Two `@mcp.tool()` async wrappers over `_impl` functions, Settings-builder helper, body truncation, error-to-string handling, stderr logging config, `main()` (stdio run). |
| `.mcp.json` (project root) | **New**: registers the server for Claude Code — command `uv`, args `["run", "idea-forge-mcp"]`. |
| `tests/test_mcp_server.py` | **New**: tests calling the `_impl` functions directly with mocked/monkeypatched adapters (respx or monkeypatch), covering all acceptance criteria. |

Not touched: `ingestion/*.py`, `config.py`, `errors.py`, `base.py` — reused verbatim.

**Decided:** each tool's logic lives in a module-level `async def _fetch_*_impl(...) -> str`; the `@mcp.tool()` functions are thin wrappers that `await` the impl. Tests import and call the `_impl` functions directly — no dependency on FastMCP internals.

## 3. Interfaces

`src/idea_forge/mcp_server.py`:

```python
"""MCP server (stdio) exposing Idea Forge ingestion adapters as tools for Claude Code.

CRITICAL: stdio transport uses stdout for the MCP protocol. All logging MUST go to
stderr. Never print()/log to stdout in this module.
"""

import json
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from idea_forge.config import Settings
from idea_forge.ingestion.base import RawDocument
from idea_forge.ingestion.errors import IngestionError
from idea_forge.ingestion.hackernews import HackerNewsAdapter
from idea_forge.ingestion.reddit import RedditAdapter

logger = logging.getLogger("idea_forge.mcp_server")

MAX_BODY_CHARS = 2000   # RawDocument.body truncated to this before serialization
MAX_LIMIT = 100         # per-call limit clamp ceiling

mcp = FastMCP("idea-forge-ingestion")


def _base_settings(*, require_reddit: bool, **overrides: Any) -> Settings:
    """Build a Settings from env/.env with per-call overrides.

    First attempts a real Settings load (env + .env). If ValidationError hits only
    the reddit_* required fields:
      - require_reddit=False (HN call): retry with placeholder reddit values so
        validation passes (HN never uses them).
      - require_reddit=True (Reddit call): re-raise — caller maps to friendly error.
    """
    ...


def _serialize(docs: list[RawDocument]) -> list[dict[str, Any]]:
    """model_dump(mode='json') each doc; truncate body to MAX_BODY_CHARS and
    record metadata['body_truncated'] = True + metadata['body_original_length']
    when truncation occurred. Never mutates the original documents."""
    ...


async def _collect(adapter: HackerNewsAdapter | RedditAdapter) -> list[RawDocument]:
    """Drain the adapter's async generator inside its async context manager."""
    ...


async def _fetch_hackernews_impl(
    tags: str = "ask_hn,show_hn",
    query: str = "",
    limit_per_tag: int = 25,
) -> str:
    """Return JSON string {"documents": [...], "count": N} or a friendly 'Error: ...' string."""
    ...


async def _fetch_reddit_impl(
    subreddits: str = "",
    listing: str = "new",
    limit_per_subreddit: int = 25,
) -> str:
    ...


@mcp.tool()
async def fetch_hackernews(
    tags: str = "ask_hn,show_hn", query: str = "", limit_per_tag: int = 25
) -> str:
    """Fetch demand-signal posts from Hacker News (Algolia HN Search, no auth).

    Args:
        tags: comma-separated Algolia tags (e.g. "ask_hn,show_hn").
        query: optional free-text query; "" matches all.
        limit_per_tag: max documents per tag (kept small for LLM context).
    """
    return await _fetch_hackernews_impl(tags, query, limit_per_tag)


@mcp.tool()
async def fetch_reddit(
    subreddits: str = "", listing: str = "new", limit_per_subreddit: int = 25
) -> str:
    """Fetch demand-signal posts from Reddit subreddits (requires REDDIT_* in .env).

    Args:
        subreddits: comma-separated subreddit names; "" uses the configured default.
        listing: "new" or "top".
        limit_per_subreddit: max documents per subreddit.
    """
    return await _fetch_reddit_impl(subreddits, listing, limit_per_subreddit)


def _configure_logging() -> None:
    """Attach a StreamHandler(sys.stderr) to the root logger; never stdout."""
    ...


def main() -> None:
    """Entry point: configure stderr logging, then mcp.run(transport='stdio')."""
    ...
```

`.mcp.json` (project root):

```json
{
  "mcpServers": {
    "idea-forge-ingestion": {
      "command": "uv",
      "args": ["run", "idea-forge-mcp"]
    }
  }
}
```

`pyproject.toml` additions:

```toml
[project]
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "mcp>=1.2",
]

[project.scripts]
idea-forge-mcp = "idea_forge.mcp_server:main"
```

## 4. Behavior & edge cases

1. **Return type is always a `str`.** Both tools return either a JSON string `{"documents": [...], "count": N}` (documents = list of `RawDocument.model_dump(mode="json")`) or a plain, human-readable error string starting with `"Error: "`. Tools never raise out to the MCP runtime.
2. **Settings construction & per-call overrides.** Build settings via `_base_settings(...)`. For `fetch_hackernews`, override `hn_tags` (pass the raw CSV string — existing `_split_hn_tags` validator parses it), `hn_query`, and `hn_limit_per_tag`. For `fetch_reddit`, override `reddit_subreddits` (only when `subreddits != ""`; empty string keeps the configured default), `reddit_listing`, and `reddit_limit_per_subreddit`.
3. **HN needs no Reddit creds — but `Settings` requires them.** `_base_settings(require_reddit=False)` first attempts a real load; if `ValidationError` covers only missing `reddit_*` required fields, retry with placeholder values so validation passes (HN never uses them). Any other validation failure propagates as a normal error string. This keeps `fetch_hackernews` "always works, no auth".
4. **Reddit creds missing → friendly error, no crash.** `_fetch_reddit_impl` uses `_base_settings(require_reddit=True)`; on missing/blank creds (from env **or** `.env` — the check is on the Settings load result, not raw `os.environ`) it returns, before any network call: `"Error: Reddit credentials not configured. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT in .env."` Also catch `AuthError` raised during fetch and map it to the same friendly message.
5. **Adapter error mapping.** Wrap adapter execution in `try/except IngestionError` (covers `RateLimitError`, `AuthError`, `SubredditUnavailableError`) plus a broad `except Exception` fallback; return `f"Error: {type(exc).__name__}: {exc}"` (short, no traceback). Log full detail with `logger.exception(...)` → stderr.
6. **Body truncation.** In `_serialize`, if `len(doc.body) > MAX_BODY_CHARS`: body becomes the first `MAX_BODY_CHARS` chars, and metadata gains `body_truncated = True` + `body_original_length = <int>`. Non-truncated docs unchanged (no `body_truncated` key). Operates on the dumped dict — never mutates live adapter documents.
7. **Small defaults + clamping.** Defaults `limit_per_tag=25` / `limit_per_subreddit=25`, mapped to `hn_limit_per_tag` / `reddit_limit_per_subreddit`. Clamp incoming values to `max(1, min(limit, MAX_LIMIT))` so a caller can't blow up the context window.
8. **Invalid `listing`.** `RedditAdapter.__init__` raises `ValueError` for a listing other than `"new"`/`"top"`. Catch it and return `"Error: listing must be 'new' or 'top', got '<x>'."`.
9. **Async, no event-loop blocking.** Tools are `async def`; adapters consumed via `async with adapter:` + `async for` inside `_collect`. FastMCP awaits natively — no `asyncio.run`, no threads. Adapters own their `httpx.AsyncClient` in production (no injection).
10. **Empty results.** Zero documents → `{"documents": [], "count": 0}` (valid, not an error).
11. **Logging to stderr only.** `_configure_logging` attaches a single `StreamHandler(sys.stderr)`. Nothing in this module writes to stdout. FastMCP server name is `"idea-forge-ingestion"`.
12. **JSON serialization safety.** `RawDocument.model_dump(mode="json")` makes datetimes ISO strings; final payload via `json.dumps(..., ensure_ascii=False)`.
13. **`main()`.** Configures logging then `mcp.run(transport="stdio")`. No arg parsing in v1.

## 5. Acceptance criteria

Each testable by calling `_fetch_hackernews_impl` / `_fetch_reddit_impl` directly with a monkeypatched adapter or respx-mocked transport — no real MCP client, no live network.

1. `_fetch_hackernews_impl()` with a mocked `HackerNewsAdapter` yielding 3 docs returns a JSON string parsing to `{"documents": [...], "count": 3}`, each document a dict with `source`, `source_id`, `title`, `url`, `created_at` (ISO string).
2. `fetch_hackernews` works with **no** `REDDIT_*` env vars set (monkeypatched clean env, no `.env`) — placeholders fill in and the HN call succeeds.
3. Per-call overrides propagate: `_fetch_hackernews_impl(tags="ask_hn", query="agent", limit_per_tag=5)` builds a `Settings` with `hn_tags == ["ask_hn"]`, `hn_query == "agent"`, `hn_limit_per_tag == 5` (assert via spy/captured Settings).
4. `_fetch_reddit_impl()` with missing Reddit creds returns a string starting `"Error:"` mentioning `REDDIT_CLIENT_ID` — and constructs no adapter / makes no HTTP call.
5. `_fetch_reddit_impl()` with valid creds (monkeypatched) and a mocked adapter yielding docs returns `{"documents": [...], "count": N}`.
6. `subreddits=""` keeps the configured default subreddit list; `subreddits="a, b"` overrides to `["a", "b"]`.
7. `_fetch_reddit_impl(listing="hot")` returns a friendly `"Error:"` string about `listing` and does not raise.
8. Adapter raising `RateLimitError` → tool returns a string starting `"Error:"` containing `RateLimitError`; no exception propagates.
9. Adapter raising `AuthError` during fetch → `_fetch_reddit_impl` returns the friendly credentials/auth error string, not a traceback.
10. A document with `body` longer than `MAX_BODY_CHARS` is truncated to exactly `MAX_BODY_CHARS` chars, metadata has `body_truncated == True` and correct `body_original_length`; a short-body document has no `body_truncated` key and unchanged body.
11. Limits are clamped: `10000` → effective limit `100`; `0` or negative → `1` (assert on constructed Settings).
12. Zero yielded documents → valid JSON `{"documents": [], "count": 0}`.
13. Importing `idea_forge.mcp_server` exposes `mcp` (a `FastMCP`), `fetch_hackernews`, `fetch_reddit`, `main`; `pyproject.toml` `[project.scripts]` maps `idea-forge-mcp` to `idea_forge.mcp_server:main`.
14. `main()` (with `mcp.run` monkeypatched to a no-op) attaches a logging handler whose stream is `sys.stderr` and writes nothing to stdout (capsys assert).

## 6. Non-goals

- No HTTP/SSE transport — stdio only.
- No changes to adapters, `RawDocument`, `config.py`, or `errors.py`; no new adapter features.
- No persistence, caching, dedup, embeddings, or Knowledge Engine integration.
- No Reddit auth flow beyond reading `.env`.
- No MCP **resources** or **prompts** — tools only.
- No real-MCP-client round-trip tests; no live network in tests.
- No new adapters (arXiv, Google Trends) exposed in v1.
- No business logic in `mcp_server.py` — thin transport layer only.

## 7. Implementation order

1. `pyproject.toml` — add `mcp>=1.2` dep + `[project.scripts]`; `uv sync`.
2. `src/idea_forge/mcp_server.py` — build in order: `_configure_logging` → `_base_settings` (real-load-first + placeholder retry + clamps) → `_serialize` → `_collect` → `_fetch_hackernews_impl` → `_fetch_reddit_impl` → `@mcp.tool()` wrappers → `main()`.
3. `.mcp.json` at project root.
4. `tests/test_mcp_server.py` — monkeypatch adapters / respx transport; cover all 14 acceptance criteria.
5. `uv run pytest`, `uv run mypy src/`, `ruff check --fix . && ruff format .`.
