"""Write-ahead log file: framing, the user-space buffer, and finding the end of the log."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from mvrs_db.errors import CorruptWalError
from mvrs_db.recovery.wal import (
    RECORD_HEADER_SIZE,
    WAL_HEADER_SIZE,
    RecordType,
    WalWriter,
    encode_record,
    read_wal,
)

PAGE_SIZE = 4096
DB_ID = 0x1122334455667788


def new_wal(path: Path, base_lsn: int = 1) -> WalWriter:
    return WalWriter.create(path, PAGE_SIZE, DB_ID, base_lsn)


def three_records(path: Path) -> list[bytes]:
    """A log holding three records of different sizes; returns their payloads."""
    payloads = [b"first", b"", b"third-and-longer" * 5]
    wal = new_wal(path)
    for txn_id, payload in enumerate(payloads, start=1):
        wal.append(RecordType.PAGE_IMAGE if payload else RecordType.COMMIT, txn_id, payload)
    wal.flush()
    wal.close()
    return payloads


def test_new_log_is_just_a_header(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    new_wal(path, base_lsn=500).close()

    contents = read_wal(path)

    assert WAL_HEADER_SIZE == 40
    assert path.stat().st_size == 40
    assert contents is not None
    assert (contents.page_size, contents.db_id, contents.base_lsn) == (PAGE_SIZE, DB_ID, 500)
    assert contents.records == []
    assert contents.next_lsn == 500


def test_missing_log_reads_as_none(tmp_path: Path) -> None:
    assert read_wal(tmp_path / "absent.wal") is None


def test_round_trip_and_lsns(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    wal = new_wal(path, base_lsn=100)

    first = wal.append(RecordType.PAGE_IMAGE, 7, b"abc")
    second = wal.append(RecordType.HEADER, 7, b"h" * 64)
    third = wal.append(RecordType.COMMIT, 7)
    wal.flush()
    wal.close()

    assert first == 100
    assert second == first + RECORD_HEADER_SIZE + 3  # an LSN is a position in the log
    assert third == second + RECORD_HEADER_SIZE + 64
    contents = read_wal(path)
    assert contents is not None
    assert [(r.lsn, r.txn_id, r.type, r.payload) for r in contents.records] == [
        (first, 7, RecordType.PAGE_IMAGE, b"abc"),
        (second, 7, RecordType.HEADER, b"h" * 64),
        (third, 7, RecordType.COMMIT, b""),
    ]
    assert contents.next_lsn == third + RECORD_HEADER_SIZE
    assert contents.valid_bytes == contents.file_bytes


def test_appended_records_stay_in_memory_until_flush(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    wal = new_wal(path)

    wal.append(RecordType.PAGE_IMAGE, 1, b"x" * 100)
    wal.append(RecordType.COMMIT, 1)

    assert wal.buffered_bytes == 2 * RECORD_HEADER_SIZE + 100
    assert path.stat().st_size == WAL_HEADER_SIZE  # nothing has reached the file
    on_disk = read_wal(path)
    assert on_disk is not None and on_disk.records == []

    wal.flush()
    assert wal.buffered_bytes == 0
    after = read_wal(path)
    assert after is not None and len(after.records) == 2
    wal.close()


def test_discard_buffer_drops_unflushed_records(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    wal = new_wal(path)
    wal.append(RecordType.COMMIT, 1)
    wal.flush()

    wal.append(RecordType.PAGE_IMAGE, 2, b"never written")
    wal.discard_buffer()
    lsn = wal.append(RecordType.COMMIT, 3)
    wal.flush()
    wal.close()

    contents = read_wal(path)
    assert contents is not None
    assert [r.txn_id for r in contents.records] == [1, 3]
    assert contents.records[1].lsn == lsn  # the discarded record left no gap


def test_unsynced_flush_is_synced_on_demand(tmp_path: Path) -> None:
    wal = new_wal(tmp_path / "db.wal")
    wal.append(RecordType.COMMIT, 1)

    wal.flush(sync=False)
    assert wal._unsynced
    wal.sync()
    assert not wal._unsynced
    wal.sync()  # nothing to do the second time
    wal.close()


def test_truncating_the_log_anywhere_yields_a_clean_prefix(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    payloads = three_records(path)
    full = path.read_bytes()
    ends = []
    offset = WAL_HEADER_SIZE
    for payload in payloads:
        offset += RECORD_HEADER_SIZE + len(payload)
        ends.append(offset)

    for length in range(WAL_HEADER_SIZE, len(full) + 1):
        path.write_bytes(full[:length])
        contents = read_wal(path)
        assert contents is not None
        expected = sum(1 for end in ends if end <= length)
        assert len(contents.records) == expected, f"truncated at {length}"
        assert [r.payload for r in contents.records] == payloads[:expected]


def test_damaging_any_byte_cuts_the_log_at_that_record(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    payloads = three_records(path)
    full = path.read_bytes()
    starts = [WAL_HEADER_SIZE]
    for payload in payloads[:-1]:
        starts.append(starts[-1] + RECORD_HEADER_SIZE + len(payload))

    for offset in range(WAL_HEADER_SIZE, len(full)):
        damaged = bytearray(full)
        damaged[offset] ^= 0x01
        path.write_bytes(damaged)
        contents = read_wal(path)
        assert contents is not None
        intact = sum(1 for start in starts[1:] if start <= offset)
        # Records before the damaged one survive; the damaged one and all after it do not.
        assert len(contents.records) == intact, f"damage at {offset}"
        assert contents.valid_bytes == starts[intact]


@pytest.mark.parametrize("offset", range(WAL_HEADER_SIZE))
def test_damaged_header_is_an_error(tmp_path: Path, offset: int) -> None:
    path = tmp_path / "db.wal"
    three_records(path)
    raw = bytearray(path.read_bytes())
    raw[offset] ^= 0x01
    path.write_bytes(raw)

    with pytest.raises(CorruptWalError):
        read_wal(path)


def test_truncated_header_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    path.write_bytes(b"MVRSWAL\x00 too short")

    with pytest.raises(CorruptWalError):
        read_wal(path)


def test_record_with_valid_crc_but_wrong_lsn_ends_the_log(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    wal = new_wal(path, base_lsn=1)
    wal.append(RecordType.COMMIT, 1)
    wal.flush()
    wal.close()
    stale = encode_record(lsn=999, txn_id=2, record_type=RecordType.COMMIT, payload=b"")
    path.write_bytes(path.read_bytes() + stale)

    contents = read_wal(path)

    assert contents is not None
    assert [r.txn_id for r in contents.records] == [1]
    assert contents.file_bytes - contents.valid_bytes == len(stale)


def test_reset_empties_the_log_and_moves_the_base_forward(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    wal = new_wal(path, base_lsn=1)
    wal.append(RecordType.PAGE_IMAGE, 1, b"p" * 200)
    wal.append(RecordType.COMMIT, 1)
    wal.flush()
    next_lsn = wal.next_lsn

    wal.reset(next_lsn)
    assert path.stat().st_size == WAL_HEADER_SIZE
    lsn = wal.append(RecordType.COMMIT, 2)
    wal.flush()
    wal.close()

    assert lsn == next_lsn  # LSNs keep growing across the reset
    contents = read_wal(path)
    assert contents is not None
    assert contents.base_lsn == next_lsn
    assert [r.txn_id for r in contents.records] == [2]
    with pytest.raises(ValueError, match="forward"):
        new_wal(tmp_path / "other.wal", base_lsn=10).reset(5)


def test_records_left_behind_by_an_interrupted_reset_are_ignored(tmp_path: Path) -> None:
    """A crash between "rewrite the header" and "truncate" leaves old records in place."""
    path = tmp_path / "db.wal"
    three_records(path)
    old = path.read_bytes()
    before = read_wal(path)
    assert before is not None

    new_base = before.next_lsn
    body = struct.pack("<8sHHIQQ4s", b"MVRSWAL\x00", 1, 0, PAGE_SIZE, DB_ID, new_base, bytes(4))
    path.write_bytes(body + struct.pack("<I", zlib.crc32(body)) + old[WAL_HEADER_SIZE:])

    contents = read_wal(path)

    assert contents is not None
    assert contents.records == []  # valid CRCs, but LSNs from before the new base
    assert contents.next_lsn == new_base


def test_absurd_record_length_ends_the_log_without_reading_past_it(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    new_wal(path).close()
    path.write_bytes(path.read_bytes() + struct.pack("<IIQQB", 2**31, 0, 1, 1, 1) + b"x" * 50)

    contents = read_wal(path)

    assert contents is not None and contents.records == []


def test_create_replaces_an_existing_log(tmp_path: Path) -> None:
    path = tmp_path / "db.wal"
    three_records(path)

    new_wal(path, base_lsn=1000).close()

    contents = read_wal(path)
    assert contents is not None
    assert contents.records == []
    assert contents.base_lsn == 1000
