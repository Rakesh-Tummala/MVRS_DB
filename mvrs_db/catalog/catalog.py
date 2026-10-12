"""The catalog: which tables exist, and the storage objects behind each one.

Table definitions are records in a heap file whose first page is named by the
file header (catalog_root). On open the whole catalog is read into memory; after
that, lookups never touch the catalog pages.

Because the catalog lives in ordinary pages, creating and dropping tables is
logged and recovered exactly like row changes. After a rollback the in-memory
copy is simply reloaded from the pages.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace

from mvrs_db import config
from mvrs_db.catalog.ann_store import AnnStore
from mvrs_db.catalog.schema import Column, TableSchema
from mvrs_db.errors import (
    CatalogError,
    CorruptCatalogError,
    RecordTooLargeError,
    TableExistsError,
    TableNotFoundError,
)
from mvrs_db.index.btree import BTree
from mvrs_db.sql import ast
from mvrs_db.storage import heap_page
from mvrs_db.storage.buffer_pool import PagePool
from mvrs_db.storage.heap_file import HeapFile, RecordId
from mvrs_db.storage.record import ColumnType, Value, raw_vector
from mvrs_db.vector.hnsw import HnswIndex

# Rebuild an in-memory vector index once this share of its entries are deleted.
MAX_DEAD_FRACTION = 0.3


@dataclass(slots=True)
class Table:
    """One table: its definition plus live handles on its heap and indexes."""

    schema: TableSchema
    heap: HeapFile
    indexes: dict[int, BTree]  # column position -> index on that column
    catalog_rid: RecordId
    # Row count for the planner; None until first needed, then kept up to date.
    row_count: int | None = None
    _stats_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Approximate vector indexes that have been built, by column position. They
    # exist only in memory: built on first use, updated as rows change, and
    # discarded with this Table object when a rollback reloads the catalog.
    _ann: dict[int, HnswIndex] = field(default_factory=dict, repr=False)
    _ann_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # True while the running transaction has changed a built index. Such an
    # index cannot survive a rollback, because it describes rows that are gone.
    _ann_changed: bool = False
    # Where saved copies of the indexes live; None for tables with no store.
    _store: AnnStore | None = field(default=None, repr=False)

    def ann_index(self, position: int) -> HnswIndex:
        """The HNSW index on a vector column, building it from the table if needed.

        Building reads every row, so the first approximate query after opening
        the database (or after a rollback) pays for it. An index in which many
        entries have been deleted is rebuilt the same way.
        """
        with self._ann_lock:
            index = self._ann.get(position)
            if index is None or index.dead_fraction > MAX_DEAD_FRACTION:
                column = self.schema.columns[position]
                index = None
                # A saved copy is usable only between transactions: inside one
                # the heap may differ from what the copy was saved from.
                if self._store is not None and not self.heap.pool.in_transaction:
                    index = self._store.load(self.schema, self.heap, position)
                if index is not None and index.dead_fraction <= MAX_DEAD_FRACTION:
                    self._ann[position] = index
                    return index
                index = HnswIndex(column.dimension, column.ann)
                types = self.schema.column_types
                for rid, record in self.heap.scan():
                    raw = raw_vector(types, record, position, column.dimension)
                    if raw is not None:
                        index.add(rid, raw)
                self._ann[position] = index
                # Built while a write transaction is open, the index may hold
                # rows that a rollback will remove. Treat it as changed, so a
                # rollback discards it; a commit clears the mark.
                if self.heap.pool.in_transaction:
                    self._ann_changed = True
            return index

    def ann_built(self, position: int) -> bool:
        return position in self._ann

    def ann_saved(self, position: int) -> bool:
        """True if a usable saved copy of the index exists (nothing is loaded)."""
        if self._store is None or self.heap.pool.in_transaction:
            return False
        return self._store.path(self.schema.table_id, position).exists()

    def save_vector_indexes(self) -> int:
        """Write every built index that changed since it was saved. Returns how many.

        Call only between transactions, so the heap holds exactly the committed
        rows the in-memory index describes.
        """
        if self._store is None:
            return 0
        saved = 0
        with self._ann_lock:
            for position, index in self._ann.items():
                if index.dirty and not self._ann_changed:
                    self._store.save(self.schema, self.heap, position, index)
                    saved += 1
        return saved

    def vector_row_added(self, rid: RecordId, row: tuple[Value, ...]) -> None:
        """Keep built vector indexes in step with an inserted row."""
        for position, index in self._ann.items():
            vector = row[position]
            if vector is not None:
                index.add(rid, vector)  # type: ignore[arg-type]
                self._ann_changed = True

    def vector_row_removed(self, rid: RecordId) -> None:
        for index in self._ann.values():
            if index.remove(rid):
                self._ann_changed = True

    def ensure_stats(self) -> tuple[int, int]:
        """(rows, heap pages). Rows are counted once by walking the heap, then maintained."""
        with self._stats_lock:
            if self.row_count is None:
                self.row_count = self.heap.count()[0]
            return self.row_count, self.heap.page_count()

    def rows_changed(self, delta: int) -> None:
        with self._stats_lock:
            if self.row_count is not None:
                self.row_count += delta


class Catalog:
    def __init__(self, pool: PagePool, store: AnnStore | None = None) -> None:
        self._pool = pool
        self._store = store
        self._lock = threading.RLock()
        self._tables: dict[str, Table] = {}
        self._heap: HeapFile | None = None
        self._next_table_id = 1
        # Changes whenever the set of Table objects changes (a load, CREATE or
        # DROP). A query plan holds Table objects, so it is valid only for the
        # epoch it was built in.
        self.epoch = 0

    # ------------------------------------------------------------------ loading

    def bootstrap(self) -> None:
        """Create the catalog heap in a new database. Call inside a transaction."""
        first_page = HeapFile.create(self._pool)
        self._pool.update_header(catalog_root=first_page)

    def load(self) -> None:
        """(Re)read every table definition from the catalog pages."""
        with self._lock:
            root = self._pool.disk.header.catalog_root
            if root == 0:
                raise CorruptCatalogError("the database has no catalog")
            heap = HeapFile(self._pool, root)
            tables: dict[str, Table] = {}
            next_id = 1
            for rid, record in heap.scan():
                schema = TableSchema.decode(record)
                if schema.name in tables:
                    raise CorruptCatalogError(f"C1: table {schema.name!r} is defined twice")
                table = self._open_table(schema, rid)
                # A reload happens after a rollback. A built vector index is
                # expensive to rebuild, so keep it when the rolled-back
                # transaction did not touch it and the table is unchanged.
                previous = self._tables.get(schema.name)
                if previous is not None and previous.schema == schema and not previous._ann_changed:
                    table._ann = previous._ann
                tables[schema.name] = table
                next_id = max(next_id, schema.table_id + 1)
            self._heap = heap
            self._tables = tables
            self._next_table_id = next_id
            self.epoch += 1

    def view(self, pool: PagePool) -> Catalog:
        """The catalog as `pool` sees it (a SnapshotPool gives the catalog at that commit).

        Row and page counts are copied from this catalog when known. They only
        steer the planner's estimates, so a count that includes an uncommitted
        change is harmless, and copying avoids reading every page of a table
        to count it for each new snapshot.
        """
        view = Catalog(pool)
        view.load()
        with self._lock:
            live = dict(self._tables)
        for name, table in view._tables.items():
            current = live.get(name)
            if current is None or current.schema.table_id != table.schema.table_id:
                continue
            table.row_count = current.row_count
            pages = current.heap.cached_page_count()
            if pages is not None:
                table.heap.hint_page_count(pages)
        return view

    def save_vector_indexes(self) -> int:
        with self._lock:
            return sum(table.save_vector_indexes() for table in self._tables.values())

    def transaction_committed(self) -> None:
        """The changes made to in-memory vector indexes are now permanent."""
        with self._lock:
            for table in self._tables.values():
                table._ann_changed = False

    def _open_table(self, schema: TableSchema, rid: RecordId) -> Table:
        indexes = {
            position: BTree(self._pool, column.index_root)
            for position, column in enumerate(schema.columns)
            if column.index_root
        }
        return Table(
            schema,
            HeapFile(self._pool, schema.first_heap_page),
            indexes,
            rid,
            _store=self._store,
        )

    # ------------------------------------------------------------------ lookup

    def get(self, name: str) -> Table:
        with self._lock:
            table = self._tables.get(name)
        if table is None:
            raise TableNotFoundError(f"no such table: {name}")
        return table

    def table_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def tables(self) -> list[Table]:
        with self._lock:
            return [self._tables[name] for name in sorted(self._tables)]

    @property
    def heap(self) -> HeapFile:
        if self._heap is None:
            raise CorruptCatalogError("the catalog has not been loaded")
        return self._heap

    # ------------------------------------------------------------------ DDL

    def create_table(self, statement: ast.CreateTable) -> Table:
        """Validate a CREATE TABLE and build the table, its heap and its indexes."""
        with self._lock:
            if statement.table in self._tables:
                raise TableExistsError(f"table {statement.table} already exists")
            definitions = statement.columns
            if len(definitions) > config.MAX_COLUMNS:
                raise CatalogError(f"a table can have at most {config.MAX_COLUMNS} columns")
            names = [definition.name for definition in definitions]
            if len(set(names)) != len(names):
                duplicate = next(name for name in names if names.count(name) > 1)
                raise CatalogError(f"column {duplicate} is defined more than once")
            primary_keys = [definition for definition in definitions if definition.primary_key]
            if len(primary_keys) != 1:
                raise CatalogError(
                    f"a table needs exactly one PRIMARY KEY column; "
                    f"{statement.table} has {len(primary_keys)}"
                )

            for definition in definitions:
                if definition.type_name != "VECTOR":
                    if definition.hnsw:
                        raise CatalogError(
                            f"column {definition.name}: HNSW can only index a VECTOR column"
                        )
                    continue
                if definition.primary_key or definition.unique:
                    raise CatalogError(
                        f"column {definition.name}: a VECTOR column cannot be PRIMARY KEY or UNIQUE"
                    )
                if not 1 <= definition.dimension <= config.MAX_VECTOR_DIMENSION:
                    raise CatalogError(
                        f"column {definition.name}: VECTOR needs between 1 and "
                        f"{config.MAX_VECTOR_DIMENSION} dimensions, not {definition.dimension}"
                    )

            # Check the definition fits in a catalog record before allocating anything.
            draft = TableSchema(
                0,
                statement.table,
                0,
                tuple(
                    Column(
                        d.name,
                        ColumnType[d.type_name],
                        d.not_null,
                        d.primary_key,
                        d.unique,
                        dimension=d.dimension,
                        ann=d.hnsw,
                    )
                    for d in definitions
                ),
            )
            if len(draft.encode()) > heap_page.max_record_size(self._pool.page_size):
                raise RecordTooLargeError(
                    f"the definition of table {statement.table} does not fit in one page"
                )

            first_heap_page = HeapFile.create(self._pool)
            columns = tuple(
                replace(
                    column,
                    not_null=column.not_null or column.primary_key,
                    index_root=(
                        BTree.create(self._pool) if column.primary_key or column.unique else 0
                    ),
                )
                for column in draft.columns
            )
            schema = TableSchema(self._next_table_id, statement.table, first_heap_page, columns)
            rid = self.heap.insert(schema.encode())
            table = self._open_table(schema, rid)
            table.row_count = 0
            if self._store is not None:
                self._store.discard(schema.table_id)
            self._tables[schema.name] = table
            self._next_table_id += 1
            self.epoch += 1
            return table

    def drop_table(self, name: str) -> None:
        """Free every page of the table and its indexes, and forget its definition."""
        with self._lock:
            table = self.get(name)
            for index in table.indexes.values():
                index.drop()
            table.heap.drop()
            self.heap.delete(table.catalog_rid)
            del self._tables[name]
            self.epoch += 1
            if self._store is not None:
                self._store.discard(table.schema.table_id)
