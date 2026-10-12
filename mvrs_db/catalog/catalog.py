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
from mvrs_db.catalog.stats import STATS_RECORD_FORMAT, ColumnStats, collect, fit
from mvrs_db.errors import (
    CatalogError,
    CorruptCatalogError,
    InvalidValueError,
    KeyTooLargeError,
    RecordTooLargeError,
    TableExistsError,
    TableNotFoundError,
)
from mvrs_db.index.btree import BTree
from mvrs_db.index.keys import (
    RID_MAX,
    decode_key,
    decode_secondary_value,
    encode_key,
    secondary_key,
    secondary_prefix,
)
from mvrs_db.sql import ast
from mvrs_db.storage import heap_page
from mvrs_db.storage.buffer_pool import PagePool
from mvrs_db.storage.heap_file import HeapFile, RecordId
from mvrs_db.storage.record import ColumnType, Value, decode_record, raw_vector
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
    # What ANALYZE found out about each column, by column position. Empty until
    # ANALYZE has run; the planner then falls back to fixed guesses.
    stats: dict[int, ColumnStats] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ B+ tree indexes

    def is_unique(self, position: int) -> bool:
        """True if the column has a primary-key or UNIQUE index (one entry per value)."""
        return position in self.indexes and not self.schema.columns[position].secondary

    def index_key(self, position: int, value: Value, rid: RecordId) -> bytes:
        """The key under which this row's value is filed in the column's index.

        A unique index files the value alone; a secondary index appends the record
        ID so that equal values get different keys (see index/keys.py).
        """
        column_type = self.schema.columns[position].type
        if self.schema.columns[position].secondary:
            return secondary_key(column_type, value, rid.page_id, rid.slot)
        return encode_key(column_type, value)

    def index_value(self, position: int, key: bytes) -> Value:
        """The column value an index key holds (for MIN/MAX and statistics)."""
        column_type = self.schema.columns[position].type
        if self.schema.columns[position].secondary:
            return decode_secondary_value(column_type, key)
        return decode_key(column_type, key)

    def count_equal(self, position: int, value: Value, cap: int) -> int:
        """How many rows a secondary index holds for `value`, counting no further than `cap`."""
        column_type = self.schema.columns[position].type
        try:
            prefix = secondary_prefix(column_type, value)
        except (InvalidValueError, KeyTooLargeError):
            return 0
        found = 0
        for _key in self.indexes[position].scan(prefix, prefix + RID_MAX):
            found += 1
            if found >= cap:
                break
        return found

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
        # Where each column's statistics record lives in the catalog heap.
        self._stats_rids: dict[tuple[int, int], RecordId] = {}
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
            stats_found: dict[tuple[int, int], tuple[RecordId, ColumnStats]] = {}
            for rid, record in heap.scan():
                if record[0] == STATS_RECORD_FORMAT:
                    table_id, position, column_stats = ColumnStats.decode(record)
                    if (table_id, position) in stats_found:
                        raise CorruptCatalogError(
                            f"C5: column {position} of table {table_id} has two statistics records"
                        )
                    stats_found[(table_id, position)] = (rid, column_stats)
                    continue
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
            by_id = {table.schema.table_id: table for table in tables.values()}
            for (table_id, position), (_rid, column_stats) in stats_found.items():
                owner = by_id.get(table_id)
                if owner is None or position >= len(owner.schema.columns):
                    raise CorruptCatalogError(
                        f"C5: statistics for column {position} of table {table_id}, "
                        f"which does not exist"
                    )
                if owner.schema.columns[position].type is not column_stats.column_type:
                    column_name = owner.schema.columns[position].name
                    raise CorruptCatalogError(
                        f"C5: statistics for {owner.schema.name}.{column_name} "
                        f"have the wrong column type"
                    )
                owner.stats[position] = column_stats
            self._stats_rids = {key: rid for key, (rid, _stats) in stats_found.items()}
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

    # ------------------------------------------------------------------ indexes

    def index_owner(self, name: str) -> tuple[Table, int]:
        """The table and column position of the secondary index called `name`."""
        with self._lock:
            for table in self._tables.values():
                for position, column in enumerate(table.schema.columns):
                    if column.index_name == name:
                        return table, position
        raise CatalogError(f"no such index: {name}")

    def _replace_table(self, old: Table, schema: TableSchema) -> Table:
        """Store a changed definition of `old` and swap in a Table object for it."""
        rid = self.heap.update(old.catalog_rid, schema.encode())
        table = self._open_table(schema, rid)
        table.row_count = old.row_count
        table.stats = old.stats
        table._ann = old._ann  # in-memory vector indexes describe rows, not definitions
        table._ann_changed = old._ann_changed
        self._tables[schema.name] = table
        self.epoch += 1
        return table

    def create_index(self, statement: ast.CreateIndex) -> Table:
        """Build a secondary index over the table's existing rows.

        The index is a new B+ tree filled from a scan of the heap, all inside the
        statement's transaction, so a failure (or ROLLBACK) removes it again. A
        transaction cannot change more pages than the buffer pool holds, which
        bounds the size of table an index can be built on in one go.
        """
        with self._lock:
            table = self.get(statement.table)
            schema = table.schema
            position = schema.column_index(statement.column)
            if position is None:
                raise CatalogError(f"table {schema.name} has no column named {statement.column}")
            column = schema.columns[position]
            if column.type is ColumnType.VECTOR:
                raise CatalogError(f"{column.name} is a VECTOR column; use HNSW to index it")
            if column.indexed:
                what = "its index" if column.secondary else "a PRIMARY KEY or UNIQUE index"
                raise CatalogError(f"column {column.name} already has an index: {what}")
            try:
                self.index_owner(statement.name)
            except CatalogError:
                pass
            else:
                raise CatalogError(f"an index named {statement.name} already exists")
            if len(statement.name.encode("utf-8")) > 255:
                raise CatalogError("index names are at most 255 bytes")

            root = BTree.create(self._pool)
            tree = BTree(self._pool, root)
            types = schema.column_types
            for rid, record in table.heap.scan():
                value = decode_record(types, record)[position]
                if value is not None:
                    tree.insert(secondary_key(column.type, value, rid.page_id, rid.slot), rid)
            columns = tuple(
                replace(c, index_root=root, index_name=statement.name) if i == position else c
                for i, c in enumerate(schema.columns)
            )
            new_schema = replace(schema, columns=columns)
            if len(new_schema.encode()) > heap_page.max_record_size(self._pool.page_size):
                raise RecordTooLargeError(
                    f"the definition of table {schema.name} does not fit in one page"
                )
            return self._replace_table(table, new_schema)

    def drop_index(self, name: str) -> None:
        """Remove a secondary index and free its pages."""
        with self._lock:
            table, position = self.index_owner(name)
            table.indexes[position].drop()
            columns = tuple(
                replace(c, index_root=0, index_name="") if i == position else c
                for i, c in enumerate(table.schema.columns)
            )
            self._replace_table(table, replace(table.schema, columns=columns))

    def analyze(self, name: str | None) -> list[str]:
        """Collect column statistics for one table, or for all of them. Returns the tables done.

        Each column is counted from a full scan, and its statistics are stored as a
        record in the catalog heap, replacing the previous one. That makes ANALYZE
        a transaction like any other: it is logged, it is undone by ROLLBACK, and a
        crash cannot leave half of it. Vector columns are skipped.
        """
        with self._lock:
            names = [name] if name is not None else self.table_names()
            limit = heap_page.max_record_size(self._pool.page_size)
            for table_name in names:
                table = self.get(table_name)
                schema = table.schema
                types = schema.column_types
                columns: list[list[Value]] = [[] for _ in schema.columns]
                rows = 0
                for _rid, record in table.heap.scan():
                    row = decode_record(types, record)
                    rows += 1
                    for position, value in enumerate(row):
                        if types[position] is not ColumnType.VECTOR:
                            columns[position].append(value)
                fresh: dict[int, ColumnStats] = {}
                for position, column in enumerate(schema.columns):
                    if column.type is ColumnType.VECTOR:
                        continue
                    found = fit(
                        collect(column.type, columns[position], rows),
                        schema.table_id,
                        position,
                        limit,
                    )
                    fresh[position] = found
                    key = (schema.table_id, position)
                    record = found.encode(schema.table_id, position)
                    existing = self._stats_rids.get(key)
                    if existing is None:
                        self._stats_rids[key] = self.heap.insert(record)
                    else:
                        self._stats_rids[key] = self.heap.update(existing, record)
                table.stats = fresh
                table.row_count = rows
            self.epoch += 1  # plans were made with the old estimates
            return names

    def drop_table(self, name: str) -> None:
        """Free every page of the table and its indexes, and forget its definition."""
        with self._lock:
            table = self.get(name)
            for key in [key for key in self._stats_rids if key[0] == table.schema.table_id]:
                self.heap.delete(self._stats_rids.pop(key))
            for index in table.indexes.values():
                index.drop()
            table.heap.drop()
            self.heap.delete(table.catalog_rid)
            del self._tables[name]
            self.epoch += 1
            if self._store is not None:
                self._store.discard(table.schema.table_id)
