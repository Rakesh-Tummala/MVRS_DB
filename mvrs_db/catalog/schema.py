"""Table definitions and their on-disk encoding.

A catalog record describes one table:

    [format u8 = 1][table_id u32][first_heap_page u32]
    [name_len u8][name]
    [column_count u16]
    per column: [name_len u8][name][type u8][flags u8][index_root u32]
                [dimension u16]   only when the type is VECTOR

flags: bit 0 = NOT NULL, bit 1 = PRIMARY KEY, bit 2 = UNIQUE,
       bit 3 = HNSW index for Euclidean distance, bit 4 = HNSW index for cosine,
       bit 5 = secondary (non-unique) index, created by CREATE INDEX.
index_root is the root page of the column's B+ tree, or 0 if it has no index.
A column with bit 5 set is followed, after the dimension if it has one, by
[name_len u8][name]: the index's name. Records written before secondary indexes
existed never have the bit and decode unchanged.
dimension is the n of VECTOR(n). Storing it only for vector columns means
records written before vectors existed still decode unchanged.

This is an explicit binary layout, not the row format, because the catalog must
be readable before any table schema is known.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from mvrs_db import config
from mvrs_db.errors import CorruptCatalogError
from mvrs_db.storage.record import ColumnType

CATALOG_RECORD_FORMAT = 1
_FIXED = struct.Struct("<BII")
_COLUMN_TAIL = struct.Struct("<BBI")
_U16 = struct.Struct("<H")

_NOT_NULL = 1
_PRIMARY_KEY = 2
_UNIQUE = 4
_HNSW_L2 = 8
_HNSW_COSINE = 16
_SECONDARY = 32
_ANN_FLAG = {"": 0, "l2": _HNSW_L2, "cosine": _HNSW_COSINE}


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: ColumnType
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False
    index_root: int = 0
    dimension: int = 0  # n for VECTOR(n); 0 for every other type
    # "" for no approximate index, otherwise the distance its HNSW graph is
    # built for: "l2" or "cosine". Only the declaration is stored; the graph
    # itself lives in memory.
    ann: str = ""
    # Name of the secondary (non-unique) index on this column, "" if there is none.
    # A primary-key or UNIQUE column has an index too, but it is not named.
    index_name: str = ""

    @property
    def indexed(self) -> bool:
        return self.index_root != 0

    @property
    def secondary(self) -> bool:
        return self.index_name != ""


@dataclass(frozen=True, slots=True)
class TableSchema:
    table_id: int
    name: str
    first_heap_page: int
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def column_types(self) -> tuple[ColumnType, ...]:
        return tuple(column.type for column in self.columns)

    def column_index(self, name: str) -> int | None:
        for index, column in enumerate(self.columns):
            if column.name == name:
                return index
        return None

    def encode(self) -> bytes:
        name = self.name.encode("utf-8")
        parts = [
            _FIXED.pack(CATALOG_RECORD_FORMAT, self.table_id, self.first_heap_page),
            bytes((len(name),)),
            name,
            _U16.pack(len(self.columns)),
        ]
        for column in self.columns:
            column_name = column.name.encode("utf-8")
            flags = (
                (_NOT_NULL if column.not_null else 0)
                | (_PRIMARY_KEY if column.primary_key else 0)
                | (_UNIQUE if column.unique else 0)
                | (_SECONDARY if column.secondary else 0)
                | _ANN_FLAG[column.ann]
            )
            parts.append(bytes((len(column_name),)))
            parts.append(column_name)
            parts.append(_COLUMN_TAIL.pack(int(column.type), flags, column.index_root))
            if column.type is ColumnType.VECTOR:
                parts.append(_U16.pack(column.dimension))
            if column.secondary:
                index_name = column.index_name.encode("utf-8")
                parts.append(bytes((len(index_name),)))
                parts.append(index_name)
        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> TableSchema:
        try:
            record_format, table_id, first_heap_page = _FIXED.unpack_from(data, 0)
            if record_format != CATALOG_RECORD_FORMAT:
                raise CorruptCatalogError(f"unknown catalog record format {record_format}")
            position = _FIXED.size
            name, position = _read_name(data, position)
            column_count = _U16.unpack_from(data, position)[0]
            position += _U16.size
            if not 1 <= column_count <= config.MAX_COLUMNS:
                raise CorruptCatalogError(f"table {name!r} claims {column_count} columns")
            columns = []
            for _ in range(column_count):
                column_name, position = _read_name(data, position)
                raw_type, flags, index_root = _COLUMN_TAIL.unpack_from(data, position)
                position += _COLUMN_TAIL.size
                column_type = ColumnType(raw_type)
                dimension = 0
                if column_type is ColumnType.VECTOR:
                    dimension = _U16.unpack_from(data, position)[0]
                    position += _U16.size
                    if not 1 <= dimension <= config.MAX_VECTOR_DIMENSION:
                        raise CorruptCatalogError(
                            f"column {column_name!r} claims {dimension} vector dimensions"
                        )
                index_name = ""
                if flags & _SECONDARY:
                    index_name, position = _read_name(data, position)
                    if (
                        not index_root
                        or flags & (_PRIMARY_KEY | _UNIQUE)
                        or column_type is ColumnType.VECTOR
                    ):
                        raise CorruptCatalogError(
                            f"column {column_name!r} has an invalid secondary index record"
                        )
                columns.append(
                    Column(
                        name=column_name,
                        type=column_type,
                        not_null=bool(flags & _NOT_NULL),
                        primary_key=bool(flags & _PRIMARY_KEY),
                        unique=bool(flags & _UNIQUE),
                        index_root=index_root,
                        dimension=dimension,
                        ann=_decode_ann(flags, column_type, column_name),
                        index_name=index_name,
                    )
                )
        except (struct.error, ValueError, UnicodeDecodeError, IndexError) as exc:
            raise CorruptCatalogError(f"catalog record cannot be decoded: {exc}") from exc
        if position != len(data):
            raise CorruptCatalogError("catalog record has unexpected trailing bytes")
        return cls(table_id, name, first_heap_page, tuple(columns))


def _decode_ann(flags: int, column_type: ColumnType, name: str) -> str:
    wanted = flags & (_HNSW_L2 | _HNSW_COSINE)
    if wanted == 0:
        return ""
    if column_type is not ColumnType.VECTOR or wanted == (_HNSW_L2 | _HNSW_COSINE):
        raise CorruptCatalogError(f"column {name!r} has invalid vector index flags")
    return "l2" if wanted == _HNSW_L2 else "cosine"


def _read_name(data: bytes, position: int) -> tuple[str, int]:
    length = data[position]
    end = position + 1 + length
    if length == 0 or end > len(data):
        raise CorruptCatalogError("catalog record has an invalid name")
    return data[position + 1 : end].decode("utf-8"), end
