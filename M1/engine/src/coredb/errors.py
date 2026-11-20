# Milestone 2 additions

class IndexError(DBError):
    """Index operation failure (duplicate key, unsupported key type, corrupt node)."""


class DuplicateKeyError(IndexError):
    """Unique index constraint violation."""


class ObjectStorageError(StorageError):
    """Large object storage or retrieval failure."""


class CryptoError(DBError):
    """Encryption or decryption hook failure."""


class QueryError(DBError):
    """Invalid SQL-like query syntax or execution error."""