"""Every error KestrelDB raises on purpose derives from KestrelError.

Callers can catch one specific failure, a family (for example CorruptionError),
or everything the engine raises deliberately.
"""

from __future__ import annotations


class KestrelError(Exception):
    """Base class for all KestrelDB errors."""


class StorageError(KestrelError):
    """A failure in the storage layer."""


class StorageIOError(StorageError):
    """The operating system reported an I/O failure (disk full, permissions, ...)."""


class DatabaseNotFoundError(StorageError):
    """The database file does not exist."""


class DatabaseExistsError(StorageError):
    """Refused to create a database over an existing file."""


class DatabaseClosedError(StorageError):
    """The database was used after close()."""


class DatabaseFullError(StorageError):
    """The file has reached the maximum number of pages."""


class NotADatabaseError(StorageError):
    """The file does not start with the KestrelDB magic number."""


class UnsupportedVersionError(StorageError):
    """The file or page was written by a format version this code does not know."""


class InvalidPageSizeError(StorageError):
    """The requested page size is not one of the allowed sizes."""


class InvalidPageIdError(StorageError):
    """A page ID is outside the range of pages that exist in the file."""


class InvalidPageBufferError(StorageError):
    """A buffer handed to the storage layer is not exactly one page long."""


class CorruptionError(StorageError):
    """Bytes on disk are not what KestrelDB wrote."""


class CorruptHeaderError(CorruptionError):
    """The file header is damaged or internally inconsistent."""


class CorruptFileError(CorruptionError):
    """The file's length does not match its header (truncated or has extra bytes)."""


class CorruptPageError(CorruptionError):
    """A page's header fields are invalid."""


class ChecksumMismatchError(CorruptPageError):
    """A page's stored checksum does not match its contents."""


class PageIdMismatchError(CorruptPageError):
    """A page's header names a different page ID than the one requested."""
