"""Cost model and selectivity estimates.

The optimiser answers one question: for this WHERE clause, is it cheaper to read
the whole table, or to use an index and fetch only the matching rows?

Costs are in abstract units where 1.0 is roughly "process one heap page".

    sequential scan   pages * SEQ_PAGE + rows * ROW_CPU
    index scan        height * SEQ_PAGE + matching_rows * INDEX_ROW

A sequential scan touches every row but does so cheaply, a page at a time. An
index scan touches only matching rows, but each one costs a separate heap
lookup. So the index wins when few rows match and loses when many do. The
break-even point depends on the ratio INDEX_ROW / (cost per row of a scan).

The constants are measured, not guessed. benchmarks/calibrate_optimizer.py times
scans of a narrow and a wide table to separate the per-page cost from the
per-row cost, then times index scans. On the development machine (i5-11320H,
Python 3.13, warm buffer pool) repeated runs gave ROW_CPU 0.24-0.31, INDEX_ROW
0.56-0.69 and SORT_ROW about 0.005; the values below are in the middle of those
ranges. Fetching a row through the index costs a little over twice what a
sequential scan pays per row, so the index wins until roughly a third to a half
of the table matches. The "crossover" section of the benchmark report checks
the planner's choices against measured times.

These numbers describe a warm cache, where a "random" heap fetch is a
dictionary lookup, not a disk seek. On a cold cache with a spinning disk the
index would lose much sooner; the model does not account for that.

Selectivity is the estimated fraction of rows a condition keeps:

    equality on a unique column     1 / rows
    range on an indexed number      interpolated between the index's smallest
                                    and largest key, assuming values are spread
                                    evenly (often wrong for skewed data)
    anything else                   fixed guesses, as in most textbooks
"""

from __future__ import annotations

import math

from mvrs_db.catalog.catalog import Table
from mvrs_db.execution.operators import KeyRange
from mvrs_db.sql import ast
from mvrs_db.storage.record import ColumnType, Value

SEQ_PAGE = 1.0
ROW_CPU = 0.28
INDEX_ROW = 0.63
SORT_ROW = 0.005  # per row, per log2(rows)
# Joins, measured with benchmarks/join_methods.py on the development machine:
# a hash join spends about 0.45 microseconds per row built or probed, and an
# index join about 20 microseconds per outer row against an index of height 2
# (a tree descent, a heap fetch and a row decode). In sequential-page units
# that is 0.085 and about 3.7. Only one index height was measured, so the split
# between the fixed part and the per-level part is an assumption.
HASH_ROW = 0.085  # per row built or probed
INDEX_LOOKUP = 2.6  # per outer row: fetch and decode the matching inner row
INDEX_LEVEL = 0.55  # per outer row, per level of the inner index
# Per vector element, for an exact nearest-neighbour scan. Measured with
# benchmarks/vector_search.py: about 10 microseconds per 768-element row on the
# raw NumPy path, which is roughly 0.002 sequential pages per element. There is
# only one plan for such a query, so this number affects the cost EXPLAIN shows
# and nothing else.
VECTOR_ELEMENT = 0.002
# Vectors measured per candidate kept during an HNSW search. An estimate for
# EXPLAIN only; the planner's choice between exact and approximate search is
# made by rules (see planner._plan_nearest), not by comparing costs.
HNSW_FANOUT = 8

DEFAULT_EQUALITY = 0.1
# How many entries to count when estimating an equality match on a secondary index.
EQUALITY_PROBE = 64
DEFAULT_RANGE = 1 / 3
DEFAULT_NOT_EQUAL = 0.9
DEFAULT_IS_NULL = 0.1


def seq_scan_cost(rows: int, pages: int) -> float:
    return pages * SEQ_PAGE + rows * ROW_CPU


def index_scan_cost(matching_rows: float, height: int) -> float:
    return height * SEQ_PAGE + matching_rows * INDEX_ROW


def sort_cost(rows: float) -> float:
    return rows * math.log2(rows + 2) * SORT_ROW


def vector_scan_cost(rows: float, dimensions: int) -> float:
    """Extra cost of measuring the distance from a query vector to every candidate row."""
    return rows * dimensions * VECTOR_ELEMENT


def hash_join_cost(left_rows: float, right_rows: float) -> float:
    """Building and probing a hash table: one dictionary operation per row of each input."""
    return (left_rows + right_rows) * HASH_ROW


def index_join_cost(outer_rows: float, height: int) -> float:
    """One index lookup and one row fetch for every outer row."""
    return outer_rows * (INDEX_LOOKUP + height * INDEX_LEVEL)


def vector_index_cost(ef: int, dimensions: int) -> float:
    """Rough cost of an HNSW search: it measures on the order of ef * HNSW_FANOUT vectors.

    The point of the index is that this does not depend on the table size.
    """
    return ef * HNSW_FANOUT * dimensions * VECTOR_ELEMENT


def range_selectivity(table: Table, column: int, key_range: KeyRange, rows: int) -> float:
    """Estimated fraction of rows whose indexed column falls in the range."""
    if rows <= 0:
        return 0.0
    if key_range.is_unbounded:
        return 1.0
    stats = table.stats.get(column)
    if key_range.is_point:
        if table.is_unique(column):
            return 1.0 / rows
        if isinstance(key_range.low, ast.Param):
            # The value is not known until the statement runs, so every value has to
            # be treated alike: the average share. Without statistics there is no
            # such average; the planner refuses to build a reusable plan then.
            average = stats.average_equal_fraction() if stats is not None else DEFAULT_EQUALITY
            return min(1.0, max(average, 1.0 / rows))
        # A secondary index can hold a value many times. Count its entries, up to
        # a cap: an exact answer for rare values. A value that reaches the cap is
        # common; its share comes from the statistics if there are any, else it is
        # assumed to be a default share of the table.
        matches = table.count_equal(column, key_range.low, EQUALITY_PROBE)
        if matches < EQUALITY_PROBE:
            return min(1.0, max(matches, 1) / rows)
        if stats is not None:
            return min(1.0, max(stats.equal_fraction(key_range.low), EQUALITY_PROBE / rows))
        return min(1.0, max(float(EQUALITY_PROBE), rows * DEFAULT_EQUALITY) / rows)
    low, high = key_range.low, key_range.high
    if low is not None and high is not None and _greater(low, high):
        return 0.0
    if stats is not None and stats.rows:
        fraction = stats.range_fraction(
            low, key_range.low_inclusive, high, key_range.high_inclusive
        )
        return min(1.0, max(fraction, 1.0 / rows))
    column_type = table.schema.columns[column].type
    if column_type is ColumnType.TEXT:
        return DEFAULT_RANGE if (low is None or high is None) else DEFAULT_RANGE**2

    tree = table.indexes[column]
    first, last = tree.first_key(), tree.last_key()
    if first is None or last is None:
        return 0.0
    smallest = float(table.index_value(column, first))  # type: ignore[arg-type]
    largest = float(table.index_value(column, last))  # type: ignore[arg-type]
    if largest <= smallest:
        return 1.0
    # Only INTEGER and REAL columns reach this point, so the bounds are numbers.
    start = smallest if low is None else max(smallest, float(low))  # type: ignore[arg-type]
    end = largest if high is None else min(largest, float(high))  # type: ignore[arg-type]
    if end < start:
        return 0.0
    fraction = (end - start) / (largest - smallest)
    return min(1.0, max(fraction, 1.0 / rows))


def _greater(a: Value, b: Value) -> bool:
    return a > b  # type: ignore[operator]


_FLIPPED = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "!=": "!="}


def _column_versus_literal(expr: ast.Compare, table: Table) -> tuple[int, str, Value] | None:
    """(column position, operator, literal) for `column op literal` or `literal op column`."""
    left, right, op = expr.left, expr.right, expr.op
    if isinstance(left, ast.Literal) and isinstance(right, ast.ColumnRef):
        left, right, op = right, left, _FLIPPED[op]
    if not (isinstance(left, ast.ColumnRef) and isinstance(right, ast.Literal)):
        return None
    position = table.schema.column_index(left.name)
    return None if position is None else (position, op, right.value)


def _from_statistics(expr: ast.Compare, table: Table, rows: int) -> float | None:
    """Selectivity of a comparison with a literal, from column statistics, if there are any."""
    found = _column_versus_literal(expr, table)
    if found is None:
        return None
    position, op, value = found
    stats = table.stats.get(position)
    if stats is None or not stats.rows or value is None:
        return None
    unknown = isinstance(value, ast.Param)  # a `?`: the value arrives when the plan runs
    if op == "=":
        equal = stats.average_equal_fraction() if unknown else stats.equal_fraction(value)
        return max(equal, 1.0 / max(rows, 1))
    if op == "!=":
        equal = stats.average_equal_fraction() if unknown else stats.equal_fraction(value)
        return max(1.0 - stats.null_fraction - equal, 1.0 / max(rows, 1))
    if unknown:
        return DEFAULT_RANGE
    if op in ("<", "<="):
        fraction = stats.range_fraction(None, True, value, op == "<=")
    else:
        fraction = stats.range_fraction(value, op == ">=", None, True)
    return max(fraction, 1.0 / max(rows, 1))


def default_selectivity(expr: ast.Expr, table: Table, rows: int) -> float:
    """The share of rows a condition keeps, from column statistics where there are
    some and from fixed guesses where there are not."""
    if isinstance(expr, ast.Compare):
        measured = _from_statistics(expr, table, rows)
        if measured is not None and not (
            expr.op == "=" and _is_unique_column(expr, table) and rows > 0
        ):
            return measured
        if expr.op == "=":
            for operand in (expr.left, expr.right):
                if isinstance(operand, ast.ColumnRef):
                    position = table.schema.column_index(operand.name)
                    if position is not None and table.is_unique(position) and rows > 0:
                        return 1.0 / rows
            return DEFAULT_EQUALITY
        if expr.op == "!=":
            return DEFAULT_NOT_EQUAL
        return DEFAULT_RANGE
    if isinstance(expr, ast.IsNull):
        if isinstance(expr.operand, ast.ColumnRef):
            position = table.schema.column_index(expr.operand.name)
            stats = None if position is None else table.stats.get(position)
            if stats is not None and stats.rows:
                share = stats.null_fraction
                return max(1.0 - share if expr.negated else share, 1.0 / max(rows, 1))
        return 1.0 - DEFAULT_IS_NULL if expr.negated else DEFAULT_IS_NULL
    if isinstance(expr, ast.Not):
        return 1.0 - default_selectivity(expr.operand, table, rows)
    left = default_selectivity(expr.left, table, rows)
    right = default_selectivity(expr.right, table, rows)
    if isinstance(expr, ast.And):
        return left * right
    return left + right - left * right


def _is_unique_column(expr: ast.Compare, table: Table) -> bool:
    for operand in (expr.left, expr.right):
        if isinstance(operand, ast.ColumnRef):
            position = table.schema.column_index(operand.name)
            if position is not None and table.is_unique(position):
                return True
    return False
