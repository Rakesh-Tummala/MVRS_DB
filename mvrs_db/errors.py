"""Every error MVRS_DB raises on purpose derives from MvrsDbError.

Callers can catch one specific failure, a family (for example CorruptionError),
or everything the engine raises deliberately.
"""

from __future__ import annotations


class MvrsDbError(Exception):
    """Base class for all MVRS_DB errors."""


class InternalError(MvrsDbError):
    """An internal invariant was violated. This is a bug in MVRS_DB."""


# ---------------------------------------------------------------- storage


class StorageError(MvrsDbError):
    """A failure in the storage layer."""


class StorageIOError(StorageError):
    """The operating system reported an I/O failure (disk full, permissions, ...)."""


class DatabaseNotFoundError(StorageError):
    """The database file does not exist."""


class DatabaseExistsError(StorageError):
    """Refused to create a database over an existing file."""


class DatabaseClosedError(StorageError):
    """The database was used after close()."""


class DatabaseLockedError(StorageError):
    """Another process (or another handle in this process) has the database open."""


class DatabaseFullError(StorageError):
    """The file has reached the maximum number of pages."""


class NotADatabaseError(StorageError):
    """The file does not start with the MVRS_DB magic number."""


class UnsupportedVersionError(StorageError):
    """The file or page was written by a format version this code does not know."""


class InvalidPageSizeError(StorageError):
    """The requested page size is not one of the allowed sizes."""


class InvalidPageIdError(StorageError):
    """A page ID is outside the range of pages that exist in the file."""


class InvalidPageBufferError(StorageError):
    """A buffer handed to the storage layer is not exactly one page long."""


class InvalidRecordIdError(StorageError):
    """A record ID does not refer to a live record."""


class InvalidValueError(StorageError):
    """A value cannot be stored in a column of the given type."""


class RecordTooLargeError(StorageError):
    """A record does not fit in one page."""


class BufferPoolFullError(StorageError):
    """Every frame in the buffer pool is pinned, so no page can be loaded."""


class PagePinError(StorageError):
    """A page was unpinned more often than it was pinned."""


class CorruptionError(StorageError):
    """Bytes on disk are not what MVRS_DB wrote."""


class CorruptHeaderError(CorruptionError):
    """The file header is damaged or internally inconsistent."""


class CorruptFileError(CorruptionError):
    """The file's length does not match its header (truncated or has extra bytes)."""


class CorruptPageError(CorruptionError):
    """A page's header fields or layout are invalid."""


class ChecksumMismatchError(CorruptPageError):
    """A page's stored checksum does not match its contents."""


class PageIdMismatchError(CorruptPageError):
    """A page's header names a different page ID than the one requested."""


class CorruptRecordError(CorruptionError):
    """Record bytes do not decode against the table's schema."""


class CorruptIndexError(CorruptionError):
    """A B+ tree violates one of its invariants."""


class CorruptCatalogError(CorruptionError):
    """A catalog record cannot be decoded."""


# ---------------------------------------------------------------- index


class DuplicateKeyError(MvrsDbError):
    """The key is already present in a unique index."""


class KeyTooLargeError(MvrsDbError):
    """An index key is longer than the maximum key size."""


# ---------------------------------------------------------------- log and recovery


class WalError(MvrsDbError):
    """A failure in the write-ahead log."""


class CorruptWalError(WalError, CorruptionError):
    """The log file's header is damaged."""


class WalMismatchError(WalError):
    """The log file belongs to a different database."""


class EngineFailedError(MvrsDbError):
    """A durability step failed earlier; the database must be reopened before writing."""


# ---------------------------------------------------------------- transactions


class TransactionError(MvrsDbError):
    """Misuse of the transaction interface, or a transaction that had to be aborted."""


class TransactionTooLargeError(TransactionError):
    """The transaction changed more pages than the buffer pool can hold."""


class DeadlockError(TransactionError):
    """Granting this lock would create a cycle of waiting transactions."""


class LockTimeoutError(TransactionError):
    """A lock could not be acquired within the timeout."""


class SerializationError(TransactionError):
    """A snapshot transaction tried to write after the database changed under it.

    Its reads came from a snapshot that is no longer current, so writing on the
    strength of them could undo someone else's committed change. Roll back and
    run the transaction again.
    """


# ---------------------------------------------------------------- SQL


class SqlError(MvrsDbError):
    """A problem with a SQL statement."""


class SqlSyntaxError(SqlError):
    """The statement is not valid in MVRS_DB's SQL subset."""


class ParameterError(SqlError):
    """The parameters given do not fit the statement's `?` placeholders."""


class QueryTooLargeError(SqlError):
    """The statement text exceeds the size limit."""


class CatalogError(SqlError):
    """A problem with table or column definitions."""


class TableExistsError(CatalogError):
    """A table with this name already exists."""


class TableNotFoundError(CatalogError):
    """No table has this name."""


class ColumnNotFoundError(CatalogError):
    """The table has no column with this name."""


class TypeMismatchError(SqlError):
    """A value or comparison has incompatible types."""


class ConstraintViolationError(SqlError):
    """A NOT NULL, PRIMARY KEY or UNIQUE constraint would be violated."""
