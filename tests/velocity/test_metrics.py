"""Tests for idea_forge.velocity.metrics (pure aggregate/compute_delta).

Covers spec acceptance criteria 1-5
(specs/2026-07-24-gap-velocity-alerts.md §5).
No I/O — everything here is pure functions over in-memory RawDocument lists.

fetch_limit_hit is computed by callers via compute_fetch_limit_hit (per-tag,
before docs from multiple tags are combined) and passed into aggregate as an
already-computed bool — see service.py and spec §4 "fetch_limit_hit".
"""

from datetime import UTC, datetime, timedelta

from idea_forge.ingestion.base import RawDocument
from idea_forge.velocity.metrics import aggregate, compute_delta, compute_fetch_limit_hit

CAPTURED_AT = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def make_doc(
    *,
    source_id: str = "1",
    created_at: datetime,
    points: int | None = 10,
    num_comments: int | None = 2,
) -> RawDocument:
    return RawDocument(
        source="hackernews",
        source_id=source_id,
        title=f"post {source_id}",
        body="",
        url=f"https://news.ycombinator.com/item?id={source_id}",
        author=None,
        created_at=created_at,
        fetched_at=CAPTURED_AT,
        metadata={"points": points, "num_comments": num_comments},
    )


# --- Criterion 1: totals/rates match the formula for known-value docs ------


def test_aggregate_computes_totals_and_rates_for_known_values():
    docs = [
        make_doc(
            source_id="a", created_at=CAPTURED_AT - timedelta(days=1), points=10, num_comments=2
        ),
        make_doc(
            source_id="b", created_at=CAPTURED_AT - timedelta(days=2), points=20, num_comments=3
        ),
    ]

    snap = aggregate(
        docs, watch_name="w1", captured_at=CAPTURED_AT, window_days=7, fetch_limit_hit=False
    )

    assert snap.watch_name == "w1"
    assert snap.post_count == 2
    assert snap.total_points == 30
    assert snap.total_comments == 5
    assert snap.posts_per_day == 2 / 7
    assert snap.points_per_day == 30 / 7
    assert snap.comments_per_day == 5 / 7
    assert snap.window_days == 7
    assert snap.window_start == CAPTURED_AT - timedelta(days=7)


def test_aggregate_treats_missing_points_and_comments_as_zero():
    docs = [
        make_doc(
            source_id="a",
            created_at=CAPTURED_AT - timedelta(days=1),
            points=None,
            num_comments=None,
        ),
    ]

    snap = aggregate(
        docs, watch_name="w1", captured_at=CAPTURED_AT, window_days=7, fetch_limit_hit=False
    )

    assert snap.total_points == 0
    assert snap.total_comments == 0


def test_aggregate_sets_newest_and_oldest_post_at():
    newest = CAPTURED_AT - timedelta(hours=1)
    oldest = CAPTURED_AT - timedelta(days=5)
    docs = [
        make_doc(source_id="a", created_at=newest),
        make_doc(source_id="b", created_at=oldest),
    ]

    snap = aggregate(
        docs, watch_name="w1", captured_at=CAPTURED_AT, window_days=7, fetch_limit_hit=False
    )

    assert snap.newest_post_at == newest
    assert snap.oldest_post_at == oldest


# --- Criterion 2: docs outside the window are filtered out -----------------


def test_aggregate_filters_out_docs_older_than_window():
    in_window = make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=3))
    out_of_window = make_doc(source_id="b", created_at=CAPTURED_AT - timedelta(days=10))

    snap = aggregate(
        [in_window, out_of_window],
        watch_name="w1",
        captured_at=CAPTURED_AT,
        window_days=7,
        fetch_limit_hit=False,
    )

    assert snap.post_count == 1
    assert snap.newest_post_at == in_window.created_at
    assert snap.oldest_post_at == in_window.created_at


def test_aggregate_includes_doc_exactly_at_window_boundary():
    boundary_doc = make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=7))

    snap = aggregate(
        [boundary_doc],
        watch_name="w1",
        captured_at=CAPTURED_AT,
        window_days=7,
        fetch_limit_hit=False,
    )

    assert snap.post_count == 1


# --- Criterion 3: empty doc list -------------------------------------------


def test_aggregate_empty_docs_returns_zeroed_snapshot():
    snap = aggregate(
        [], watch_name="w1", captured_at=CAPTURED_AT, window_days=7, fetch_limit_hit=False
    )

    assert snap.post_count == 0
    assert snap.total_points == 0
    assert snap.total_comments == 0
    assert snap.posts_per_day == 0.0
    assert snap.points_per_day == 0.0
    assert snap.comments_per_day == 0.0
    assert snap.newest_post_at is None
    assert snap.oldest_post_at is None
    assert snap.fetch_limit_hit is False


# --- Criterion 4: fetch_limit_hit flag --------------------------------------
# aggregate itself just stores the precomputed bool (passthrough); the
# computation lives in compute_fetch_limit_hit so it can be evaluated
# per-tag by the caller before docs from multiple tags are combined.


def test_aggregate_fetch_limit_hit_is_a_passthrough_of_the_precomputed_flag():
    snap_true = aggregate(
        [make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=1))],
        watch_name="w1",
        captured_at=CAPTURED_AT,
        window_days=7,
        fetch_limit_hit=True,
    )
    snap_false = aggregate(
        [make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=1))],
        watch_name="w1",
        captured_at=CAPTURED_AT,
        window_days=7,
        fetch_limit_hit=False,
    )

    assert snap_true.fetch_limit_hit is True
    assert snap_false.fetch_limit_hit is False


def test_compute_fetch_limit_hit_true_when_capped_and_oldest_still_in_window():
    # fetch_limit=2, oldest doc still well within the 7-day window -> undercount possible
    docs = [
        make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=1)),
        make_doc(source_id="b", created_at=CAPTURED_AT - timedelta(days=2)),
    ]
    window_start = CAPTURED_AT - timedelta(days=7)

    hit = compute_fetch_limit_hit(docs, fetch_limit=2, window_start=window_start)

    assert hit is True


def test_compute_fetch_limit_hit_false_when_oldest_doc_falls_outside_window():
    # fetch_limit=2 reached, but the oldest fetched doc is already outside the window,
    # meaning the window itself is fully covered (no undercount).
    docs = [
        make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=1)),
        make_doc(source_id="b", created_at=CAPTURED_AT - timedelta(days=10)),
    ]
    window_start = CAPTURED_AT - timedelta(days=7)

    hit = compute_fetch_limit_hit(docs, fetch_limit=2, window_start=window_start)

    assert hit is False


def test_compute_fetch_limit_hit_false_when_under_limit():
    docs = [make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=1))]
    window_start = CAPTURED_AT - timedelta(days=7)

    hit = compute_fetch_limit_hit(docs, fetch_limit=100, window_start=window_start)

    assert hit is False


def test_compute_fetch_limit_hit_false_when_docs_empty():
    window_start = CAPTURED_AT - timedelta(days=7)

    hit = compute_fetch_limit_hit([], fetch_limit=100, window_start=window_start)

    assert hit is False


# --- Criterion 5: compute_delta pct_change rules ----------------------------


def test_compute_delta_pct_change_none_when_previous_rate_zero():
    previous = aggregate(
        [],
        watch_name="w1",
        captured_at=CAPTURED_AT - timedelta(days=1),
        window_days=7,
        fetch_limit_hit=False,
    )
    current_docs = [make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(hours=2))]
    current = aggregate(
        current_docs,
        watch_name="w1",
        captured_at=CAPTURED_AT,
        window_days=7,
        fetch_limit_hit=False,
    )

    delta = compute_delta(current, previous)

    assert delta.posts_per_day_pct_change is None
    assert delta.post_count_delta == 1
    assert delta.posts_per_day_delta == current.posts_per_day - previous.posts_per_day


def test_compute_delta_pct_change_correct_when_previous_rate_positive():
    previous_docs = [make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=1, hours=1))]
    previous = aggregate(
        previous_docs,
        watch_name="w1",
        captured_at=CAPTURED_AT - timedelta(days=1),
        window_days=7,
        fetch_limit_hit=False,
    )
    current_docs = [
        make_doc(source_id="a", created_at=CAPTURED_AT - timedelta(days=2, hours=1)),
        make_doc(source_id="b", created_at=CAPTURED_AT - timedelta(hours=1)),
    ]
    current = aggregate(
        current_docs,
        watch_name="w1",
        captured_at=CAPTURED_AT,
        window_days=7,
        fetch_limit_hit=False,
    )

    delta = compute_delta(current, previous)

    expected_pct = (current.posts_per_day - previous.posts_per_day) / previous.posts_per_day * 100
    assert delta.posts_per_day_pct_change == expected_pct
    assert delta.hours_since_prev == 24.0


def test_compute_delta_hours_since_prev_matches_captured_at_difference():
    previous = aggregate(
        [],
        watch_name="w1",
        captured_at=CAPTURED_AT - timedelta(hours=6),
        window_days=7,
        fetch_limit_hit=False,
    )
    current = aggregate(
        [], watch_name="w1", captured_at=CAPTURED_AT, window_days=7, fetch_limit_hit=False
    )

    delta = compute_delta(current, previous)

    assert delta.hours_since_prev == 6.0
    assert delta.total_points_delta == 0
    assert delta.total_comments_delta == 0
