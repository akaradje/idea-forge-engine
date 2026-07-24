"""Tests for the 4 velocity MCP tools in idea_forge.mcp_server
(watch_gap, unwatch_gap, list_watches, check_velocity).

Covers spec acceptance criteria 11, 12, 13, 14
(specs/2026-07-24-gap-velocity-alerts.md §5).
Follows the pattern in tests/test_mcp_server.py: the underlying VelocityService
is monkeypatched with a fake that captures calls / raises recorded errors, so
no real sqlite or HTTP happens here (those are covered in test_store.py and
test_service.py). No live network, no real MCP client.
"""

import json
from datetime import UTC, datetime

import pytest

import idea_forge.mcp_server as mcp_server
from idea_forge.config import Settings, resolve_velocity_db_path
from idea_forge.velocity.errors import DuplicateWatchError, StorageError, WatchNotFoundError
from idea_forge.velocity.models import Watch, WatchListResult
from idea_forge.velocity.store import WatchStore

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def make_watch(name: str = "collab-editors") -> Watch:
    return Watch(name=name, query="collaborative editor", tags=["story"], created_at=NOW)


class FakeVelocityService:
    """Stand-in for velocity.service.VelocityService: captures constructor args,
    each method is a configurable stub set via class-level toggles.
    """

    last_settings = None
    last_store: "WatchStore | None" = None
    constructed = False

    watch_gap_return: Watch | None = None
    watch_gap_side_effect: Exception | None = None
    unwatch_gap_side_effect: Exception | None = None
    list_watches_return: WatchListResult | None = None
    check_velocity_return: object = None
    check_velocity_side_effect: Exception | None = None

    def __init__(self, settings, store) -> None:
        type(self).last_settings = settings
        type(self).last_store = store
        type(self).constructed = True

    async def watch_gap(self, name, query, tags):
        if self.watch_gap_side_effect is not None:
            raise self.watch_gap_side_effect
        return self.watch_gap_return or make_watch(name)

    async def unwatch_gap(self, name):
        if self.unwatch_gap_side_effect is not None:
            raise self.unwatch_gap_side_effect
        return None

    async def list_watches(self):
        return self.list_watches_return or WatchListResult(watches=[], count=0)

    async def check_velocity(self, name):
        if self.check_velocity_side_effect is not None:
            raise self.check_velocity_side_effect
        return self.check_velocity_return


def reset_fake(**kwargs) -> None:
    FakeVelocityService.last_settings = None
    FakeVelocityService.last_store = None
    FakeVelocityService.constructed = False
    FakeVelocityService.watch_gap_return = kwargs.get("watch_gap_return")
    FakeVelocityService.watch_gap_side_effect = kwargs.get("watch_gap_side_effect")
    FakeVelocityService.unwatch_gap_side_effect = kwargs.get("unwatch_gap_side_effect")
    FakeVelocityService.list_watches_return = kwargs.get("list_watches_return")
    FakeVelocityService.check_velocity_return = kwargs.get("check_velocity_return")
    FakeVelocityService.check_velocity_side_effect = kwargs.get("check_velocity_side_effect")


@pytest.fixture(autouse=True)
def _reset_fakes():
    reset_fake()
    yield
    reset_fake()


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"):
        monkeypatch.delenv(var, raising=False)
    # impl สร้าง WatchStore จริงเสมอ (fake เฉพาะ VelocityService) — ชี้ db
    # ไป tmp_path กันเทสต์ไปสร้างไฟล์ใน ~/.idea-forge จริง
    monkeypatch.setenv("VELOCITY_DB_PATH", str(tmp_path / "mcp" / "watchlist.db"))


# --- Criterion 12: tools return JSON string or "Error: ..." — never raise --


async def test_watch_gap_success_returns_parseable_json(monkeypatch):
    reset_fake(watch_gap_return=make_watch("collab-editors"))
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._watch_gap_impl("collab-editors", "collaborative editor", "story")

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["name"] == "collab-editors"


# --- Criterion 11: watch_gap duplicate name -> "Error: ..." ----------------


async def test_watch_gap_duplicate_name_returns_error_string(monkeypatch):
    reset_fake(watch_gap_side_effect=DuplicateWatchError("collab-editors"))
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._watch_gap_impl("collab-editors", "query", "story")

    assert result.startswith("Error:")
    assert "collab-editors" in result


# --- Criterion 11: check_velocity with unknown name -> "Error: ..." --------


async def test_check_velocity_unknown_name_returns_error_string(monkeypatch):
    reset_fake(check_velocity_side_effect=WatchNotFoundError("ghost"))
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._check_velocity_impl("ghost")

    assert result.startswith("Error:")
    assert "ghost" in result
    assert "watch_gap" in result


async def test_unwatch_gap_unknown_name_returns_error_string(monkeypatch):
    reset_fake(unwatch_gap_side_effect=WatchNotFoundError("ghost"))
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._unwatch_gap_impl("ghost")

    assert result.startswith("Error:")


async def test_unwatch_gap_success_returns_json_not_error(monkeypatch):
    reset_fake()
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._unwatch_gap_impl("collab-editors")

    assert not result.startswith("Error:")


async def test_list_watches_success_returns_parseable_json(monkeypatch):
    reset_fake(
        list_watches_return=WatchListResult(watches=[make_watch("a"), make_watch("b")], count=2)
    )
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._list_watches_impl()

    payload = json.loads(result)
    assert {w["name"] for w in payload["watches"]} == {"a", "b"}


# --- Criterion 8/9/edge: name/query validation before touching the store ---


async def test_watch_gap_blank_name_returns_error_without_constructing_service(monkeypatch):
    reset_fake()
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._watch_gap_impl("   ", "query", "story")

    assert result.startswith("Error:")
    assert "name" in result
    assert FakeVelocityService.constructed is False


async def test_watch_gap_blank_query_returns_error_without_constructing_service(monkeypatch):
    reset_fake()
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._watch_gap_impl("name", "   ", "story")

    assert result.startswith("Error:")
    assert FakeVelocityService.constructed is False


async def test_watch_gap_blank_tags_falls_back_to_story(monkeypatch):
    reset_fake(watch_gap_return=make_watch("name"))
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)
    captured = {}

    class CapturingFake(FakeVelocityService):
        async def watch_gap(self, name, query, tags):
            captured["tags"] = tags
            return make_watch(name)

    monkeypatch.setattr(mcp_server, "VelocityService", CapturingFake)

    await mcp_server._watch_gap_impl("name", "query", "")

    assert captured["tags"] == ["story"]


# --- Criterion 7: StorageError surfaces as an "Error: StorageError: ..." ---


async def test_check_velocity_storage_error_returns_error_string(monkeypatch):
    reset_fake(check_velocity_side_effect=StorageError("db is locked"))
    monkeypatch.setattr(mcp_server, "VelocityService", FakeVelocityService)

    result = await mcp_server._check_velocity_impl("collab-editors")

    assert result.startswith("Error: StorageError")


# --- Criterion 14: velocity_db_path="" resolves to ~/.idea-forge/watchlist.db
# (helper อยู่ที่ idea_forge.config ตามสเปค §2 และรับ Settings ไม่ใช่ str)


def _settings_with_db_path(value: str) -> Settings:
    return Settings(
        reddit_client_id="cid",
        reddit_client_secret="csecret",
        reddit_user_agent="idea-forge-tests/0.1",
        velocity_db_path=value,
    )


def test_resolve_velocity_db_path_empty_defaults_under_home_idea_forge(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    # expanduser อ่านจาก env: USERPROFILE บน Windows, HOME บน POSIX
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))

    resolved = resolve_velocity_db_path(_settings_with_db_path(""))

    assert resolved == fake_home / ".idea-forge" / "watchlist.db"
    assert resolved.parent.exists()


def test_resolve_velocity_db_path_explicit_value_expands_user_and_creates_dirs(tmp_path):
    target = tmp_path / "custom" / "nested" / "watchlist.db"

    resolved = resolve_velocity_db_path(_settings_with_db_path(str(target)))

    assert resolved == target
    assert resolved.parent.exists()


# --- Criterion 13: module exposes the 4 tools + their _*_impl counterparts --


def test_module_exposes_velocity_tools_and_impls():
    assert callable(mcp_server.watch_gap)
    assert callable(mcp_server.unwatch_gap)
    assert callable(mcp_server.list_watches)
    assert callable(mcp_server.check_velocity)
    assert callable(mcp_server._watch_gap_impl)
    assert callable(mcp_server._unwatch_gap_impl)
    assert callable(mcp_server._list_watches_impl)
    assert callable(mcp_server._check_velocity_impl)
    assert mcp_server.watch_gap.__doc__
    assert mcp_server.check_velocity.__doc__
