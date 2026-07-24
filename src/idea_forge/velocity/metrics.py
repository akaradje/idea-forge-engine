"""Pure aggregation/delta functions for gap velocity snapshots."""

from datetime import datetime, timedelta

from idea_forge.ingestion.base import RawDocument
from idea_forge.velocity.models import Snapshot, SnapshotDelta


def compute_fetch_limit_hit(
    docs: list[RawDocument], *, fetch_limit: int, window_start: datetime
) -> bool:
    """True when len(docs) >= fetch_limit AND the oldest fetched doc is still
    within the window (meaning in-window posts may have been missed).

    Callers that fetch per-tag (to avoid a combined-tag undercount false
    positive, see spec §4 "fetch_limit_hit") should call this once per tag
    against that tag's own docs, then OR the results together.
    """
    if not docs or len(docs) < fetch_limit:
        return False
    oldest_fetched = min(doc.created_at for doc in docs)
    return oldest_fetched >= window_start


def aggregate(
    docs: list[RawDocument],
    *,
    watch_name: str,
    captured_at: datetime,
    window_days: int,
    fetch_limit_hit: bool,
) -> Snapshot:
    """Aggregate raw documents into a Snapshot.

    Filters docs to those with created_at >= window_start = captured_at - window_days.
    fetch_limit_hit must be computed by the caller (see compute_fetch_limit_hit)
    since it needs to be evaluated per-tag before docs from multiple tags are
    combined here.
    """
    window_start = captured_at - timedelta(days=window_days)

    in_window = [doc for doc in docs if doc.created_at >= window_start]

    post_count = len(in_window)
    total_points = sum(int(doc.metadata.get("points") or 0) for doc in in_window)
    total_comments = sum(int(doc.metadata.get("num_comments") or 0) for doc in in_window)

    divisor = max(window_days, 1)
    posts_per_day = post_count / divisor
    points_per_day = total_points / divisor
    comments_per_day = total_comments / divisor

    newest_post_at: datetime | None = None
    oldest_post_at: datetime | None = None
    if in_window:
        newest_post_at = max(doc.created_at for doc in in_window)
        oldest_post_at = min(doc.created_at for doc in in_window)

    return Snapshot(
        watch_name=watch_name,
        captured_at=captured_at,
        window_days=window_days,
        window_start=window_start,
        post_count=post_count,
        total_points=total_points,
        total_comments=total_comments,
        posts_per_day=posts_per_day,
        points_per_day=points_per_day,
        comments_per_day=comments_per_day,
        newest_post_at=newest_post_at,
        oldest_post_at=oldest_post_at,
        fetch_limit_hit=fetch_limit_hit,
    )


def compute_delta(current: Snapshot, previous: Snapshot) -> SnapshotDelta:
    """Compute the delta between two snapshots of the same watch."""
    hours_since_prev = (current.captured_at - previous.captured_at).total_seconds() / 3600.0

    posts_per_day_pct_change: float | None = None
    if previous.posts_per_day > 0:
        posts_per_day_pct_change = (
            (current.posts_per_day - previous.posts_per_day) / previous.posts_per_day * 100
        )

    return SnapshotDelta(
        hours_since_prev=hours_since_prev,
        post_count_delta=current.post_count - previous.post_count,
        total_points_delta=current.total_points - previous.total_points,
        total_comments_delta=current.total_comments - previous.total_comments,
        posts_per_day_delta=current.posts_per_day - previous.posts_per_day,
        points_per_day_delta=current.points_per_day - previous.points_per_day,
        comments_per_day_delta=current.comments_per_day - previous.comments_per_day,
        posts_per_day_pct_change=posts_per_day_pct_change,
    )
