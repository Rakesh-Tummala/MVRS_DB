"""Secondary-index keys: value prefix + record ID, ordered like (value, record ID)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mvrs_db import config
from mvrs_db.errors import KeyTooLargeError
from mvrs_db.index.keys import (
    RID_MAX,
    RID_SIZE,
    decode_secondary_value,
    secondary_key,
    secondary_prefix,
)
from mvrs_db.storage.record import ColumnType

pages = st.integers(min_value=0, max_value=2**32 - 1)
slots = st.integers(min_value=0, max_value=2**16 - 1)
texts = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=["Cs"]), max_size=40)
ints = st.integers(min_value=-(2**63), max_value=2**63 - 1)
reals = st.floats(allow_nan=False, allow_infinity=True)


@given(a=ints, b=ints, pa=pages, sa=slots, pb=pages, sb=slots)
def test_integer_keys_order_by_value_then_record_id(
    a: int, b: int, pa: int, sa: int, pb: int, sb: int
) -> None:
    ka = secondary_key(ColumnType.INTEGER, a, pa, sa)
    kb = secondary_key(ColumnType.INTEGER, b, pb, sb)
    assert (ka < kb) == ((a, pa, sa) < (b, pb, sb))
    assert (ka == kb) == ((a, pa, sa) == (b, pb, sb))


@given(a=reals, b=reals, pa=pages, sa=slots, pb=pages, sb=slots)
def test_real_keys_order_by_value_then_record_id(
    a: float, b: float, pa: int, sa: int, pb: int, sb: int
) -> None:
    ka = secondary_key(ColumnType.REAL, a, pa, sa)
    kb = secondary_key(ColumnType.REAL, b, pb, sb)
    # -0.0 and 0.0 are one value, so compare as numbers, not as bit patterns.
    assert (ka < kb) == ((a + 0.0, pa, sa) < (b + 0.0, pb, sb))


@given(a=texts, b=texts, pa=pages, sa=slots, pb=pages, sb=slots)
def test_text_keys_order_by_value_then_record_id(
    a: str, b: str, pa: int, sa: int, pb: int, sb: int
) -> None:
    ka = secondary_key(ColumnType.TEXT, a, pa, sa)
    kb = secondary_key(ColumnType.TEXT, b, pb, sb)
    # Code point order is UTF-8 byte order, which is what the plain key relies on too.
    assert (ka < kb) == ((a.encode(), pa, sa) < (b.encode(), pb, sb))


@given(value=texts, other=texts, page=pages, slot=slots)
def test_a_value_owns_a_contiguous_run_of_keys(
    value: str, other: str, page: int, slot: int
) -> None:
    """Every key of `value` lies in [prefix, prefix + RID_MAX], and no other value's key does."""
    prefix = secondary_prefix(ColumnType.TEXT, value)
    inside = prefix <= secondary_key(ColumnType.TEXT, value, page, slot) <= prefix + RID_MAX
    assert inside
    foreign = secondary_key(ColumnType.TEXT, other, page, slot)
    assert (prefix <= foreign <= prefix + RID_MAX) == (other == value)


def test_a_text_value_that_starts_another_does_not_swallow_it() -> None:
    # "ab" is the beginning of "abc" and of "ab\x00"; none may land inside "ab"'s run.
    low = secondary_prefix(ColumnType.TEXT, "ab")
    for longer in ("abc", "ab\x00", "ab\x00\x00", "ab\x01", "abb" * 5):
        key = secondary_key(ColumnType.TEXT, longer, 0, 0)
        assert not low <= key <= low + RID_MAX, longer


@given(value=texts)
def test_text_round_trips(value: str) -> None:
    key = secondary_key(ColumnType.TEXT, value, 7, 9)
    assert decode_secondary_value(ColumnType.TEXT, key) == value


@given(value=ints)
def test_integer_round_trips(value: int) -> None:
    assert (
        decode_secondary_value(ColumnType.INTEGER, secondary_key(ColumnType.INTEGER, value, 1, 2))
        == value
    )


@given(value=reals)
def test_real_round_trips(value: float) -> None:
    got = decode_secondary_value(ColumnType.REAL, secondary_key(ColumnType.REAL, value, 1, 2))
    assert got == value


def test_the_record_id_takes_six_bytes() -> None:
    assert len(secondary_key(ColumnType.INTEGER, 5, 1, 2)) == 8 + RID_SIZE == 14


def test_the_longest_text_that_fits() -> None:
    longest = config.MAX_KEY_SIZE - RID_SIZE - 2  # prefix adds two terminator bytes
    key = secondary_key(ColumnType.TEXT, "x" * longest, 1, 1)
    assert len(key) == config.MAX_KEY_SIZE
    with pytest.raises(KeyTooLargeError, match="secondary index key"):
        secondary_prefix(ColumnType.TEXT, "x" * (longest + 1))


def test_a_nul_character_costs_an_extra_byte() -> None:
    with pytest.raises(KeyTooLargeError):
        secondary_prefix(ColumnType.TEXT, "\x00" * 200)
    assert secondary_prefix(ColumnType.TEXT, "\x00") == b"\x00\xff\x00\x00"
