"""Pydantic models for the gap velocity alerts layer."""

from datetime import datetime

from pydantic import BaseModel


class Watch(BaseModel):
    name: str  # unique handle, chosen by the caller
    query: str  # free-text query sent to hn_query
    tags: list[str]  # Algolia tags (default ["story"])
    created_at: datetime
    last_checked_at: datetime | None = None


class Snapshot(BaseModel):
    watch_name: str
    captured_at: datetime  # when the snapshot was taken (UTC)
    window_days: int
    window_start: datetime  # captured_at - window_days
    post_count: int  # posts within the window
    total_points: int
    total_comments: int
    posts_per_day: float  # post_count / window_days
    points_per_day: float
    comments_per_day: float
    newest_post_at: datetime | None
    oldest_post_at: datetime | None
    fetch_limit_hit: bool = False  # True = fetch hit its cap and the oldest fetched
    # post is still inside the window -> the numbers in this snapshot are an undercount


class SnapshotDelta(BaseModel):
    hours_since_prev: float
    post_count_delta: int
    total_points_delta: int
    total_comments_delta: int
    posts_per_day_delta: float  # rate now - rate prev
    points_per_day_delta: float
    comments_per_day_delta: float
    posts_per_day_pct_change: float | None  # None when prev rate == 0


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
    previous: Snapshot | None  # None on the first check
    delta: SnapshotDelta | None  # None when there is no previous snapshot
    new_evidence: list[EvidencePost]  # posts with created_at > previous.captured_at
    # (first check: all posts in window)
    note: str  # "Evidence only — no opinion score. Judge acceleration yourself."


class WatchListResult(BaseModel):
    watches: list[Watch]
    count: int
