"""Buffer pool: a fixed number of pages kept in memory.

Every read and write of a page by the layers above goes through here. That makes
this the one place that decides when a changed page may be written back to disk,
which is what the recovery design depends on.

Pinning
    fetch() pins a page; unpin() releases it. A pinned page is never evicted
    (invariant B2), because the caller holds a reference to the frame's memory.

Dirty tracking
    fetch(write=True) marks the page dirty. A dirty page is written to disk
    before its frame is reused (B4).

Eviction
    Least recently used. `_frames` is an ordered dictionary whose first entry is
    the least recently used page; fetch() moves a page to the end.

Transactions (no-steal)
    Between begin() and commit()/rollback(), the first write to each page saves
    a copy of the page as it was (a before-image). Pages changed by the active
    transaction are never evicted or flushed (B7), so nothing uncommitted reaches
    the data file. rollback() copies the before-images back.

Page allocation
    Free pages form a linked list through the file header's free_list_head. A
    free page stores the next free page ID at offset 24.
"""

from __future__ import annotations

import struct
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from kestreldb import config
from kestreldb.errors import (
    BufferPoolFullError,
    CorruptFileError,
    CorruptPageError,
    InternalError,
    PagePinError,
    TransactionError,
    TransactionTooLargeError,
)
from kestreldb.storage.disk_manager import DiskManager, FileHeader
from kestreldb.storage.page import PAGE_HEADER_SIZE, PageType, new_page, page_type_of

_NEXT_FREE = struct.Struct("<I")
_NEXT_FREE_OFFSET = PAGE_HEADER_SIZE


@dataclass(slots=True)
class Frame:
    page_id: int
    data: bytearray
    pin_count: int = 0
    dirty: bool = False


@dataclass(slots=True)
class PoolStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    page_writes: int = 0


class BufferPool:
    def __init__(
        self,
        disk: DiskManager,
        capacity: int = config.DEFAULT_BUFFER_PAGES,
        *,
        before_page_write: Callable[[], None] | None = None,
    ) -> None:
        """`before_page_write` is called before any page is written to the data
        file. The database passes the log's sync function, which enforces the
        WAL rule (B6) without this layer knowing that a log exists."""
        if capacity < config.MIN_BUFFER_PAGES:
            raise ValueError(f"buffer pool needs at least {config.MIN_BUFFER_PAGES} frames")
        self._before_page_write = before_page_write
        self._disk = disk
        self._capacity = capacity
        self._frames: OrderedDict[int, Frame] = OrderedDict()
        self._latch = threading.RLock()
        # Active transaction: page ID -> (before-image or None for a new page, was dirty).
        self._txn: dict[int, tuple[bytes | None, bool]] | None = None
        self._txn_header: FileHeader | None = None
        self.stats = PoolStats()
        # Counts every change to a page or the header. Comparing it before and
        # after a statement tells whether the statement modified anything.
        self.change_count = 0

    # ------------------------------------------------------------------ basics

    @property
    def disk(self) -> DiskManager:
        return self._disk

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def page_size(self) -> int:
        return self._disk.page_size

    def __len__(self) -> int:
        return len(self._frames)

    def is_cached(self, page_id: int) -> bool:
        return page_id in self._frames

    def pin_count(self, page_id: int) -> int:
        frame = self._frames.get(page_id)
        return frame.pin_count if frame else 0

    def is_dirty(self, page_id: int) -> bool:
        frame = self._frames.get(page_id)
        return frame.dirty if frame else False

    def cached_page_ids(self) -> list[int]:
        """Cached pages from least to most recently used."""
        return list(self._frames)

    # ------------------------------------------------------------------ pin / unpin

    def fetch(self, page_id: int, *, write: bool = False) -> bytearray:
        """Return the page's buffer, pinned. Call unpin() when done.

        `write=True` declares that the caller will modify the page: it is marked
        dirty and, inside a transaction, its before-image is saved first.
        """
        with self._latch:
            frame = self._frames.get(page_id)
            if frame is None:
                # Read before making room, so a failed read changes nothing.
                data = self._disk.read_page(page_id)
                self._make_room()
                frame = Frame(page_id, data)
                self._frames[page_id] = frame
                self.stats.misses += 1
            else:
                self._frames.move_to_end(page_id)
                self.stats.hits += 1
            if write:
                self._mark_written(frame)
            frame.pin_count += 1
            return frame.data

    def unpin(self, page_id: int) -> None:
        with self._latch:
            frame = self._frames.get(page_id)
            if frame is None or frame.pin_count == 0:
                raise PagePinError(f"page {page_id} is not pinned")
            frame.pin_count -= 1

    @contextmanager
    def page(self, page_id: int, *, write: bool = False) -> Iterator[bytearray]:
        """Pin a page for the duration of a `with` block."""
        data = self.fetch(page_id, write=write)
        try:
            yield data
        finally:
            self.unpin(page_id)

    def _mark_written(self, frame: Frame) -> None:
        self.change_count += 1
        if self._txn is not None and frame.page_id not in self._txn:
            self._txn[frame.page_id] = (bytes(frame.data), frame.dirty)
        frame.dirty = True

    # ------------------------------------------------------------------ eviction

    def _make_room(self) -> None:
        """Ensure there is a free frame, evicting the least recently used page if needed."""
        if len(self._frames) < self._capacity:
            return
        held_by_txn = False
        for page_id, frame in self._frames.items():
            if frame.pin_count:
                continue
            if self._txn is not None and page_id in self._txn:
                held_by_txn = True
                continue
            if frame.dirty:
                # If this write fails the frame stays in the pool, still dirty.
                self._write(page_id, frame)
            del self._frames[page_id]
            self.stats.evictions += 1
            return
        if held_by_txn:
            raise TransactionTooLargeError(
                f"the transaction has changed more pages than the buffer pool's "
                f"{self._capacity} frames can hold"
            )
        raise BufferPoolFullError(f"all {self._capacity} buffer pool frames are pinned")

    # ------------------------------------------------------------------ flushing

    def _write(self, page_id: int, frame: Frame) -> None:
        if self._before_page_write is not None:
            self._before_page_write()
        self._disk.write_page(page_id, frame.data)
        self.stats.page_writes += 1
        frame.dirty = False

    def flush_page(self, page_id: int) -> None:
        with self._latch:
            frame = self._frames.get(page_id)
            if frame is None or not frame.dirty:
                return
            if self._txn is not None and page_id in self._txn:
                raise TransactionError(f"page {page_id} belongs to the active transaction")
            self._write(page_id, frame)

    def flush_all(self) -> None:
        """Write every dirty page and the file header. Does not fsync."""
        with self._latch:
            if self._txn is not None:
                raise TransactionError("cannot flush everything during a transaction")
            for page_id in sorted(self._frames):
                frame = self._frames[page_id]
                if frame.dirty:
                    self._write(page_id, frame)
            if self._before_page_write is not None:
                self._before_page_write()
            self._disk.flush_header()

    def dirty_page_count(self) -> int:
        return sum(1 for frame in self._frames.values() if frame.dirty)

    # ------------------------------------------------------------------ allocation

    def allocate(self, page_type: PageType) -> int:
        """Return the ID of a zeroed page of `page_type`. The page is not pinned."""
        with self._latch:
            header = self._disk.header
            page_id = header.free_list_head
            if page_id:
                with self.page(page_id, write=True) as buf:
                    if page_type_of(buf) != PageType.FREE:
                        raise CorruptPageError(
                            f"page {page_id} is on the free list but is not a free page"
                        )
                    next_free = _NEXT_FREE.unpack_from(buf, _NEXT_FREE_OFFSET)[0]
                    buf[:] = new_page(self.page_size, page_id, page_type)
                self._disk.set_header(replace(self._disk.header, free_list_head=next_free))
                return int(page_id)
            self._make_room()
            page_id = self._disk.allocate()
            self.change_count += 1
            self._frames[page_id] = Frame(
                page_id, new_page(self.page_size, page_id, page_type), dirty=True
            )
            if self._txn is not None:
                self._txn[page_id] = (None, False)
            return page_id

    def free(self, page_id: int) -> None:
        """Put a page on the free list."""
        with self._latch:
            with self.page(page_id, write=True) as buf:
                if page_type_of(buf) == PageType.FREE:
                    raise InternalError(f"page {page_id} was freed twice")
                head = self._disk.header.free_list_head
                buf[:] = new_page(self.page_size, page_id, PageType.FREE)
                _NEXT_FREE.pack_into(buf, _NEXT_FREE_OFFSET, head)
            self._disk.set_header(replace(self._disk.header, free_list_head=page_id))

    def free_list(self) -> list[int]:
        """Every page on the free list, checking invariant A1 on the way."""
        with self._latch:
            pages: list[int] = []
            seen: set[int] = set()
            page_id = self._disk.header.free_list_head
            while page_id:
                if page_id in seen:
                    raise CorruptFileError(f"A1: the free list loops back to page {page_id}")
                seen.add(page_id)
                with self.page(page_id) as buf:
                    if page_type_of(buf) != PageType.FREE:
                        raise CorruptFileError(f"A1: page {page_id} on the free list is not free")
                    next_free = _NEXT_FREE.unpack_from(buf, _NEXT_FREE_OFFSET)[0]
                pages.append(page_id)
                page_id = next_free
            return pages

    def update_header(self, **changes: int) -> None:
        """Change fields of the in-memory file header (rolled back with the transaction)."""
        with self._latch:
            self.change_count += 1
            self._disk.set_header(replace(self._disk.header, **changes))

    # ------------------------------------------------------------------ transactions

    @property
    def in_transaction(self) -> bool:
        return self._txn is not None

    def begin(self) -> None:
        with self._latch:
            if self._txn is not None:
                raise TransactionError("a transaction is already active")
            self._txn = {}
            self._txn_header = self._disk.header

    def transaction_pages(self) -> list[tuple[int, bytearray]]:
        """The pages changed by the active transaction, in page order."""
        with self._latch:
            if self._txn is None:
                raise TransactionError("no transaction is active")
            return [(page_id, self._frames[page_id].data) for page_id in sorted(self._txn)]

    def header_changed(self) -> bool:
        return self._txn is not None and self._disk.header != self._txn_header

    def commit(self) -> None:
        """End the transaction, keeping its changes. The pages stay dirty until flushed."""
        with self._latch:
            if self._txn is None:
                raise TransactionError("no transaction is active")
            self._txn = None
            self._txn_header = None

    def rollback(self) -> None:
        """End the transaction, restoring every page and the header to how they were."""
        with self._latch:
            if self._txn is None or self._txn_header is None:
                raise TransactionError("no transaction is active")
            for page_id, (before, was_dirty) in self._txn.items():
                frame = self._frames[page_id]
                if before is None:
                    if frame.pin_count:
                        raise InternalError(f"page {page_id} is still pinned during rollback")
                    del self._frames[page_id]
                else:
                    frame.data[:] = before
                    frame.dirty = was_dirty
            self._disk.set_header(self._txn_header)
            self._txn = None
            self._txn_header = None
