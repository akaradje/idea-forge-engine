"""Errors raised by the gap detection layer."""


class GapDetectionError(Exception):
    """Raised when gap detection fails irrecoverably (missing key, SDK/API error, invalid output)."""
