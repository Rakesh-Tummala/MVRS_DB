"""The write-ahead log file.

File layout: a 40-byte header, then records back to back.

    header (<8sHHIQQ4sI)
        magic        b"MVRSWAL\\0"
        version
        reserved
        page_size    must match the data file
        db_id        must match the data file
        base_lsn     LSN of the first record in this file
        reserved
        crc          CRC32 of the 36 bytes above

    record (<IIQQB + payload)
        length       payload length
        crc          CRC32 of length + lsn + txn_id + type + payload
        lsn          must equal the record's computed position
        txn_id
        type         PAGE_IMAGE, HEADER or COMMIT
        payload

LSN (log sequence number)
    base_lsn plus the record's byte offset after the header. LSNs only ever grow:
    when a checkpoint empties the file, base_lsn moves forward past every LSN
    already handed out.

Finding the end of the log
    Reading stops at the first record that is incomplete, has an impossible
    length, fails its CRC, or carries the wrong LSN. Everything after that point
    is treated as never written. The stored LSN matters after a checkpoint: old
    records left behind by a crash between "rewrite header" and "truncate" have
    valid CRCs but LSNs below the new base, so they are rejected.

User-space buffer
    append() only adds to a buffer in this process. flush() hands the bytes to
    the operating system, and fsync makes them durable. A process that dies
    before flush() loses the buffered records, which is exactly what the crash
    tests rely on.
"""

from __future__ import annotations

import contextlib
import os
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from mvrs_db import config, faults
from mvrs_db.errors import CorruptWalError, StorageIOError

_HEADER = struct.Struct("<8sHHIQQ4sI")
_RECORD = struct.Struct("<IIQQB")
_CRC = struct.Struct("<I")
_LENGTH = struct.Struct("<I")

WAL_HEADER_SIZE = _HEADER.size  # 40
RECORD_HEADER_SIZE = _RECORD.size  # 25
_HEADER_CRC_OFFSET = WAL_HEADER_SIZE - _CRC.size
# A record holds at most one page plus a few bytes; anything bigger is garbage.
_MAX_PAYLOAD = max(config.ALLOWED_PAGE_SIZES) + 64

_O_BINARY = getattr(os, "O_BINARY", 0)


class RecordType(IntEnum):
    PAGE_IMAGE = 1  # payload: page ID (u32) + the whole page after the change
    HEADER = 2  # payload: the 64-byte data file header after the change
    COMMIT = 3  # no payload; the transaction's earlier records now count


@dataclass(frozen=True, slots=True)
class WalRecord:
    lsn: int
    txn_id: int
    type: RecordType
    payload: bytes


@dataclass(frozen=True, slots=True)
class WalContents:
    page_size: int
    db_id: int
    base_lsn: int
    records: list[WalRecord]
    valid_bytes: int  # offset just past the last valid record
    file_bytes: int  # actual file length; larger means a discarded tail

    @property
    def next_lsn(self) -> int:
        return self.base_lsn + self.valid_bytes - WAL_HEADER_SIZE


def _pack_header(page_size: int, db_id: int, base_lsn: int) -> bytes:
    body = _HEADER.pack(
        config.WAL_MAGIC, config.WAL_FORMAT_VERSION, 0, page_size, db_id, base_lsn, bytes(4), 0
    )
    return body[:_HEADER_CRC_OFFSET] + _CRC.pack(zlib.crc32(body[:_HEADER_CRC_OFFSET]))


def _record_crc(length: int, lsn: int, txn_id: int, record_type: int, payload: bytes) -> int:
    head = _LENGTH.pack(length) + struct.pack("<QQB", lsn, txn_id, record_type)
    return zlib.crc32(payload, zlib.crc32(head))


def encode_record(lsn: int, txn_id: int, record_type: RecordType, payload: bytes) -> bytes:
    crc = _record_crc(len(payload), lsn, txn_id, record_type, payload)
    return _RECORD.pack(len(payload), crc, lsn, txn_id, record_type) + payload


def read_wal(path: str | os.PathLike[str]) -> WalContents | None:
    """Read every valid record. Returns None if the file does not exist.

    Raises CorruptWalError only for a damaged header. A damaged or partial
    record is not an error: it marks the end of the log.
    """
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageIOError(f"cannot read log {path}: {exc}") from exc

    if len(data) < WAL_HEADER_SIZE:
        raise CorruptWalError(f"{path}: log header is truncated ({len(data)} bytes)")
    magic, version, _r1, page_size, db_id, base_lsn, _r2, crc = _HEADER.unpack_from(data, 0)
    if magic != config.WAL_MAGIC:
        raise CorruptWalError(f"{path}: not a MVRS_DB log file")
    if crc != zlib.crc32(data[:_HEADER_CRC_OFFSET]):
        raise CorruptWalError(f"{path}: log header checksum does not match")
    if version != config.WAL_FORMAT_VERSION:
        raise CorruptWalError(f"{path}: unsupported log format version {version}")

    records: list[WalRecord] = []
    offset = WAL_HEADER_SIZE
    end = len(data)
    while offset + RECORD_HEADER_SIZE <= end:
        length, crc, lsn, txn_id, raw_type = _RECORD.unpack_from(data, offset)
        payload_start = offset + RECORD_HEADER_SIZE
        if length > _MAX_PAYLOAD or payload_start + length > end:
            break
        if lsn != base_lsn + offset - WAL_HEADER_SIZE:
            break
        payload = data[payload_start : payload_start + length]
        if crc != _record_crc(length, lsn, txn_id, raw_type, payload):
            break
        try:
            record_type = RecordType(raw_type)
        except ValueError:
            break
        records.append(WalRecord(lsn, txn_id, record_type, payload))
        offset = payload_start + length
    return WalContents(page_size, db_id, base_lsn, records, offset, end)


class WalWriter:
    """Appends records to the log. One writer per database, used by one thread at a time."""

    def __init__(self, fd: int, path: Path, page_size: int, db_id: int, base_lsn: int) -> None:
        self._fd: int | None = fd
        self._path = path
        self._page_size = page_size
        self._db_id = db_id
        self._base_lsn = base_lsn
        self._file_size = WAL_HEADER_SIZE
        self._buffer = bytearray()
        self._unsynced = False

    @classmethod
    def create(
        cls, path: str | os.PathLike[str], page_size: int, db_id: int, base_lsn: int
    ) -> WalWriter:
        """Start an empty log, replacing any existing file at `path`."""
        file_path = Path(path)
        try:
            fd = os.open(file_path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
        except OSError as exc:
            raise StorageIOError(f"cannot open log {file_path}: {exc}") from exc
        writer = cls(fd, file_path, page_size, db_id, base_lsn)
        try:
            writer.reset(base_lsn)
        except BaseException:
            writer.close()
            raise
        return writer

    @property
    def path(self) -> Path:
        return self._path

    @property
    def base_lsn(self) -> int:
        return self._base_lsn

    @property
    def next_lsn(self) -> int:
        """The LSN the next appended record will get."""
        return self._base_lsn + self._file_size + len(self._buffer) - WAL_HEADER_SIZE

    @property
    def size(self) -> int:
        """Bytes in the file plus bytes still buffered."""
        return self._file_size + len(self._buffer)

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def _require_open(self) -> int:
        if self._fd is None:
            raise StorageIOError(f"log {self._path} is closed")
        return self._fd

    def append(self, record_type: RecordType, txn_id: int, payload: bytes = b"") -> int:
        """Buffer one record and return its LSN. Nothing reaches the file yet."""
        self._require_open()
        lsn = self.next_lsn
        self._buffer += encode_record(lsn, txn_id, record_type, payload)
        return lsn

    def discard_buffer(self) -> None:
        """Drop records that were appended but not flushed."""
        self._buffer.clear()

    def flush(self, *, sync: bool = True) -> None:
        """Write buffered records to the file, and fsync them if `sync`."""
        fd = self._require_open()
        if not self._buffer:
            return
        faults.crash_point("wal.before_flush")
        data = bytes(self._buffer)
        try:
            os.lseek(fd, self._file_size, os.SEEK_SET)
            if faults.armed("wal.partial_flush"):
                _write_all(fd, data[: len(data) // 2])
                faults.crash()
            _write_all(fd, data)
            self._unsynced = True
            faults.crash_point("wal.after_write")
            if sync:
                os.fsync(fd)
                self._unsynced = False
        except OSError as exc:
            raise StorageIOError(f"cannot write log {self._path}: {exc}") from exc
        self._file_size += len(data)
        self._buffer.clear()
        faults.crash_point("wal.after_sync")

    def sync(self) -> None:
        """fsync the log if it has written-but-unsynced records.

        With synchronous commits this never has anything to do. With
        synchronous=False it is what keeps the WAL rule true: it is called
        before any page is written to the data file.
        """
        if not self._unsynced or self._fd is None:
            return
        try:
            os.fsync(self._fd)
        except OSError as exc:
            raise StorageIOError(f"cannot sync log {self._path}: {exc}") from exc
        self._unsynced = False

    def reset(self, base_lsn: int) -> None:
        """Empty the log. Called once every change it describes is safely in the data file.

        The header is rewritten and synced before the file is truncated. If the
        process dies in between, the leftover records carry LSNs below the new
        base and are ignored by read_wal().
        """
        fd = self._require_open()
        if base_lsn < self._base_lsn:
            raise ValueError("base LSN can only move forward")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, _pack_header(self._page_size, self._db_id, base_lsn))
            os.fsync(fd)
            faults.crash_point("wal.reset_before_truncate")
            os.ftruncate(fd, WAL_HEADER_SIZE)
            os.fsync(fd)
        except OSError as exc:
            raise StorageIOError(f"cannot reset log {self._path}: {exc}") from exc
        self._base_lsn = base_lsn
        self._file_size = WAL_HEADER_SIZE
        self._buffer.clear()
        self._unsynced = False

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while len(view) > 0:
        written = os.write(fd, view)
        view = view[written:]
