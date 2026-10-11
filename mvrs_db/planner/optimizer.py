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
from mvrs_db.index.keys import decode_key
from mvrs_db.sql import ast
from mvrs_db.storage.record import ColumnType, Value

SEQ_PAGE = 1.0
ROW_CPU = 0.28
INDEX_ROW = 0.63
SORT_ROW = 0.005  # per row, per log2(rows)

DEFAULT_EQUALITY = 0.1
DEFAULT_RANGE = 1 / 3
DEFAULT_NOT_EQUAL = 0.9
DEFAULT_IS_NULL = 0.1


def seq_scan_cost(rows: int, pages: int) -> float:
    return pages * SEQ_PAGE + rows * ROW_CPU


def index_scan_cost(matching_rows: float, height: int) -> float:
    return height * SEQ_PAGE + matching_rows * INDEX_ROW


def sort_cost(rows: float) -> float:
    return rows * math.log2(rows + 2) * SORT_ROW


def range_selectivity(table: Table, column: int, key_range: KeyRange, rows: int) -> float:
    """Estimated fraction of rows whose indexed column falls in the range."""
    if rows <= 0:
        return 0.0
    if key_range.is_unbounded:
        return 1.0
    if key_range.is_point:
        return 1.0 / rows  # the index is unique
    low, high = key_range.low, key_range.high
    if low is not None and high is not None and _greater(low, high):
        return 0.0
    column_type = table.schema.columns[column].type
    if column_type is ColumnType.TEXT:
        return DEFAULT_RANGE if (low is None or high is None) else DEFAULT_RANGE**2

    tree = table.indexes[column]
    first, last = tree.first_key(), tree.last_key()
    if first is None or last is None:
        return 0.0
    smallest = float(decode_key(column_type, first))
    largest = float(decode_key(column_type, last))
    if largest <= smallest:
        return 1.0
    start = smallest if low is None else max(smallest, float(low))
    end = largest if high is None else min(largest, float(high))
    if end < start:
        return 0.0
    fraction = (end - start) / (largest - smallest)
    return min(1.0, max(fraction, 1.0 / rows))


def _greater(a: Value, b: Value) -> bool:
    return a > b  # type: ignore[operator]


def default_selectivity(expr: ast.Expr, table: Table, rows: int) -> float:
    """A guess for a condition the index statistics cannot help with."""
    if isinstance(expr, ast.Compare):
        if expr.op == "=":
            for operand in (expr.left, expr.right):
                if isinstance(operand, ast.ColumnRef):
                    position = table.schema.column_index(operand.name)
                    if position is not None and position in table.indexes and rows > 0:
                        return 1.0 / rows
            return DEFAULT_EQUALITY
        if expr.op == "!=":
            return DEFAULT_NOT_EQUAL
        return DEFAULT_RANGE
    if isinstance(expr, ast.IsNull):
        return 1.0 - DEFAULT_IS_NULL if expr.negated else DEFAULT_IS_NULL
    if isinstance(expr, ast.Not):
        return 1.0 - default_selectivity(expr.operand, table, rows)
    left = default_selectivity(expr.left, table, rows)
    right = default_selectivity(expr.right, table, rows)
    if isinstance(expr, ast.And):
        return left * right
    return left + right - left * right
