"""Reddit ingestion adapter: OAuth token manager + subreddit listing fetch loop."""

import asyncio
import logging
import math
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from idea_forge.config import Settings
from idea_forge.ingestion.base import IngestionAdapter, RawDocument
from idea_forge.ingestion.errors import (
    AuthError,
    IngestionError,
    RateLimitError,
    SubredditUnavailableError,
)

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 60.0

_METADATA_FIELDS = (
    "subreddit",
    "score",
    "num_comments",
    "upvote_ratio",
    "over_18",
    "link_flair_text",
    "permalink",
)


@dataclass
class _Token:
    value: str
    expires_at: float  # time.monotonic() deadline; treated expired 60s early


class RedditAdapter(IngestionAdapter):
    source = "reddit"

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    API_BASE = "https://oauth.reddit.com"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if settings.reddit_listing not in ("new", "top"):
            raise ValueError(f"unknown reddit_listing: {settings.reddit_listing!r}")

        self.settings = settings
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()
        self._token: _Token | None = None
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> "RedditAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> AsyncIterator[RawDocument]:
        for subreddit in self.settings.reddit_subreddits:
            try:
                async for doc in self._fetch_subreddit(subreddit):
                    yield doc
            except SubredditUnavailableError:
                logger.warning("subreddit unavailable, skipping: %s", subreddit)
                continue

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        if not self.settings.reddit_user_agent.strip():
            raise AuthError("reddit_user_agent must not be empty")

        async with self._token_lock:
            if (
                not force_refresh
                and self._token is not None
                and self._token.expires_at > time.monotonic()
            ):
                return self._token.value

            try:
                response = await self._client.post(
                    self.TOKEN_URL,
                    auth=(self.settings.reddit_client_id, self.settings.reddit_client_secret),
                    data={"grant_type": "client_credentials"},
                    headers={"User-Agent": self.settings.reddit_user_agent},
                    timeout=self.settings.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise AuthError(f"token acquisition failed: {exc}") from exc

            if response.status_code != 200:
                raise AuthError(f"token acquisition failed with status {response.status_code}")

            payload = response.json()
            try:
                access_token = str(payload["access_token"])
                expires_in = float(payload["expires_in"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AuthError(f"malformed token response: {exc}") from exc

            self._token = _Token(
                value=access_token,
                expires_at=time.monotonic() + expires_in - 60,
            )
            return self._token.value

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = (2 ** (attempt - 1)) + random.uniform(0, 1)
        await asyncio.sleep(delay)

    async def _request_listing(self, subreddit: str, after: str | None) -> dict[str, Any]:
        page_size = min(self.settings.reddit_page_size, 100)
        params: dict[str, Any] = {"limit": page_size}
        if after is not None:
            params["after"] = after
        if self.settings.reddit_listing == "top":
            params["t"] = self.settings.reddit_top_time_filter

        url = f"{self.API_BASE}/r/{subreddit}/{self.settings.reddit_listing}"

        token = await self._get_token()
        attempt = 0
        retried_auth = False

        while True:
            headers = {
                "User-Agent": self.settings.reddit_user_agent,
                "Authorization": f"bearer {token}",
            }
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.settings.request_timeout_seconds,
                )
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self.settings.max_retries:
                    raise IngestionError(
                        f"transport error fetching listing for r/{subreddit}"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 401:
                if retried_auth:
                    raise AuthError(f"authentication failed for r/{subreddit}")
                retried_auth = True
                token = await self._get_token(force_refresh=True)
                continue

            if response.status_code == 429:
                attempt += 1
                if attempt > self.settings.max_retries:
                    raise RateLimitError(f"rate limited fetching r/{subreddit}")
                retry_after = response.headers.get("Retry-After")
                retry_after_seconds: float | None = None
                if retry_after is not None:
                    try:
                        retry_after_seconds = float(retry_after)
                    except ValueError:
                        retry_after_seconds = None
                    else:
                        # a hostile/broken server must not park us on sleep(inf)
                        if not math.isfinite(retry_after_seconds) or retry_after_seconds < 0:
                            retry_after_seconds = None
                        else:
                            retry_after_seconds = min(retry_after_seconds, MAX_RETRY_AFTER_SECONDS)
                if retry_after_seconds is not None:
                    await asyncio.sleep(retry_after_seconds)
                else:
                    await self._sleep_backoff(attempt)
                continue

            if response.status_code in (403, 404):
                raise SubredditUnavailableError(
                    f"r/{subreddit} unavailable ({response.status_code})"
                )

            if response.status_code >= 500:
                attempt += 1
                if attempt > self.settings.max_retries:
                    raise IngestionError(
                        f"server error fetching r/{subreddit}: {response.status_code}"
                    )
                await self._sleep_backoff(attempt)
                continue

            response.raise_for_status()

            remaining = response.headers.get("X-Ratelimit-Remaining")
            reset = response.headers.get("X-Ratelimit-Reset")
            if remaining is not None and reset is not None:
                try:
                    if float(remaining) <= 0:
                        await asyncio.sleep(float(reset))
                except ValueError:
                    pass

            result: dict[str, Any] = response.json()
            return result

    async def _fetch_subreddit(self, subreddit: str) -> AsyncIterator[RawDocument]:
        after: str | None = None
        yielded = 0
        limit = self.settings.reddit_limit_per_subreddit
        effective_page_size = min(self.settings.reddit_page_size, 100)
        max_requests = -(-limit // max(effective_page_size, 1)) + 2  # ceil + slack
        requests_made = 0

        while True:
            if requests_made >= max_requests:
                return
            requests_made += 1
            data = await self._request_listing(subreddit, after)
            listing_data = data.get("data") or {}
            children = listing_data.get("children") or []

            for child in children:
                if yielded >= limit:
                    return
                child_data = child.get("data") if isinstance(child, dict) else None
                doc = self._normalize(child_data) if isinstance(child_data, dict) else None
                if doc is None:
                    continue
                yield doc
                yielded += 1

            after = listing_data.get("after") or None
            if not after or yielded >= limit:
                return

    @staticmethod
    def _normalize(child_data: dict[str, Any]) -> RawDocument | None:
        if not isinstance(child_data, dict):
            return None

        title = child_data.get("title")
        post_id = child_data.get("id")
        if not title or not post_id:
            return None

        created_utc = child_data.get("created_utc")
        if created_utc is None:
            return None
        try:
            created_at = datetime.fromtimestamp(float(created_utc), tz=UTC)
        except (TypeError, ValueError, OSError):
            return None

        author = child_data.get("author")
        if not author or author == "[deleted]":
            author = None

        permalink = child_data.get("permalink")
        url = f"https://www.reddit.com{permalink}" if permalink else (child_data.get("url") or "")

        source_id = child_data.get("name") or f"t3_{post_id}"

        metadata: dict[str, Any] = {
            key: child_data[key]
            for key in _METADATA_FIELDS
            if key in child_data and child_data[key] is not None
        }

        return RawDocument(
            source="reddit",
            source_id=source_id,
            title=title,
            body=child_data.get("selftext") or "",
            url=url,
            author=author,
            created_at=created_at,
            fetched_at=datetime.now(UTC),
            metadata=metadata,
        )
