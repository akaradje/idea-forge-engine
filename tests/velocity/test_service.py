"""Tests for idea_forge.velocity.service.VelocityService (orchestration).

Covers spec acceptance criteria 9, 10, 12 (service-level), 14, 15
(specs/2026-07-24-gap-velocity-alerts.md §5).
HN/Algolia calls are mocked via respx at the HTTP boundary (same pattern as
tests/ingestion/test_hackernews.py); WatchStore runs against a tmp_path sqlite
file. No live network is hit.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from idea_forge.config import Settings
from idea_forge.ingestion.errors import IngestionError
from idea_forge.velocity.errors import WatchNotFoundError
from idea_forge.velocity.store import WatchStore

API_BASE = "https://hn.algolia.com/api/v1"
SEARCH_URL = f"{API_BASE}/search_by_date"


def make_settings(**overrides) -> Settings:
    base = dict(
        reddit_client_id="cid",
        reddit_client_secret="csecret",
        reddit_user_agent="idea-forge-tests/0.1",
        hn_tags=["story"],
        hn_query="",
        hn_limit_per_tag=100,
        hn_page_size=100,
        request_timeout_seconds=1.0,
        max_retries=3,
        velocity_db_path="",
        velocity_window_days=7,
        velocity_max_evidence=10,
        velocity_limit_per_check=100,
    )
    base.update(overrides)
    return Settings(**base)


def make_hit(
    *,
    object_id: str,
    title: str = "post",
    created_at_i: int,
    points: int = 10,
    num_comments: int = 2,
) -> dict:
    return {
        "objectID": object_id,
        "title": title,
        "story_text": "",
        "author": "someuser",
        "created_at_i": created_at_i,
        "created_at": None,
        "url": None,
        "points": points,
        "num_comments": num_comments,
    }


def search_response(hits: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"hits": hits, "page": 0, "nbPages": 1})


@pytest.fixture
async def store(tmp_path):
    s = WatchStore(tmp_path / "watchlist.db")
    await s.init()
    return s


def _service(store, **overrides):
    from idea_forge.velocity.service import VelocityService

    return VelocityService(make_settings(**overrides), store)


# --- watch_gap / unwatch_gap / list_watches ---------------------------------


async def test_watch_gap_persists_a_new_watch(store):
    service = _service(store)

    watch = await service.watch_gap("collab-editors", "collaborative editor", ["story"])

    assert watch.name == "collab-editors"
    listed = await service.list_watches()
    assert "collab-editors" in [w.name for w in listed.watches]


async def test_unwatch_gap_missing_name_raises_watch_not_found_error(store):
    service = _service(store)

    with pytest.raises(WatchNotFoundError):
        await service.unwatch_gap("nope")


# --- Criterion 9: first check_velocity -> previous=None, delta=None --------


@respx.mock
async def test_check_velocity_first_time_has_no_previous_or_delta(store):
    now_epoch = int(datetime.now(UTC).timestamp())
    respx.get(SEARCH_URL).mock(
        return_value=search_response(
            [
                make_hit(object_id="p1", created_at_i=now_epoch - 3600),
                make_hit(object_id="p2", created_at_i=now_epoch - 7200),
            ]
        )
    )
    service = _service(store)
    await service.watch_gap("collab-editors", "collaborative editor", ["story"])

    report = await service.check_velocity("collab-editors")

    assert report.previous is None
    assert report.delta is None
    assert report.current.post_count == 2
    assert len(report.new_evidence) == 2
    assert "Evidence only" in report.note


# --- Criterion 10: second check_velocity -> delta present, evidence filtered


@respx.mock
async def test_check_velocity_second_time_has_delta_and_new_evidence_only(store):
    now_epoch = int(datetime.now(UTC).timestamp())
    old_hit = make_hit(object_id="old1", created_at_i=now_epoch - 3600 * 5)
    # ต้องใหม่กว่า previous.captured_at (= เวลาเช็คครั้งแรก ≈ ตอนนี้) ตามนิยาม
    # new_evidence ในสเปค จึงตั้ง timestamp ล้ำไปข้างหน้าเล็กน้อย
    new_hit = make_hit(object_id="new1", created_at_i=now_epoch + 3600)
    first_hits = [old_hit]
    second_hits = [old_hit, new_hit]
    route = respx.get(SEARCH_URL).mock(
        side_effect=[search_response(first_hits), search_response(second_hits)]
    )
    service = _service(store)
    await service.watch_gap("collab-editors", "collaborative editor", ["story"])

    first_report = await service.check_velocity("collab-editors")
    second_report = await service.check_velocity("collab-editors")

    assert route.call_count == 2
    assert second_report.previous is not None
    assert second_report.previous.captured_at == first_report.current.captured_at
    assert second_report.delta is not None
    evidence_keys = {e.unique_key for e in second_report.new_evidence}
    assert evidence_keys == {"hackernews:new1"}


async def test_check_velocity_unknown_watch_raises_watch_not_found_error(store):
    service = _service(store)

    with pytest.raises(WatchNotFoundError):
        await service.check_velocity("never-registered")


# --- Edge case 10: HN fetch failure does not insert a snapshot -------------


@respx.mock
async def test_check_velocity_ingestion_error_does_not_insert_snapshot_or_touch_watch(store):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))
    service = _service(store, max_retries=1)
    await service.watch_gap("collab-editors", "collaborative editor", ["story"])

    with pytest.raises(IngestionError):
        await service.check_velocity("collab-editors")

    assert await store.latest_snapshot("collab-editors") is None
    watch = await store.get_watch("collab-editors")
    assert watch.last_checked_at is None


# --- Criterion 15: no live network, tmp_path db -----------------------------


@respx.mock
async def test_check_velocity_uses_only_mocked_http_no_live_network(store):
    now_epoch = int(datetime.now(UTC).timestamp())
    route = respx.get(SEARCH_URL).mock(
        return_value=search_response([make_hit(object_id="p1", created_at_i=now_epoch - 60)])
    )
    service = _service(store)
    await service.watch_gap("w", "query", ["story"])

    await service.check_velocity("w")

    assert route.call_count == 1


# --- Evidence trimmed to velocity_max_evidence ------------------------------


@respx.mock
async def test_check_velocity_evidence_trimmed_to_max_evidence(store):
    now_epoch = int(datetime.now(UTC).timestamp())
    hits = [make_hit(object_id=f"p{i}", created_at_i=now_epoch - i * 60) for i in range(5)]
    respx.get(SEARCH_URL).mock(return_value=search_response(hits))
    service = _service(store, velocity_max_evidence=2)
    await service.watch_gap("w", "query", ["story"])

    report = await service.check_velocity("w")

    assert len(report.new_evidence) == 2


# --- Bug 1 regression: fetch_limit_hit must be computed per-tag, not on the
# combined (all tags summed) doc count -----------------------------------


@respx.mock
async def test_check_velocity_multi_tag_fetch_limit_hit_false_when_each_tag_under_cap(store):
    """Two tags, 60 in-window posts each (120 combined >= cap 100), neither
    tag individually hits the per-tag cap of 100 -> fetch_limit_hit False."""
    now_epoch = int(datetime.now(UTC).timestamp())

    def responder(request: httpx.Request) -> httpx.Response:
        params = dict(httpx.QueryParams(request.url.params))
        tag = params.get("tags")
        hits = [
            make_hit(object_id=f"{tag}-{i}", created_at_i=now_epoch - i * 60) for i in range(60)
        ]
        return search_response(hits)

    respx.get(SEARCH_URL).mock(side_effect=responder)
    service = _service(store, velocity_limit_per_check=100)
    await service.watch_gap("multi", "query", ["ask_hn", "show_hn"])

    report = await service.check_velocity("multi")

    assert report.current.post_count == 120
    assert report.current.fetch_limit_hit is False


@respx.mock
async def test_check_velocity_single_tag_hitting_cap_sets_fetch_limit_hit_true(store):
    now_epoch = int(datetime.now(UTC).timestamp())
    hits = [make_hit(object_id=f"p{i}", created_at_i=now_epoch - i * 60) for i in range(100)]
    respx.get(SEARCH_URL).mock(return_value=search_response(hits))
    service = _service(store, velocity_limit_per_check=100)
    await service.watch_gap("single", "query", ["story"])

    report = await service.check_velocity("single")

    assert report.current.fetch_limit_hit is True


# --- Bug 2 regression: dedup the same post across multiple tags ------------


@respx.mock
async def test_check_velocity_dedup_same_post_across_tags_counted_once(store):
    now_epoch = int(datetime.now(UTC).timestamp())
    dup_hit = make_hit(object_id="dup1", created_at_i=now_epoch - 60)
    respx.get(SEARCH_URL).mock(return_value=search_response([dup_hit]))
    service = _service(store)
    await service.watch_gap("dupwatch", "query", ["ask_hn", "show_hn"])

    report = await service.check_velocity("dupwatch")

    assert report.current.post_count == 1
    assert len(report.new_evidence) == 1
    assert report.new_evidence[0].unique_key == "hackernews:dup1"
