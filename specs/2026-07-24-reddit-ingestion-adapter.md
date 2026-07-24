# Spec: Reddit Ingestion Adapter + Base Ingestion Pattern (v1)

Date: 2026-07-24 · Status: approved for implementation

## 1. Problem

The Ingestion layer (pipeline stage 1) has no code yet. We need the **first** data source adapter — Reddit — to pull demand signals (people describing problems in subreddits) and hand them downstream as normalized documents. Because this is the first adapter, it must also establish the **reusable base pattern** (`IngestionAdapter` contract + `RawDocument` model + settings) that arXiv/Google Trends adapters will follow.

Decisions made: official Reddit OAuth API (application-only, script app creds in `.env`); v1 scope = new/top posts from a configured subreddit list (no comments, no search). Reddit's API requires OAuth, a mandatory `User-Agent`, and enforces rate limits — the adapter must handle token lifecycle, 401/429, pagination, and malformed responses per the project's hard rules (async, timeout, retry, secrets in `.env`).

## 2. Files touched

| File | Reason |
|---|---|
| `pyproject.toml` | Add runtime deps: `httpx>=0.27`, `pydantic>=2.7`, `pydantic-settings>=2.3`. Add dev dep `respx>=0.21` for mocking httpx. |
| `src/idea_forge/__init__.py` | Package marker (new package root). |
| `src/idea_forge/ingestion/__init__.py` | Ingestion subpackage; re-export `IngestionAdapter`, `RawDocument`. |
| `src/idea_forge/ingestion/base.py` | Reusable `IngestionAdapter` ABC + `RawDocument` pydantic model — shared contract for all future adapters. |
| `src/idea_forge/ingestion/errors.py` | Adapter exception hierarchy (`IngestionError`, `AuthError`, `RateLimitError`, `SubredditUnavailableError`). |
| `src/idea_forge/config.py` | `Settings` (pydantic-settings) reading `.env`: Reddit credentials + adapter config. |
| `src/idea_forge/ingestion/reddit.py` | `RedditAdapter` implementation: OAuth token manager + fetch loop. |
| `.env.example` | Document required env vars (no secrets committed). |
| `tests/__init__.py`, `tests/ingestion/__init__.py` | Test package markers. |
| `tests/ingestion/test_base.py` | Tests for `RawDocument` validation + unique-key helper. |
| `tests/ingestion/test_reddit.py` | Tests for `RedditAdapter` behavior + all edge cases (mocked HTTP). |
| `tests/ingestion/test_config.py` | Tests that `Settings` loads/validates env correctly. |

## 3. Interfaces

`src/idea_forge/ingestion/base.py`

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RawDocument(BaseModel):
    source: str                              # e.g. "reddit"
    source_id: str                           # stable id within the source (e.g. reddit fullname "t3_abc123")
    title: str
    body: str = ""                           # selftext; "" for link-only posts
    url: str
    author: str | None = None                # None for deleted/removed authors
    created_at: datetime                     # tz-aware UTC (post creation)
    fetched_at: datetime                     # tz-aware UTC (when adapter pulled it)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def unique_key(self) -> str: ...         # f"{source}:{source_id}" — stable dedup key for callers


class IngestionAdapter(ABC):
    source: str                              # class attribute, e.g. "reddit"

    @abstractmethod
    def fetch(self) -> AsyncIterator[RawDocument]: ...
        # async generator; yields normalized documents lazily.
        # Chosen over `-> list[...]` so pagination streams without buffering all pages.
```

`src/idea_forge/ingestion/errors.py`

```python
class IngestionError(Exception): ...
class AuthError(IngestionError): ...                  # token acquisition failed / repeated 401
class RateLimitError(IngestionError): ...             # 429 after backoff budget exhausted
class SubredditUnavailableError(IngestionError): ...  # 403 private/banned, 404 not found
```

`src/idea_forge/config.py`

```python
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str                             # REQUIRED by Reddit; must be non-empty

    reddit_subreddits: list[str] = Field(default_factory=lambda: ["SomebodyMakeThis"])
    reddit_listing: str = "new"                        # "new" | "top"
    reddit_limit_per_subreddit: int = 100              # hard cap on docs yielded per subreddit
    reddit_page_size: int = 100                        # Reddit max per request is 100
    reddit_top_time_filter: str = "week"               # only used when listing == "top"

    request_timeout_seconds: float = 10.0
    max_retries: int = 3

    @field_validator("reddit_subreddits", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object: ...
        # Accept comma-separated string from .env ("a,b,c" -> ["a", "b", "c"]),
        # since pydantic-settings otherwise requires JSON for list fields.
        # Strip whitespace, drop empty entries.


def get_settings() -> Settings: ...                    # cached accessor (functools.lru_cache)
```

`src/idea_forge/ingestion/reddit.py`

```python
from collections.abc import AsyncIterator

import httpx

from idea_forge.config import Settings
from idea_forge.ingestion.base import IngestionAdapter, RawDocument


class _Token:               # internal; plain dataclass
    value: str
    expires_at: float       # time.monotonic() deadline; treated expired 60s early


class RedditAdapter(IngestionAdapter):
    source = "reddit"

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    API_BASE = "https://oauth.reddit.com"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None: ...
        # If client is None, adapter owns/creates one and closes it. Injectable for tests.

    async def __aenter__(self) -> "RedditAdapter": ...
    async def __aexit__(self, *exc: object) -> None: ...

    async def fetch(self) -> AsyncIterator[RawDocument]: ...
        # iterates configured subreddits; for each, paginates and yields RawDocument

    async def _get_token(self, *, force_refresh: bool = False) -> str: ...
    async def _fetch_subreddit(self, subreddit: str) -> AsyncIterator[RawDocument]: ...
    async def _request_listing(self, subreddit: str, after: str | None) -> dict: ...
    @staticmethod
    def _normalize(child_data: dict) -> RawDocument | None: ...   # None if unusable/malformed
```

## 4. Behavior & edge cases

1. **Token acquisition**: POST to `TOKEN_URL` with HTTP Basic auth (`client_id`:`client_secret`), body `grant_type=client_credentials`, and the configured `User-Agent` header. Parse `access_token` + `expires_in`; cache token and compute `expires_at = now + expires_in - 60` (60s safety margin, `time.monotonic()`).
2. **Token caching/expiry**: `_get_token()` returns the cached token if not expired; otherwise fetches a new one. Concurrent callers must not trigger duplicate token fetches (guard with an `asyncio.Lock`).
3. **User-Agent required**: every request (token + API) sends `User-Agent: reddit_user_agent`. If `reddit_user_agent` is empty/blank, raise `AuthError` before any network call.
4. **401 on API call**: retry the request **once** after forcing a token refresh (`_get_token(force_refresh=True)`). If it 401s again, raise `AuthError`.
5. **429 rate limit**: honor `Retry-After` header if present; else exponential backoff (base 1s, doubling) with jitter, up to `max_retries`. On exhaustion raise `RateLimitError`. Also proactively respect `X-Ratelimit-Remaining`/`X-Ratelimit-Reset` headers when present (sleep if remaining is 0).
6. **Network timeouts / transient errors**: `httpx.TimeoutException` and 5xx are retried with backoff up to `max_retries`; final failure raises `IngestionError` (wrapping the cause). Timeout comes from `request_timeout_seconds`.
7. **Pagination**: use the `after` cursor from `data.after` in the listing response; request `page_size` items per call; stop when `after` is null OR `reddit_limit_per_subreddit` documents have been yielded for that subreddit (whichever first). Never exceed Reddit's 100/request cap.
8. **Listing type**: `new` → `GET /r/{sub}/new`; `top` → `GET /r/{sub}/top?t={reddit_top_time_filter}`. Unknown listing value → `ValueError` at construction.
9. **Empty subreddit**: listing returns zero children → yield nothing for that subreddit, continue to next (not an error).
10. **Private (403) / banned / not found (404)**: `_request_listing` raises `SubredditUnavailableError`. The top-level `fetch()` catches it **per subreddit**, logs a warning, and continues with the remaining subreddits — one bad subreddit never aborts the run. (`_fetch_subreddit` called directly does propagate the error.)
11. **Malformed / missing fields**: `_normalize` tolerates missing `selftext` (→ `""`), missing/`None`/`"[deleted]"` author (→ `None`); missing `title` or `id` → return `None` (skip document). `created_utc` parsed as UTC tz-aware datetime; if missing/invalid → skip document. Non-dict child → skip.
12. **URL building**: `url` = `permalink` prefixed with `https://www.reddit.com` if present, else the `url` field. `source_id` = `name` (fullname like `t3_...`); fall back to `t3_{id}` if `name` absent.
13. **metadata**: capture demand-signal fields when present: `subreddit`, `score`, `num_comments`, `upvote_ratio`, `over_18`, `link_flair_text`, `permalink`. Missing ones simply absent.
14. **fetched_at**: `datetime.now(UTC)` at document creation time.
15. **Client lifecycle**: adapter usable as async context manager; if it created the client it closes it on exit; an injected client is never closed by the adapter.
16. **No event-loop blocking**: all sleeps via `asyncio.sleep`; all HTTP via `httpx.AsyncClient`.
17. **Duplicates within a run are possible** (listing shifts between pages while paginating). The adapter does not dedup; callers dedup via `unique_key`. `source` + `source_id` is the stable unique key.

## 5. Acceptance criteria

Each is directly testable with a mocked `httpx` transport (respx); no live network.

1. `RawDocument(**valid).unique_key == "reddit:t3_abc"`; `source`+`source_id` uniquely identify a doc.
2. `RawDocument` rejects a missing required field (`title`/`url`/`created_at`) with `ValidationError`.
3. `RawDocument` accepts missing `body`/`author` (defaults `""` / `None`).
4. `fetch()` over a mocked 2-page listing yields all posts across pages in order, following the `after` cursor.
5. Pagination stops at `reddit_limit_per_subreddit` even if more pages exist (yielded count == limit, request count bounded).
6. Token endpoint is called once; a second request within `expires_in` reuses the cached token (token POST called exactly once).
7. Expired token triggers exactly one refresh before the next API call.
8. A `401` API response triggers one forced token refresh + one retry; success after retry yields docs. Two consecutive 401s raise `AuthError`.
9. A `429` with `Retry-After: 2` causes an `asyncio.sleep` of ≈2s (patched/asserted) then retry; exhausting `max_retries` raises `RateLimitError`.
10. `httpx.TimeoutException` is retried up to `max_retries`, then raises `IngestionError`.
11. `403`/`404` for one subreddit skips it (warning logged) while other configured subreddits still produce docs.
12. Empty listing (`children: []`) yields zero docs and does not raise.
13. Malformed child (missing `id`/`title`, `author == "[deleted]"`, missing `selftext`) is skipped or normalized per rules — no exception; author becomes `None`, body becomes `""`.
14. Every outbound request (token + listing) includes the configured `User-Agent`; empty user agent raises `AuthError` before any HTTP call.
15. `listing == "top"` builds `/r/{sub}/top?t={filter}`; invalid listing value raises `ValueError`.
16. `Settings` loads all three Reddit creds from a temp `.env`; missing a required cred raises `ValidationError`; `REDDIT_SUBREDDITS="a, b,c"` parses to `["a", "b", "c"]`.
17. Injected `httpx.AsyncClient` is not closed by the adapter; self-created client is closed on `__aexit__`.

## 6. Non-goals

- No database / persistence / dedup implementation — adapter only returns `RawDocument`s; dedup is the caller's job using `unique_key`.
- No comments, no Reddit search, no user/multireddit endpoints (v1 scope = subreddit new/top only).
- No scheduling/cron, no FastAPI routes, no CLI.
- No embeddings / Knowledge Engine integration.
- No other adapters (arXiv, Google Trends) — only the base pattern + Reddit.
- No proactive tuning against Reddit's 60 req/min quota beyond header-driven backoff.

## 7. Implementation order

1. `pyproject.toml` — add `httpx`, `pydantic`, `pydantic-settings` runtime deps + `respx` dev dep; `uv sync`.
2. `src/idea_forge/__init__.py`, `ingestion/__init__.py`, `.env.example`.
3. `ingestion/base.py` (`RawDocument`, `IngestionAdapter`) + `tests/ingestion/test_base.py`.
4. `config.py` (`Settings`, `get_settings`) + `tests/ingestion/test_config.py`.
5. `ingestion/errors.py`.
6. `ingestion/reddit.py` — token manager first (`_get_token`), then `_request_listing` with retry/backoff, then `_normalize`, then `_fetch_subreddit` + `fetch()`.
7. `tests/ingestion/test_reddit.py` covering all edge cases with mocked transport.
8. `uv run pytest`, `uv run mypy src/`, `ruff check --fix . && ruff format .`.
