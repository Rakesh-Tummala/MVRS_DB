"""Slotted heap page: variable-length records inside one page.

    0        24        36                                      P
    +--------+---------+---------+.........+------------------+
    | common | heap    | slots → |  free   | ← record bytes   |
    | header | header  |         |         |                  |
    +--------+---------+---------+.........+------------------+
                                  ^free_end

Heap header at offset 24 (format <HHHHI):
    slot_count   slots in the directory, including dead ones
    free_end     offset of the lowest record byte (page size when empty)
    frag_bytes   bytes inside [free_end, P) that no live record uses
    reserved
    next_page    next heap page of the same table, 0 = last

Slot i at offset 36 + 4i (format <HH): offset, length. Offset 0 marks a dead slot.

The key property (invariant H5): a slot number never changes while its record is
alive. Compaction moves record bytes and rewrites slot offsets, but a record ID
(page, slot) that an index holds stays valid.

These are plain functions over a page buffer. They know nothing about files,
tables, or the buffer pool.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

from mvrs_db.errors import CorruptPageError, InvalidRecordIdError, InvalidValueError
from mvrs_db.storage.page import PAGE_HEADER_SIZE

_HEAP_HEADER = struct.Struct("<HHHHI")
_SLOT = struct.Struct("<HH")
_NEXT = struct.Struct("<I")

HEAP_HEADER_OFFSET = PAGE_HEADER_SIZE  # 24
SLOTS_START = HEAP_HEADER_OFFSET + _HEAP_HEADER.size  # 36
SLOT_SIZE = _SLOT.size  # 4
_NEXT_OFFSET = HEAP_HEADER_OFFSET + 8  # 32


def max_record_size(page_size: int) -> int:
    """The largest record a page can hold: everything but the headers and one slot."""
    return page_size - SLOTS_START - SLOT_SIZE


def init_heap_page(buf: bytearray) -> None:
    """Format the body of a page whose common header is already written."""
    _HEAP_HEADER.pack_into(buf, HEAP_HEADER_OFFSET, 0, len(buf), 0, 0, 0)


def _header(buf: bytes | bytearray) -> tuple[int, int, int]:
    slot_count, free_end, frag, _reserved, _next = _HEAP_HEADER.unpack_from(buf, HEAP_HEADER_OFFSET)
    return slot_count, free_end, frag


def _set_header(buf: bytearray, slot_count: int, free_end: int, frag: int) -> None:
    struct.pack_into("<HHH", buf, HEAP_HEADER_OFFSET, slot_count, free_end, frag)


def slot_count(buf: bytes | bytearray) -> int:
    return _header(buf)[0]


def next_page(buf: bytes | bytearray) -> int:
    value: int = _NEXT.unpack_from(buf, _NEXT_OFFSET)[0]
    return value


def set_next_page(buf: bytearray, page_id: int) -> None:
    _NEXT.pack_into(buf, _NEXT_OFFSET, page_id)


def free_space(buf: bytes | bytearray) -> int:
    """Bytes available for new data after compaction, including any new slot entry."""
    count, free_end, frag = _header(buf)
    return free_end - (SLOTS_START + SLOT_SIZE * count) + frag


def _compact(buf: bytearray) -> None:
    """Slide every live record to the end of the page. Slot numbers do not change."""
    count, _free_end, _frag = _header(buf)
    live = []
    for slot in range(count):
        offset, length = _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * slot)
        if offset:
            live.append((slot, bytes(buf[offset : offset + length])))
    position = len(buf)
    for slot, record in live:
        position -= len(record)
        buf[position : position + len(record)] = record
        _SLOT.pack_into(buf, SLOTS_START + SLOT_SIZE * slot, position, len(record))
    slots_end = SLOTS_START + SLOT_SIZE * count
    buf[slots_end:position] = bytes(position - slots_end)
    _set_header(buf, count, position, 0)


def insert(buf: bytearray, record: bytes) -> int | None:
    """Store a record and return its slot, or None if the page has no room."""
    if not record:
        raise InvalidValueError("a record must be at least one byte")
    count, free_end, frag = _header(buf)
    slot = count
    for i in range(count):
        if _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * i)[0] == 0:
            slot = i
            break
    new_count = count + 1 if slot == count else count
    slots_end = SLOTS_START + SLOT_SIZE * new_count
    size = len(record)
    if size > free_end - slots_end:
        if size > free_end - slots_end + frag:
            return None
        _compact(buf)
        _count, free_end, frag = _header(buf)
    offset = free_end - size
    buf[offset:free_end] = record
    _SLOT.pack_into(buf, SLOTS_START + SLOT_SIZE * slot, offset, size)
    _set_header(buf, new_count, offset, frag)
    return slot


def _live_slot(buf: bytes | bytearray, slot: int) -> tuple[int, int]:
    count = _header(buf)[0]
    if not 0 <= slot < count:
        raise InvalidRecordIdError(f"slot {slot} is out of range (page has {count} slots)")
    offset, length = _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * slot)
    if offset == 0:
        raise InvalidRecordIdError(f"slot {slot} is empty")
    return offset, length


def read(buf: bytes | bytearray, slot: int) -> bytes:
    """Return the record in `slot`. Raises InvalidRecordIdError if it is not live."""
    offset, length = _live_slot(buf, slot)
    return bytes(buf[offset : offset + length])


def delete(buf: bytearray, slot: int) -> None:
    """Remove the record in `slot`. The slot number becomes reusable."""
    _offset, length = _live_slot(buf, slot)
    count, free_end, frag = _header(buf)
    _SLOT.pack_into(buf, SLOTS_START + SLOT_SIZE * slot, 0, 0)
    frag += length
    # Trailing dead slots can be dropped: no record ID refers to them.
    while count and _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * (count - 1))[0] == 0:
        count -= 1
    if count == 0:
        free_end, frag = len(buf), 0
    _set_header(buf, count, free_end, frag)


def update(buf: bytearray, slot: int, record: bytes) -> bool:
    """Replace the record in `slot`, keeping its slot number.

    Returns False, leaving the page unchanged, if the new record cannot fit.
    """
    if not record:
        raise InvalidValueError("a record must be at least one byte")
    offset, old_size = _live_slot(buf, slot)
    count, free_end, frag = _header(buf)
    size = len(record)
    slot_offset = SLOTS_START + SLOT_SIZE * slot
    if size <= old_size:
        buf[offset : offset + size] = record
        _SLOT.pack_into(buf, slot_offset, offset, size)
        _set_header(buf, count, free_end, frag + old_size - size)
        return True
    contiguous = free_end - (SLOTS_START + SLOT_SIZE * count)
    if size > contiguous + frag + old_size:
        return False
    # Give up the old bytes, then place the record like a fresh insert.
    _SLOT.pack_into(buf, slot_offset, 0, 0)
    frag += old_size
    _set_header(buf, count, free_end, frag)
    if size > contiguous:
        _compact(buf)
        count, free_end, frag = _header(buf)
    new_offset = free_end - size
    buf[new_offset:free_end] = record
    _SLOT.pack_into(buf, slot_offset, new_offset, size)
    _set_header(buf, count, new_offset, frag)
    return True


def live_records(buf: bytes | bytearray) -> Iterator[tuple[int, bytes]]:
    """Yield (slot, record) for every live record, in slot order."""
    count = _header(buf)[0]
    for slot in range(count):
        offset, length = _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * slot)
        if offset:
            yield slot, bytes(buf[offset : offset + length])


def live_count(buf: bytes | bytearray) -> int:
    count = _header(buf)[0]
    return sum(
        1 for slot in range(count) if _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * slot)[0] != 0
    )


def validate(buf: bytes | bytearray) -> None:
    """Check invariants H1 to H4. Raises CorruptPageError naming the first violation."""
    size = len(buf)
    count, free_end, frag = _header(buf)
    slots_end = SLOTS_START + SLOT_SIZE * count
    if not slots_end <= free_end <= size:
        raise CorruptPageError(
            f"H1: free_end {free_end} is outside [{slots_end}, {size}] with {count} slots"
        )
    extents = []
    for slot in range(count):
        offset, length = _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * slot)
        if offset == 0:
            if length != 0:
                raise CorruptPageError(f"dead slot {slot} has a non-zero length")
            continue
        if length == 0 or offset < free_end or offset + length > size:
            raise CorruptPageError(
                f"H2: slot {slot} covers [{offset}, {offset + length}) outside [{free_end}, {size})"
            )
        extents.append((offset, offset + length, slot))
    extents.sort()
    used = 0
    previous_end = free_end
    for start, end, slot in extents:
        if start < previous_end:
            raise CorruptPageError(f"H3: slot {slot} overlaps another record")
        used += end - start
        previous_end = end
    if frag != size - free_end - used:
        raise CorruptPageError(
            f"H4: frag_bytes is {frag} but {size - free_end - used} bytes are unused"
        )
    if count and _SLOT.unpack_from(buf, SLOTS_START + SLOT_SIZE * (count - 1))[0] == 0:
        raise CorruptPageError("the last slot in the directory is dead")
