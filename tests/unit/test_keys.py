"""Order-preserving key encoding: byte order must equal value order."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kestreldb.errors import InvalidValueError, KeyTooLargeError
from kestreldb.index.keys import decode_key, encode_key
from kestreldb.storage.record import INT64_MAX, INT64_MIN, ColumnType

INTEGERS = st.integers(INT64_MIN, INT64_MAX)
REALS = st.floats(allow_nan=False)
TEXTS = st.text(max_size=20)


@given(INTEGERS, INTEGERS)
def test_integer_order(a: int, b: int) -> None:
    ka, kb = encode_key(ColumnType.INTEGER, a), encode_key(ColumnType.INTEGER, b)
    assert (a < b) == (ka < kb)
    assert (a == b) == (ka == kb)


@given(REALS, REALS)
def test_real_order(a: float, b: float) -> None:
    ka, kb = encode_key(ColumnType.REAL, a), encode_key(ColumnType.REAL, b)
    assert (a < b) == (ka < kb)
    assert (a == b) == (ka == kb)


@given(TEXTS, TEXTS)
def test_text_order_is_code_point_order(a: str, b: str) -> None:
    ka, kb = encode_key(ColumnType.TEXT, a), encode_key(ColumnType.TEXT, b)
    code_points_a, code_points_b = [ord(c) for c in a], [ord(c) for c in b]
    assert (code_points_a < code_points_b) == (ka < kb)
    assert (a == b) == (ka == kb)


@given(INTEGERS)
def test_integer_round_trip(value: int) -> None:
    assert decode_key(ColumnType.INTEGER, encode_key(ColumnType.INTEGER, value)) == value


@given(REALS)
def test_real_round_trip(value: float) -> None:
    assert decode_key(ColumnType.REAL, encode_key(ColumnType.REAL, value)) == value


@given(TEXTS)
def test_text_round_trip(value: str) -> None:
    assert decode_key(ColumnType.TEXT, encode_key(ColumnType.TEXT, value)) == value


def test_known_orderings() -> None:
    integers = [INT64_MIN, -1, 0, 1, INT64_MAX]
    assert sorted(integers, key=lambda v: encode_key(ColumnType.INTEGER, v)) == integers
    reals = [-math.inf, -1e300, -1.5, -5e-324, 0.0, 5e-324, 1.5, 1e300, math.inf]
    assert sorted(reals, key=lambda v: encode_key(ColumnType.REAL, v)) == reals


def test_negative_zero_equals_zero() -> None:
    assert encode_key(ColumnType.REAL, -0.0) == encode_key(ColumnType.REAL, 0.0)


def test_fixed_width_for_numbers() -> None:
    assert len(encode_key(ColumnType.INTEGER, 5)) == 8
    assert len(encode_key(ColumnType.REAL, 5.0)) == 8


def test_text_key_size_limit() -> None:
    assert len(encode_key(ColumnType.TEXT, "a" * 255)) == 255
    with pytest.raises(KeyTooLargeError):
        encode_key(ColumnType.TEXT, "a" * 256)
    with pytest.raises(KeyTooLargeError):
        encode_key(ColumnType.TEXT, "é" * 128)  # 256 bytes of UTF-8


@pytest.mark.parametrize(
    ("column_type", "value"),
    [
        (ColumnType.INTEGER, 1.0),
        (ColumnType.INTEGER, True),
        (ColumnType.INTEGER, 2**63),
        (ColumnType.REAL, 1),
        (ColumnType.REAL, math.nan),
        (ColumnType.TEXT, 1),
        (ColumnType.TEXT, "\ud800"),
    ],
)
def test_wrong_types_are_rejected(column_type: ColumnType, value: object) -> None:
    with pytest.raises(InvalidValueError):
        encode_key(column_type, value)  # type: ignore[arg-type]
