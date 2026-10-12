"""Order-preserving key encoding.

The B+ tree compares keys as raw bytes. That only works if, for two values a and
b of the same column type, a < b exactly when encode(a) < encode(b) bytewise.
Each type therefore gets an encoding with that property:

INTEGER  8 bytes big-endian with the sign bit flipped. Flipping the sign bit maps
         the signed range onto 0 .. 2**64-1 without changing the order.
REAL     The IEEE-754 bits, big-endian. For positive numbers the bit pattern
         already sorts correctly once the sign bit is set; for negative numbers
         every bit is inverted, because a larger magnitude means a smaller value.
         -0.0 is stored as 0.0 so that equal values have equal keys.
TEXT     UTF-8, whose byte order equals Unicode code point order.

The payoff is that the tree never needs to know column types, and comparing two
keys is a single bytes comparison running in C.
"""

from __future__ import annotations

import struct

from mvrs_db import config
from mvrs_db.errors import InvalidValueError, KeyTooLargeError
from mvrs_db.storage.record import INT64_MAX, INT64_MIN, ColumnType

_F64 = struct.Struct(">d")
_U64 = struct.Struct(">Q")
_SIGN = 1 << 63
_MASK = (1 << 64) - 1


def encode_key(column_type: ColumnType, value: object) -> bytes:
    """Encode one non-NULL value as an index key.

    The value's type is checked here, at run time. Only INTEGER, REAL and TEXT
    columns can be indexed; a vector has no order, so it cannot be a key.
    """
    if column_type is ColumnType.INTEGER:
        if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
            raise InvalidValueError(f"{value!r} is not a 64-bit integer")
        return _U64.pack(value + _SIGN)
    if column_type is ColumnType.REAL:
        if type(value) is not float or value != value:
            raise InvalidValueError(f"{value!r} is not a storable real number")
        bits = _U64.unpack(_F64.pack(value + 0.0))[0]  # -0.0 + 0.0 == 0.0
        return _U64.pack(bits ^ _MASK if bits & _SIGN else bits | _SIGN)
    if column_type is ColumnType.TEXT:
        if type(value) is not str:
            raise InvalidValueError(f"{value!r} is not text")
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvalidValueError("text is not valid Unicode") from exc
        if len(raw) > config.MAX_KEY_SIZE:
            raise KeyTooLargeError(
                f"key of {len(raw)} bytes exceeds the {config.MAX_KEY_SIZE}-byte limit"
            )
        return raw
    raise InvalidValueError(f"unknown column type {column_type!r}")


def decode_key(column_type: ColumnType, key: bytes) -> int | float | str:
    """Inverse of encode_key (used by statistics and diagnostics)."""
    if column_type is ColumnType.INTEGER:
        value: int = _U64.unpack(key)[0] - _SIGN
        return value
    if column_type is ColumnType.REAL:
        bits = _U64.unpack(key)[0]
        bits = bits ^ _SIGN if bits & _SIGN else bits ^ _MASK
        number: float = _F64.unpack(_U64.pack(bits))[0]
        return number
    return key.decode("utf-8")


# ---------------------------------------------------------------------- secondary indexes
#
# A primary-key or UNIQUE index maps each value to one row, so the value is the
# key. A secondary (non-unique) index must hold the same value many times, but
# the tree stores each key once. The fix is to make the key longer, not the tree
# smarter: key = (value as above) + (the row's record ID). The key is then unique
# because the record ID is, and all rows with one value sit next to each other,
# ordered by record ID, so "every row with value v" is a range scan over
#   [prefix(v), prefix(v) + FF FF FF FF FF FF]
# where prefix(v) is the value's encoding and the six bytes are the largest
# possible record ID.
#
# That only works if one value's prefix can never be the beginning of another
# value's prefix. INTEGER and REAL keys are 8 bytes, so they cannot. TEXT can:
# "ab" is the start of "abc", and the bytes after "ab" would be mistaken for the
# start of the next value's text. So TEXT gets a terminator that sorts before any
# character, and a NUL inside the text is escaped so it cannot be mistaken for
# it:
#     NUL  ->  00 FF        end of text  ->  00 00
# Order is preserved: after a shared prefix, "ab" continues with 00 00 and any
# longer text continues with a byte that is at least 00 FF (an escaped NUL) or
# above 00 (any other byte).

RID_SIZE = 6
_RID = struct.Struct(">IH")
RID_MAX = b"\xff" * RID_SIZE


def secondary_prefix(column_type: ColumnType, value: object) -> bytes:
    """The value's part of a secondary-index key. It never contains the record ID."""
    if column_type is not ColumnType.TEXT:
        return encode_key(column_type, value)
    if type(value) is not str:
        raise InvalidValueError(f"{value!r} is not text")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidValueError("text is not valid Unicode") from exc
    prefix = raw.replace(b"\x00", b"\x00\xff") + b"\x00\x00"
    if len(prefix) + RID_SIZE > config.MAX_KEY_SIZE:
        raise KeyTooLargeError(
            f"text of {len(raw)} bytes does not fit a secondary index key "
            f"(at most {config.MAX_KEY_SIZE - RID_SIZE - 2} bytes without NUL characters)"
        )
    return prefix


def secondary_key(column_type: ColumnType, value: object, page_id: int, slot: int) -> bytes:
    """The full key for one row in a secondary index: value prefix + record ID."""
    return secondary_prefix(column_type, value) + _RID.pack(page_id, slot)


def decode_secondary_value(column_type: ColumnType, key: bytes) -> int | float | str:
    """The value stored in a secondary-index key (used for MIN/MAX and statistics)."""
    if column_type is not ColumnType.TEXT:
        return decode_key(column_type, key[: len(key) - RID_SIZE])
    out = bytearray()
    i = 0
    while True:
        if key[i] == 0:
            if key[i + 1] == 0:
                return out.decode("utf-8")
            out.append(0)  # an escaped NUL: 00 FF
            i += 2
        else:
            out.append(key[i])
            i += 1
