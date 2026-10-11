"""B+ tree node pages: layout, encoding, and in-page search.

After the 24-byte common page header:

    offset  size  field
    24      2     key_count
    26      2     free_end     offset of the lowest cell byte
    28      4     link         leaf: right sibling (0 = none)
                               internal: leftmost child
    32      4     frag_bytes   bytes between free_end and the end of the page
                               that no live cell uses (holes left by deletes)
    36      2*n   cell pointers, in key order

Cells are written from the end of the page downward:

    leaf cell      [key_len u8][key][record page u32][record slot u16]
    internal cell  [key_len u8][key][child u32]

An internal node with keys k1..kn has n+1 children. The leftmost child (in the
header) holds keys < k1; the child stored in cell i holds keys >= ki and below
the next key.

Three ways a node changes:

* Inserting or deleting one key edits the page in place. The pointer array is
  shifted by one entry, one cell is written or forgotten, and the header is
  updated. Only those bytes change, which keeps the work small and the log
  record (a delta of the changed bytes) small. A deleted cell leaves a hole,
  counted in frag_bytes; a later insert that cannot find contiguous room
  repacks the cells first.
* A split decodes the node into Python lists and writes two nodes from scratch.
  Splits are rare and this keeps the logic short enough to check by eye.
* Searching reads only the keys a binary search touches.

Page sizes of up to 32 KiB mean offsets fit in 16 bits.
"""

from __future__ import annotations

import struct

from mvrs_db.errors import CorruptIndexError
from mvrs_db.storage.heap_file import RecordId
from mvrs_db.storage.page import PAGE_HEADER_SIZE

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


def locate_internal(buf: bytes | bytearray, key: bytes) -> tuple[int, int]:
    """(index, child): how many separators are <= `key`, and the child to descend into.

    `index` is also the cell position at which a new separator for a split of
    that child belongs.
    """
    count, _free_end, leftmost, _frag = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)
    low, high = 0, count
    while low < high:
        middle = (low + high) // 2
        if _key_at(buf, middle)[0] <= key:
            low = middle + 1
        else:
            high = middle
    if low == 0:
        return 0, int(leftmost)
    _key, end = _key_at(buf, low - 1)
    child: int = _CHILD.unpack_from(buf, end)[0]
    return low, child


def internal_child(buf: bytes | bytearray, key: bytes) -> int:
    """The child of an internal page whose subtree could contain `key`."""
    return locate_internal(buf, key)[1]


def locate_leaf(buf: bytes | bytearray, key: bytes) -> tuple[bool, int]:
    """(found, index): the position of `key` in a leaf, or where it would be inserted."""
    low, high = 0, _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)[0]
    while low < high:
        middle = (low + high) // 2
        candidate, _end = _key_at(buf, middle)
        if candidate < key:
            low = middle + 1
        elif candidate > key:
            high = middle
        else:
            return True, middle
    return False, low


def leftmost_child(buf: bytes | bytearray) -> int:
    """The first child of an internal page."""
    child: int = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)[2]
    return child


def rightmost_child(buf: bytes | bytearray) -> int:
    """The last child of an internal page."""
    count = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)[0]
    if count == 0:
        return leftmost_child(buf)
    _key, end = _key_at(buf, count - 1)
    child: int = _CHILD.unpack_from(buf, end)[0]
    return child


# ---------------------------------------------------------------------- in-place edits


def _insert_cell(
    buf: bytearray, index: int, cell: bytes, capacity: int, fits_regardless_below: int, leaf: bool
) -> bool:
    """Write `cell` at cell position `index`. False if the node has no room for it.

    `capacity` caps the bytes the node may use (the page size, except in tests).
    A node that would hold fewer than `fits_regardless_below` keys afterwards is
    accepted whatever the cap: a split needs at least that many to leave two
    usable halves.
    """
    size = len(buf)
    count, free_end, link, frag = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)
    need = len(cell) + POINTER_SIZE
    used = CELLS_START + POINTER_SIZE * count + (size - free_end - frag)
    if used + need > capacity and count + 1 >= fits_regardless_below:
        return False
    gap = free_end - (CELLS_START + POINTER_SIZE * count)
    if gap < need:
        if gap + frag < need:
            return False
        # Enough room in total, but split into holes: repack the cells.
        if leaf:
            keys, rids, link = decode_leaf(buf)
            encode_leaf(buf, keys, rids, link)
        else:
            keys, children = decode_internal(buf)
            encode_internal(buf, keys, children)
        count, free_end, link, frag = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)
    offset = free_end - len(cell)
    buf[offset:free_end] = cell
    start = CELLS_START + POINTER_SIZE * index
    end = CELLS_START + POINTER_SIZE * count
    buf[start + POINTER_SIZE : end + POINTER_SIZE] = buf[start:end]
    _POINTER.pack_into(buf, start, offset)
    _NODE_HEADER.pack_into(buf, NODE_HEADER_OFFSET, count + 1, offset, link, frag)
    return True


def leaf_insert(buf: bytearray, index: int, key: bytes, rid: RecordId, capacity: int) -> bool:
    """Insert (key, rid) at cell position `index` of a leaf, in place. False if full."""
    cell = bytes((len(key),)) + key + _RID.pack(rid.page_id, rid.slot)
    return _insert_cell(buf, index, cell, capacity, fits_regardless_below=2, leaf=True)


def internal_insert(buf: bytearray, index: int, key: bytes, child: int, capacity: int) -> bool:
    """Insert separator `key` and its right child at cell position `index`, in place."""
    cell = bytes((len(key),)) + key + _CHILD.pack(child)
    return _insert_cell(buf, index, cell, capacity, fits_regardless_below=3, leaf=False)


def leaf_delete(buf: bytearray, index: int) -> None:
    """Remove the entry at cell position `index` of a leaf, in place.

    The cell's bytes stay where they are as a hole, counted in frag_bytes. If it
    was the lowest cell the free area simply grows over it.
    """
    size = len(buf)
    count, free_end, link, frag = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)
    if not 0 <= index < count:
        raise CorruptIndexError(f"leaf has no cell {index} (it has {count})")
    pointer_at = CELLS_START + POINTER_SIZE * index
    offset = _POINTER.unpack_from(buf, pointer_at)[0]
    cell_size = 1 + buf[offset] + _RID.size
    end = CELLS_START + POINTER_SIZE * count
    buf[pointer_at : end - POINTER_SIZE] = buf[pointer_at + POINTER_SIZE : end]
    count -= 1
    if count == 0:
        free_end, frag = size, 0
    elif offset == free_end:
        free_end += cell_size
    else:
        frag += cell_size
    _NODE_HEADER.pack_into(buf, NODE_HEADER_OFFSET, count, free_end, link, frag)


def validate(buf: bytes | bytearray, *, leaf: bool) -> None:
    """Check a node page's own layout. Raises CorruptIndexError naming the first problem.

    N1  the header is consistent: pointers fit below the cells
    N2  every cell lies inside the cell area, and cells do not overlap
    N3  frag_bytes equals the cell area's bytes that no live cell uses
    """
    size = len(buf)
    count, free_end, _link, frag = _NODE_HEADER.unpack_from(buf, NODE_HEADER_OFFSET)
    pointers_end = CELLS_START + POINTER_SIZE * count
    if not pointers_end <= free_end <= size:
        raise CorruptIndexError(f"N1: {count} keys and free_end {free_end} do not fit a page")
    payload = _RID.size if leaf else _CHILD.size
    extents = []
    for number in range(count):
        offset = _POINTER.unpack_from(buf, CELLS_START + POINTER_SIZE * number)[0]
        if offset < free_end or offset >= size:
            raise CorruptIndexError(
                f"N2: cell {number} at offset {offset} is outside the cell area"
            )
        end = offset + 1 + buf[offset] + payload
        if end > size:
            raise CorruptIndexError(f"N2: cell {number} runs past the end of the page")
        extents.append((offset, end, number))
    extents.sort()
    used = 0
    previous_end = free_end
    for start, end, number in extents:
        if start < previous_end:
            raise CorruptIndexError(f"N2: cell {number} overlaps another cell")
        used += end - start
        previous_end = end
    if frag != size - free_end - used:
        unused = size - free_end - used
        raise CorruptIndexError(
            f"N3: frag_bytes is {frag} but {unused} bytes of the cell area are unused"
        )
