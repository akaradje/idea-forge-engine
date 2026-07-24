"""SQLite persistence for watches and snapshots, wrapped in asyncio.to_thread."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from idea_forge.velocity.errors import DuplicateWatchError, StorageError, WatchNotFoundError
from idea_forge.velocity.models import Snapshot, Watch

_CREATE_WATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS watches (
    name TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    tags TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT
);
"""

_CREATE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_name TEXT NOT NULL REFERENCES watches(name) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    post_count INTEGER NOT NULL,
    total_points INTEGER NOT NULL,
    total_comments INTEGER NOT NULL,
    posts_per_day REAL NOT NULL,
    points_per_day REAL NOT NULL,
    comments_per_day REAL NOT NULL,
    newest_post_at TEXT,
    oldest_post_at TEXT,
    fetch_limit_hit INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_SNAPSHOTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_snapshots_watch_captured
    ON snapshots(watch_name, captured_at DESC);
"""

_CONNECT_TIMEOUT_SECONDS = 10.0


def _isoformat(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        name=row["name"],
        query=row["query"],
        tags=[t for t in row["tags"].split(",") if t],
        created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
        last_checked_at=_parse_dt(row["last_checked_at"]),
    )


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        watch_name=row["watch_name"],
        captured_at=_parse_dt(row["captured_at"]) or datetime.now(UTC),
        window_days=row["window_days"],
        window_start=_parse_dt(row["window_start"]) or datetime.now(UTC),
        post_count=row["post_count"],
        total_points=row["total_points"],
        total_comments=row["total_comments"],
        posts_per_day=row["posts_per_day"],
        points_per_day=row["points_per_day"],
        comments_per_day=row["comments_per_day"],
        newest_post_at=_parse_dt(row["newest_post_at"]),
        oldest_post_at=_parse_dt(row["oldest_post_at"]),
        fetch_limit_hit=bool(row["fetch_limit_hit"]),
    )


class WatchStore:
    """SQLite-backed persistence for watches and snapshots.

    Every method that touches sqlite runs in asyncio.to_thread; each call opens
    and closes its own sqlite3 connection (no connection shared across threads).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=_CONNECT_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _sync_init(self) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute(_CREATE_WATCHES_TABLE)
                conn.execute(_CREATE_SNAPSHOTS_TABLE)
                conn.execute(_CREATE_SNAPSHOTS_INDEX)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to initialize database: {exc}") from exc

    async def init(self) -> None:
        await asyncio.to_thread(self._sync_init)

    def _sync_add_watch(self, watch: Watch) -> Watch:
        try:
            conn = self._connect()
            try:
                try:
                    conn.execute(
                        "INSERT INTO watches (name, query, tags, created_at, last_checked_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            watch.name,
                            watch.query,
                            ",".join(watch.tags),
                            _isoformat(watch.created_at),
                            _isoformat(watch.last_checked_at) if watch.last_checked_at else None,
                        ),
                    )
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    raise DuplicateWatchError(f"watch '{watch.name}' already exists") from exc
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to add watch: {exc}") from exc
        return watch

    async def add_watch(self, watch: Watch) -> Watch:
        return await asyncio.to_thread(self._sync_add_watch, watch)

    def _sync_remove_watch(self, name: str) -> None:
        try:
            conn = self._connect()
            try:
                cursor = conn.execute("DELETE FROM watches WHERE name = ?", (name,))
                conn.commit()
                if cursor.rowcount == 0:
                    raise WatchNotFoundError(f"no watch named '{name}'")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to remove watch: {exc}") from exc

    async def remove_watch(self, name: str) -> None:
        await asyncio.to_thread(self._sync_remove_watch, name)

    def _sync_get_watch(self, name: str) -> Watch:
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM watches WHERE name = ?", (name,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to get watch: {exc}") from exc
        if row is None:
            raise WatchNotFoundError(f"no watch named '{name}'")
        return _row_to_watch(row)

    async def get_watch(self, name: str) -> Watch:
        return await asyncio.to_thread(self._sync_get_watch, name)

    def _sync_list_watches(self) -> list[Watch]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM watches ORDER BY name").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to list watches: {exc}") from exc
        return [_row_to_watch(row) for row in rows]

    async def list_watches(self) -> list[Watch]:
        return await asyncio.to_thread(self._sync_list_watches)

    def _sync_insert_snapshot(self, snapshot: Snapshot) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO snapshots ("
                    "watch_name, captured_at, window_days, window_start, post_count, "
                    "total_points, total_comments, posts_per_day, points_per_day, "
                    "comments_per_day, newest_post_at, oldest_post_at, fetch_limit_hit"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot.watch_name,
                        _isoformat(snapshot.captured_at),
                        snapshot.window_days,
                        _isoformat(snapshot.window_start),
                        snapshot.post_count,
                        snapshot.total_points,
                        snapshot.total_comments,
                        snapshot.posts_per_day,
                        snapshot.points_per_day,
                        snapshot.comments_per_day,
                        _isoformat(snapshot.newest_post_at) if snapshot.newest_post_at else None,
                        _isoformat(snapshot.oldest_post_at) if snapshot.oldest_post_at else None,
                        int(snapshot.fetch_limit_hit),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to insert snapshot: {exc}") from exc

    async def insert_snapshot(self, snapshot: Snapshot) -> None:
        await asyncio.to_thread(self._sync_insert_snapshot, snapshot)

    def _sync_latest_snapshot(self, watch_name: str) -> Snapshot | None:
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM snapshots WHERE watch_name = ? "
                    "ORDER BY captured_at DESC, id DESC LIMIT 1",
                    (watch_name,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to get latest snapshot: {exc}") from exc
        if row is None:
            return None
        return _row_to_snapshot(row)

    async def latest_snapshot(self, watch_name: str) -> Snapshot | None:
        return await asyncio.to_thread(self._sync_latest_snapshot, watch_name)

    def _sync_touch_last_checked(self, name: str, when: datetime) -> None:
        try:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "UPDATE watches SET last_checked_at = ? WHERE name = ?",
                    (_isoformat(when), name),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    raise WatchNotFoundError(f"no watch named '{name}'")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise StorageError(f"failed to touch last_checked_at: {exc}") from exc

    async def touch_last_checked(self, name: str, when: datetime) -> None:
        await asyncio.to_thread(self._sync_touch_last_checked, name, when)
