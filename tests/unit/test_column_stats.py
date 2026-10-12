"""Column statistics: collecting them, estimating from them, storing them."""

from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mvrs_db.catalog.stats import (
    HISTOGRAM_BUCKETS,
    MAX_COMMON,
    MAX_TEXT,
    ColumnStats,
    collect,
    fit,
)
from mvrs_db.errors import CorruptCatalogError
from mvrs_db.storage.record import ColumnType

INT = ColumnType.INTEGER


def truth_equal(values: list[object], value: object) -> float:
    return sum(1 for v in values if v == value) / len(values)


def truth_range(values: list[object], low: object, li: bool, high: object, hi: bool) -> float:
    def inside(v: object) -> bool:
        if v is None:
            return False
        if low is not None and (v < low or (v == low and not li)):  # type: ignore[operator]
            return False
        return not (high is not None and (v > high or (v == high and not hi)))  # type: ignore[operator]

    return sum(1 for v in values if inside(v)) / len(values)


# ---------------------------------------------------------------------- collecting


def test_counts_are_exact() -> None:
    values = [1, 2, 2, 3, None, None, 3, 3]
    stats = collect(INT, values, len(values))
    assert (stats.rows, stats.nulls, stats.distinct) == (8, 2, 3)
    assert stats.null_fraction == 0.25


def test_an_empty_column_and_an_all_null_column() -> None:
    assert collect(INT, [], 0) == ColumnStats(INT, 0, 0, 0)
    stats = collect(INT, [None] * 5, 5)
    assert (stats.rows, stats.nulls, stats.distinct, stats.common, stats.bounds) == (
        5,
        5,
        0,
        (),
        (),
    )
    assert stats.equal_fraction(3) == 0.0 and stats.average_equal_fraction() == 0.0


def test_a_single_value() -> None:
    stats = collect(INT, [7] * 50, 50)
    assert stats.distinct == 1
    assert stats.equal_fraction(7) == 1.0
    assert stats.range_fraction(7, True, 7, True) == 1.0
    assert stats.range_fraction(8, True, None, True) == 0.0


def test_uniform_data_has_no_common_values() -> None:
    values = list(range(1000))
    assert collect(INT, values, 1000).common == ()


def test_skewed_data_lists_the_common_values_with_exact_counts() -> None:
    rng = random.Random(1)
    values: list[object] = [0] * 600 + [1] * 250 + [rng.randrange(2, 400) for _ in range(150)]
    stats = collect(INT, values, len(values))
    assert dict(stats.common)[0] == 600 and dict(stats.common)[1] == 250
    assert stats.equal_fraction(0) == 0.6 and stats.equal_fraction(1) == 0.25


def test_at_most_ten_common_values_and_they_are_the_most_common() -> None:
    values: list[object] = []
    for v in range(30):
        values += [v] * (100 + v)  # all well above average? none stands out much
    values += list(range(1000, 1500))  # many singletons pull the average down
    stats = collect(INT, values, len(values))
    assert len(stats.common) == MAX_COMMON
    counts = [count for _v, count in stats.common]
    assert counts == sorted(counts, reverse=True)
    assert min(counts) >= max(
        count
        for v, count in __import__("collections").Counter(values).items()
        if v not in dict(stats.common)
    )


def test_the_histogram_is_ascending_and_equi_depth() -> None:
    rng = random.Random(2)
    values = [int(rng.gauss(500, 120)) for _ in range(5000)]
    stats = collect(INT, values, len(values))
    bounds = stats.bounds
    assert len(bounds) == HISTOGRAM_BUCKETS + 1
    assert list(bounds) == sorted(bounds)
    rest = [v for v in values if v not in dict(stats.common)]
    per_bucket = len(rest) / HISTOGRAM_BUCKETS
    for low, high in zip(bounds, bounds[1:], strict=False):
        held = sum(1 for v in rest if low <= v < high)
        assert held <= per_bucket * 1.6, (low, high, held, per_bucket)  # duplicates make it lumpy


def test_long_text_is_not_kept_whole() -> None:
    long = "x" * (MAX_TEXT + 30)
    values: list[object] = [long] * 40 + [f"v{i}" for i in range(100)]
    stats = collect(ColumnType.TEXT, values, len(values))
    assert all(len(str(v).encode()) <= MAX_TEXT for v, _c in stats.common)
    assert all(len(str(b).encode()) <= MAX_TEXT for b in stats.bounds)


# ---------------------------------------------------------------------- estimating


@pytest.mark.parametrize("shape", ["uniform", "skewed", "normal", "few"])
def test_estimates_track_the_truth(shape: str) -> None:
    rng = random.Random(shape)
    if shape == "uniform":
        values: list[object] = [rng.randrange(0, 2000) for _ in range(8000)]
    elif shape == "skewed":
        values = [min(int(rng.paretovariate(1.2)), 500) for _ in range(8000)]
    elif shape == "normal":
        values = [int(rng.gauss(100, 25)) for _ in range(8000)]
    else:
        values = [rng.randrange(0, 4) for _ in range(8000)]
    values += [None] * 400
    stats = collect(INT, values, len(values))
    worst_range = 0.0
    for _ in range(300):
        a, b = sorted(rng.randrange(-10, 2100) for _ in range(2))
        li, hi = rng.random() < 0.5, rng.random() < 0.5
        guess = stats.range_fraction(a, li, b, hi)
        worst_range = max(worst_range, abs(guess - truth_range(values, a, li, b, hi)))
    assert worst_range < 0.09, worst_range
    for _ in range(100):
        v = rng.choice([x for x in values if x is not None])
        # a common value is exact; any other is the average of the rest
        error = abs(stats.equal_fraction(v) - truth_equal(values, v))
        if v in dict(stats.common):
            assert error < 1e-12
        else:
            assert error < 0.05


def test_open_ranges_and_inverted_ranges() -> None:
    values = list(range(1000))
    stats = collect(INT, values, 1000)
    assert abs(stats.range_fraction(None, True, None, True) - 1.0) < 1e-9
    assert stats.range_fraction(500, True, 400, True) == 0.0
    assert stats.range_fraction(2000, True, None, True) == 0.0
    assert stats.range_fraction(None, True, -5, True) == 0.0


@given(
    data=st.lists(st.one_of(st.none(), st.integers(-50, 50)), min_size=1, max_size=300),
    a=st.integers(-60, 60),
    b=st.integers(-60, 60),
    c=st.integers(-60, 60),
)
@settings(max_examples=150, deadline=None)
def test_a_wider_range_never_selects_less(data: list[int | None], a: int, b: int, c: int) -> None:
    stats = collect(INT, data, len(data))
    low, mid, high = sorted((a, b, c))
    inner = stats.range_fraction(mid, True, mid, True)
    middle = stats.range_fraction(low, True, high, True)
    outer = stats.range_fraction(None, True, None, True)
    assert 0.0 <= inner <= middle + 1e-9 <= outer + 1e-9 <= 1.0 + 1e-9
    assert (
        stats.range_fraction(low, True, high, False)
        <= stats.range_fraction(low, True, high, True) + 1e-9
    )


def test_text_estimates() -> None:
    words = [f"w{i % 40:02d}" for i in range(4000)]
    stats = collect(ColumnType.TEXT, words, 4000)
    assert abs(stats.equal_fraction("w07") - 1 / 40) < 0.01
    assert abs(stats.range_fraction("w10", True, "w19", True) - 10 / 40) < 0.1


# ---------------------------------------------------------------------- storing


@given(
    data=st.lists(st.one_of(st.none(), st.integers(-(2**40), 2**40)), min_size=0, max_size=400),
    table_id=st.integers(1, 2**31),
    column=st.integers(0, 500),
)
@settings(max_examples=100, deadline=None)
def test_integer_statistics_round_trip(data: list[int | None], table_id: int, column: int) -> None:
    stats = collect(INT, data, len(data))
    assert ColumnStats.decode(stats.encode(table_id, column)) == (table_id, column, stats)


@given(
    data=st.lists(st.one_of(st.none(), st.text(max_size=20)), min_size=0, max_size=200),
)
@settings(max_examples=60, deadline=None)
def test_text_statistics_round_trip(data: list[str | None]) -> None:
    stats = collect(ColumnType.TEXT, data, len(data))
    assert ColumnStats.decode(stats.encode(3, 1))[2] == stats


def test_real_statistics_round_trip() -> None:
    rng = random.Random(4)
    values = [None if rng.random() < 0.1 else round(rng.gauss(0, 5), 2) for _ in range(500)]
    stats = collect(ColumnType.REAL, values, 500)
    assert ColumnStats.decode(stats.encode(9, 2))[2] == stats


def test_fit_thins_statistics_until_they_fit_and_keeps_them_valid() -> None:
    values = [f"text-{i % 300:04d}-{'y' * 30}" for i in range(6000)] + ["hot-value"] * 3000
    stats = collect(ColumnType.TEXT, values, len(values))
    full = len(stats.encode(1, 0))
    for limit in (full, full // 2, 300, 120, 60):
        smaller = fit(stats, 1, 0, limit)
        encoded = smaller.encode(1, 0)
        assert len(encoded) <= limit or (not smaller.common and not smaller.bounds)
        assert ColumnStats.decode(encoded)[2] == smaller
        assert (smaller.rows, smaller.nulls, smaller.distinct) == (
            stats.rows,
            stats.nulls,
            stats.distinct,
        )


def test_damaged_records_are_refused_or_still_consistent() -> None:
    rng = random.Random(6)
    data = [rng.choice([None, 1, 2, 3, 99, 99, 99]) for _ in range(300)] + list(range(40))
    stats = collect(INT, data, len(data))
    good = stats.encode(5, 2)
    for cut in range(len(good)):
        with pytest.raises(CorruptCatalogError):
            ColumnStats.decode(good[:cut])
    for _ in range(400):
        broken = bytearray(good)
        broken[rng.randrange(len(broken))] ^= 1 << rng.randrange(8)
        try:
            ColumnStats.decode(bytes(broken))
        except CorruptCatalogError:
            continue  # refused: fine; anything else (a crash, another error) is not
    with pytest.raises(CorruptCatalogError):
        ColumnStats.decode(good + b"\x00")


def test_few_but_more_than_ten_distinct_values_still_estimate_well() -> None:
    values: list[object] = [v for v in range(15) for _ in range(300)]
    stats = collect(INT, values, len(values))
    assert len(set(stats.bounds)) == len(stats.bounds)  # no repeated bounds
    for low, high in ((0, 4), (3, 3), (10, 14), (-5, 20), (7, 8)):
        want = truth_range(values, low, True, high, True)
        assert abs(stats.range_fraction(low, True, high, True) - want) < 0.07, (low, high)
