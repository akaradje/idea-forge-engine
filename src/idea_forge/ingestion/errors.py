"""Exception hierarchy for ingestion adapters."""


class IngestionError(Exception):
    """Base error for all ingestion adapter failures."""


class AuthError(IngestionError):
    """Token acquisition failed, or repeated 401 responses."""


class RateLimitError(IngestionError):
    """429 rate limit exceeded after the backoff budget was exhausted."""


class SubredditUnavailableError(IngestionError):
    """403 private/banned or 404 not found for a subreddit."""
