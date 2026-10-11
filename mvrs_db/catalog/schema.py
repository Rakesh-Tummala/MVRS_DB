"""Table definitions and their on-disk encoding.

A catalog record describes one table:

    [format u8 = 1][table_id u32][first_heap_page u32]
    [name_len u8][name]
    [column_count u16]
    per column: [name_len u8][name][type u8][flags u8][index_root u32]

flags: bit 0 = NOT NULL, bit 1 = PRIMARY KEY, bit 2 = UNIQUE.
index_root is the root page of the column's B+ tree, or 0 if it has no index.

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


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: ColumnType
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False
    index_root: int = 0

    @property
    def indexed(self) -> bool:
        return self.index_root != 0


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
            )
            parts.append(bytes((len(column_name),)))
            parts.append(column_name)
            parts.append(_COLUMN_TAIL.pack(int(column.type), flags, column.index_root))
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
                columns.append(
                    Column(
                        name=column_name,
                        type=ColumnType(raw_type),
                        not_null=bool(flags & _NOT_NULL),
                        primary_key=bool(flags & _PRIMARY_KEY),
                        unique=bool(flags & _UNIQUE),
                        index_root=index_root,
                    )
                )
        except (struct.error, ValueError, UnicodeDecodeError, IndexError) as exc:
            raise CorruptCatalogError(f"catalog record cannot be decoded: {exc}") from exc
        if position != len(data):
            raise CorruptCatalogError("catalog record has unexpected trailing bytes")
        return cls(table_id, name, first_heap_page, tuple(columns))


def _read_name(data: bytes, position: int) -> tuple[str, int]:
    length = data[position]
    end = position + 1 + length
    if length == 0 or end > len(data):
        raise CorruptCatalogError("catalog record has an invalid name")
    return data[position + 1 : end].decode("utf-8"), end
