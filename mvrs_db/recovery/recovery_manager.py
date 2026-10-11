"""Crash recovery: bring the data file up to date with the log.

The log holds, for each committed transaction, the full after-image of every
page it changed (and of the file header if that changed), followed by a COMMIT
record. Recovery is therefore short:

1. Scan    Read the log up to its valid end. A transaction is committed exactly
           when its COMMIT record lies inside that valid part (invariant W2).
2. Redo    For each page, take the newest image belonging to a committed
           transaction. Write it unless the page on disk is intact and already
           at least as new (its LSN is >= the image's LSN).
3. Finish  The caller syncs the data file and then empties the log.

Why this is safe to repeat (W4): step 2 only moves pages forward to a state the
log fully describes, and the log is not touched until the data file is synced.
Crashing anywhere and running recovery again reaches the same result.

Why torn pages are repaired: a page whose checksum fails is treated as older
than any image, so it is overwritten whole. Every page that can have been
written since the last checkpoint has an image in the log, because a checkpoint
leaves all pages clean and every later change is logged at commit.

Images of uncommitted transactions are simply never applied. Nothing needs
undoing, because the buffer pool never lets an uncommitted page reach the data
file (no-steal).
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from mvrs_db import faults
from mvrs_db.errors import CorruptionError, CorruptWalError, WalMismatchError
from mvrs_db.recovery.wal import RecordType, WalContents, read_wal
from mvrs_db.storage.disk_manager import DiskManager, FileHeader
from mvrs_db.storage.page import page_lsn

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

    # Later records win, so one pass leaves the newest committed image per page.
    newest_header: bytes | None = None
    newest_image: dict[int, tuple[int, bytes]] = {}
    for record in records:
        if record.txn_id not in committed:
            continue
        if record.type is RecordType.HEADER:
            newest_header = record.payload
        elif record.type is RecordType.PAGE_IMAGE:
            if len(record.payload) != _PAGE_ID.size + disk.page_size:
                raise CorruptWalError(f"page image at LSN {record.lsn} has the wrong size")
            page_id = _PAGE_ID.unpack_from(record.payload, 0)[0]
            newest_image[page_id] = (record.lsn, record.payload[_PAGE_ID.size :])

    if newest_header is not None:
        logged = FileHeader.unpack(newest_header)
        if logged.page_size != disk.page_size or logged.db_id != disk.header.db_id:
            raise CorruptWalError("logged file header belongs to a different database")
        disk.set_header(logged)

    restored = current = 0
    physical_pages = disk.physical_page_count()
    for page_id in sorted(newest_image):
        lsn, image = newest_image[page_id]
        if not 1 <= page_id < disk.page_count:
            raise CorruptWalError(f"log refers to page {page_id}, which is outside the file")
        faults.crash_point("recovery.apply")
        if page_id < physical_pages and _on_disk_lsn(disk, page_id) >= lsn:
            current += 1
            continue
        disk.write_page(page_id, image)
        restored += 1

    last_lsn = max(disk.header.lsn, records[-1].lsn if records else contents.base_lsn - 1)
    return RecoveryStats(
        records_scanned=len(records),
        committed_transactions=len(committed),
        uncommitted_transactions=len(all_transactions - committed),
        pages_restored=restored,
        pages_already_current=current,
        discarded_tail_bytes=contents.file_bytes - contents.valid_bytes,
        last_lsn=last_lsn,
    )


def _check_belongs(disk: DiskManager, contents: WalContents) -> None:
    if contents.db_id != disk.header.db_id or contents.page_size != disk.page_size:
        raise WalMismatchError(
            f"log file belongs to a different database "
            f"(log db_id {contents.db_id:#x}, data file db_id {disk.header.db_id:#x})"
        )


def _on_disk_lsn(disk: DiskManager, page_id: int) -> int:
    """The LSN of the page on disk, or -1 if the page is damaged (torn)."""
    try:
        return page_lsn(disk.read_page(page_id))
    except CorruptionError:
        return -1
