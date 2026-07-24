# Spec: Hacker News Ingestion Adapter (Algolia HN Search API) — v1

Date: 2026-07-24 · Status: approved for implementation

## 1. Problem

The Ingestion layer needs a **second** data-source adapter — Hacker News — to pull demand signals (people asking for tools/solutions, e.g. "Ask HN: is there a tool that…", "Show HN: I built…"). This adapter is NOT a new pattern: it reuses the base contract (`IngestionAdapter` ABC + `RawDocument`), the error hierarchy, and the async / injectable-client / retry-backoff conventions already established by `RedditAdapter`. HN is fetched via the **Algolia HN Search API** (`https://hn.algolia.com/api/v1`), which requires no auth and no key — strictly simpler than Reddit (no token lifecycle, no `AuthError` paths). It fetches **stories** filtered by configured Algolia tags (default `["ask_hn", "show_hn"]`), each tag fetched separately, with optional free-text query, using the `search_by_date` endpoint (newest first, stable `page`-based pagination).

## 2. Files touched

| File | Reason |
|---|---|
| `src/idea_forge/config.py` | Extend `Settings` with `hn_`-prefixed fields (tags list, query, limit-per-tag, page size). Reuses existing `request_timeout_seconds` / `max_retries`. |
| `src/idea_forge/ingestion/hackernews.py` | **New**: `HackerNewsAdapter` — per-tag fetch loop + Algolia pagination + `_normalize`. Same shape as `reddit.py`. |
| `src/idea_forge/ingestion/__init__.py` | Re-export `HackerNewsAdapter` alongside existing exports. |
| `.env.example` | Document new `HN_*` env vars (all optional, have defaults). |
| `tests/ingestion/test_hackernews.py` | **New**: respx-mocked tests covering all acceptance criteria. |
| `tests/ingestion/test_config.py` | Add cases for `HN_*` field loading + CSV parse of `HN_TAGS`. |

`errors.py` is reused as-is (`IngestionError`, `RateLimitError`); no new exception classes.

## 3. Interfaces

`src/idea_forge/config.py` — add to `Settings` (keep all existing Reddit + shared fields unchanged):

```python
class Settings(BaseSettings):
    # ... existing reddit_* + request_timeout_seconds + max_retries ...

    # Hacker News (Algolia HN Search API — no auth)
    hn_tags: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ask_hn", "show_hn"]
    )                                       # one Algolia tag per entry, fetched separately
    hn_query: str = ""                      # optional free-text query; "" = match all
    hn_limit_per_tag: int = 100             # hard cap on docs yielded per tag entry
    hn_page_size: int = 100                 # Algolia hitsPerPage; clamped to [1, 1000] at request time

    @field_validator("hn_tags", mode="before")
    @classmethod
    def _split_hn_tags(cls, v: object) -> object: ...
        # Same CSV behavior as reddit_subreddits: "ask_hn, show_hn" -> ["ask_hn", "show_hn"].
        # Strip whitespace, drop empty entries.
```

`src/idea_forge/ingestion/hackernews.py`:

```python
from collections.abc import AsyncIterator
from typing import Any

import httpx

from idea_forge.config import Settings
from idea_forge.ingestion.base import IngestionAdapter, RawDocument


class HackerNewsAdapter(IngestionAdapter):
    source = "hackernews"

    API_BASE = "https://hn.algolia.com/api/v1"
    SEARCH_ENDPOINT = "search_by_date"          # stable newest-first pagination
    HN_ITEM_URL = "https://news.ycombinator.com/item?id={object_id}"
    MAX_HITS_PER_PAGE = 1000                    # Algolia hard cap

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None: ...
        # If client is None, adapter owns/creates one and closes it on exit. Injectable for tests.
        # No token state, no lock — HN needs no auth.

    async def __aenter__(self) -> "HackerNewsAdapter": ...
    async def __aexit__(self, *exc: object) -> None: ...

    async def fetch(self) -> AsyncIterator[RawDocument]: ...
        # iterates configured hn_tags; per-tag failures are logged and skipped, never abort the run.

    async def _sleep_backoff(self, attempt: int) -> None: ...          # same formula as reddit.py

    async def _fetch_tag(self, tag: str) -> AsyncIterator[RawDocument]: ...
        # paginate one tag; yields RawDocument up to hn_limit_per_tag.

    async def _request_page(self, tag: str, page: int) -> dict[str, Any]: ...
        # GET search_by_date with tags/query/page/hitsPerPage; retry+backoff; returns parsed JSON.

    @staticmethod
    def _normalize(hit: dict[str, Any], *, tag: str, query: str) -> RawDocument | None: ...
        # None if unusable/malformed.
```

Algolia request shape used by `_request_page`:
- URL: `{API_BASE}/search_by_date`
- params: `tags=<tag>` (single tag, e.g. `ask_hn`), `page=<int>` (0-based), `hitsPerPage=<clamped page_size>`, and `query=<hn_query>` only when non-empty.
- Response fields consumed: `hits` (list), `nbPages` (int), `page` (int).

## 4. Behavior & edge cases

1. **No auth at all.** No token acquisition, no `Authorization` header, no `asyncio.Lock`, no `AuthError` path. Module docstring states explicitly: Algolia HN Search is keyless.
2. **Per-tag iteration.** `fetch()` iterates `settings.hn_tags` in order; for each it delegates to `_fetch_tag`. A failure while fetching one tag (`IngestionError`, `RateLimitError`, or any unexpected exception from that tag's pagination) is caught **per tag**, logged as a warning with context (`source`, `tag`), and iteration continues with remaining tags. `_fetch_tag` called directly still propagates.
3. **Pagination (three bounds, same defensive pattern as `reddit.py`).** Start at `page = 0`. Per page request `hitsPerPage = clamp(hn_page_size, 1, 1000)`. Stop when the FIRST of these holds:
   - `hn_limit_per_tag` documents have been yielded for the tag, OR
   - `page >= nbPages` reported by the response (page 0-based; `nbPages` is a count), OR
   - a **defensive request ceiling** `max_requests = ceil(limit / clamped_page_size) + 2` has been reached (guards against a server that never advances `nbPages`).
   After each page, increment `page` by 1.
4. **429 rate limit.** Honor `Retry-After` header if present and numeric (`asyncio.sleep` that many seconds); non-numeric or absent → exponential backoff with jitter (`_sleep_backoff`, same formula as Reddit). Retry up to `max_retries`; on exhaustion raise `RateLimitError`.
5. **5xx and transport errors.** `httpx.TransportError` and `status_code >= 500` are retried with backoff up to `max_retries`; final failure raises `IngestionError` wrapping the cause. Timeout is `settings.request_timeout_seconds` on every request.
6. **Other non-200 (4xx besides 429).** Raise `IngestionError` with the status code (no per-tag 403/404 "unavailable" concept for Algolia search). Propagates to the per-tag guard in `fetch()`, which skips just that tag.
7. **Empty results page.** `hits == []` → yield nothing; `page >= nbPages` bound ends the loop normally. Not an error.
8. **`_normalize` malformed-hit rules** (return `None` = skip, never raise):
   - Non-dict hit → skip.
   - Missing/empty `objectID` → skip. `source_id = str(objectID)`.
   - Missing/empty/`null` `title` → skip (comment hits have `null` title and must not slip through).
   - `url` missing/`None`/empty → build `https://news.ycombinator.com/item?id={objectID}`.
   - `story_text` missing/`None` → `body = ""`. (Ask HN self-posts carry text in `story_text`.)
   - `author` missing/`None` → `author = None`.
   - `created_at_i` (epoch seconds) preferred → `datetime.fromtimestamp(..., tz=UTC)`. Absent/invalid → fall back to ISO `created_at` (`datetime.fromisoformat`, coerced tz-aware UTC). Both missing/unparseable → skip.
9. **Metadata capture.** `metadata` includes when present/non-None: `points`, `num_comments`; plus always `tags` (the configured tag that produced this hit) and `query` (the configured `hn_query`, even when `""`). Missing numeric fields simply absent.
10. **`fetched_at`** = `datetime.now(UTC)` at document creation.
11. **Client lifecycle.** Async context manager; self-created client closed on `__aexit__`; injected client never closed. Identical to Reddit.
12. **No event-loop blocking.** All sleeps via `asyncio.sleep`; all HTTP via `httpx.AsyncClient`.
13. **No dedup.** Duplicates possible if listing shifts between pages; callers dedup via `unique_key` (`hackernews:{objectID}`).
14. **Empty `hn_tags`.** `fetch()` yields nothing. Not an error.

## 5. Acceptance criteria

Each directly testable with respx-mocked transport; no live network.

1. `RawDocument(source="hackernews", source_id="12345", ...).unique_key == "hackernews:12345"`.
2. `fetch()` over a mocked 2-page response (`nbPages=2`) for a single tag yields all hits across both pages in order, requesting `page=0` then `page=1`.
3. Pagination stops at `hn_limit_per_tag` even when more pages/hits exist (yielded count == limit; request count bounded by the defensive ceiling).
4. Pagination stops when `page >= nbPages` (`nbPages=1` → exactly one request for that tag).
5. Each configured tag produces a separate request with `tags=<that tag>`; the two default tags yield docs from both.
6. A failing tag fetch (persistent 500) is caught and logged, and does NOT prevent the other tag from yielding docs; `_fetch_tag` called directly propagates the error.
7. `429` with `Retry-After: 2` triggers `asyncio.sleep(≈2)` (patched/asserted) then retry; exhausting `max_retries` raises `RateLimitError`.
8. `httpx.TransportError` and `>=500` are retried up to `max_retries`, then raise `IngestionError`.
9. A non-retryable 4xx (e.g. 400) raises `IngestionError` immediately (no retries).
10. Empty page (`hits: []`, `nbPages: 1`) yields zero docs and does not raise.
11. `_normalize`: hit missing `objectID` → skipped; missing/`null` `title` → skipped; non-dict hit → skipped. No exception raised.
12. `_normalize`: `url: null` builds `https://news.ycombinator.com/item?id={objectID}`; `story_text: null` → `body == ""`; `author: null` → `author is None`.
13. `_normalize`: `created_at_i` epoch produces tz-aware UTC `created_at`; when absent, ISO `created_at` string is parsed; both invalid/missing → skipped.
14. `metadata` contains `points`, `num_comments`, `tags` (== configured tag), and `query` (== configured `hn_query`, including `""`).
15. Every outbound request carries no `Authorization` header; `hitsPerPage` is clamped into `[1, 1000]`; `query` param present only when `hn_query` non-empty.
16. `Settings` loads `HN_*` from a temp `.env`; `HN_TAGS="ask_hn, show_hn"` parses to `["ask_hn", "show_hn"]`; all `hn_*` fields have working defaults when absent.
17. Injected `httpx.AsyncClient` is not closed by the adapter; self-created client is closed on `__aexit__`.

## 6. Non-goals

- No new base pattern — reuse `IngestionAdapter`, `RawDocument`, `errors.py`, and Reddit conventions verbatim. Do NOT refactor `base.py` or `reddit.py`.
- No auth/token/key logic of any kind. No new exception classes.
- No multi-tag Algolia AND-filters (e.g. `"story,ask_hn"` as one filter) — CSV config expresses one tag per entry only; AND-filters are a possible v2.
- No database / persistence / dedup — callers dedup via `unique_key`.
- No HN comments, polls, front-page/Firebase API, or user endpoints — v1 = stories via `search_by_date` + tags only.
- No relevance scoring, embeddings, or Knowledge Engine integration.
- No scheduling/cron, FastAPI routes, or CLI.
- Do not modify the Reddit adapter or its tests.

## 7. Implementation order

1. `config.py` — add `hn_*` fields + `_split_hn_tags` validator; extend `tests/ingestion/test_config.py`.
2. `.env.example` — document `HN_TAGS`, `HN_QUERY`, `HN_LIMIT_PER_TAG`, `HN_PAGE_SIZE`.
3. `ingestion/hackernews.py` — build in order: `__init__`/context-manager/lifecycle → `_request_page` (params + retry/backoff/429) → `_normalize` → `_fetch_tag` (pagination bounds) → `fetch()` (per-tag guard).
4. `ingestion/__init__.py` — re-export `HackerNewsAdapter`.
5. `tests/ingestion/test_hackernews.py` — cover all acceptance criteria with respx.
6. `uv run pytest`, `uv run mypy src/`, `ruff check --fix . && ruff format .`.
