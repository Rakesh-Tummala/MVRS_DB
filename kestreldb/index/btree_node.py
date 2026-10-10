"""B+ tree node pages: layout, encoding, and in-page search.

After the 24-byte common page header:

    offset  size  field
    24      2     key_count
    26      2     free_end     offset of the lowest cell byte
    28      4     link         leaf: right sibling (0 = none)
                               internal: leftmost child
    32      4     reserved
    36      2*n   cell pointers, in key order

Cells are packed from the end of the page downward:

    leaf cell      [key_len u8][key][record page u32][record slot u16]
    internal cell  [key_len u8][key][child u32]

An internal node with keys k1..kn has n+1 children. The leftmost child (in the
header) holds keys < k1; the child stored in cell i holds keys >= ki and below
the next key.

Searching reads only the keys a binary search touches. Modifying decodes the
node into Python lists and rewrites the whole page, which keeps the split logic
easy to verify at the cost of some speed.
"""

from __future__ import annotations

import struct

from kestreldb.errors import CorruptIndexError
from kestreldb.storage.heap_file import RecordId
from kestreldb.storage.page import PAGE_HEADER_SIZE

_NODE_HEADER = struct.Struct("<HHII")
_POINTER = struct.Struct("<H")
_RID = struct.Struct("<IH")
_CHILD = struct.Struct("<I")

NODE_HEADER_OFFSET = PAGE_HEADER_SIZE  # 24
CELLS_START = NODE_HEADER_OFFSET + _NODE_HEADER.size  # 36
POINTER_SIZE = _POINTER.size  # 2

# Bytes a cell costs beyond its key: pointer + length byte + payload.
LEAF_CELL_OVERHEAD = POINTER_SIZE + 1 + _RID.size  # 9
INTERNAL_CELL_OVERHEAD = POINTER_SIZE + 1 + _CHILD.size  # 7


def leaf_size(keys: list[bytes]) -> int:
    """Bytes a leaf with these keys occupies, headers included."""
    return CELLS_START + sum(map(len, keys)) + LEAF_CELL_OVERHEAD * len(keys)


def internal_size(keys: list[bytes]) -> int:
    return CELLS_START + sum(map(len, keys)) + INTERNAL_CELL_OVERHEAD * len(keys)


def init_leaf(buf: bytearray) -> None:
    _NODE_HEADER.pack_into(buf, NODE_HEADER_OFFSET, 0, len(buf), 0, 0)


def encode_leaf(buf: bytearray, keys: list[bytes], rids: list[RecordId], link: int) -> None:
    position = len(buf)
    pointer = CELLS_START
    for key, rid in zip(keys, rids, strict=True):
        key_len = len(key)
        position -= 1 + key_len + _RID.size
        buf[position] = key_len
        buf[position + 1 : position + 1 + key_len] = key
        _RID.pack_into(buf, position + 1 + key_len, rid.page_id, rid.slot)
        _POINTER.pack_into(buf, pointer, position)
        pointer += POINTER_SIZE
    if pointer > position:
        raise CorruptIndexError("leaf contents do not fit in the page")
    buf[pointer:position] = bytes(position - pointer)
    _NODE_HEADER.pack_into(buf, NODE_HEADER_OFFSET, len(keys), position, link, 0)


def encode_internal(buf: bytearray, keys: list[bytes], children: list[int]) -> None:
    if len(children) != len(keys) + 1:
        raise CorruptIndexError("an internal node needs one more child than keys")
    position = len(buf)
    pointer = CELLS_START
    for key, child in zip(keys, children[1:], strict=True):
        key_len = len(key)
        position -= 1 + key_len + _CHILD.size
        buf[position] = key_len
        buf[position + 1 : position + 1 + key_len] = key
        _CHILD.pack_into(buf, position + 1 + key_len, child)
        _POINTER.pack_into(buf, pointer, position)
        pointer += POINTER_SIZE
    if pointer > position:
        raise CorruptIndexError("internal node contents do not fit in the page")
    buf[pointer:position] = bytes(position - pointer)
    _NODE_HEADER.pack_into(buf, NODE_HEADER_OFFSET, len(keys), position, children[0], 0)


def _cell_offsets(buf: bytes | bytearray, payload_size: int) -> tuple[list[int], int]:
    """Validated cell offsets and the link field."""
    size = len(buf)
    count, free_end, link, _reserved = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)
    pointers_end = CELLS_START + POINTER_SIZE * count
    if not pointers_end <= free_end <= size:
        raise CorruptIndexError(f"node header is inconsistent: {count} keys, free_end {free_end}")
    offsets = list(struct.unpack_from(f"<{count}H", buf, CELLS_START))
    for offset in offsets:
        if offset < free_end or offset + 1 + buf[offset] + payload_size > size:
            raise CorruptIndexError(f"cell at offset {offset} lies outside the cell area")
    return offsets, link


def decode_leaf(buf: bytes | bytearray) -> tuple[list[bytes], list[RecordId], int]:
    """Return (keys, record IDs, right sibling)."""
    offsets, link = _cell_offsets(buf, _RID.size)
    keys: list[bytes] = []
    rids: list[RecordId] = []
    for offset in offsets:
        end = offset + 1 + buf[offset]
        keys.append(bytes(buf[offset + 1 : end]))
        rids.append(RecordId(*_RID.unpack_from(buf, end)))
    return keys, rids, link


def decode_internal(buf: bytes | bytearray) -> tuple[list[bytes], list[int]]:
    """Return (keys, children) where children has one more entry than keys."""
    offsets, leftmost = _cell_offsets(buf, _CHILD.size)
    keys: list[bytes] = []
    children = [leftmost]
    for offset in offsets:
        end = offset + 1 + buf[offset]
        keys.append(bytes(buf[offset + 1 : end]))
        children.append(_CHILD.unpack_from(buf, end)[0])
    return keys, children


def _key_at(buf: bytes | bytearray, index: int) -> tuple[bytes, int]:
    """The key of cell `index` and the offset just past it."""
    offset = _POINTER.unpack_from(buf, CELLS_START + POINTER_SIZE * index)[0]
    end = offset + 1 + buf[offset]
    return bytes(buf[offset + 1 : end]), end


def leaf_find(buf: bytes | bytearray, key: bytes) -> RecordId | None:
    """Binary search a leaf page without decoding the whole node."""
    low, high = 0, _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)[0]
    while low < high:
        middle = (low + high) // 2
        candidate, end = _key_at(buf, middle)
        if candidate < key:
            low = middle + 1
        elif candidate > key:
            high = middle
        else:
            return RecordId(*_RID.unpack_from(buf, end))
    return None


def internal_child(buf: bytes | bytearray, key: bytes) -> int:
    """The child of an internal page whose subtree could contain `key`."""
    count, _free_end, leftmost, _reserved = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)
    # Find how many separators are <= key.
    low, high = 0, count
    while low < high:
        middle = (low + high) // 2
        if _key_at(buf, middle)[0] <= key:
            low = middle + 1
        else:
            high = middle
    if low == 0:
        return int(leftmost)
    _key, end = _key_at(buf, low - 1)
    child: int = _CHILD.unpack_from(buf, end)[0]
    return child
