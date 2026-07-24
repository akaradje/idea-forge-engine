"""Tests for idea_forge.ingestion.hackernews.HackerNewsAdapter.

Covers spec acceptance criteria 1-15, 17
(specs/2026-07-24-hackernews-ingestion-adapter.md §5).
All HTTP is mocked via respx; no live network calls are made.
"""

import asyncio
import math
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from idea_forge.config import Settings
from idea_forge.ingestion.base import RawDocument
from idea_forge.ingestion.errors import IngestionError, RateLimitError
from idea_forge.ingestion.hackernews import HackerNewsAdapter

API_BASE = "https://hn.algolia.com/api/v1"
SEARCH_URL = f"{API_BASE}/search_by_date"


def make_settings(**overrides) -> Settings:
    base = dict(
        reddit_client_id="cid",
        reddit_client_secret="csecret",
        reddit_user_agent="idea-forge-tests/0.1",
        hn_tags=["ask_hn", "show_hn"],
        hn_query="",
        hn_limit_per_tag=100,
        hn_page_size=100,
        request_timeout_seconds=1.0,
        max_retries=3,
    )
    base.update(overrides)
    return Settings(**base)


def make_hit(
    *,
    object_id: str = "12345",
    title: str | None = "Ask HN: is there a tool that...",
    story_text: str | None = "I keep looking for this...",
    author: str | None = "someuser",
    created_at_i: int | None = 1_753_000_000,
    created_at: str | None = "2025-07-20T12:00:00.000Z",
    url: str | None = None,
    points: int | None = 42,
    num_comments: int | None = 7,
) -> dict:
    data = {
        "objectID": object_id,
        "title": title,
        "story_text": story_text,
        "author": author,
        "created_at_i": created_at_i,
        "created_at": created_at,
        "url": url,
        "points": points,
        "num_comments": num_comments,
    }
    return data


def search_response(hits: list[dict], *, page: int = 0, nb_pages: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={"hits": hits, "page": page, "nbPages": nb_pages},
    )


# --- Criterion 1: unique_key ---------------------------------------------


def test_unique_key_is_source_prefixed_object_id():
    now = datetime.now(UTC)
    doc = RawDocument(
        source="hackernews",
        source_id="12345",
        title="t",
        body="",
        url="https://news.ycombinator.com/item?id=12345",
        author=None,
        created_at=now,
        fetched_at=now,
        metadata={},
    )
    assert doc.unique_key == "hackernews:12345"


# --- Criterion 2: pagination across two pages, in order -------------------


@respx.mock
async def test_fetch_yields_all_hits_across_two_pages_in_order():
    page1 = search_response(
        [make_hit(object_id="p1", title="Post 1"), make_hit(object_id="p2", title="Post 2")],
        page=0,
        nb_pages=2,
    )
    page2 = search_response([make_hit(object_id="p3", title="Post 3")], page=1, nb_pages=2)
    route = respx.get(SEARCH_URL).mock(side_effect=[page1, page2])

    settings = make_settings(hn_tags=["ask_hn"])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Post 1", "Post 2", "Post 3"]
    assert route.call_count == 2
    first_params = dict(httpx.QueryParams(route.calls[0].request.url.params))
    second_params = dict(httpx.QueryParams(route.calls[1].request.url.params))
    assert first_params.get("page") == "0"
    assert second_params.get("page") == "1"


# --- Criterion 3: pagination stops at hn_limit_per_tag ---------------------


@respx.mock
async def test_pagination_stops_at_configured_limit_per_tag():
    page1 = search_response([make_hit(object_id=f"p{i}") for i in range(100)], page=0, nb_pages=10)
    page2 = search_response([make_hit(object_id=f"q{i}") for i in range(100)], page=1, nb_pages=10)
    route = respx.get(SEARCH_URL).mock(side_effect=[page1, page2] + [page2] * 10)

    settings = make_settings(hn_tags=["ask_hn"], hn_limit_per_tag=120, hn_page_size=100)
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert len(docs) == 120
    # defensive ceiling: ceil(120/100)+2 = 4, bounded well below nbPages=10
    assert route.call_count <= 4


# --- Criterion 4: pagination stops when page >= nbPages --------------------


@respx.mock
async def test_pagination_stops_when_page_reaches_nb_pages():
    route = respx.get(SEARCH_URL).mock(
        return_value=search_response([make_hit(object_id="p1")], page=0, nb_pages=1)
    )

    settings = make_settings(hn_tags=["ask_hn"])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert len(docs) == 1
    assert route.call_count == 1


# --- Criterion 5: each configured tag produces a separate request ----------


@respx.mock
async def test_each_configured_tag_produces_separate_request_and_docs():
    def responder(request: httpx.Request) -> httpx.Response:
        params = dict(httpx.QueryParams(request.url.params))
        tag = params.get("tags")
        return search_response([make_hit(object_id=f"{tag}-1", title=f"From {tag}")])

    route = respx.get(SEARCH_URL).mock(side_effect=responder)

    settings = make_settings(hn_tags=["ask_hn", "show_hn"])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    titles = {d.title for d in docs}
    assert titles == {"From ask_hn", "From show_hn"}
    assert route.call_count == 2
    tags_requested = {
        dict(httpx.QueryParams(c.request.url.params)).get("tags") for c in route.calls
    }
    assert tags_requested == {"ask_hn", "show_hn"}


# --- Criterion 6: failing tag caught & logged, other tag still yields ------


@respx.mock
async def test_failing_tag_is_skipped_other_tag_still_yields_docs():
    def responder(request: httpx.Request) -> httpx.Response:
        params = dict(httpx.QueryParams(request.url.params))
        if params.get("tags") == "ask_hn":
            return httpx.Response(500)
        return search_response([make_hit(object_id="ok1", title="Good doc")])

    respx.get(SEARCH_URL).mock(side_effect=responder)

    settings = make_settings(hn_tags=["ask_hn", "show_hn"], max_retries=1)
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Good doc"]


@respx.mock
async def test_fetch_tag_called_directly_propagates_error():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))

    settings = make_settings(hn_tags=["ask_hn"], max_retries=1)
    async with HackerNewsAdapter(settings) as adapter:
        with pytest.raises(IngestionError):
            async for _ in adapter._fetch_tag("ask_hn"):
                pass


# --- Criterion 7: 429 honors Retry-After, exhaustion raises RateLimitError --


@respx.mock
async def test_429_with_retry_after_sleeps_then_retries(monkeypatch):
    respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            search_response([make_hit(object_id="p1", title="After backoff")]),
        ]
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    settings = make_settings(hn_tags=["ask_hn"])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["After backoff"]
    assert sleep_mock.await_count >= 1
    slept_for = sleep_mock.await_args_list[0].args[0]
    assert slept_for == pytest.approx(2, abs=1)


@respx.mock
@pytest.mark.parametrize("retry_after", ["inf", "nan", "-5", "999999"])
async def test_429_hostile_retry_after_never_sleeps_unbounded(monkeypatch, retry_after):
    respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": retry_after}),
            search_response([make_hit(object_id="p1", title="Recovered")]),
        ]
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    settings = make_settings(hn_tags=["ask_hn"])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Recovered"]
    for call in sleep_mock.await_args_list:
        delay = call.args[0]
        assert math.isfinite(delay) and 0 <= delay <= 60


@respx.mock
async def test_429_exhausting_retries_raises_rate_limit_error(monkeypatch):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    settings = make_settings(hn_tags=["ask_hn"], max_retries=3)
    async with HackerNewsAdapter(settings) as adapter:
        with pytest.raises(RateLimitError):
            async for _ in adapter._fetch_tag("ask_hn"):
                pass

    assert sleep_mock.await_count >= 1


# --- Criterion 8: transport errors / 5xx retried, then IngestionError ------


@respx.mock
async def test_transport_error_retried_then_raises_ingestion_error(monkeypatch):
    route = respx.get(SEARCH_URL).mock(side_effect=httpx.TransportError("boom"))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    settings = make_settings(hn_tags=["ask_hn"], max_retries=3)
    async with HackerNewsAdapter(settings) as adapter:
        with pytest.raises(IngestionError):
            async for _ in adapter._fetch_tag("ask_hn"):
                pass

    assert route.call_count >= 1
    assert sleep_mock.await_count >= 1


@respx.mock
async def test_500_retried_then_success_yields_docs(monkeypatch):
    respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(500),
            search_response([make_hit(object_id="p1", title="Recovered after 500")]),
        ]
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    settings = make_settings(hn_tags=["ask_hn"])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Recovered after 500"]


# --- Criterion 9: non-retryable 4xx raises IngestionError immediately ------


@respx.mock
async def test_non_retryable_4xx_raises_ingestion_error_immediately(monkeypatch):
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(400))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    settings = make_settings(hn_tags=["ask_hn"], max_retries=3)
    async with HackerNewsAdapter(settings) as adapter:
        with pytest.raises(IngestionError):
            async for _ in adapter._fetch_tag("ask_hn"):
                pass

    assert route.call_count == 1
    sleep_mock.assert_not_called()


# --- Criterion 10: empty page yields zero docs, no error -------------------


@respx.mock
async def test_empty_page_yields_zero_docs_without_error():
    respx.get(SEARCH_URL).mock(return_value=search_response([], page=0, nb_pages=1))

    settings = make_settings(hn_tags=["ask_hn"])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert docs == []


# --- Criterion 11: _normalize skips malformed hits --------------------------


def test_normalize_missing_object_id_returns_none():
    hit = make_hit()
    del hit["objectID"]
    assert HackerNewsAdapter._normalize(hit, tag="ask_hn", query="") is None


def test_normalize_empty_object_id_returns_none():
    hit = make_hit(object_id="")
    assert HackerNewsAdapter._normalize(hit, tag="ask_hn", query="") is None


def test_normalize_missing_title_returns_none():
    hit = make_hit()
    del hit["title"]
    assert HackerNewsAdapter._normalize(hit, tag="ask_hn", query="") is None


def test_normalize_null_title_returns_none():
    hit = make_hit(title=None)
    assert HackerNewsAdapter._normalize(hit, tag="ask_hn", query="") is None


def test_normalize_non_dict_hit_returns_none():
    assert HackerNewsAdapter._normalize("not-a-dict", tag="ask_hn", query="") is None  # type: ignore[arg-type]


# --- Criterion 12: url/story_text/author fallback rules ---------------------


def test_normalize_null_url_builds_hn_item_url():
    hit = make_hit(object_id="999", url=None)
    doc = HackerNewsAdapter._normalize(hit, tag="ask_hn", query="")
    assert doc is not None
    assert doc.url == "https://news.ycombinator.com/item?id=999"


def test_normalize_null_story_text_becomes_empty_body():
    hit = make_hit(story_text=None)
    doc = HackerNewsAdapter._normalize(hit, tag="ask_hn", query="")
    assert doc is not None
    assert doc.body == ""


def test_normalize_null_author_becomes_none():
    hit = make_hit(author=None)
    doc = HackerNewsAdapter._normalize(hit, tag="ask_hn", query="")
    assert doc is not None
    assert doc.author is None


# --- Criterion 13: created_at parsing rules ---------------------------------


def test_normalize_uses_created_at_i_epoch_for_tz_aware_utc():
    hit = make_hit(created_at_i=1_753_000_000, created_at=None)
    doc = HackerNewsAdapter._normalize(hit, tag="ask_hn", query="")
    assert doc is not None
    assert doc.created_at is not None
    assert doc.created_at.tzinfo is not None


def test_normalize_falls_back_to_iso_created_at_when_epoch_absent():
    hit = make_hit(created_at_i=None, created_at="2025-07-20T12:00:00.000Z")
    doc = HackerNewsAdapter._normalize(hit, tag="ask_hn", query="")
    assert doc is not None
    assert doc.created_at is not None
    assert doc.created_at.tzinfo is not None


def test_normalize_both_created_fields_missing_returns_none():
    hit = make_hit(created_at_i=None, created_at=None)
    assert HackerNewsAdapter._normalize(hit, tag="ask_hn", query="") is None


def test_normalize_both_created_fields_invalid_returns_none():
    hit = make_hit(created_at_i="not-a-number", created_at="also-not-a-date")
    assert HackerNewsAdapter._normalize(hit, tag="ask_hn", query="") is None


# --- Criterion 14: metadata capture -----------------------------------------


def test_normalize_metadata_contains_points_num_comments_tags_query():
    hit = make_hit(points=42, num_comments=7)
    doc = HackerNewsAdapter._normalize(hit, tag="ask_hn", query="widget")
    assert doc is not None
    assert doc.metadata["points"] == 42
    assert doc.metadata["num_comments"] == 7
    assert doc.metadata["tags"] == "ask_hn"
    assert doc.metadata["query"] == "widget"


def test_normalize_metadata_query_present_even_when_empty_string():
    hit = make_hit()
    doc = HackerNewsAdapter._normalize(hit, tag="show_hn", query="")
    assert doc is not None
    assert doc.metadata["query"] == ""
    assert doc.metadata["tags"] == "show_hn"


# --- Criterion 15: no auth header; hitsPerPage clamped; query only when set -


@respx.mock
async def test_requests_carry_no_authorization_header():
    route = respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=["ask_hn"])
    async with HackerNewsAdapter(settings) as adapter:
        async for _ in adapter.fetch():
            pass

    assert "authorization" not in {h.lower() for h in route.calls[0].request.headers}


@respx.mock
async def test_hits_per_page_clamped_above_max():
    route = respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=["ask_hn"], hn_page_size=5000)
    async with HackerNewsAdapter(settings) as adapter:
        async for _ in adapter.fetch():
            pass

    params = dict(httpx.QueryParams(route.calls[0].request.url.params))
    assert params.get("hitsPerPage") == "1000"


@respx.mock
async def test_hits_per_page_clamped_below_min():
    route = respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=["ask_hn"], hn_page_size=0)
    async with HackerNewsAdapter(settings) as adapter:
        async for _ in adapter.fetch():
            pass

    params = dict(httpx.QueryParams(route.calls[0].request.url.params))
    assert params.get("hitsPerPage") == "1"


@respx.mock
async def test_query_param_present_only_when_hn_query_non_empty():
    route = respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=["ask_hn"], hn_query="widget")
    async with HackerNewsAdapter(settings) as adapter:
        async for _ in adapter.fetch():
            pass

    params = dict(httpx.QueryParams(route.calls[0].request.url.params))
    assert params.get("query") == "widget"


@respx.mock
async def test_query_param_absent_when_hn_query_empty():
    route = respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=["ask_hn"], hn_query="")
    async with HackerNewsAdapter(settings) as adapter:
        async for _ in adapter.fetch():
            pass

    params = dict(httpx.QueryParams(route.calls[0].request.url.params))
    assert "query" not in params


# --- Criterion 17: client lifecycle -----------------------------------------


@respx.mock
async def test_injected_client_is_not_closed_by_adapter():
    respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=["ask_hn"])
    client = httpx.AsyncClient()
    async with HackerNewsAdapter(settings, client=client) as adapter:
        async for _ in adapter.fetch():
            pass

    assert client.is_closed is False
    await client.aclose()


@respx.mock
async def test_self_created_client_is_closed_on_aexit():
    respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=["ask_hn"])
    adapter = HackerNewsAdapter(settings)
    async with adapter:
        async for _ in adapter.fetch():
            pass

    assert adapter._client.is_closed is True


# --- Extra: empty hn_tags yields nothing, not an error (spec §4.14) --------


@respx.mock
async def test_empty_hn_tags_yields_nothing():
    route = respx.get(SEARCH_URL).mock(return_value=search_response([make_hit()]))

    settings = make_settings(hn_tags=[])
    docs: list[RawDocument] = []
    async with HackerNewsAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert docs == []
    assert route.call_count == 0
