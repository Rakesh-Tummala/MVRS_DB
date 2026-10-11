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
from mvrs_db.storage.buffer_pool import BufferPool
from mvrs_db.storage.heap_file import HeapFile, RecordId
from mvrs_db.storage.record import ColumnType


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
    def __init__(self, pool: BufferPool) -> None:
        self._pool = pool
        self._lock = threading.RLock()
        self._tables: dict[str, Table] = {}
        self._heap: HeapFile | None = None
        self._next_table_id = 1

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
                tables[schema.name] = self._open_table(schema, rid)
                next_id = max(next_id, schema.table_id + 1)
            self._heap = heap
            self._tables = tables
            self._next_table_id = next_id

    def _open_table(self, schema: TableSchema, rid: RecordId) -> Table:
        indexes = {
            position: BTree(self._pool, column.index_root)
            for position, column in enumerate(schema.columns)
            if column.index_root
        }
        return Table(schema, HeapFile(self._pool, schema.first_heap_page), indexes, rid)

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

            # Check the definition fits in a catalog record before allocating anything.
            draft = TableSchema(
                0,
                statement.table,
                0,
                tuple(
                    Column(d.name, ColumnType[d.type_name], d.not_null, d.primary_key, d.unique)
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
            self._tables[schema.name] = table
            self._next_table_id += 1
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
