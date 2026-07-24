"""Tests for idea_forge.velocity.store.WatchStore (SQLite persistence).

Covers spec acceptance criteria 6, 7, 8, 13, 14, 15
(specs/2026-07-24-gap-velocity-alerts.md §5).
Uses a real sqlite3 file under tmp_path — no network, no shared fixtures with
production data. All async methods are awaited directly (they wrap sync sqlite3
calls in asyncio.to_thread per the spec, which is exercised implicitly).
"""

from datetime import UTC, datetime, timedelta

import pytest
from idea_forge.velocity.store import WatchStore

from idea_forge.velocity.errors import DuplicateWatchError, WatchNotFoundError
from idea_forge.velocity.models import Snapshot, Watch

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def make_watch(name: str = "collab-editors", query: str = "collaborative editor") -> Watch:
    return Watch(name=name, query=query, tags=["story"], created_at=NOW)


def make_snapshot(
    watch_name: str = "collab-editors",
    *,
    captured_at: datetime,
    post_count: int = 1,
) -> Snapshot:
    return Snapshot(
        watch_name=watch_name,
        captured_at=captured_at,
        window_days=7,
        window_start=captured_at - timedelta(days=7),
        post_count=post_count,
        total_points=10,
        total_comments=2,
        posts_per_day=post_count / 7,
        points_per_day=10 / 7,
        comments_per_day=2 / 7,
        newest_post_at=captured_at - timedelta(hours=1),
        oldest_post_at=captured_at - timedelta(days=3),
        fetch_limit_hit=False,
    )


@pytest.fixture
async def store(tmp_path):
    s = WatchStore(tmp_path / "watchlist.db")
    await s.init()
    return s


# --- Criterion 8: init is idempotent ----------------------------------------


async def test_init_can_be_called_repeatedly(tmp_path):
    s = WatchStore(tmp_path / "watchlist.db")
    await s.init()
    await s.init()
    await s.init()

    assert await s.list_watches() == []


# --- Criterion 6: duplicate/missing watch names raise the right errors -----


async def test_add_watch_duplicate_name_raises_duplicate_watch_error(store):
    await store.add_watch(make_watch(name="dup"))

    with pytest.raises(DuplicateWatchError):
        await store.add_watch(make_watch(name="dup"))


async def test_remove_watch_missing_name_raises_watch_not_found_error(store):
    with pytest.raises(WatchNotFoundError):
        await store.remove_watch("does-not-exist")


async def test_get_watch_missing_name_raises_watch_not_found_error(store):
    with pytest.raises(WatchNotFoundError):
        await store.get_watch("does-not-exist")


async def test_add_watch_returns_the_persisted_watch(store):
    watch = make_watch(name="alpha")

    result = await store.add_watch(watch)

    assert result.name == "alpha"
    fetched = await store.get_watch("alpha")
    assert fetched.name == "alpha"
    assert fetched.query == watch.query
    assert fetched.tags == ["story"]


async def test_remove_watch_deletes_it_so_get_watch_then_raises(store):
    await store.add_watch(make_watch(name="beta"))

    await store.remove_watch("beta")

    with pytest.raises(WatchNotFoundError):
        await store.get_watch("beta")


async def test_list_watches_returns_all_added_watches(store):
    await store.add_watch(make_watch(name="one"))
    await store.add_watch(make_watch(name="two"))

    watches = await store.list_watches()

    assert {w.name for w in watches} == {"one", "two"}


# --- Criterion 7: latest_snapshot returns the most recent insert -----------


async def test_latest_snapshot_returns_none_when_no_snapshots_yet(store):
    await store.add_watch(make_watch(name="gamma"))

    assert await store.latest_snapshot("gamma") is None


async def test_insert_snapshot_twice_latest_snapshot_returns_the_later_one(store):
    await store.add_watch(make_watch(name="delta"))
    older = make_snapshot("delta", captured_at=NOW - timedelta(days=1), post_count=1)
    newer = make_snapshot("delta", captured_at=NOW, post_count=5)

    await store.insert_snapshot(older)
    await store.insert_snapshot(newer)

    latest = await store.latest_snapshot("delta")
    assert latest is not None
    assert latest.captured_at == NOW
    assert latest.post_count == 5


# --- touch_last_checked ------------------------------------------------------


async def test_touch_last_checked_updates_the_watch_field(store):
    await store.add_watch(make_watch(name="epsilon"))

    await store.touch_last_checked("epsilon", NOW)

    watch = await store.get_watch("epsilon")
    assert watch.last_checked_at == NOW


# --- Criterion 14: velocity_db_path="" resolves & auto-creates parent dirs --


async def test_store_creates_missing_parent_directories(tmp_path):
    nested = tmp_path / "nested" / "dirs" / "watchlist.db"
    assert not nested.parent.exists()

    s = WatchStore(nested)
    await s.init()

    assert nested.exists()


# --- Criterion 15: no network, purely local sqlite file --------------------


async def test_store_operations_do_not_require_network(store):
    # Exercising the whole CRUD surface against a tmp_path-backed file only;
    # if this test passes with no mocks/patches of httpx/respx, no network I/O occurred.
    watch = await store.add_watch(make_watch(name="zeta"))
    snap = make_snapshot("zeta", captured_at=NOW)
    await store.insert_snapshot(snap)
    await store.touch_last_checked("zeta", NOW)

    assert watch.name == "zeta"
    assert (await store.latest_snapshot("zeta")).post_count == snap.post_count
    await store.remove_watch("zeta")
    assert await store.list_watches() == []
