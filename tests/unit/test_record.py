"""Record encoding and decoding."""

from __future__ import annotations

import contextlib
import math
import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kestreldb.errors import CorruptRecordError, InvalidValueError, RecordTooLargeError
from kestreldb.storage.record import (
    INT64_MAX,
    INT64_MIN,
    ColumnType,
    Value,
    decode_record,
    encode_record,
)

I, R, T = ColumnType.INTEGER, ColumnType.REAL, ColumnType.TEXT  # noqa: E741

VALUE_FOR = {
    I: st.integers(INT64_MIN, INT64_MAX),
    R: st.floats(allow_nan=False),
    T: st.text(max_size=12),
}


@st.composite
def typed_rows(draw: st.DrawFn) -> tuple[list[ColumnType], list[Value]]:
    # Up to 10 columns crosses the one-byte null bitmap boundary at 8.
    types = draw(st.lists(st.sampled_from([I, R, T]), max_size=10))
    values = [draw(st.one_of(st.none(), VALUE_FOR[t])) for t in types]
    return types, values


def same(a: Value, b: Value) -> bool:
    """Equality that tells 0.0 from -0.0, so the round trip is checked bit for bit."""
    if isinstance(a, float) and isinstance(b, float):
        return struct.pack("<d", a) == struct.pack("<d", b)
    return type(a) is type(b) and a == b


@given(typed_rows())
def test_round_trip(row: tuple[list[ColumnType], list[Value]]) -> None:
    types, values = row
    decoded = decode_record(types, encode_record(types, values))
    assert len(decoded) == len(values)
    assert all(same(a, b) for a, b in zip(decoded, values, strict=True))


def test_documented_layout() -> None:
    data = encode_record([I, T, R, T], [1, None, 2.5, "hé"])

    assert data[0] == 1  # format
    assert data[1] == 0b0000_0010  # column 1 is NULL
    assert data[2:10] == struct.pack("<q", 1)
    assert data[10:18] == struct.pack("<d", 2.5)
    assert data[18:20] == struct.pack("<H", 3)  # "hé" is 3 bytes of UTF-8
    assert data[20:] == "hé".encode()


@pytest.mark.parametrize(
    "values",
    [
        [INT64_MIN, 0.0, ""],
        [INT64_MAX, math.inf, "x" * 1000],
        [0, -math.inf, "\x00 nul and emoji \U0001f600"],
        [None, None, None],
        [-1, 5e-324, "'quoted'"],
    ],
)
def test_boundary_values(values: list[Value]) -> None:
    assert decode_record([I, R, T], encode_record([I, R, T], values)) == tuple(values)


def test_empty_row() -> None:
    assert encode_record([], []) == b"\x01"
    assert decode_record([], b"\x01") == ()


@pytest.mark.parametrize(
    ("types", "values"),
    [
        ([I], [2**63]),
        ([I], [-(2**63) - 1]),
        ([I], [1.0]),
        ([I], [True]),
        ([I], ["1"]),
        ([R], [1]),
        ([R], [math.nan]),
        ([R], ["1.0"]),
        ([T], [1]),
        ([T], [b"bytes"]),
        ([T], ["\ud800"]),  # a lone surrogate is not valid Unicode
        ([I, I], [1]),
        ([I], [1, 2]),
    ],
)
def test_values_that_do_not_fit_their_column(types: list[ColumnType], values: list[Value]) -> None:
    with pytest.raises(InvalidValueError):
        encode_record(types, values)


def test_text_longer_than_65535_bytes_is_too_large() -> None:
    assert len(encode_record([T], ["a" * 65535])) == 1 + 1 + 2 + 65535
    with pytest.raises(RecordTooLargeError):
        encode_record([T], ["a" * 65536])


GOOD = encode_record([I, T, R], [7, "abc", 1.5])


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x01",  # no bitmap
        b"\x02" + GOOD[1:],  # unknown format
        GOOD[:-1],  # truncated real
        GOOD[:5],  # truncated integer
        GOOD + b"\x00",  # trailing byte
        bytes([1, 0b0000_1000]) + GOOD[2:],  # bitmap bit beyond column 3
        GOOD[:10] + struct.pack("<H", 500) + GOOD[12:],  # text length past the end
        GOOD[:12] + b"\xff\xfe\xfd" + GOOD[15:],  # invalid UTF-8
        encode_record([I, T], [7, "abc"]) + struct.pack("<d", math.nan),  # stored NaN
    ],
    ids=[
        "empty",
        "no-bitmap",
        "bad-format",
        "short-real",
        "short-int",
        "trailing",
        "bitmap-overflow",
        "text-length",
        "bad-utf8",
        "nan",
    ],
)
def test_malformed_records_are_rejected(data: bytes) -> None:
    with pytest.raises(CorruptRecordError):
        decode_record([I, T, R], data)


@given(st.binary(max_size=60))
def test_decoder_never_crashes_on_arbitrary_bytes(data: bytes) -> None:
    with contextlib.suppress(CorruptRecordError):
        decode_record([I, T, R, T], data)
