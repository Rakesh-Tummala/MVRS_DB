"""Page deltas: logging only the bytes of a page that changed.

A full page image costs a whole page (4 KiB by default) in the log however small
the change. Inserting one 50-byte row changes about 60 bytes of a heap page: the
record itself, a 4-byte slot entry, and a few header fields. A delta records
just those byte ranges.

Payload of a PAGE_DELTA log record:

    page_id    u32
    prev_lsn   u64   the LSN the page had before this change
    count      u16   number of ranges
    per range: offset u16, length u16, then `length` bytes

Why prev_lsn
    A delta only makes sense applied to the exact page state it was computed
    against. prev_lsn names that state. During recovery a delta is applied only
    to a page whose LSN equals prev_lsn; anything else means the log and the
    page have come apart, and recovery stops instead of producing a page that
    never existed.

What is not in a delta
    The first 16 bytes of a page: its checksum (recomputed whenever the page is
    written), its page ID (never changes), and its LSN (set from the log
    record's own LSN when the delta is applied).

When a full image is logged instead
    * The first change to a page since the last checkpoint. That image is what
      repairs the page if a later write of it is torn, so it cannot be skipped.
    * A brand-new page.
    * A change so large that the delta would be more than half a page.

Finding the changed ranges
    The two versions are compared a block at a time with a slice comparison,
    which runs in C. Unequal neighbouring blocks merge into one range, and each
    range is then trimmed byte by byte at both ends.
"""

from __future__ import annotations

import struct

from mvrs_db.errors import CorruptWalError

_HEAD = struct.Struct("<IQH")
_RANGE = struct.Struct("<HH")
_BLOCK = 32
# Bytes 0-15 hold the checksum, the page ID and the LSN; see the module docstring.
UNLOGGED_PREFIX = 16


def changed_ranges(before: bytes, after: bytes | bytearray) -> list[tuple[int, int]]:
    """(offset, length) of every run of bytes that differs, ignoring the first 16 bytes."""
    size = len(after)
    ranges: list[tuple[int, int]] = []
    start = -1
    for offset in range(UNLOGGED_PREFIX, size, _BLOCK):
        if before[offset : offset + _BLOCK] != after[offset : offset + _BLOCK]:
            if start < 0:
                start = offset
        elif start >= 0:
            ranges.append((start, offset))
            start = -1
    if start >= 0:
        ranges.append((start, size))

    trimmed = []
    for low, high in ranges:
        while before[low] == after[low]:
            low += 1
        while before[high - 1] == after[high - 1]:
            high -= 1
        trimmed.append((low, high - low))
    return trimmed


def encode_delta(
    page_id: int, prev_lsn: int, after: bytes | bytearray, ranges: list[tuple[int, int]]
) -> bytes:
    parts = [_HEAD.pack(page_id, prev_lsn, len(ranges))]
    for offset, length in ranges:
        parts.append(_RANGE.pack(offset, length))
        parts.append(bytes(after[offset : offset + length]))
    return b"".join(parts)


def delta_size(ranges: list[tuple[int, int]]) -> int:
    """Payload bytes a delta with these ranges would take."""
    return _HEAD.size + sum(_RANGE.size + length for _offset, length in ranges)


def delta_header(payload: bytes) -> tuple[int, int]:
    """(page_id, prev_lsn) of a delta payload."""
    if len(payload) < _HEAD.size:
        raise CorruptWalError("page delta record is too short")
    page_id, prev_lsn, _count = _HEAD.unpack_from(payload, 0)
    return page_id, prev_lsn


def apply_delta(page: bytearray, payload: bytes) -> None:
    """Write a delta's ranges into `page`. Every range is bounds-checked first."""
    _page_id, _prev_lsn, count = _HEAD.unpack_from(payload, 0)
    position = _HEAD.size
    size = len(page)
    end = len(payload)
    for _ in range(count):
        if position + _RANGE.size > end:
            raise CorruptWalError("page delta record is cut short")
        offset, length = _RANGE.unpack_from(payload, position)
        position += _RANGE.size
        if offset < UNLOGGED_PREFIX or offset + length > size or position + length > end:
            raise CorruptWalError("page delta record has a range outside the page")
        page[offset : offset + length] = payload[position : position + length]
        position += length
    if position != end:
        raise CorruptWalError("page delta record has unexpected trailing bytes")
