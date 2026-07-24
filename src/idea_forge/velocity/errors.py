"""Exception hierarchy for the velocity (watchlist/snapshot) layer."""


class VelocityError(Exception):
    """Base error for all velocity layer failures."""


class WatchNotFoundError(VelocityError):
    """No watch exists with the given name."""


class DuplicateWatchError(VelocityError):
    """A watch with the given name already exists."""


class StorageError(VelocityError):
    """Wraps sqlite3.Error (corrupt db, lock timeout, etc.)."""
