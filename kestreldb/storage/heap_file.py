"""A table's rows: a chain of heap pages linked by next_page.

The first page of the chain never changes for the life of the table, so the
catalog can refer to a table by it.

To find a page with room for a new record without walking the whole chain, the
heap file keeps an in-memory map of free space per page, grouped into power-of-two
size classes. The map is never stored on disk: it is rebuilt by walking the chain
the first time the table is written after open, so it cannot be wrong after a
crash.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import NamedTuple

from kestreldb.errors import (
    CorruptFileError,
    InternalError,
    InvalidRecordIdError,
    RecordTooLargeError,
)
from kestreldb.storage import heap_page
from kestreldb.storage.buffer_pool import BufferPool
from kestreldb.storage.page import PageType, page_type_of

_SIZE_CLASSES = 17  # free space is below 2**16, so class indexes run 0..16


class RecordId(NamedTuple):
    page_id: int
    slot: int


class HeapFile:
    def __init__(self, pool: BufferPool, first_page: int) -> None:
        self._pool = pool
        self._first_page = first_page
        self._free: dict[int, int] | None = None
        self._classes: list[set[int]] = []
        self._last_page = first_page

    @staticmethod
    def create(pool: BufferPool) -> int:
        """Allocate an empty heap and return its first page ID."""
        page_id = pool.allocate(PageType.HEAP)
        with pool.page(page_id, write=True) as buf:
            heap_page.init_heap_page(buf)
        return page_id

    @property
    def first_page(self) -> int:
        return self._first_page

    def invalidate(self) -> None:
        """Forget the free-space map (after a rollback changed pages underneath it)."""
        self._free = None

    # ------------------------------------------------------------------ free-space map

    def _load(self) -> dict[int, int]:
        if self._free is None:
            free: dict[int, int] = {}
            self._classes = [set() for _ in range(_SIZE_CLASSES)]
            last = self._first_page
            for page_id in self.pages():
                with self._pool.page(page_id) as buf:
                    space = heap_page.free_space(buf)
                free[page_id] = space
                self._classes[space.bit_length()].add(page_id)
                last = page_id
            self._free = free
            self._last_page = last
        return self._free

    def _record_space(self, page_id: int, space: int) -> None:
        free = self._load()
        old = free.get(page_id)
        if old is not None:
            self._classes[old.bit_length()].discard(page_id)
        free[page_id] = space
        self._classes[space.bit_length()].add(page_id)

    def _find_page(self, need: int) -> int | None:
        """A page guaranteed to have at least `need` free bytes, or None.

        Class c holds pages with free space in [2**(c-1), 2**c). Every page in a
        class above need.bit_length() has room, so this is a constant-time lookup
        at the price of sometimes skipping a page that would just have fitted.
        """
        free = self._load()
        # A few pages of the borderline class are worth a look: without this, an
        # emptied page could never be reused by a record larger than half a page.
        for page_id in islice(self._classes[need.bit_length()], 8):
            if free[page_id] >= need:
                return page_id
        for size_class in range(need.bit_length() + 1, _SIZE_CLASSES):
            if self._classes[size_class]:
                return next(iter(self._classes[size_class]))
        return None

    # ------------------------------------------------------------------ pages

    def pages(self) -> list[int]:
        """Every page in the chain, in order (checks invariant H6)."""
        result: list[int] = []
        seen: set[int] = set()
        page_id = self._first_page
        while page_id:
            if page_id in seen:
                raise CorruptFileError(f"H6: heap chain loops back to page {page_id}")
            seen.add(page_id)
            with self._pool.page(page_id) as buf:
                if page_type_of(buf) != PageType.HEAP:
                    raise CorruptFileError(f"H6: page {page_id} in a heap chain is not a heap page")
                next_id = heap_page.next_page(buf)
            result.append(page_id)
            page_id = next_id
        return result

    def _heap_page(self, rid: RecordId, *, write: bool = False) -> bytearray:
        """Pin the page a record ID points at, checking that it is a heap page."""
        page_count = self._pool.disk.page_count
        if not 1 <= rid.page_id < page_count:
            raise InvalidRecordIdError(f"{rid}: page is out of range")
        buf = self._pool.fetch(rid.page_id, write=write)
        if page_type_of(buf) != PageType.HEAP:
            self._pool.unpin(rid.page_id)
            raise InvalidRecordIdError(f"{rid}: page is not a heap page")
        return buf

    # ------------------------------------------------------------------ records

    def insert(self, record: bytes) -> RecordId:
        limit = heap_page.max_record_size(self._pool.page_size)
        if len(record) > limit:
            raise RecordTooLargeError(
                f"record of {len(record)} bytes exceeds the {limit}-byte limit for this page size"
            )
        need = len(record) + heap_page.SLOT_SIZE
        page_id = self._find_page(need)
        if page_id is None:
            page_id = self._append_page()
        with self._pool.page(page_id, write=True) as buf:
            slot = heap_page.insert(buf, record)
            space = heap_page.free_space(buf)
        if slot is None:
            raise InternalError(f"free-space map said page {page_id} had room for {need} bytes")
        self._record_space(page_id, space)
        return RecordId(page_id, slot)

    def _append_page(self) -> int:
        self._load()
        page_id = self._pool.allocate(PageType.HEAP)
        with self._pool.page(page_id, write=True) as buf:
            heap_page.init_heap_page(buf)
            space = heap_page.free_space(buf)
        with self._pool.page(self._last_page, write=True) as buf:
            heap_page.set_next_page(buf, page_id)
        self._last_page = page_id
        self._record_space(page_id, space)
        return page_id

    def read(self, rid: RecordId) -> bytes:
        buf = self._heap_page(rid)
        try:
            return heap_page.read(buf, rid.slot)
        finally:
            self._pool.unpin(rid.page_id)

    def delete(self, rid: RecordId) -> None:
        buf = self._heap_page(rid, write=True)
        try:
            heap_page.delete(buf, rid.slot)
            space = heap_page.free_space(buf)
        finally:
            self._pool.unpin(rid.page_id)
        if self._free is not None:
            self._record_space(rid.page_id, space)

    def update(self, rid: RecordId, record: bytes) -> RecordId:
        """Replace a record. Returns its record ID, which changes only if it had to move."""
        limit = heap_page.max_record_size(self._pool.page_size)
        if len(record) > limit:
            raise RecordTooLargeError(
                f"record of {len(record)} bytes exceeds the {limit}-byte limit for this page size"
            )
        buf = self._heap_page(rid, write=True)
        try:
            fitted = heap_page.update(buf, rid.slot, record)
            space = heap_page.free_space(buf)
        finally:
            self._pool.unpin(rid.page_id)
        if fitted:
            if self._free is not None:
                self._record_space(rid.page_id, space)
            return rid
        new_rid = self.insert(record)
        self.delete(rid)
        return new_rid

    def scan(self) -> Iterator[tuple[RecordId, bytes]]:
        """Yield every live record. No page stays pinned between yields."""
        page_id = self._first_page
        seen: set[int] = set()
        while page_id:
            if page_id in seen:
                raise CorruptFileError(f"H6: heap chain loops back to page {page_id}")
            seen.add(page_id)
            with self._pool.page(page_id) as buf:
                if page_type_of(buf) != PageType.HEAP:
                    raise CorruptFileError(f"H6: page {page_id} in a heap chain is not a heap page")
                records = list(heap_page.live_records(buf))
                next_id = heap_page.next_page(buf)
            for slot, record in records:
                yield RecordId(page_id, slot), record
            page_id = next_id

    def count(self) -> tuple[int, int]:
        """(live records, pages) by walking the chain without decoding records."""
        rows = 0
        pages = self.pages()
        for page_id in pages:
            with self._pool.page(page_id) as buf:
                rows += heap_page.live_count(buf)
        return rows, len(pages)

    def validate(self) -> None:
        """Check the page-level invariants of every page in the chain."""
        for page_id in self.pages():
            with self._pool.page(page_id) as buf:
                heap_page.validate(buf)

    def drop(self) -> None:
        """Return every page of the heap to the free list."""
        for page_id in self.pages():
            self._pool.free(page_id)
        self._free = None
