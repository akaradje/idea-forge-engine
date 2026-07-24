"""MCP server (stdio) exposing Idea Forge ingestion adapters as tools for Claude Code.

CRITICAL: stdio transport uses stdout for the MCP protocol. All logging MUST go to
stderr. Never print()/log to stdout in this module.
"""

import json
import logging
import os
import sys
from collections.abc import AsyncGenerator
from typing import Any, cast

from anthropic import AsyncAnthropic
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from idea_forge.config import Settings, resolve_velocity_db_path
from idea_forge.gaps.detector import GapDetector
from idea_forge.gaps.errors import GapDetectionError
from idea_forge.ingestion.base import RawDocument
from idea_forge.ingestion.errors import AuthError, IngestionError
from idea_forge.ingestion.hackernews import HackerNewsAdapter
from idea_forge.ingestion.reddit import RedditAdapter
from idea_forge.velocity.errors import DuplicateWatchError, StorageError, WatchNotFoundError
from idea_forge.velocity.service import VelocityService
from idea_forge.velocity.store import WatchStore

logger = logging.getLogger("idea_forge.mcp_server")

MAX_BODY_CHARS = 2000  # RawDocument.body truncated to this before serialization
MAX_LIMIT = 100  # per-call limit clamp ceiling
MAX_NOVELTY_RESULTS = 20  # ceiling for check_novelty max_results
NOVELTY_QUERY_MAX_CHARS = 300  # idea string truncated to this before hitting Algolia

_REDDIT_CREDS_ERROR = (
    "Error: Reddit credentials not configured. Set REDDIT_CLIENT_ID, "
    "REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT in .env."
)

_REDDIT_REQUIRED_FIELDS = frozenset(
    {"reddit_client_id", "reddit_client_secret", "reddit_user_agent"}
)

_ANTHROPIC_MISSING_MSG = (
    "ANTHROPIC_API_KEY is not set, so detect_gaps cannot run its LLM analysis. "
    "Two options:\n"
    "  1. Set it: add ANTHROPIC_API_KEY=sk-ant-... to your .env (or export it in the "
    "environment) and call detect_gaps again.\n"
    "  2. Keyless alternative: call fetch_hackernews to pull the same posts, then "
    "analyze them for demand-supply gaps yourself — you are an LLM."
)

mcp = FastMCP("idea-forge-ingestion")


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _is_blank_csv(value: str) -> bool:
    """True when a comma-separated override is empty/whitespace/commas only
    (e.g. "", "  ", ",") — such values should not override a Settings default."""
    return not value.strip(" ,")


def _base_settings(*, require_reddit: bool, **overrides: Any) -> Settings:
    """Build a Settings from env/.env with per-call overrides.

    First attempts a real Settings load (env + .env). If ValidationError hits only
    the reddit_* required fields:
      - require_reddit=False (HN call): retry with placeholder reddit values so
        validation passes (HN never uses them).
      - require_reddit=True (Reddit call): re-raise — caller maps to friendly error.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        error_fields = {str(err["loc"][0]) for err in exc.errors() if err["loc"]}
        if not require_reddit and error_fields and error_fields.issubset(_REDDIT_REQUIRED_FIELDS):
            placeholders: dict[str, Any] = {
                "reddit_client_id": "placeholder",
                "reddit_client_secret": "placeholder",
                "reddit_user_agent": "placeholder",
            }
            placeholders.update(overrides)
            return Settings(**placeholders)
        raise


def _serialize(docs: list[RawDocument]) -> list[dict[str, Any]]:
    """model_dump(mode='json') each doc; truncate body to MAX_BODY_CHARS and
    record metadata['body_truncated'] = True + metadata['body_original_length']
    when truncation occurred. Never mutates the original documents."""
    serialized: list[dict[str, Any]] = []
    for doc in docs:
        dumped = doc.model_dump(mode="json")
        body = dumped.get("body")
        if isinstance(body, str) and len(body) > MAX_BODY_CHARS:
            original_length = len(body)
            dumped["body"] = body[:MAX_BODY_CHARS]
            metadata = dict(dumped.get("metadata") or {})
            metadata["body_truncated"] = True
            metadata["body_original_length"] = original_length
            dumped["metadata"] = metadata
        serialized.append(dumped)
    return serialized


async def _collect(adapter: HackerNewsAdapter | RedditAdapter) -> list[RawDocument]:
    """Drain the adapter's async generator inside its async context manager.

    Explicitly closes the generator in a finally block so a mid-stream error
    (or any early return) doesn't leave a suspended async generator for the
    GC to finalize later (which would otherwise emit stderr warning noise).
    """
    async with adapter:
        # fetch() is declared -> AsyncIterator[RawDocument] but every concrete
        # adapter implements it as an async generator (uses `yield`), so aclose()
        # is always present at runtime.
        gen = cast("AsyncGenerator[RawDocument, None]", adapter.fetch())
        docs: list[RawDocument] = []
        try:
            async for doc in gen:
                docs.append(doc)
        finally:
            await gen.aclose()
        return docs


async def _fetch_hackernews_impl(
    tags: str = "ask_hn,show_hn",
    query: str = "",
    limit_per_tag: int = 25,
) -> str:
    """Return JSON string {"documents": [...], "count": N} or a friendly 'Error: ...' string."""
    hn_overrides: dict[str, Any] = {
        "hn_query": query,
        "hn_limit_per_tag": _clamp_limit(limit_per_tag),
    }
    if not _is_blank_csv(tags):
        hn_overrides["hn_tags"] = tags

    try:
        settings = _base_settings(require_reddit=False, **hn_overrides)
        adapter = HackerNewsAdapter(settings)
        docs = await _collect(adapter)
        payload = {"documents": _serialize(docs), "count": len(docs)}
        return json.dumps(payload, ensure_ascii=False)
    except IngestionError as exc:
        logger.exception("hackernews fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"
    except Exception as exc:
        logger.exception("hackernews fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"


async def _fetch_reddit_impl(
    subreddits: str = "",
    listing: str = "new",
    limit_per_subreddit: int = 25,
) -> str:
    """Return JSON string {"documents": [...], "count": N} or a friendly 'Error: ...' string."""
    overrides: dict[str, Any] = {
        "reddit_listing": listing,
        "reddit_limit_per_subreddit": _clamp_limit(limit_per_subreddit),
    }
    if not _is_blank_csv(subreddits):
        overrides["reddit_subreddits"] = subreddits

    try:
        settings = _base_settings(require_reddit=True, **overrides)
    except ValidationError as exc:
        error_fields = {str(err["loc"][0]) for err in exc.errors() if err["loc"]}
        if error_fields and error_fields.issubset(_REDDIT_REQUIRED_FIELDS):
            return _REDDIT_CREDS_ERROR
        return f"Error: {exc}"
    except Exception as exc:
        logger.exception("reddit settings load failed")
        return f"Error: {type(exc).__name__}: {exc}"

    if (
        not settings.reddit_client_id.strip()
        or not settings.reddit_client_secret.strip()
        or not settings.reddit_user_agent.strip()
    ):
        return _REDDIT_CREDS_ERROR

    try:
        adapter = RedditAdapter(settings)
    except ValueError:
        return f"Error: listing must be 'new' or 'top', got {listing!r}."

    try:
        docs = await _collect(adapter)
        payload = {"documents": _serialize(docs), "count": len(docs)}
        return json.dumps(payload, ensure_ascii=False)
    except AuthError:
        logger.exception("reddit fetch failed: auth error")
        return _REDDIT_CREDS_ERROR
    except IngestionError as exc:
        logger.exception("reddit fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"
    except Exception as exc:
        logger.exception("reddit fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"


@mcp.tool()
async def fetch_hackernews(
    tags: str = "ask_hn,show_hn", query: str = "", limit_per_tag: int = 25
) -> str:
    """Fetch demand-signal posts from Hacker News (Algolia HN Search, no auth).

    Args:
        tags: comma-separated Algolia tags (e.g. "ask_hn,show_hn").
        query: optional free-text query; "" matches all.
        limit_per_tag: max documents per tag (kept small for LLM context).
    """
    return await _fetch_hackernews_impl(tags, query, limit_per_tag)


@mcp.tool()
async def fetch_reddit(
    subreddits: str = "", listing: str = "new", limit_per_subreddit: int = 25
) -> str:
    """Fetch demand-signal posts from Reddit subreddits (requires REDDIT_* in .env).

    Args:
        subreddits: comma-separated subreddit names; "" uses the configured default.
        listing: "new" or "top".
        limit_per_subreddit: max documents per subreddit.
    """
    return await _fetch_reddit_impl(subreddits, listing, limit_per_subreddit)


def _anthropic_key_present(settings: Settings) -> bool:
    """True if settings.anthropic_api_key is non-blank OR ANTHROPIC_API_KEY env is set."""
    return bool(settings.anthropic_api_key.strip()) or bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


def _hn_prior_art_entry(doc: RawDocument) -> dict[str, Any]:
    """Compact prior-art record: unique_key, title, url, points, num_comments,
    created_at (ISO string). points/num_comments from doc.metadata; absent → None."""
    return {
        "unique_key": doc.unique_key,
        "title": doc.title,
        "url": doc.url,
        "points": doc.metadata.get("points"),
        "num_comments": doc.metadata.get("num_comments"),
        "created_at": doc.created_at.isoformat(),
    }


async def _check_novelty_impl(idea: str, max_results: int = 10) -> str:
    """Return JSON {"query", "prior_art": [...], "count", "note"} or 'Error: ...'."""
    query = idea.strip()[:NOVELTY_QUERY_MAX_CHARS]
    if not query:
        return (
            "Error: idea must not be empty — pass a short description or keywords "
            "to check for prior art."
        )

    clamped = max(1, min(max_results, MAX_NOVELTY_RESULTS))
    try:
        settings = _base_settings(
            require_reddit=False,
            hn_tags="story",
            hn_query=query,
            hn_limit_per_tag=clamped,
        )
        adapter = HackerNewsAdapter(settings)
        docs = await _collect(adapter)
        payload = {
            "query": query,
            "prior_art": [_hn_prior_art_entry(doc) for doc in docs],
            "count": len(docs),
            "note": "Judge similarity yourself — this tool returns evidence, not verdicts.",
        }
        return json.dumps(payload, ensure_ascii=False)
    except IngestionError as exc:
        logger.exception("check_novelty fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"
    except Exception as exc:
        logger.exception("check_novelty fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"


async def _detect_gaps_impl(
    source: str = "hackernews",
    tags: str = "ask_hn",
    query: str = "",
    limit: int = 25,
) -> str:
    """Return JSON {"gaps": [...], "doc_count", "gap_count"}, the key-missing guidance
    message, or an 'Error: ...' string."""
    if source != "hackernews":
        return f"Error: source '{source}' is not supported in v1. Only 'hackernews' is available."

    hn_overrides: dict[str, Any] = {
        "hn_query": query,
        "hn_limit_per_tag": _clamp_limit(limit),
    }
    if not _is_blank_csv(tags):
        hn_overrides["hn_tags"] = tags

    try:
        settings = _base_settings(require_reddit=False, **hn_overrides)

        if not _anthropic_key_present(settings):
            return _ANTHROPIC_MISSING_MSG

        adapter = HackerNewsAdapter(settings)
        docs = await _collect(adapter)
        # Inject a context-managed client so its httpx pool is closed per call —
        # a self-constructed GapDetector client would leak on a long-lived server.
        async with AsyncAnthropic(
            api_key=settings.anthropic_api_key or None,
            timeout=settings.gap_request_timeout_seconds,
        ) as anthropic_client:
            gaps = await GapDetector(settings, client=anthropic_client).detect(docs)
        payload = {
            "gaps": [gap.model_dump(mode="json") for gap in gaps],
            "doc_count": len(docs),
            "gap_count": len(gaps),
        }
        return json.dumps(payload, ensure_ascii=False)
    except GapDetectionError as exc:
        logger.exception("detect_gaps analysis failed")
        return f"Error: {exc}"
    except IngestionError as exc:
        logger.exception("detect_gaps fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"
    except Exception as exc:
        logger.exception("detect_gaps failed")
        return f"Error: {type(exc).__name__}: {exc}"


@mcp.tool()
async def check_novelty(idea: str, max_results: int = 10) -> str:
    """Search Hacker News for prior art related to an idea (keyless, no API key needed).

    Use this to sanity-check whether an idea already exists or has been discussed
    before you invest in it. It searches Hacker News stories (via Algolia HN Search)
    for posts matching your idea and returns them as EVIDENCE for you to judge — it
    does NOT score similarity or decide whether your idea is novel. That judgment is
    yours: read the returned titles, engagement, and links, then reason about overlap.

    Args:
        idea: a short description of the idea to check (a sentence or a few keywords).
            Long strings are truncated before searching; keep it focused for best matches.
        max_results: how many prior-art posts to return (default 10, clamped to 1..20).

    Returns:
        A JSON string:
        {
          "query": "<the search text actually sent to Hacker News>",
          "prior_art": [
            {"unique_key", "title", "url", "points", "num_comments", "created_at"}, ...
          ],
          "count": <number of results>,
          "note": "Judge similarity yourself — this tool returns evidence, not verdicts."
        }
        An empty "prior_art" list (count 0) means no matching HN stories were found — that
        is a signal, not an error. On failure, a plain string beginning "Error: ".
    """
    return await _check_novelty_impl(idea, max_results)


@mcp.tool()
async def detect_gaps(
    source: str = "hackernews",
    tags: str = "ask_hn",
    query: str = "",
    limit: int = 25,
) -> str:
    """Fetch demand-signal posts and analyze them for demand-supply GAPS (needs ANTHROPIC_API_KEY).

    Use this when you want structured, evidence-cited gap candidates — problems where
    demand looks strong but existing supply looks weak — rather than raw posts. It fetches
    Hacker News posts and runs them through the gap-detection LLM analysis in one call.

    This tool requires ANTHROPIC_API_KEY (it makes an LLM call). If the key is not set, it
    returns a friendly message explaining how to set it AND the keyless alternative: call
    fetch_hackernews and analyze the posts for gaps yourself.

    Args:
        source: data source; only "hackernews" is supported in v1 (anything else returns a
            friendly error naming the supported source).
        tags: comma-separated Algolia HN tags to fetch (e.g. "ask_hn", "ask_hn,show_hn").
        query: optional free-text query to narrow the fetched posts; "" fetches all.
        limit: max posts to fetch per tag before analysis (default 25, clamped to 1..100).

    Returns:
        A JSON string:
        {
          "gaps": [ <GapCandidate>, ... ],   # problem, evidence[{unique_key, quote}],
                                             # demand_signal, supply_signal, confidence, metadata
          "doc_count": <posts analyzed>,
          "gap_count": <gaps found>
        }
        A GapDetectionError (LLM/API failure, invalid output) yields a string beginning
        "Error: ". Missing ANTHROPIC_API_KEY yields the friendly guidance message.
    """
    return await _detect_gaps_impl(source, tags, query, limit)


async def _velocity_service(settings: Settings) -> VelocityService:
    db_path = resolve_velocity_db_path(settings)
    store = WatchStore(db_path)
    await store.init()
    return VelocityService(settings, store)


async def _watch_gap_impl(name: str, query: str, tags: str = "story") -> str:
    """Return JSON string of the created Watch or a friendly 'Error: ...' string."""
    clean_name = name.strip()
    if not clean_name:
        return "Error: name must not be empty."
    clean_query = query.strip()[:NOVELTY_QUERY_MAX_CHARS]
    if not clean_query:
        return "Error: query must not be empty."

    tag_list = (
        ["story"] if _is_blank_csv(tags) else [t.strip() for t in tags.split(",") if t.strip()]
    )

    try:
        settings = _base_settings(require_reddit=False)
        service = await _velocity_service(settings)
        watch = await service.watch_gap(clean_name, clean_query, tag_list)
        return json.dumps(watch.model_dump(mode="json"), ensure_ascii=False)
    except DuplicateWatchError:
        return (
            f"Error: watch '{clean_name}' already exists. "
            "Call unwatch_gap first or pick another name."
        )
    except StorageError as exc:
        logger.exception("watch_gap storage failed")
        return f"Error: StorageError: {exc}"
    except Exception as exc:
        logger.exception("watch_gap failed")
        return f"Error: {type(exc).__name__}: {exc}"


async def _unwatch_gap_impl(name: str) -> str:
    """Return JSON string {"removed": name} or a friendly 'Error: ...' string."""
    clean_name = name.strip()
    if not clean_name:
        return "Error: name must not be empty."

    try:
        settings = _base_settings(require_reddit=False)
        service = await _velocity_service(settings)
        await service.unwatch_gap(clean_name)
        return json.dumps({"removed": clean_name}, ensure_ascii=False)
    except WatchNotFoundError:
        return f"Error: no watch named '{clean_name}'."
    except StorageError as exc:
        logger.exception("unwatch_gap storage failed")
        return f"Error: StorageError: {exc}"
    except Exception as exc:
        logger.exception("unwatch_gap failed")
        return f"Error: {type(exc).__name__}: {exc}"


async def _list_watches_impl() -> str:
    """Return JSON string {"watches": [...], "count": N} or a friendly 'Error: ...' string."""
    try:
        settings = _base_settings(require_reddit=False)
        service = await _velocity_service(settings)
        result = await service.list_watches()
        return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    except StorageError as exc:
        logger.exception("list_watches storage failed")
        return f"Error: StorageError: {exc}"
    except Exception as exc:
        logger.exception("list_watches failed")
        return f"Error: {type(exc).__name__}: {exc}"


async def _check_velocity_impl(name: str) -> str:
    """Return JSON string of the VelocityReport or a friendly 'Error: ...' string."""
    clean_name = name.strip()
    if not clean_name:
        return "Error: name must not be empty."

    try:
        settings = _base_settings(require_reddit=False)
        service = await _velocity_service(settings)
        report = await service.check_velocity(clean_name)
        return json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    except WatchNotFoundError:
        return f"Error: no watch named '{clean_name}'. Call watch_gap first."
    except IngestionError as exc:
        logger.exception("check_velocity fetch failed")
        return f"Error: {type(exc).__name__}: {exc}"
    except StorageError as exc:
        logger.exception("check_velocity storage failed")
        return f"Error: StorageError: {exc}"
    except Exception as exc:
        logger.exception("check_velocity failed")
        return f"Error: {type(exc).__name__}: {exc}"


@mcp.tool()
async def watch_gap(name: str, query: str, tags: str = "story") -> str:
    """Register a gap/query to watch for demand velocity on Hacker News.

    Persists a named watch (query + Algolia tags) to a local SQLite watchlist so
    future check_velocity calls can compare snapshots over time.

    Args:
        name: unique handle for this watch (you choose it).
        query: free-text query sent to Hacker News search (e.g. "ai code review").
        tags: comma-separated Algolia tags (default "story").

    Returns:
        A JSON string of the created Watch, or "Error: ..." (e.g. duplicate name).
    """
    return await _watch_gap_impl(name, query, tags)


@mcp.tool()
async def unwatch_gap(name: str) -> str:
    """Remove a watch (and its snapshot history) from the watchlist.

    Args:
        name: the watch's unique handle, as passed to watch_gap.

    Returns:
        A JSON string {"removed": name}, or "Error: ..." if no such watch exists.
    """
    return await _unwatch_gap_impl(name)


@mcp.tool()
async def list_watches() -> str:
    """List all registered gap watches.

    Returns:
        A JSON string {"watches": [Watch, ...], "count": N}, or "Error: ...".
    """
    return await _list_watches_impl()


@mcp.tool()
async def check_velocity(name: str) -> str:
    """Check demand velocity for a watched gap: fetch fresh HN data, snapshot it,
    and return the delta versus the previous snapshot as evidence (no opinion score).

    Use this to see whether a gap is accelerating or cooling off. It fetches current
    Hacker News signal for the watch's query/tags, records a snapshot, and diffs it
    against the previous snapshot (if any) for this watch — post/point/comment counts,
    per-day rates, and their deltas. You judge whether that's "accelerating" yourself;
    this tool never assigns a score or opinion.

    Watch for `fetch_limit_hit: true` in either snapshot — it means the fetch hit its
    cap while the oldest fetched post was still inside the time window, so counts for
    that snapshot are an undercount rather than the true total.

    Args:
        name: the watch's unique handle, as passed to watch_gap.

    Returns:
        A JSON string of VelocityReport {"watch", "current", "previous", "delta",
        "new_evidence", "note"}, or "Error: ..." (e.g. no watch with that name).
    """
    return await _check_velocity_impl(name)


def _configure_logging() -> None:
    """Attach a StreamHandler(sys.stderr) to the root logger; never stdout."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def main() -> None:
    """Entry point: configure stderr logging, then mcp.run(transport='stdio')."""
    _configure_logging()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
