"""Where saved vector indexes live, and when a saved index may be used.

One file per (table, vector column), next to the database file:
`shop.mvrs.ann.<table_id>.<column>`. The file is a cache of something the table
can always recompute, so every failure here (missing, unreadable, corrupt, out
of date) means "build from the table instead", never an error for the user.

A snapshot is used only if all of these hold:
  * it belongs to this database file (db_id), table (id and first heap page),
    column, dimension and metric;
  * its stamp equals the heap's current stamp, i.e. no row has changed since it
    was written (HeapFile.stamp);
  * its checksum and its graph structure validate.

Saving writes a temporary file and renames it over the old one, so a crash
leaves either the old snapshot or the new one, never half of one.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from mvrs_db.catalog.schema import TableSchema
from mvrs_db.storage.heap_file import HeapFile, RecordId
from mvrs_db.vector import snapshot
from mvrs_db.vector.hnsw import HnswIndex

SUFFIX = ".ann"


class AnnStore:
    def __init__(self, database_path: Path, db_id: int) -> None:
        self._base = str(database_path) + SUFFIX
        self._db_id = db_id

    def path(self, table_id: int, column: int) -> Path:
        return Path(f"{self._base}.{table_id}.{column}")

    def _key(self, schema: TableSchema, heap: HeapFile, column: int) -> snapshot.SnapshotKey:
        definition = schema.columns[column]
        return snapshot.SnapshotKey(
            self._db_id,
            schema.table_id,
            schema.first_heap_page,
            column,
            definition.dimension,
            definition.ann,
            heap.stamp(),
        )

    def load(self, schema: TableSchema, heap: HeapFile, column: int) -> HnswIndex | None:
        """The saved index if it is present and still describes the table, else None."""
        path = self.path(schema.table_id, column)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            key, arrays = snapshot.decode(data)
            if key != self._key(schema, heap, column):
                return None
            index = HnswIndex.from_arrays(key.dimension, key.metric, arrays, RecordId)
        except (snapshot.SnapshotError, ValueError):
            return None
        return index

    def save(self, schema: TableSchema, heap: HeapFile, column: int, index: HnswIndex) -> None:
        path = self.path(schema.table_id, column)
        data = snapshot.encode(self._key(schema, heap, column), index.to_arrays())
        temporary = path.with_name(path.name + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        index.dirty = False

    def discard(self, table_id: int) -> None:
        """Delete every snapshot of a table (it was dropped or created anew)."""
        prefix = f"{Path(self._base).name}.{table_id}."
        directory = Path(self._base).parent
        for candidate in directory.glob(prefix + "*"):
            with contextlib.suppress(OSError):
                candidate.unlink()
