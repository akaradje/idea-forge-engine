"""MCP server (stdio) exposing Idea Forge ingestion adapters as tools for Claude Code.

CRITICAL: stdio transport uses stdout for the MCP protocol. All logging MUST go to
stderr. Never print()/log to stdout in this module.
"""

import json
import logging
import sys
from collections.abc import AsyncGenerator
from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from idea_forge.config import Settings
from idea_forge.ingestion.base import RawDocument
from idea_forge.ingestion.errors import AuthError, IngestionError
from idea_forge.ingestion.hackernews import HackerNewsAdapter
from idea_forge.ingestion.reddit import RedditAdapter

logger = logging.getLogger("idea_forge.mcp_server")

MAX_BODY_CHARS = 2000  # RawDocument.body truncated to this before serialization
MAX_LIMIT = 100  # per-call limit clamp ceiling

_REDDIT_CREDS_ERROR = (
    "Error: Reddit credentials not configured. Set REDDIT_CLIENT_ID, "
    "REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT in .env."
)

_REDDIT_REQUIRED_FIELDS = frozenset(
    {"reddit_client_id", "reddit_client_secret", "reddit_user_agent"}
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
