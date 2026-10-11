"""Saving an HNSW index to a file, and reading it back without trusting it.

Why: building the index reads every row and inserts each into the graph, which
costs seconds for tens of thousands of vectors. A saved copy loads in a
fraction of that. The snapshot is only a cache: the table is the truth, and any
doubt about the snapshot means rebuilding from the table.

Layout (little-endian; every section's size follows from the header counts):

    0   8   magic            b"MVRSANN1"
    8   8   db_id            which database file this was built from
    16  4   table_id
    20  4   first_heap_page  identifies the table even if its id was reused
    24  2   column           position of the vector column
    26  4   dimension
    30  1   metric           0 = l2, 1 = cosine
    31  2   m
    33  2   ef_construction
    35  24  stamp            heap state it describes (3 x u64, see HeapFile.stamp)
    59  8   count            nodes (live and dead)
    67  8   degree_count     entries in the degree list
    75  8   neighbor_count   entries in the neighbour list
    83  8   entry            entry-point node, -1 if empty (i64)
    91  4   top_layer        i32
    95  ... vectors  count*dimension*4   float32
        ... pages    count*4             u32   record id: page
        ... slots    count*2             u16   record id: slot
        ... dead     count*1             u8
        ... layers   count*1             u8    layers per node
        ... degrees  degree_count*2      u16
        ... neighbors neighbor_count*4   u32
        4   crc32 of every byte before it

Trade-off: a flat array format needs no parsing loop and loads with
numpy.frombuffer; the cost is that the graph is rebuilt into Python lists on
load, which is the dominant load cost. The alternative, keeping the graph in
numpy arrays during search, would slow every search to speed up one load.

Validation is two-layered: this module checks the file's structure (magic,
length arithmetic, checksum), and HnswIndex.from_arrays checks the graph's
meaning (links in range, layers consistent, entry point valid).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Any

from mvrs_db.vector import distance

_np: Any = distance._np

MAGIC = b"MVRSANN1"
_HEADER = struct.Struct("<8sQIIHIBHH3QQQQqi")
_METRICS = ("l2", "cosine")
# Refuse files whose header claims more than this, before allocating anything.
_MAX_NODES = 1 << 28


class SnapshotError(Exception):
    """The snapshot is unusable. Callers fall back to rebuilding from the table."""


@dataclass(frozen=True, slots=True)
class SnapshotKey:
    """What a snapshot must match to be used: the table it was built from."""

    db_id: int
    table_id: int
    first_heap_page: int
    column: int
    dimension: int
    metric: str
    stamp: tuple[int, int, int]


def encode(key: SnapshotKey, arrays: dict[str, Any]) -> bytes:
    count = int(arrays["vectors"].shape[0])
    header = _HEADER.pack(
        MAGIC,
        key.db_id,
        key.table_id,
        key.first_heap_page,
        key.column,
        key.dimension,
        _METRICS.index(key.metric),
        int(arrays["m"]),
        int(arrays["ef_construction"]),
        *key.stamp,
        count,
        len(arrays["degrees"]),
        len(arrays["neighbors"]),
        int(arrays["entry"]),
        int(arrays["top_layer"]),
    )
    body = b"".join(
        arrays[name].tobytes()
        for name in ("vectors", "pages", "slots", "dead", "node_layers", "degrees", "neighbors")
    )
    payload = header + body
    return payload + struct.pack("<I", zlib.crc32(payload))


def decode(data: bytes) -> tuple[SnapshotKey, dict[str, Any]]:
    """Parse a snapshot. Raises SnapshotError for any structural problem."""
    if _np is None:
        raise SnapshotError("NumPy is not available")
    if len(data) < _HEADER.size + 4 or data[:8] != MAGIC:
        raise SnapshotError("not a snapshot")
    if zlib.crc32(data[:-4]) != struct.unpack_from("<I", data, len(data) - 4)[0]:
        raise SnapshotError("checksum mismatch")
    (
        _magic,
        db_id,
        table_id,
        first_page,
        column,
        dimension,
        metric,
        m,
        ef,
        s0,
        s1,
        s2,
        count,
        degree_count,
        neighbor_count,
        entry,
        top_layer,
    ) = _HEADER.unpack_from(data, 0)
    if metric >= len(_METRICS):
        raise SnapshotError("unknown metric")
    if count > _MAX_NODES or dimension == 0 or dimension > 65536:
        raise SnapshotError("implausible size")
    sizes = [
        ("vectors", count * dimension * 4, "<f4"),
        ("pages", count * 4, "<u4"),
        ("slots", count * 2, "<u2"),
        ("dead", count, "u1"),
        ("node_layers", count, "u1"),
        ("degrees", degree_count * 2, "<u2"),
        ("neighbors", neighbor_count * 4, "<u4"),
    ]
    if _HEADER.size + sum(size for _, size, _ in sizes) + 4 != len(data):
        raise SnapshotError("length does not match the header")
    arrays: dict[str, Any] = {}
    offset = _HEADER.size
    for name, size, dtype in sizes:
        array = _np.frombuffer(
            data, dtype=dtype, count=size // _np.dtype(dtype).itemsize, offset=offset
        )
        arrays[name] = array.reshape(count, dimension) if name == "vectors" else array
        offset += size
    arrays.update(
        entry=entry,
        top_layer=top_layer,
        m=m,
        ef_construction=ef,
    )
    key = SnapshotKey(
        db_id, table_id, first_page, column, dimension, _METRICS[metric], (s0, s1, s2)
    )
    return key, arrays
