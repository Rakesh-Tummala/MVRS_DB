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


def encode_key(column_type: ColumnType, value: int | float | str) -> bytes:
    """Encode one non-NULL value as an index key."""
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
