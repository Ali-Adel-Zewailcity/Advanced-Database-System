"""Exception hierarchy for coredb."""


class DBError(Exception):
    """Base class for every coredb error."""


class ConfigError(DBError):
    """Invalid configuration value."""


class StorageError(DBError):
    """Low-level storage failure."""


class CorruptDatabaseError(StorageError):
    """The database file does not look like a valid coredb file."""


class PageNotFoundError(StorageError):
    """Page id is unknown, unallocated or already freed."""


class RecordNotFoundError(StorageError):
    """Record id does not point to a live record."""


class RecordTooLargeError(StorageError):
    """A record cannot fit in a single page (values larger than a page are not supported)."""


class BufferPoolFullError(DBError):
    """Every frame is pinned; no victim can be evicted."""


class SchemaError(DBError):
    """Row or schema violates the table definition."""


class CatalogError(DBError):
    """Unknown/duplicate table, bad layout name, ..."""
