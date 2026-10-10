"""Row format 1: turning a tuple of column values into bytes and back.

    [format: u8 = 1] [null bitmap: ceil(n/8) bytes] [values of non-NULL columns, in order]

    INTEGER  8 bytes, signed, little-endian
    REAL     8 bytes, IEEE-754 double
    TEXT     u16 byte length, then UTF-8

A record is not self-describing: it is decoded against the table's column types.
The decoder treats its input as untrusted and rejects anything it did not write.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from enum import IntEnum

from kestreldb.errors import CorruptRecordError, InvalidValueError, RecordTooLargeError

RECORD_FORMAT = 1

_I64 = struct.Struct("<q")
_F64 = struct.Struct("<d")
_U16 = struct.Struct("<H")

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
MAX_TEXT_BYTES = 0xFFFF

Value = int | float | str | None


class ColumnType(IntEnum):
    INTEGER = 1
    REAL = 2
    TEXT = 3


def encode_record(types: Sequence[ColumnType], values: Sequence[Value]) -> bytes:
    """Encode one row. Raises InvalidValueError if a value does not fit its column."""
    count = len(types)
    if len(values) != count:
        raise InvalidValueError(f"expected {count} values, got {len(values)}")
    bitmap = bytearray((count + 7) // 8)
    parts: list[bytes] = []
    for i in range(count):
        value = values[i]
        if value is None:
            bitmap[i >> 3] |= 1 << (i & 7)
            continue
        column_type = types[i]
        if column_type is ColumnType.INTEGER:
            if type(value) is not int:
                raise InvalidValueError(f"column {i}: INTEGER cannot store {value!r}")
            if not INT64_MIN <= value <= INT64_MAX:
                raise InvalidValueError(f"column {i}: integer {value} does not fit in 64 bits")
            parts.append(_I64.pack(value))
        elif column_type is ColumnType.REAL:
            if type(value) is not float:
                raise InvalidValueError(f"column {i}: REAL cannot store {value!r}")
            if value != value:
                raise InvalidValueError(f"column {i}: NaN cannot be stored")
            parts.append(_F64.pack(value))
        elif column_type is ColumnType.TEXT:
            if type(value) is not str:
                raise InvalidValueError(f"column {i}: TEXT cannot store {value!r}")
            try:
                raw = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise InvalidValueError(f"column {i}: text is not valid Unicode") from exc
            if len(raw) > MAX_TEXT_BYTES:
                raise RecordTooLargeError(
                    f"column {i}: text of {len(raw)} bytes exceeds {MAX_TEXT_BYTES}"
                )
            parts.append(_U16.pack(len(raw)))
            parts.append(raw)
        else:
            raise InvalidValueError(f"column {i}: unknown column type {column_type!r}")
    return bytes((RECORD_FORMAT,)) + bytes(bitmap) + b"".join(parts)


def decode_record(types: Sequence[ColumnType], data: bytes) -> tuple[Value, ...]:
    """Decode one row. Raises CorruptRecordError on anything malformed."""
    count = len(types)
    bitmap_len = (count + 7) // 8
    if len(data) < 1 + bitmap_len:
        raise CorruptRecordError(f"record of {len(data)} bytes is too short for {count} columns")
    if data[0] != RECORD_FORMAT:
        raise CorruptRecordError(f"unknown record format {data[0]}")
    bitmap = data[1 : 1 + bitmap_len]
    # Bits beyond the last column must be clear.
    if count % 8 and bitmap[-1] >> (count % 8):
        raise CorruptRecordError("null bitmap has bits set beyond the last column")
    pos = 1 + bitmap_len
    end = len(data)
    values: list[Value] = []
    for i in range(count):
        if bitmap[i >> 3] & (1 << (i & 7)):
            values.append(None)
            continue
        column_type = types[i]
        if column_type is ColumnType.INTEGER:
            if pos + 8 > end:
                raise CorruptRecordError(f"column {i}: integer runs past the end of the record")
            values.append(_I64.unpack_from(data, pos)[0])
            pos += 8
        elif column_type is ColumnType.REAL:
            if pos + 8 > end:
                raise CorruptRecordError(f"column {i}: real runs past the end of the record")
            number: float = _F64.unpack_from(data, pos)[0]
            if number != number:
                raise CorruptRecordError(f"column {i}: stored value is NaN")
            values.append(number)
            pos += 8
        elif column_type is ColumnType.TEXT:
            if pos + 2 > end:
                raise CorruptRecordError(f"column {i}: text length runs past the end of the record")
            length = _U16.unpack_from(data, pos)[0]
            pos += 2
            if pos + length > end:
                raise CorruptRecordError(f"column {i}: text runs past the end of the record")
            try:
                values.append(data[pos : pos + length].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise CorruptRecordError(f"column {i}: text is not valid UTF-8") from exc
            pos += length
        else:
            raise CorruptRecordError(f"column {i}: unknown column type {column_type!r}")
    if pos != end:
        raise CorruptRecordError(f"record has {end - pos} unexpected trailing bytes")
    return tuple(values)
