"""Page-level I/O for a KestrelDB data file.

This is the only module that reads or writes the data file. Everything above it
works with page IDs, never byte offsets.

The file is an array of fixed-size pages; page n lives at byte offset
n * page_size. Page 0 holds the 64-byte file header:

    offset  size  field
    0       8     magic            b"KESTREL\\0"
    8       2     format_version
    10      2     reserved         0
    12      4     page_size
    16      4     page_count       pages in the file, including page 0
    20      4     free_list_head   0 = none
    24      4     catalog_root     0 = no catalog yet
    28      4     reserved         0
    32      8     lsn
    40      8     db_id            random at creation
    48      12    reserved         0
    60      4     header_crc       CRC32 of bytes 0..59
"""

from __future__ import annotations

import contextlib
import os
import struct
import threading
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType

from kestreldb import config
from kestreldb.errors import (
    CorruptFileError,
    CorruptHeaderError,
    DatabaseClosedError,
    DatabaseExistsError,
    DatabaseFullError,
    DatabaseNotFoundError,
    InvalidPageBufferError,
    InvalidPageIdError,
    InvalidPageSizeError,
    NotADatabaseError,
    StorageIOError,
    UnsupportedVersionError,
)
from kestreldb.storage.page import (
    Buffer,
    PageType,
    check_header,
    new_page,
    stamp_checksum,
    verify_page,
)

_FILE_HEADER = struct.Struct("<8sHHIIIIIQQ12sI")
_CRC = struct.Struct("<I")
_CRC_OFFSET = config.FILE_HEADER_SIZE - _CRC.size  # 60

# Without O_BINARY, Windows translates newline bytes and corrupts pages.
_O_BINARY = getattr(os, "O_BINARY", 0)


@dataclass(frozen=True, slots=True)
class FileHeader:
    page_size: int
    page_count: int
    free_list_head: int
    catalog_root: int
    lsn: int
    db_id: int
    format_version: int = config.FILE_FORMAT_VERSION

    def pack(self) -> bytes:
        body = _FILE_HEADER.pack(
            config.MAGIC,
            self.format_version,
            0,
            self.page_size,
            self.page_count,
            self.free_list_head,
            self.catalog_root,
            0,
            self.lsn,
            self.db_id,
            bytes(12),
            0,
        )
        return body[:_CRC_OFFSET] + _CRC.pack(zlib.crc32(body[:_CRC_OFFSET]))

    @classmethod
    def unpack(cls, raw: bytes) -> FileHeader:
        """Decode and validate a file header.

        The checks run from "is this our file at all" to "is it self-consistent",
        so the error says as precisely as possible what is wrong.
        """
        magic_len = len(config.MAGIC)
        if raw[:magic_len] != config.MAGIC:
            raise NotADatabaseError("file does not start with the KestrelDB magic number")
        if len(raw) < config.FILE_HEADER_SIZE:
            raise CorruptHeaderError(
                f"file header is truncated: {len(raw)} of {config.FILE_HEADER_SIZE} bytes"
            )
        raw = raw[: config.FILE_HEADER_SIZE]
        stored_crc = _CRC.unpack_from(raw, _CRC_OFFSET)[0]
        if stored_crc != zlib.crc32(raw[:_CRC_OFFSET]):
            raise CorruptHeaderError("file header checksum does not match its contents")
        (
            _magic,
            version,
            reserved_a,
            page_size,
            page_count,
            free_list_head,
            catalog_root,
            reserved_b,
            lsn,
            db_id,
            reserved_c,
            _crc,
        ) = _FILE_HEADER.unpack(raw)
        if version != config.FILE_FORMAT_VERSION:
            raise UnsupportedVersionError(
                f"file format version {version} is not supported "
                f"(expected {config.FILE_FORMAT_VERSION})"
            )
        if page_size not in config.ALLOWED_PAGE_SIZES:
            raise CorruptHeaderError(f"file header has an invalid page size: {page_size}")
        if reserved_a != 0 or reserved_b != 0 or any(reserved_c):
            raise CorruptHeaderError("reserved file header bytes are not zero")
        if page_count < 1:
            raise CorruptHeaderError("file header says the file has no pages")
        if free_list_head >= page_count or catalog_root >= page_count:
            raise CorruptHeaderError("file header points at a page beyond the end of the file")
        return cls(
            page_size=page_size,
            page_count=page_count,
            free_list_head=free_list_head,
            catalog_root=catalog_root,
            lsn=lsn,
            db_id=db_id,
            format_version=version,
        )


class DiskManager:
    """Reads and writes whole pages of one data file.

    Build one with DiskManager.create() or DiskManager.open(), not directly.

    Every page handed out by read_page() has passed its checksum, page ID, version
    and type checks. Every page accepted by write_page() gets a fresh checksum.
    """

    def __init__(self, fd: int, path: Path, header: FileHeader) -> None:
        self._fd: int | None = fd
        self._path = path
        self._header = header
        # Windows has no pread/pwrite, so seek + read must be one atomic step.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls, path: str | os.PathLike[str], page_size: int = config.DEFAULT_PAGE_SIZE
    ) -> DiskManager:
        """Create a new database file. Refuses to touch an existing file."""
        if page_size not in config.ALLOWED_PAGE_SIZES:
            raise InvalidPageSizeError(
                f"page size {page_size} is not one of {config.ALLOWED_PAGE_SIZES}"
            )
        file_path = Path(path)
        try:
            fd = os.open(file_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        except FileExistsError:
            raise DatabaseExistsError(f"refusing to overwrite existing file: {file_path}") from None
        except OSError as exc:
            raise StorageIOError(f"cannot create {file_path}: {exc}") from exc

        header = FileHeader(
            page_size=page_size,
            page_count=1,
            free_list_head=0,
            catalog_root=0,
            lsn=0,
            db_id=int.from_bytes(os.urandom(8), "little"),
        )
        page_zero = header.pack() + bytes(page_size - config.FILE_HEADER_SIZE)
        try:
            _write_all(fd, 0, page_zero)
            os.fsync(fd)
        except OSError as exc:
            # We created this file a moment ago, so removing the partial file is safe.
            os.close(fd)
            with contextlib.suppress(OSError):
                file_path.unlink()
            raise StorageIOError(f"cannot initialise {file_path}: {exc}") from exc
        return cls(fd, file_path, header)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> DiskManager:
        """Open an existing database file. A file that fails validation is not modified."""
        file_path = Path(path)
        try:
            fd = os.open(file_path, os.O_RDWR | _O_BINARY)
        except FileNotFoundError:
            raise DatabaseNotFoundError(f"no such database file: {file_path}") from None
        except OSError as exc:
            raise StorageIOError(f"cannot open {file_path}: {exc}") from exc

        try:
            header = FileHeader.unpack(_read_up_to(fd, 0, config.FILE_HEADER_SIZE))
            actual_size = os.fstat(fd).st_size
            expected_size = header.page_count * header.page_size
            if actual_size != expected_size:
                raise CorruptFileError(
                    f"{file_path}: file is {actual_size} bytes but its header describes "
                    f"{header.page_count} pages of {header.page_size} bytes ({expected_size})"
                )
        except OSError as exc:
            os.close(fd)
            raise StorageIOError(f"cannot read {file_path}: {exc}") from exc
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, file_path, header)

    def close(self) -> None:
        """Flush to disk and close. Safe to call more than once."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.fsync(fd)
        except OSError as exc:
            raise StorageIOError(f"cannot sync {self._path}: {exc}") from exc
        finally:
            os.close(fd)

    def __enter__(self) -> DiskManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ properties

    @property
    def path(self) -> Path:
        return self._path

    @property
    def header(self) -> FileHeader:
        return self._header

    @property
    def page_size(self) -> int:
        return self._header.page_size

    @property
    def page_count(self) -> int:
        return self._header.page_count

    @property
    def is_closed(self) -> bool:
        return self._fd is None

    # ------------------------------------------------------------------ page I/O

    def read_page(self, page_id: int) -> bytearray:
        """Return a verified copy of a page.

        Raises InvalidPageIdError, ChecksumMismatchError, PageIdMismatchError,
        UnsupportedVersionError, CorruptPageError or CorruptFileError.
        """
        self._check_page_id(page_id)
        raw = self._pread(page_id * self.page_size, self.page_size)
        if len(raw) != self.page_size:
            raise CorruptFileError(
                f"page {page_id}: read {len(raw)} of {self.page_size} bytes; the file is truncated"
            )
        buf = bytearray(raw)
        verify_page(buf, page_id)
        return buf

    def write_page(self, page_id: int, data: Buffer) -> None:
        """Checksum and write one page.

        The caller's buffer is not modified; the checksum is stamped on a copy.
        A page whose header names a different page ID is refused, which stops a
        bug in a higher layer from overwriting the wrong page.
        """
        self._check_page_id(page_id)
        if len(data) != self.page_size:
            raise InvalidPageBufferError(
                f"page {page_id}: buffer is {len(data)} bytes, expected {self.page_size}"
            )
        buf = bytearray(data)
        check_header(buf, page_id)
        stamp_checksum(buf)
        self._pwrite(page_id * self.page_size, buf)

    def extend(self) -> int:
        """Grow the file by one page and return its ID.

        The new page is written as a valid FREE page, so every page in the file
        passes verification at all times. The page allocator (milestone 1.3)
        decides what it becomes.
        """
        self._require_open()
        new_id = self._header.page_count
        if new_id > config.MAX_PAGE_ID:
            raise DatabaseFullError(f"{self._path}: cannot grow past {config.MAX_PAGE_ID} pages")
        buf = new_page(self.page_size, new_id, PageType.FREE)
        stamp_checksum(buf)
        old_size = new_id * self.page_size
        self._pwrite(old_size, buf)
        try:
            self._write_header(replace(self._header, page_count=new_id + 1))
        except StorageIOError:
            # Keep "file length == page_count * page_size" true if we can.
            with contextlib.suppress(OSError):
                os.ftruncate(self._require_open(), old_size)
            raise
        return new_id

    def sync(self) -> None:
        """Ask the OS to make everything written so far durable."""
        fd = self._require_open()
        try:
            os.fsync(fd)
        except OSError as exc:
            raise StorageIOError(f"cannot sync {self._path}: {exc}") from exc

    # ------------------------------------------------------------------ internals

    def _require_open(self) -> int:
        if self._fd is None:
            raise DatabaseClosedError(f"{self._path} is closed")
        return self._fd

    def _check_page_id(self, page_id: int) -> None:
        self._require_open()
        if isinstance(page_id, bool) or not isinstance(page_id, int):
            raise InvalidPageIdError(f"page ID must be an integer, got {page_id!r}")
        if not 1 <= page_id < self._header.page_count:
            raise InvalidPageIdError(
                f"page {page_id} is out of range; valid page IDs are "
                f"1 to {self._header.page_count - 1}"
            )

    def _write_header(self, header: FileHeader) -> None:
        self._pwrite(0, header.pack())
        self._header = header

    def _pread(self, offset: int, length: int) -> bytes:
        fd = self._require_open()
        try:
            with self._lock:
                return _read_up_to(fd, offset, length)
        except OSError as exc:
            raise StorageIOError(f"read failed at offset {offset} of {self._path}: {exc}") from exc

    def _pwrite(self, offset: int, data: Buffer) -> None:
        fd = self._require_open()
        try:
            with self._lock:
                _write_all(fd, offset, data)
        except OSError as exc:
            raise StorageIOError(f"write failed at offset {offset} of {self._path}: {exc}") from exc


def _read_up_to(fd: int, offset: int, length: int) -> bytes:
    """Read up to `length` bytes at `offset`. Shorter only if the file ends first."""
    os.lseek(fd, offset, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, offset: int, data: Buffer) -> None:
    """Write all of `data` at `offset`. os.write may write less than it was given."""
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    while len(view) > 0:
        written = os.write(fd, view)
        view = view[written:]
