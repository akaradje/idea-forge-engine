# Spec: Gap Velocity Alerts (watchlist + snapshots)

สถานะ: อนุมัติแล้ว (ผู้ใช้เลือก watchlist+snapshots, SQLite) — 2026-07-24
ที่มา: ไอเดีย B ใน `research/2026-07-24-dogfood-findings.md` (confidence 0.65)

## 1. Problem

วันนี้ Idea Forge ตอบได้แค่ "ตอนนี้ demand หน้าตาเป็นอย่างไร" (`detect_gaps`, `check_novelty`) แต่ตอบไม่ได้ว่า "demand กำลังเร่งตัวหรือแผ่วลง" ซึ่งเป็นสัญญาณที่มีค่าที่สุดในการเลือก gap. ฟีเจอร์นี้ให้ agent ลงทะเบียน "gap ที่จะเฝ้า" (query บน HN) ลง watchlist ที่ persist ข้ามรอบ แล้วแต่ละครั้งที่เช็คจะเก็บ snapshot ของสัญญาณ (post count / total points / total comments ในหน้าต่างเวลา) และคืน **delta เทียบ snapshot ก่อนหน้า** เป็นหลักฐานล้วน ๆ (evidence-only, ไม่มี AI opinion score) ให้ agent ตัดสินเอง

## 2. Files touched

สร้างชั้นใหม่ `velocity/` mirror ด้วย `tests/velocity/`

| ไฟล์ | เหตุผล |
|------|--------|
| `src/idea_forge/velocity/__init__.py` | ประกาศ package ชั้นใหม่ |
| `src/idea_forge/velocity/models.py` | Pydantic models: `Watch`, `Snapshot`, `SnapshotDelta`, `EvidencePost`, `VelocityReport`, `WatchListResult` |
| `src/idea_forge/velocity/errors.py` | `VelocityError` (base), `WatchNotFoundError`, `DuplicateWatchError`, `StorageError` |
| `src/idea_forge/velocity/store.py` | `WatchStore` — SQLite persistence ผ่าน stdlib `sqlite3` wrapped ใน `asyncio.to_thread` |
| `src/idea_forge/velocity/metrics.py` | ฟังก์ชัน pure: aggregate จาก `list[RawDocument]`, compute delta ระหว่างสอง snapshot |
| `src/idea_forge/velocity/service.py` | `VelocityService` — fetch HN → aggregate → persist snapshot → build `VelocityReport` |
| `src/idea_forge/config.py` | เพิ่ม `velocity_db_path`, `velocity_window_days`, `velocity_max_evidence`, `velocity_limit_per_check` |
| `src/idea_forge/mcp_server.py` | เพิ่ม 4 tools + `_*_impl` ตาม pattern เดิม |
| `tests/velocity/__init__.py` | package marker |
| `tests/velocity/test_metrics.py` | เทสต์สูตร aggregate/delta (pure) |
| `tests/velocity/test_store.py` | เทสต์ SQLite CRUD + init บน tmp db |
| `tests/velocity/test_service.py` | เทสต์ orchestration ด้วย respx (mock Algolia) + tmp db |
| `tests/velocity/test_mcp_velocity_tools.py` | เทสต์ 4 tools ผ่าน `_*_impl` |

## 3. Interfaces

### `velocity/models.py`

```python
from datetime import datetime
from pydantic import BaseModel

class Watch(BaseModel):
    name: str                 # unique handle, ผู้ใช้ตั้ง
    query: str                # free-text ส่งเข้า hn_query
    tags: list[str]           # Algolia tags (default ["story"])
    created_at: datetime
    last_checked_at: datetime | None = None

class Snapshot(BaseModel):
    watch_name: str
    captured_at: datetime          # เวลาที่ทำ snapshot (UTC)
    window_days: int
    window_start: datetime         # captured_at - window_days
    post_count: int                # โพสต์ใน window
    total_points: int
    total_comments: int
    posts_per_day: float           # post_count / window_days
    points_per_day: float
    comments_per_day: float
    newest_post_at: datetime | None
    oldest_post_at: datetime | None
    fetch_limit_hit: bool = False  # True = ดึงชน limit และโพสต์เก่าสุดที่ได้ยังอยู่ใน window
                                   # → ตัวเลขใน window นี้เป็น undercount (ดู §4)

class SnapshotDelta(BaseModel):
    hours_since_prev: float
    post_count_delta: int
    total_points_delta: int
    total_comments_delta: int
    posts_per_day_delta: float          # rate now - rate prev
    points_per_day_delta: float
    comments_per_day_delta: float
    posts_per_day_pct_change: float | None   # None เมื่อ prev rate == 0

class EvidencePost(BaseModel):
    unique_key: str
    title: str
    url: str
    points: int | None
    num_comments: int | None
    created_at: datetime

class VelocityReport(BaseModel):
    watch: Watch
    current: Snapshot
    previous: Snapshot | None            # None เมื่อเช็คครั้งแรก
    delta: SnapshotDelta | None          # None เมื่อไม่มี previous
    new_evidence: list[EvidencePost]     # โพสต์ที่ created_at > previous.captured_at (ครั้งแรก: ทั้งหมดใน window)
    note: str                            # "Evidence only — no opinion score. Judge acceleration yourself."
```

### `velocity/errors.py`

```python
class VelocityError(Exception): ...
class WatchNotFoundError(VelocityError): ...
class DuplicateWatchError(VelocityError): ...
class StorageError(VelocityError): ...   # wraps sqlite3.Error/corrupt/lock
```

### `velocity/store.py`

```python
class WatchStore:
    def __init__(self, db_path: Path) -> None: ...
    async def init(self) -> None: ...                       # CREATE ... IF NOT EXISTS (idempotent)
    async def add_watch(self, watch: Watch) -> Watch: ...   # DuplicateWatchError ถ้า name ชน
    async def remove_watch(self, name: str) -> None: ...    # WatchNotFoundError ถ้าไม่มี
    async def get_watch(self, name: str) -> Watch: ...      # WatchNotFoundError ถ้าไม่มี
    async def list_watches(self) -> list[Watch]: ...
    async def insert_snapshot(self, snapshot: Snapshot) -> None: ...
    async def latest_snapshot(self, watch_name: str) -> Snapshot | None: ...
    async def touch_last_checked(self, name: str, when: datetime) -> None: ...
```

ทุกเมธอดที่แตะ sqlite ทำงานใน `asyncio.to_thread(self._sync_xxx, ...)`; แต่ละ call เปิด/ปิด `sqlite3.connect(...)` ของตัวเอง (ไม่ share connection ข้าม thread) — ตั้ง `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON` และ `connect(..., timeout=...)` เพื่อกัน lock

### `velocity/metrics.py`

```python
def aggregate(
    docs: list[RawDocument], *, watch_name: str, captured_at: datetime,
    window_days: int, fetch_limit: int
) -> Snapshot: ...

def compute_delta(current: Snapshot, previous: Snapshot) -> SnapshotDelta: ...
```

### `velocity/service.py`

```python
class VelocityService:
    def __init__(self, settings: Settings, store: WatchStore) -> None: ...   # inject store (เทสต์ง่าย)
    async def watch_gap(self, name: str, query: str, tags: list[str]) -> Watch: ...
    async def unwatch_gap(self, name: str) -> None: ...
    async def list_watches(self) -> WatchListResult: ...
    async def check_velocity(self, name: str) -> VelocityReport: ...
```

### `config.py` (fields เพิ่ม)

```python
velocity_db_path: str = ""          # "" → ~/.idea-forge/watchlist.db (expanduser + mkdir parents ตอน resolve)
velocity_window_days: int = 7
velocity_max_evidence: int = 10
velocity_limit_per_check: int = 100 # map เป็น hn_limit_per_tag
```

### `mcp_server.py` (tools ใหม่)

```python
@mcp.tool()
async def watch_gap(name: str, query: str, tags: str = "story") -> str: ...
@mcp.tool()
async def unwatch_gap(name: str) -> str: ...
@mcp.tool()
async def list_watches() -> str: ...
@mcp.tool()
async def check_velocity(name: str) -> str: ...
```

แต่ละตัวมี `_*_impl` คู่กัน คืน JSON string หรือ `"Error: ..."` ตาม convention เดิม; ใช้ `_base_settings(require_reddit=False, ...)` เหมือน HN tools; mcp_server สร้าง `WatchStore(resolved_path)` แล้ว `await store.init()` ต่อ tool call (ถูกเพราะ idempotent และไม่ hold connection ค้าง)

## 4. Behavior & edge cases

### สูตร metrics (จาก Algolia fields: `points`, `num_comments`, `created_at_i`)

- Window: นับเฉพาะ doc ที่ `created_at >= captured_at - window_days` (adapter ดึง newest-first ผ่าน `search_by_date`; โพสต์นอก window กรองทิ้งตอน aggregate)
- `post_count` = จำนวน doc ใน window
- `total_points` = Σ `metadata["points"]` (ค่าหาย → 0); `total_comments` เช่นเดียวกัน
- `posts_per_day = post_count / window_days` (window_days >= 1); `points_per_day`, `comments_per_day` ทำนองเดียวกัน
- delta = current rate − previous rate; `hours_since_prev` เป็นชั่วโมง
- `posts_per_day_pct_change = (cur − prev)/prev * 100` เมื่อ `prev > 0`, ไม่งั้น `None`
- **fetch_limit_hit**: `True` เมื่อ `len(docs) >= fetch_limit` **และ** doc เก่าสุดที่ได้ยังอยู่ใน window — แปลว่ามีโพสต์ใน window ที่ไม่ถูกดึงมา ตัวเลขทั้งหมดของ snapshot นี้เป็น undercount; tool docstring ต้องบอก agent ให้ระวัง flag นี้ (no silent caps — สอดคล้ม evidence-only)

### check_velocity flow

1. `get_watch(name)` → ไม่มี → `"Error: no watch named '<name>'. Call watch_gap first."`
2. โหลด `previous = latest_snapshot(name)` **ก่อน** insert อันใหม่
3. fetch HN ผ่าน `HackerNewsAdapter` (override `hn_query=watch.query`, `hn_tags=watch.tags`, `hn_limit_per_tag=velocity_limit_per_check`) — reuse `_collect`
4. `aggregate(...)` → `current`; `insert_snapshot(current)`; `touch_last_checked`
5. `new_evidence` = โพสต์ที่ `created_at > previous.captured_at` (ถ้ามี previous) มิฉะนั้นโพสต์ทั้งหมดใน window — เรียงใหม่→เก่า ตัดที่ `velocity_max_evidence`
6. คืน `VelocityReport.model_dump(mode="json")`

### Edge cases

1. **เช็คครั้งแรก:** `previous=None`, `delta=None`, `new_evidence` = โพสต์ทั้งหมดใน window; `note` แจ้งว่าเป็น baseline
2. **query ไม่มีผลลัพธ์:** `post_count=0`, rates=0, `newest/oldest_post_at=None` — snapshot ยังถูกบันทึก (0 คือสัญญาณ ไม่ใช่ error)
3. **prev rate = 0, cur > 0:** `pct_change=None` แต่ absolute delta รายงานปกติ (จับ "0 → มี" ได้)
4. **snapshot ถี่มาก/เก่ามาก:** ไม่ normalize ไม่บล็อก ไม่ expiry — คืน `hours_since_prev` จริงให้ agent ตีความ
5. **ชื่อ watch ซ้ำ:** `"Error: watch '<name>' already exists. Call unwatch_gap first or pick another name."` (UNIQUE บน `name`)
6. **unwatch ชื่อที่ไม่มี:** Error string ข้อความชัด
7. **db corrupt / locked:** จับ `sqlite3.Error` → `StorageError` → `"Error: StorageError: ..."`
8. **name/query ว่าง/whitespace:** validate ใน impl → `"Error: name must not be empty."` ก่อนแตะ store; trim; clamp query ยาวเหมือน `NOVELTY_QUERY_MAX_CHARS`
9. **tags ว่าง (`_is_blank_csv`):** fallback `["story"]`
10. **HN fetch ล้ม (IngestionError):** ไม่ insert snapshot, watch คงอยู่, `last_checked_at` ไม่ถูกแตะ
11. **concurrency:** ไม่ share connection; snapshots เป็น append-only — race read-previous/insert ยอมรับได้; init idempotent
12. **db path มี `~` / โฟลเดอร์ยังไม่มี:** expanduser + `mkdir(parents=True, exist_ok=True)` ก่อน connect

### SQLite schema (v1 — ไม่มี migration framework; อนาคตใช้ `PRAGMA user_version`)

```sql
CREATE TABLE IF NOT EXISTS watches (
    name TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    tags TEXT NOT NULL,              -- comma-joined
    created_at TEXT NOT NULL,        -- ISO8601 UTC
    last_checked_at TEXT
);
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
CREATE INDEX IF NOT EXISTS idx_snapshots_watch_captured
    ON snapshots(watch_name, captured_at DESC);
```

`latest_snapshot` = `ORDER BY captured_at DESC, id DESC LIMIT 1`

## 5. Acceptance criteria

1. `aggregate` กับ docs ที่รู้ค่า points/comments คืน totals/rates ตรงสูตร (เทสต์ค่าคงที่)
2. `aggregate` กรอง doc นอก window ออก
3. `aggregate` กับ list ว่าง → `post_count=0`, rates=0.0, `newest_post_at=None`, `fetch_limit_hit=False`
4. `aggregate` เมื่อ `len(docs) >= fetch_limit` และ doc เก่าสุดอยู่ใน window → `fetch_limit_hit=True`; ถ้า doc เก่าสุดหลุด window แล้ว → `False`
5. `compute_delta` คืน `pct_change=None` เมื่อ prev rate=0 และคืน % ถูกต้องเมื่อ prev>0
6. `add_watch` ชื่อซ้ำ raise `DuplicateWatchError`; `remove_watch`/`get_watch` ชื่อไม่มี raise `WatchNotFoundError`
7. `insert_snapshot` สองครั้งต่างเวลา → `latest_snapshot` คืนอันหลัง
8. `init` เรียกซ้ำได้ (idempotent)
9. `check_velocity` ครั้งแรก (respx mock) → `previous=None`, `delta=None`, evidence = โพสต์ใน window
10. `check_velocity` ครั้งที่สอง → `delta` ไม่ None, evidence เฉพาะโพสต์ `created_at > previous.captured_at`
11. tool `check_velocity` name ไม่มี → `"Error: ..."`; `watch_gap` ซ้ำ → `"Error: ..."`; สำเร็จ → JSON parse ได้
12. tool ทุกตัวคืน JSON string หรือ `"Error: ..."` — ไม่ raise ออกนอก impl
13. sync sqlite call อยู่ใน `asyncio.to_thread` ทั้งหมด
14. `velocity_db_path=""` → resolve `~/.idea-forge/watchlist.db` + สร้างโฟลเดอร์อัตโนมัติ
15. เทสต์ไม่ยิง network จริง (respx) และใช้ `tmp_path` สำหรับ db

## 6. Non-goals

- ไม่มี AI/LLM opinion หรือ score — ไม่เรียก Anthropic, ไม่ใช้ `ANTHROPIC_API_KEY`
- ไม่มี background scheduler / polling / push — snapshot เกิดเฉพาะเมื่อ agent เรียก `check_velocity`
- ไม่รองรับ source อื่นนอก HN ใน v1
- ไม่มี migration framework (schema v1)
- ไม่ใช้ Postgres/SQLAlchemy — stdlib `sqlite3` (ข้อยกเว้นกฎ async DB ของ python.md โดยเจตนา สำหรับ local store; async-safety ผ่าน `asyncio.to_thread`)
- ไม่ทำ chart/visualization/time-series API
- ไม่ dedup ข้าม snapshot / ไม่เก็บ raw post ใน db (เก็บ aggregate; evidence มาจาก fetch สด)

## 7. Implementation order

1. `velocity/models.py` + `velocity/errors.py`
2. `config.py` เพิ่ม fields + resolve helper สำหรับ db path
3. `velocity/metrics.py` + `tests/velocity/test_metrics.py` (pure, เทสต์ก่อน)
4. `velocity/store.py` + `tests/velocity/test_store.py`
5. `velocity/service.py` + `tests/velocity/test_service.py`
6. `mcp_server.py` 4 impl + tools + `tests/velocity/test_mcp_velocity_tools.py`
7. `uv run pytest` / `ruff check --fix . && ruff format .` / `uv run mypy src/`
