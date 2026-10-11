"""Crash recovery: bring the data file up to date with the log.

For each committed transaction the log holds one record per page it changed,
followed by a COMMIT record. A page record is either

    PAGE_IMAGE   the whole page after the change, or
    PAGE_DELTA   only the bytes that changed, plus the LSN the page had before.

Recovery:

1. Scan    Read the log up to its valid end. A transaction is committed exactly
           when its COMMIT record lies inside that valid part (invariant W2).
2. Redo    For each page, rebuild its newest committed state and write it once:
             * If the page on disk is intact and already at least as new as the
               last record for it, leave it alone.
             * Otherwise start from the newest full image in the log that is
               newer than the page on disk, or, if there is none, from the page
               on disk. Then apply the later deltas in order.
3. Finish  The caller syncs the data file and then empties the log.

A delta is applied only when the page's LSN equals the delta's prev_lsn, which
is the state the delta was computed from. A mismatch means the log and the page
have come apart; recovery raises instead of inventing a page.

Why this is safe to repeat (W4): each page is written once, in its final state,
and the log is untouched until the data file is synced. Crashing anywhere and
running recovery again finds those pages already current and skips them.

Why torn pages are repaired: a page whose checksum fails cannot be a starting
point, so recovery needs a full image for it. One always exists, because the
first change to a page after a checkpoint is logged as a full image, and a page
is only ever written to disk after such a change.

Records of uncommitted transactions are never applied. Nothing needs undoing,
because the buffer pool never lets an uncommitted page reach the data file
(no-steal).
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from mvrs_db import faults
from mvrs_db.errors import CorruptionError, CorruptPageError, CorruptWalError, WalMismatchError
from mvrs_db.recovery.delta import apply_delta, delta_header
from mvrs_db.recovery.wal import RecordType, WalContents, WalRecord, read_wal
from mvrs_db.storage.disk_manager import DiskManager, FileHeader
from mvrs_db.storage.page import page_lsn, set_page_lsn

_PAGE_ID = struct.Struct("<I")


@dataclass(frozen=True, slots=True)
class RecoveryStats:
    records_scanned: int = 0
    committed_transactions: int = 0
    uncommitted_transactions: int = 0
    pages_restored: int = 0
    pages_already_current: int = 0
    discarded_tail_bytes: int = 0
    last_lsn: int = 0
    deltas_applied: int = 0


def recover(disk: DiskManager, wal_path: str | os.PathLike[str]) -> RecoveryStats:
    """Redo committed changes from the log into the data file.

    Does not sync the data file and does not modify the log; the caller does
    both, in that order.
    """
    contents = read_wal(wal_path)
    if contents is None:
        return RecoveryStats(last_lsn=disk.header.lsn)
    _check_belongs(disk, contents)

    records = contents.records
    committed = {r.txn_id for r in records if r.type is RecordType.COMMIT}
    all_transactions = {r.txn_id for r in records}

    newest_header: bytes | None = None
    by_page: dict[int, list[WalRecord]] = {}
    for record in records:
        if record.txn_id not in committed:
            continue
        if record.type is RecordType.HEADER:
            newest_header = record.payload
        elif record.type is RecordType.PAGE_IMAGE:
            if len(record.payload) != _PAGE_ID.size + disk.page_size:
                raise CorruptWalError(f"page image at LSN {record.lsn} has the wrong size")
            by_page.setdefault(_PAGE_ID.unpack_from(record.payload, 0)[0], []).append(record)
        elif record.type is RecordType.PAGE_DELTA:
            by_page.setdefault(delta_header(record.payload)[0], []).append(record)

    if newest_header is not None:
        logged = FileHeader.unpack(newest_header)
        if logged.page_size != disk.page_size or logged.db_id != disk.header.db_id:
            raise CorruptWalError("logged file header belongs to a different database")
        disk.set_header(logged)

    restored = current = deltas = 0
    physical_pages = disk.physical_page_count()
    for page_id in sorted(by_page):
        page_records = by_page[page_id]
        if not 1 <= page_id < disk.page_count:
            raise CorruptWalError(f"log refers to page {page_id}, which is outside the file")
        faults.crash_point("recovery.apply")
        on_disk = _read_if_intact(disk, page_id) if page_id < physical_pages else None
        disk_lsn = -1 if on_disk is None else page_lsn(on_disk)
        if disk_lsn >= page_records[-1].lsn:
            current += 1
            continue
        page, applied = _rebuild(page_id, page_records, on_disk, disk_lsn)
        disk.write_page(page_id, page)
        restored += 1
        deltas += applied

    last_lsn = max(disk.header.lsn, records[-1].lsn if records else contents.base_lsn - 1)
    return RecoveryStats(
        records_scanned=len(records),
        committed_transactions=len(committed),
        uncommitted_transactions=len(all_transactions - committed),
        pages_restored=restored,
        pages_already_current=current,
        discarded_tail_bytes=contents.file_bytes - contents.valid_bytes,
        last_lsn=last_lsn,
        deltas_applied=deltas,
    )


def _rebuild(
    page_id: int, records: list[WalRecord], on_disk: bytearray | None, disk_lsn: int
) -> tuple[bytearray, int]:
    """The newest committed state of one page, and how many deltas it took."""
    start = None
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if record.type is RecordType.PAGE_IMAGE and record.lsn > disk_lsn:
            start = index
            break
    if start is not None:
        page = bytearray(records[start].payload[_PAGE_ID.size :])
        lsn = records[start].lsn
        pending = records[start + 1 :]
    elif on_disk is not None:
        page, lsn = on_disk, disk_lsn
        pending = [record for record in records if record.lsn > disk_lsn]
    else:
        raise CorruptPageError(
            f"page {page_id} is damaged on disk and the log holds no full image of it; "
            f"it cannot be recovered"
        )
    for record in pending:
        _page, prev_lsn = delta_header(record.payload)
        if prev_lsn != lsn:
            raise CorruptWalError(
                f"page {page_id}: the change at LSN {record.lsn} follows LSN {prev_lsn}, "
                f"but the page is at LSN {lsn}"
            )
        apply_delta(page, record.payload)
        lsn = record.lsn
    set_page_lsn(page, lsn)
    return page, len(pending)


def _check_belongs(disk: DiskManager, contents: WalContents) -> None:
    if contents.db_id != disk.header.db_id or contents.page_size != disk.page_size:
        raise WalMismatchError(
            f"log file belongs to a different database "
            f"(log db_id {contents.db_id:#x}, data file db_id {disk.header.db_id:#x})"
        )


def _read_if_intact(disk: DiskManager, page_id: int) -> bytearray | None:
    """The page from disk, or None if it is damaged (torn)."""
    try:
        return disk.read_page(page_id)
    except CorruptionError:
        return None
