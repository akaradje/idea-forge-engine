"""Tests for idea_forge.ingestion.reddit.RedditAdapter.

Covers spec acceptance criteria 4-15, 17 (specs/2026-07-24-reddit-ingestion-adapter.md §5).
All HTTP is mocked via respx; no live network calls are made.
"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from idea_forge.ingestion.reddit import RedditAdapter

from idea_forge.config import Settings
from idea_forge.ingestion.base import RawDocument
from idea_forge.ingestion.errors import (
    AuthError,
    IngestionError,
    RateLimitError,
    SubredditUnavailableError,
)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"


def make_settings(**overrides) -> Settings:
    base = dict(
        reddit_client_id="cid",
        reddit_client_secret="csecret",
        reddit_user_agent="idea-forge-tests/0.1",
        reddit_subreddits=["testsub"],
        reddit_listing="new",
        reddit_limit_per_subreddit=100,
        reddit_page_size=100,
        reddit_top_time_filter="week",
        request_timeout_seconds=1.0,
        max_retries=3,
    )
    base.update(overrides)
    return Settings(**base)


def token_response(token: str = "tok-123", expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})


def make_child(
    *,
    id: str = "abc123",
    name: str | None = None,
    title: str | None = "A problem worth solving",
    selftext: str | None = "I keep running into this issue...",
    author: str | None = "someuser",
    created_utc: float | None = 1_753_000_000.0,
    permalink: str | None = "/r/testsub/comments/abc123/a_problem/",
    url: str | None = "https://example.com/article",
    subreddit: str = "testsub",
    score: int = 42,
    num_comments: int = 7,
    upvote_ratio: float = 0.9,
    over_18: bool = False,
    link_flair_text: str | None = "Discussion",
) -> dict:
    data = {
        "id": id,
        "name": name if name is not None else f"t3_{id}",
        "title": title,
        "selftext": selftext,
        "author": author,
        "created_utc": created_utc,
        "permalink": permalink,
        "url": url,
        "subreddit": subreddit,
        "score": score,
        "num_comments": num_comments,
        "upvote_ratio": upvote_ratio,
        "over_18": over_18,
        "link_flair_text": link_flair_text,
    }
    # allow tests to simulate missing keys explicitly by passing None sentinels
    return {k: v for k, v in data.items() if v is not None or k in ("author", "selftext")}


def listing_response(children: list[dict], after: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "kind": "Listing",
            "data": {
                "children": [{"kind": "t3", "data": c} for c in children],
                "after": after,
            },
        },
    )


# --- Criterion 4: pagination across pages, in order --------------------------


@respx.mock
async def test_fetch_yields_all_posts_across_two_pages_in_order():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    page1 = listing_response(
        [make_child(id="p1", title="Post 1"), make_child(id="p2", title="Post 2")],
        after="t3_p2",
    )
    page2 = listing_response([make_child(id="p3", title="Post 3")], after=None)
    route = respx.get(f"{API_BASE}/r/testsub/new").mock(side_effect=[page1, page2])

    settings = make_settings()
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Post 1", "Post 2", "Post 3"]
    assert route.call_count == 2
    # second call must carry the `after` cursor from page 1
    second_call_params = dict(httpx.QueryParams(route.calls[1].request.url.params))
    assert second_call_params.get("after") == "t3_p2"


# --- Criterion 5: pagination stops at reddit_limit_per_subreddit -------------


@respx.mock
async def test_pagination_stops_at_configured_limit():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    page1 = listing_response(
        [make_child(id=f"p{i}") for i in range(100)],
        after="t3_p99",
    )
    page2 = listing_response(
        [make_child(id=f"q{i}") for i in range(100)],
        after="t3_q99",
    )
    route = respx.get(f"{API_BASE}/r/testsub/new").mock(side_effect=[page1, page2])

    settings = make_settings(reddit_limit_per_subreddit=120, reddit_page_size=100)
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert len(docs) == 120
    assert route.call_count == 2  # bounded: didn't fetch a third page


# --- Criterion 6: token cached, POST called exactly once ---------------------


@respx.mock
async def test_token_endpoint_called_once_and_reused_within_expiry():
    token_route = respx.post(TOKEN_URL).mock(return_value=token_response(expires_in=3600))
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=listing_response([make_child(id="p1")], after=None)
    )

    settings = make_settings()
    async with RedditAdapter(settings) as adapter:
        token_a = await adapter._get_token()
        token_b = await adapter._get_token()

    assert token_a == token_b == "tok-123"
    assert token_route.call_count == 1


@respx.mock
async def test_concurrent_get_token_calls_do_not_duplicate_fetch():
    token_route = respx.post(TOKEN_URL).mock(return_value=token_response(expires_in=3600))

    settings = make_settings()
    async with RedditAdapter(settings) as adapter:
        tokens = await asyncio.gather(*(adapter._get_token() for _ in range(5)))

    assert all(t == "tok-123" for t in tokens)
    assert token_route.call_count == 1


# --- Criterion 7: expired token triggers exactly one refresh -----------------


@respx.mock
async def test_expired_token_triggers_single_refresh(monkeypatch):
    token_route = respx.post(TOKEN_URL).mock(
        side_effect=[
            token_response(token="old-tok", expires_in=3600),
            token_response(token="new-tok", expires_in=3600),
        ]
    )

    settings = make_settings()
    async with RedditAdapter(settings) as adapter:
        first = await adapter._get_token()
        assert first == "old-tok"

        # force the cached token to look expired without waiting real time
        adapter._token.expires_at = asyncio.get_event_loop().time() - 1  # type: ignore[attr-defined]

        second = await adapter._get_token()

    assert second == "new-tok"
    assert token_route.call_count == 2


# --- Criterion 8: 401 triggers forced refresh + single retry -----------------


@respx.mock
async def test_401_triggers_forced_refresh_then_succeeds():
    respx.post(TOKEN_URL).mock(
        side_effect=[
            token_response(token="stale-tok", expires_in=3600),
            token_response(token="fresh-tok", expires_in=3600),
        ]
    )
    api_route = respx.get(f"{API_BASE}/r/testsub/new").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Unauthorized"}),
            listing_response([make_child(id="p1", title="Recovered post")], after=None),
        ]
    )

    settings = make_settings()
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Recovered post"]
    assert api_route.call_count == 2


@respx.mock
async def test_two_consecutive_401s_raise_auth_error():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )

    settings = make_settings()
    with pytest.raises(AuthError):
        async with RedditAdapter(settings) as adapter:
            async for _ in adapter.fetch():
                pass


# --- Criterion 9: 429 honors Retry-After, then RateLimitError on exhaustion --


@respx.mock
async def test_429_with_retry_after_sleeps_then_retries(monkeypatch):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            listing_response([make_child(id="p1", title="After backoff")], after=None),
        ]
    )

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    settings = make_settings()
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["After backoff"]
    assert sleep_mock.await_count >= 1
    slept_for = sleep_mock.await_args_list[0].args[0]
    assert slept_for == pytest.approx(2, abs=1)


@respx.mock
async def test_429_exhausting_retries_raises_rate_limit_error(monkeypatch):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    settings = make_settings(max_retries=3)
    with pytest.raises(RateLimitError):
        async with RedditAdapter(settings) as adapter:
            async for _ in adapter.fetch():
                pass


# --- Criterion 10: TimeoutException retried, then IngestionError -------------


@respx.mock
async def test_timeout_exception_retried_then_raises_ingestion_error(monkeypatch):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(side_effect=httpx.TimeoutException("timed out"))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    settings = make_settings(max_retries=3)
    with pytest.raises(IngestionError):
        async with RedditAdapter(settings) as adapter:
            async for _ in adapter.fetch():
                pass


@respx.mock
async def test_timeout_then_success_yields_docs(monkeypatch):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        side_effect=[
            httpx.TimeoutException("timed out"),
            listing_response([make_child(id="p1", title="Recovered after timeout")], after=None),
        ]
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    settings = make_settings()
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Recovered after timeout"]


# --- Criterion 11: 403/404 skips one subreddit, others still yield docs ------


@respx.mock
async def test_unavailable_subreddit_is_skipped_others_continue():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/private_sub/new").mock(return_value=httpx.Response(403))
    respx.get(f"{API_BASE}/r/missing_sub/new").mock(return_value=httpx.Response(404))
    respx.get(f"{API_BASE}/r/good_sub/new").mock(
        return_value=listing_response([make_child(id="p1", title="Good post")], after=None)
    )

    settings = make_settings(reddit_subreddits=["private_sub", "missing_sub", "good_sub"])
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert [d.title for d in docs] == ["Good post"]


async def test_fetch_subreddit_called_directly_propagates_unavailable_error():
    respx_mock_ctx = respx.mock
    with respx_mock_ctx() as mock:
        mock.post(TOKEN_URL).mock(return_value=token_response())
        mock.get(f"{API_BASE}/r/private_sub/new").mock(return_value=httpx.Response(403))

        settings = make_settings(reddit_subreddits=["private_sub"])
        async with RedditAdapter(settings) as adapter:
            with pytest.raises(SubredditUnavailableError):
                async for _ in adapter._fetch_subreddit("private_sub"):
                    pass


# --- Criterion 12: empty listing yields zero docs, no error ------------------


@respx.mock
async def test_empty_listing_yields_zero_docs_without_error():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(return_value=listing_response([], after=None))

    settings = make_settings()
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    assert docs == []


# --- Criterion 13: malformed children tolerated / skipped --------------------


def test_normalize_missing_id_returns_none():
    child = make_child(id="x")
    del child["id"]
    del child["name"]  # no fallback available either
    assert RedditAdapter._normalize(child) is None


def test_normalize_missing_title_returns_none():
    child = make_child(title="irrelevant")
    del child["title"]
    assert RedditAdapter._normalize(child) is None


def test_normalize_deleted_author_becomes_none():
    child = make_child(author="[deleted]")
    doc = RedditAdapter._normalize(child)
    assert doc is not None
    assert doc.author is None


def test_normalize_none_author_becomes_none():
    child = make_child(author=None)
    child["author"] = None
    doc = RedditAdapter._normalize(child)
    assert doc is not None
    assert doc.author is None


def test_normalize_missing_selftext_defaults_to_empty_body():
    child = make_child()
    del child["selftext"]
    doc = RedditAdapter._normalize(child)
    assert doc is not None
    assert doc.body == ""


def test_normalize_missing_created_utc_returns_none():
    child = make_child()
    del child["created_utc"]
    assert RedditAdapter._normalize(child) is None


def test_normalize_invalid_created_utc_returns_none():
    child = make_child()
    child["created_utc"] = "not-a-timestamp"
    assert RedditAdapter._normalize(child) is None


def test_normalize_non_dict_child_returns_none():
    assert RedditAdapter._normalize("not-a-dict") is None  # type: ignore[arg-type]


@respx.mock
async def test_fetch_skips_malformed_children_without_raising():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    good = make_child(id="good", title="Fine post")
    bad_no_title = make_child(id="bad1", title="placeholder")
    del bad_no_title["title"]
    bad_deleted_author = make_child(id="bad2", author="[deleted]", title="Deleted author post")
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=listing_response([good, bad_no_title, bad_deleted_author], after=None)
    )

    settings = make_settings()
    docs: list[RawDocument] = []
    async with RedditAdapter(settings) as adapter:
        async for doc in adapter.fetch():
            docs.append(doc)

    titles = [d.title for d in docs]
    assert "Fine post" in titles
    assert "Deleted author post" in titles
    assert len(docs) == 2  # bad_no_title was skipped
    deleted_doc = next(d for d in docs if d.title == "Deleted author post")
    assert deleted_doc.author is None


# --- Criterion 14: User-Agent on every request; empty raises AuthError -------


@respx.mock
async def test_every_request_includes_configured_user_agent():
    token_route = respx.post(TOKEN_URL).mock(return_value=token_response())
    api_route = respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=listing_response([make_child(id="p1")], after=None)
    )

    settings = make_settings(reddit_user_agent="my-custom-agent/1.0")
    async with RedditAdapter(settings) as adapter:
        async for _ in adapter.fetch():
            pass

    assert token_route.calls[0].request.headers["User-Agent"] == "my-custom-agent/1.0"
    assert api_route.calls[0].request.headers["User-Agent"] == "my-custom-agent/1.0"


@respx.mock
async def test_empty_user_agent_raises_auth_error_before_any_http_call():
    token_route = respx.post(TOKEN_URL).mock(return_value=token_response())
    api_route = respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=listing_response([make_child(id="p1")], after=None)
    )

    settings = make_settings(reddit_user_agent="")
    with pytest.raises(AuthError):
        async with RedditAdapter(settings) as adapter:
            async for _ in adapter.fetch():
                pass

    assert token_route.call_count == 0
    assert api_route.call_count == 0


# --- Criterion 15: listing type URL building / invalid listing -> ValueError -


@respx.mock
async def test_top_listing_builds_correct_url_with_time_filter():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    top_route = respx.get(f"{API_BASE}/r/testsub/top").mock(
        return_value=listing_response([make_child(id="p1")], after=None)
    )

    settings = make_settings(reddit_listing="top", reddit_top_time_filter="month")
    async with RedditAdapter(settings) as adapter:
        async for _ in adapter.fetch():
            pass

    assert top_route.call_count == 1
    params = dict(httpx.QueryParams(top_route.calls[0].request.url.params))
    assert params.get("t") == "month"


def test_invalid_listing_value_raises_value_error_at_construction():
    settings = make_settings(reddit_listing="controversial")
    with pytest.raises(ValueError):
        RedditAdapter(settings)


# --- Criterion 17: client lifecycle -------------------------------------------


@respx.mock
async def test_injected_client_is_not_closed_by_adapter():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=listing_response([make_child(id="p1")], after=None)
    )

    settings = make_settings()
    client = httpx.AsyncClient()
    async with RedditAdapter(settings, client=client) as adapter:
        async for _ in adapter.fetch():
            pass

    assert client.is_closed is False
    await client.aclose()


@respx.mock
async def test_self_created_client_is_closed_on_aexit():
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.get(f"{API_BASE}/r/testsub/new").mock(
        return_value=listing_response([make_child(id="p1")], after=None)
    )

    settings = make_settings()
    adapter = RedditAdapter(settings)
    async with adapter:
        async for _ in adapter.fetch():
            pass

    assert adapter._client.is_closed is True
