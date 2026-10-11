"""The redo pass, driven by hand-built logs."""

from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import pytest

from mvrs_db.errors import ChecksumMismatchError, CorruptWalError, WalMismatchError
from mvrs_db.recovery.recovery_manager import recover
from mvrs_db.recovery.wal import RecordType, WalWriter
from mvrs_db.storage.disk_manager import DiskManager
from mvrs_db.storage.page import PAGE_HEADER_SIZE, PageType, new_page, page_lsn, set_page_lsn

PAGE_SIZE = 4096


def make_db(path: Path, pages: int = 3) -> DiskManager:
    """A database whose pages 1..pages are heap pages filled with 0x00 at LSN 0."""
    disk = DiskManager.create(path)
    for _ in range(pages):
        page_id = disk.extend()
        disk.write_page(page_id, new_page(PAGE_SIZE, page_id, PageType.HEAP))
    return disk


def image(page_id: int, fill: int) -> bytes:
    buf = new_page(PAGE_SIZE, page_id, PageType.HEAP)
    buf[PAGE_HEADER_SIZE:] = bytes([fill]) * (PAGE_SIZE - PAGE_HEADER_SIZE)
    return bytes(buf)


class Log:
    """Builds a log the way the commit path does: image records, then COMMIT."""

    def __init__(self, path: Path, disk: DiskManager) -> None:
        self.wal = WalWriter.create(path, PAGE_SIZE, disk.header.db_id, 1)

    def page(self, txn_id: int, page_id: int, fill: int) -> int:
        buf = bytearray(image(page_id, fill))
        set_page_lsn(buf, self.wal.next_lsn)
        return self.wal.append(
            RecordType.PAGE_IMAGE, txn_id, struct.pack("<I", page_id) + bytes(buf)
        )

    def commit(self, txn_id: int) -> None:
        self.wal.append(RecordType.COMMIT, txn_id)

    def done(self) -> None:
        self.wal.flush()
        self.wal.close()


def fill_of(disk: DiskManager, page_id: int) -> int:
    return disk.read_page(page_id)[PAGE_HEADER_SIZE]


def test_no_log_means_nothing_to_do(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs")

    stats = recover(disk, tmp_path / "db.mvrs.wal")

    assert stats.records_scanned == 0
    assert stats.pages_restored == 0
    disk.close()


def test_committed_changes_are_redone_and_uncommitted_ones_ignored(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs")
    log = Log(tmp_path / "db.mvrs.wal", disk)
    log.page(1, 1, 0xAA)
    log.commit(1)
    log.page(2, 2, 0xBB)  # transaction 2 never commits
    log.page(3, 3, 0xCC)
    log.commit(3)
    log.done()

    stats = recover(disk, tmp_path / "db.mvrs.wal")

    assert [fill_of(disk, p) for p in (1, 2, 3)] == [0xAA, 0x00, 0xCC]
    assert stats.committed_transactions == 2
    assert stats.uncommitted_transactions == 1
    assert stats.pages_restored == 2
    disk.close()


def test_the_newest_committed_image_of_a_page_wins(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs")
    log = Log(tmp_path / "db.mvrs.wal", disk)
    log.page(1, 1, 0x11)
    log.commit(1)
    newest = log.page(2, 1, 0x22)
    log.commit(2)
    log.page(3, 1, 0x33)  # newer still, but not committed
    log.done()

    recover(disk, tmp_path / "db.mvrs.wal")

    assert fill_of(disk, 1) == 0x22
    assert page_lsn(disk.read_page(1)) == newest
    disk.close()


def test_commit_record_must_be_inside_the_valid_log(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs.wal"
    disk = make_db(tmp_path / "db.mvrs")
    log = Log(path, disk)
    log.page(1, 1, 0xAA)
    log.commit(1)
    log.done()
    path.write_bytes(path.read_bytes()[:-1])  # the COMMIT record loses its last byte

    stats = recover(disk, path)

    assert fill_of(disk, 1) == 0x00
    assert stats.committed_transactions == 0
    assert stats.discarded_tail_bytes > 0
    disk.close()


def test_pages_already_up_to_date_are_left_alone(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs")
    log = Log(tmp_path / "db.mvrs.wal", disk)
    lsn = log.page(1, 1, 0xAA)
    log.page(1, 2, 0xBB)
    log.commit(1)
    log.done()
    # Page 1 was already flushed with this LSN before the crash, with different
    # contents so the test can tell "skipped" from "rewritten".
    flushed = bytearray(image(1, 0x77))
    set_page_lsn(flushed, lsn)
    disk.write_page(1, flushed)

    stats = recover(disk, tmp_path / "db.mvrs.wal")

    assert fill_of(disk, 1) == 0x77
    assert fill_of(disk, 2) == 0xBB
    assert (stats.pages_already_current, stats.pages_restored) == (1, 1)
    disk.close()


def test_torn_page_is_restored_from_its_image(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    disk = make_db(path)
    log = Log(tmp_path / "db.mvrs.wal", disk)
    lsn = log.page(1, 2, 0xAA)
    log.commit(1)
    log.done()
    # The page was being written with this very image when the power failed:
    # the first half is new, the second half is still the old page.
    new = bytearray(image(2, 0xAA))
    set_page_lsn(new, lsn)
    disk.write_page(2, new)
    disk.close()
    raw = bytearray(path.read_bytes())
    raw[2 * PAGE_SIZE + PAGE_SIZE // 2 : 3 * PAGE_SIZE] = bytes(PAGE_SIZE // 2)
    path.write_bytes(raw)
    disk = DiskManager.open(path)
    with pytest.raises(ChecksumMismatchError):
        disk.read_page(2)

    stats = recover(disk, tmp_path / "db.mvrs.wal")

    assert fill_of(disk, 2) == 0xAA
    assert bytes(disk.read_page(2)[-10:]) == b"\xaa" * 10
    assert stats.pages_restored == 1
    disk.close()


def test_logged_header_extends_the_file(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs", pages=1)
    log = Log(tmp_path / "db.mvrs.wal", disk)
    grown = replace(disk.header, page_count=4, free_list_head=0)
    log.wal.append(RecordType.HEADER, 1, grown.pack())
    log.page(1, 2, 0xAA)
    log.page(1, 3, 0xBB)
    log.commit(1)
    log.done()

    recover(disk, tmp_path / "db.mvrs.wal")
    disk.flush_header()

    assert disk.page_count == 4
    assert [fill_of(disk, p) for p in (2, 3)] == [0xAA, 0xBB]
    disk.verify_length()
    disk.close()


def test_uncommitted_header_is_ignored(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs", pages=1)
    log = Log(tmp_path / "db.mvrs.wal", disk)
    log.wal.append(RecordType.HEADER, 1, replace(disk.header, page_count=9).pack())
    log.done()

    recover(disk, tmp_path / "db.mvrs.wal")

    assert disk.page_count == 2
    disk.close()


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    disk = make_db(path)
    log = Log(tmp_path / "db.mvrs.wal", disk)
    log.page(1, 1, 0xAA)
    log.page(1, 3, 0xCC)
    log.commit(1)
    log.done()

    first = recover(disk, tmp_path / "db.mvrs.wal")
    disk.sync()
    after_first = path.read_bytes()
    second = recover(disk, tmp_path / "db.mvrs.wal")
    disk.sync()

    assert path.read_bytes() == after_first
    assert first.pages_restored == 2
    assert (second.pages_restored, second.pages_already_current) == (0, 2)
    disk.close()


def test_log_from_another_database_is_refused(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs")
    other = make_db(tmp_path / "other.mvrs")
    log = Log(tmp_path / "db.mvrs.wal", other)
    log.page(1, 1, 0xAA)
    log.commit(1)
    log.done()

    with pytest.raises(WalMismatchError):
        recover(disk, tmp_path / "db.mvrs.wal")

    assert fill_of(disk, 1) == 0x00
    disk.close()
    other.close()


def test_image_for_a_page_outside_the_file_is_refused(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs")
    log = Log(tmp_path / "db.mvrs.wal", disk)
    log.page(1, 50, 0xAA)
    log.commit(1)
    log.done()

    with pytest.raises(CorruptWalError):
        recover(disk, tmp_path / "db.mvrs.wal")
    disk.close()


def test_image_with_the_wrong_size_is_refused(tmp_path: Path) -> None:
    disk = make_db(tmp_path / "db.mvrs")
    log = Log(tmp_path / "db.mvrs.wal", disk)
    log.wal.append(RecordType.PAGE_IMAGE, 1, struct.pack("<I", 1) + b"short")
    log.commit(1)
    log.done()

    with pytest.raises(CorruptWalError):
        recover(disk, tmp_path / "db.mvrs.wal")
    disk.close()
