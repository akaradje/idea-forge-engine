"""Hacker News ingestion adapter: Algolia HN Search API (keyless, no auth) fetch loop."""

import asyncio
import logging
import math
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from idea_forge.config import Settings
from idea_forge.ingestion.base import IngestionAdapter, RawDocument
from idea_forge.ingestion.errors import IngestionError, RateLimitError

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 60.0


class HackerNewsAdapter(IngestionAdapter):
    source = "hackernews"

    API_BASE = "https://hn.algolia.com/api/v1"
    SEARCH_ENDPOINT = "search_by_date"  # stable newest-first pagination
    HN_ITEM_URL = "https://news.ycombinator.com/item?id={object_id}"
    MAX_HITS_PER_PAGE = 1000  # Algolia hard cap

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    async def __aenter__(self) -> "HackerNewsAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> AsyncIterator[RawDocument]:
        hn_tags = self.settings.hn_tags
        yielded_any = False
        failed = 0
        last_error: Exception | None = None

        for tag in hn_tags:
            try:
                async for doc in self._fetch_tag(tag):
                    yielded_any = True
                    yield doc
            except Exception as exc:
                failed += 1
                last_error = exc
                logger.warning(
                    "hackernews tag fetch failed, skipping: source=%s tag=%s error=%s",
                    self.source,
                    tag,
                    exc,
                )
                continue

        if failed == len(hn_tags) and not yielded_any and last_error is not None:
            raise IngestionError(f"all hackernews tags failed: {last_error}") from last_error

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = (2 ** (attempt - 1)) + random.uniform(0, 1)
        await asyncio.sleep(delay)

    async def _fetch_tag(self, tag: str) -> AsyncIterator[RawDocument]:
        limit = self.settings.hn_limit_per_tag
        clamped_page_size = min(max(self.settings.hn_page_size, 1), self.MAX_HITS_PER_PAGE)
        max_requests = -(-limit // max(clamped_page_size, 1)) + 2  # ceil + slack

        page = 0
        yielded = 0
        requests_made = 0

        while True:
            if yielded >= limit:
                return
            if requests_made >= max_requests:
                return

            requests_made += 1
            data = await self._request_page(tag, page)
            hits = data.get("hits") or []
            nb_pages = data.get("nbPages", 0)

            for hit in hits:
                if yielded >= limit:
                    return
                doc = self._normalize(hit, tag=tag, query=self.settings.hn_query)
                if doc is None:
                    continue
                yield doc
                yielded += 1

            page += 1
            if page >= nb_pages:
                return

    async def _request_page(self, tag: str, page: int) -> dict[str, Any]:
        clamped_page_size = min(max(self.settings.hn_page_size, 1), self.MAX_HITS_PER_PAGE)
        url = f"{self.API_BASE}/{self.SEARCH_ENDPOINT}"
        params: dict[str, Any] = {
            "tags": tag,
            "page": page,
            "hitsPerPage": clamped_page_size,
        }
        if self.settings.hn_query:
            params["query"] = self.settings.hn_query

        attempt = 0

        while True:
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    timeout=self.settings.request_timeout_seconds,
                )
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self.settings.max_retries:
                    raise IngestionError(f"transport error fetching hackernews tag={tag}") from exc
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 429:
                attempt += 1
                if attempt > self.settings.max_retries:
                    raise RateLimitError(f"rate limited fetching hackernews tag={tag}")
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

            if response.status_code >= 500:
                attempt += 1
                if attempt > self.settings.max_retries:
                    raise IngestionError(
                        f"server error fetching hackernews tag={tag}: {response.status_code}"
                    )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code != 200:
                raise IngestionError(
                    f"unexpected status fetching hackernews tag={tag}: {response.status_code}"
                )

            result: dict[str, Any] = response.json()
            return result

    @staticmethod
    def _normalize(hit: dict[str, Any], *, tag: str, query: str) -> RawDocument | None:
        if not isinstance(hit, dict):
            return None

        object_id = hit.get("objectID")
        if not object_id:
            return None
        source_id = str(object_id)

        title = hit.get("title")
        if not title:
            return None

        url = hit.get("url") or HackerNewsAdapter.HN_ITEM_URL.format(object_id=source_id)

        body = hit.get("story_text") or ""

        author = hit.get("author") or None

        created_at: datetime | None = None
        created_at_i = hit.get("created_at_i")
        if created_at_i is not None:
            try:
                created_at = datetime.fromtimestamp(float(created_at_i), tz=UTC)
            except (TypeError, ValueError, OSError):
                created_at = None
        if created_at is None:
            created_at_str = hit.get("created_at")
            if created_at_str:
                try:
                    parsed = datetime.fromisoformat(created_at_str)
                    created_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                    created_at = created_at.astimezone(UTC)
                except (TypeError, ValueError):
                    created_at = None
        if created_at is None:
            return None

        metadata: dict[str, Any] = {}
        for key in ("points", "num_comments"):
            if key in hit and hit[key] is not None:
                metadata[key] = hit[key]
        metadata["tags"] = tag
        metadata["query"] = query

        return RawDocument(
            source=HackerNewsAdapter.source,
            source_id=source_id,
            title=title,
            body=body,
            url=url,
            author=author,
            created_at=created_at,
            fetched_at=datetime.now(UTC),
            metadata=metadata,
        )
