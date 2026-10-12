"""Per-column statistics: what the planner knows about the data in a column.

Without them the planner guesses: an equality matches a tenth of the rows, a
range a third. With them it can say that `status = 'done'` matches 92% of an
orders table while `status = 'refunded'` matches 0.1%, and choose differently.

What is kept, per column, by ANALYZE (a full scan, so exact at that moment):

    rows       live rows in the table
    nulls      how many of them are NULL
    distinct   how many different non-NULL values there are
    common     the most common values with their exact counts (at most 10;
               only values that occur clearly more often than average)
    bounds     an equi-depth histogram of the *other* values: B+1 ascending
               values that cut them into B buckets holding equal numbers of rows

Why a separate list of common values: a histogram is poor at spikes. If one value
fills 40% of the table, every bucket boundary lands on it and the histogram
cannot say how many rows it has. Listing the few values that matter, exactly,
and spreading the rest over equal-depth buckets, handles skewed data (most real
data) with a few hundred bytes.

What the estimates assume, and where they are wrong:
  * values not in the common list are equally likely (uniform within a bucket
    and across the distinct values left over);
  * columns are independent of each other (the planner multiplies);
  * the statistics are a snapshot. They describe the table as it was when
    ANALYZE ran, and are not updated by inserts, updates or deletes.

On disk each column's statistics are one record in the catalog heap, written by
ANALYZE inside its transaction (see catalog.py); the layout is below.
"""

from __future__ import annotations

import struct
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from mvrs_db.errors import CorruptCatalogError
from mvrs_db.storage.record import ColumnType, Value

STATS_RECORD_FORMAT = 2  # the catalog's table records are format 1
MAX_COMMON = 10
HISTOGRAM_BUCKETS = 20
# Text longer than this is not kept in the common list and is cut short in the
# histogram bounds, so one long string cannot make a record too big to store.
MAX_TEXT = 48

# [format u8][table_id u32][column u16][type u8][rows u64][nulls u64][distinct u64]
#   [common_count u8] { value, count u64 }...  [bound_count u8] value...
# value: INTEGER i64 | REAL f64 | TEXT [len u8][utf-8 bytes]
_HEAD = struct.Struct("<BIHBQQQ")
_I64 = struct.Struct("<q")
_F64 = struct.Struct("<d")
_U64 = struct.Struct("<Q")


@dataclass(frozen=True, slots=True)
class ColumnStats:
    column_type: ColumnType
    rows: int
    nulls: int
    distinct: int
    common: tuple[tuple[Value, int], ...] = ()
    bounds: tuple[Value, ...] = ()

    # ------------------------------------------------------------------ derived

    @property
    def non_null(self) -> int:
        return self.rows - self.nulls

    @property
    def null_fraction(self) -> float:
        return self.nulls / self.rows if self.rows else 0.0

    @property
    def _histogram_rows(self) -> int:
        """Rows that are neither NULL nor one of the common values."""
        return self.non_null - sum(count for _value, count in self.common)

    @property
    def _histogram_distinct(self) -> int:
        return self.distinct - len(self.common)

    # ------------------------------------------------------------------ estimates

    def average_equal_fraction(self) -> float:
        """Fraction of rows equal to a value that is not known yet (a `?`).

        Every distinct value is taken to be equally likely, so this is the same
        for any value. That is what lets a plan built for a placeholder be reused.
        """
        if not self.distinct or not self.rows:
            return 0.0
        return self.non_null / self.distinct / self.rows

    def equal_fraction(self, value: Value) -> float:
        """Fraction of rows equal to `value`."""
        if not self.rows:
            return 0.0
        for common_value, count in self.common:
            if common_value == value:
                return count / self.rows
        left = self._histogram_distinct
        if left <= 0:
            return 0.0  # every value is in the common list and this is not one of them
        return self._histogram_rows / left / self.rows

    def range_fraction(
        self, low: Value, low_inclusive: bool, high: Value, high_inclusive: bool
    ) -> float:
        """Fraction of rows whose value lies between `low` and `high` (None: unbounded)."""
        if not self.rows:
            return 0.0
        total = 0.0
        for value, count in self.common:
            if _within(value, low, low_inclusive, high, high_inclusive):
                total += count
        hist = self._histogram_rows
        if hist > 0:
            upper = 1.0 if high is None else self._below(high, high_inclusive)
            lower = 0.0 if low is None else self._below(low, not low_inclusive)
            total += hist * max(0.0, upper - lower)
        return min(1.0, total / self.rows)

    def _below(self, value: Value, inclusive: bool) -> float:
        """Share of the histogram's rows that are < value (or <= value if inclusive)."""
        bounds = self.bounds
        if len(bounds) < 2:
            return 0.0
        buckets = len(bounds) - 1
        if value < bounds[0]:  # type: ignore[operator]
            return 0.0
        if value > bounds[-1]:  # type: ignore[operator]
            return 1.0
        # Last bucket whose lower bound is <= value.
        index = 0
        while index + 1 < buckets and bounds[index + 1] <= value:  # type: ignore[operator]
            index += 1
        low, high = bounds[index], bounds[index + 1]
        if isinstance(value, str) or not isinstance(low, (int, float)) or high == low:
            fraction = 0.5 if (high != low and value != low) else 0.0
        else:
            fraction = (float(value) - float(low)) / (float(high) - float(low))  # type: ignore[arg-type]
        share = (index + min(max(fraction, 0.0), 1.0)) / buckets
        if inclusive:
            share += self._histogram_equal_share()
        return min(share, 1.0)

    def _histogram_equal_share(self) -> float:
        """Share of the histogram's rows that one value accounts for (for `<=`)."""
        left = self._histogram_distinct
        return 1.0 / left if left > 0 else 0.0

    # ------------------------------------------------------------------ encoding

    def encode(self, table_id: int, column: int) -> bytes:
        parts = [
            _HEAD.pack(
                STATS_RECORD_FORMAT,
                table_id,
                column,
                int(self.column_type),
                self.rows,
                self.nulls,
                self.distinct,
            ),
            bytes((len(self.common),)),
        ]
        for value, count in self.common:
            parts.append(_encode_value(self.column_type, value))
            parts.append(_U64.pack(count))
        parts.append(bytes((len(self.bounds),)))
        parts.extend(_encode_value(self.column_type, value) for value in self.bounds)
        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> tuple[int, int, ColumnStats]:
        """(table id, column position, statistics). Raises CorruptCatalogError if malformed."""
        try:
            fmt, table_id, column, raw_type, rows, nulls, distinct = _HEAD.unpack_from(data, 0)
            if fmt != STATS_RECORD_FORMAT:
                raise CorruptCatalogError(f"unknown statistics record format {fmt}")
            column_type = ColumnType(raw_type)
            if column_type is ColumnType.VECTOR:
                raise CorruptCatalogError("statistics for a vector column")
            position = _HEAD.size
            common_count = data[position]
            position += 1
            common = []
            for _ in range(common_count):
                value, position = _decode_value(column_type, data, position)
                (count,) = _U64.unpack_from(data, position)
                position += _U64.size
                common.append((value, count))
            bound_count = data[position]
            position += 1
            bounds = []
            for _ in range(bound_count):
                value, position = _decode_value(column_type, data, position)
                bounds.append(value)
        except (struct.error, ValueError, IndexError, UnicodeDecodeError) as exc:
            raise CorruptCatalogError(f"statistics record cannot be decoded: {exc}") from exc
        if position != len(data):
            raise CorruptCatalogError("statistics record has unexpected trailing bytes")
        if nulls > rows or distinct > rows - nulls or bound_count == 1:
            raise CorruptCatalogError("statistics record is inconsistent")
        if sum(count for _v, count in common) > rows - nulls or len(common) > distinct:
            raise CorruptCatalogError("statistics record is inconsistent")
        if any(a > b for a, b in zip(bounds, bounds[1:], strict=False)):  # type: ignore[operator]
            raise CorruptCatalogError("statistics histogram is not ascending")
        return (
            table_id,
            column,
            cls(column_type, rows, nulls, distinct, tuple(common), tuple(bounds)),
        )


def _within(
    value: Value, low: Value, low_inclusive: bool, high: Value, high_inclusive: bool
) -> bool:
    if value is None:
        return False
    if low is not None and (value < low or (value == low and not low_inclusive)):  # type: ignore[operator]
        return False
    return not (
        high is not None and (value > high or (value == high and not high_inclusive))  # type: ignore[operator]
    )


def _encode_value(column_type: ColumnType, value: Value) -> bytes:
    if column_type is ColumnType.INTEGER:
        return _I64.pack(value)
    if column_type is ColumnType.REAL:
        return _F64.pack(value)
    raw = str(value).encode("utf-8")
    return bytes((len(raw),)) + raw


def _decode_value(column_type: ColumnType, data: bytes, position: int) -> tuple[Value, int]:
    if column_type is ColumnType.INTEGER:
        return _I64.unpack_from(data, position)[0], position + _I64.size
    if column_type is ColumnType.REAL:
        return _F64.unpack_from(data, position)[0], position + _F64.size
    length = data[position]
    end = position + 1 + length
    if end > len(data):
        raise ValueError("text runs past the end of the record")
    return data[position + 1 : end].decode("utf-8"), end


def _clip(value: str) -> str:
    """A text value cut to at most MAX_TEXT bytes, on a character boundary."""
    raw = value.encode("utf-8")
    if len(raw) <= MAX_TEXT:
        return value
    return raw[:MAX_TEXT].decode("utf-8", "ignore")


def collect(column_type: ColumnType, values: Iterable[Value], rows: int) -> ColumnStats:
    """Statistics for one column from all of its values (None for NULL), by a full count."""
    counts: Counter[Value] = Counter(value for value in values if value is not None)
    non_null = sum(counts.values())
    nulls = rows - non_null
    distinct = len(counts)
    if not distinct:
        return ColumnStats(column_type, rows, nulls, 0)

    if distinct <= MAX_COMMON and all(
        not (isinstance(value, str) and len(value.encode("utf-8")) > MAX_TEXT) for value in counts
    ):
        # Few distinct values: list every one with its exact count. A histogram of a
        # handful of values would be all duplicate bounds, and estimates from it poor.
        everything = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ColumnStats(column_type, rows, nulls, distinct, tuple(everything))

    average = non_null / distinct
    candidates = [
        (value, count)
        for value, count in counts.items()
        if count >= 2
        and count > 1.25 * average
        and not (isinstance(value, str) and len(value.encode("utf-8")) > MAX_TEXT)
    ]
    candidates.sort(key=lambda item: (-item[1], item[0]))
    common = tuple(candidates[:MAX_COMMON])
    common_values = {value for value, _count in common}

    rest = sorted(
        ((value, count) for value, count in counts.items() if value not in common_values),
        key=lambda item: item[0],  # type: ignore[arg-type,return-value]
    )
    bounds: tuple[Value, ...] = ()
    rest_rows = sum(count for _value, count in rest)
    if len(rest) >= 2:
        # Never more buckets than gaps between distinct values, or bounds repeat.
        buckets = min(HISTOGRAM_BUCKETS, len(rest) - 1, rest_rows)
        picked: list[Value] = [rest[0][0]]
        cumulative = 0
        position = 0
        for k in range(1, buckets):
            target = k * rest_rows / buckets
            while position < len(rest) and cumulative + rest[position][1] < target:
                cumulative += rest[position][1]
                position += 1
            picked.append(rest[min(position, len(rest) - 1)][0])
        picked.append(rest[-1][0])
        bounds = tuple(_clip(v) if isinstance(v, str) else v for v in picked)
    return ColumnStats(column_type, rows, nulls, distinct, common, bounds)


def fit(stats: ColumnStats, table_id: int, column: int, limit: int) -> ColumnStats:
    """The statistics, thinned until their record fits in `limit` bytes.

    A small page cannot hold a full record. Dropping common values and histogram
    bounds makes the estimates coarser, never wrong.
    """
    current = stats
    while len(current.encode(table_id, column)) > limit:
        if len(current.bounds) > 3:
            keep = max(2, (len(current.bounds) - 1) // 2)
            step = (len(current.bounds) - 1) / keep
            indexes = sorted({round(i * step) for i in range(keep + 1)})
            current = ColumnStats(
                current.column_type,
                current.rows,
                current.nulls,
                current.distinct,
                current.common,
                tuple(current.bounds[i] for i in indexes),
            )
        elif current.common:
            current = ColumnStats(
                current.column_type,
                current.rows,
                current.nulls,
                current.distinct,
                current.common[:-1],
                current.bounds,
            )
        else:
            current = ColumnStats(
                current.column_type, current.rows, current.nulls, current.distinct
            )
            break
    return current
