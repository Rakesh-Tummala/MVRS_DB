"""Turns a checked statement into a tree of operators.

Planning a single-table query has three decisions:

1. Access path. Read the whole table (SeqScan) or use an index (IndexScan)?
   Conditions of the form `indexed_column <op> literal` that are joined by AND
   are collected into a key range per indexed column. Each candidate is costed
   with the model in optimizer.py and the cheapest wins.

2. Ordering. An index scan returns rows in key order. If the query's ORDER BY
   is exactly that column ascending, no sort is needed. With a LIMIT, walking
   the index in order and stopping early can beat scanning and sorting
   everything, so that plan is costed too.

3. Shape. The filter sits directly above the scan, so rows are discarded before
   they are sorted or projected. ORDER BY with LIMIT becomes TopN, which keeps
   only the best n rows instead of sorting all of them.

The full WHERE clause is always re-applied as a Filter above an index scan. The
index only narrows the candidates; the filter decides. That costs a little and
removes any chance of the range extraction changing a query's meaning.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from mvrs_db import config
from mvrs_db.catalog.catalog import Table
from mvrs_db.errors import SqlError, TypeMismatchError
from mvrs_db.execution import expressions
from mvrs_db.execution.operators import (
    DistanceSpec,
    Filter,
    IndexScan,
    KeyRange,
    Limit,
    Operator,
    Project,
    SeqScan,
    Sort,
    SortKey,
    TopN,
    VectorTopK,
)
from mvrs_db.planner import optimizer
from mvrs_db.sql import ast
from mvrs_db.storage.record import ColumnType, Value
from mvrs_db.vector import distance

_FLIPPED = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


@dataclass(slots=True)
class _Bounds:
    low: Value = None
    high: Value = None
    low_inclusive: bool = True
    high_inclusive: bool = True

    def tighten_low(self, value: Value, inclusive: bool) -> None:
        if self.low is None or value > self.low:  # type: ignore[operator]
            self.low, self.low_inclusive = value, inclusive
        elif value == self.low:
            self.low_inclusive = self.low_inclusive and inclusive

    def tighten_high(self, value: Value, inclusive: bool) -> None:
        if self.high is None or value < self.high:  # type: ignore[operator]
            self.high, self.high_inclusive = value, inclusive
        elif value == self.high:
            self.high_inclusive = self.high_inclusive and inclusive

    def to_range(self) -> KeyRange:
        return KeyRange(self.low, self.high, self.low_inclusive, self.high_inclusive)


def _index_value(column_type: ColumnType, value: Value) -> Value:
    """Convert a literal to the indexed column's type, or None if it cannot be a key.

    Returning None only means "do not use the index for this condition"; the
    Filter still evaluates the original comparison exactly.
    """
    if value is None:
        return None
    if column_type is ColumnType.INTEGER:
        return value if type(value) is int else None
    if column_type is ColumnType.REAL:
        if type(value) is float:
            return value
        if type(value) is int and float(value) == value:
            return float(value)
        return None
    if type(value) is str and len(value.encode("utf-8", "surrogatepass")) <= config.MAX_KEY_SIZE:
        return value
    return None


def extract_ranges(table: Table, where: ast.Expr | None) -> tuple[dict[int, KeyRange], set[int]]:
    """Key ranges implied by the WHERE clause, per indexed column.

    Also returns the positions (in the list of conjuncts) that became part of a
    range, so their selectivity is not counted twice.
    """
    bounds: dict[int, _Bounds] = {}
    used: set[int] = set()
    schema = table.schema
    for number, conjunct in enumerate(expressions.conjuncts(where)):
        if not isinstance(conjunct, ast.Compare) or conjunct.op == "!=":
            continue
        left, right, op = conjunct.left, conjunct.right, conjunct.op
        if isinstance(left, ast.Literal) and isinstance(right, ast.ColumnRef):
            left, right, op = right, left, _FLIPPED[op]
        if not (isinstance(left, ast.ColumnRef) and isinstance(right, ast.Literal)):
            continue
        position = schema.column_index(left.name)
        if position is None or position not in table.indexes:
            continue
        value = _index_value(schema.columns[position].type, right.value)
        if value is None:
            continue
        entry = bounds.setdefault(position, _Bounds())
        if op == "=":
            entry.tighten_low(value, True)
            entry.tighten_high(value, True)
        elif op in (">", ">="):
            entry.tighten_low(value, op == ">=")
        else:
            entry.tighten_high(value, op == "<=")
        used.add(number)
    return {position: entry.to_range() for position, entry in bounds.items()}, used


def plan_access(
    table: Table,
    where: ast.Expr | None,
    *,
    order_column: int | None = None,
    limit: int | None = None,
) -> tuple[Operator, bool]:
    """Choose how to find the rows matching `where`.

    `order_column` is a column the caller would like the rows ordered by
    (ascending). Returns the operator and whether its output is in that order.
    """
    schema = table.schema
    predicate = None
    if where is not None:
        expressions.check(where, schema)
        predicate = expressions.compile_predicate(where, schema)

    rows, pages = table.ensure_stats()
    ranges, used = extract_ranges(table, where)

    # Estimated fraction of rows that survive the whole WHERE clause.
    selectivity = 1.0
    for position, key_range in ranges.items():
        selectivity *= optimizer.range_selectivity(table, position, key_range, rows)
    for number, conjunct in enumerate(expressions.conjuncts(where)):
        if number not in used:
            selectivity *= optimizer.default_selectivity(conjunct, table, rows)
    output_rows = rows * selectivity

    best: Operator = SeqScan(table)
    best.est_rows = float(rows)
    best.cost = optimizer.seq_scan_cost(rows, pages)
    best_ordered = False
    best_total = best.cost

    def consider(scan: IndexScan, scanned_rows: float, ordered: bool, total: float) -> None:
        nonlocal best, best_ordered, best_total
        scan.est_rows = scanned_rows
        scan.cost = optimizer.index_scan_cost(scanned_rows, table.indexes[scan.column].height())
        if total < best_total:
            best, best_ordered, best_total = scan, ordered, total

    sort_penalty = optimizer.sort_cost(output_rows) if order_column is not None else 0.0
    best_total += sort_penalty

    for position, key_range in ranges.items():
        matching = rows * optimizer.range_selectivity(table, position, key_range, rows)
        ordered = position == order_column
        height = table.indexes[position].height()
        total = optimizer.index_scan_cost(matching, height) + (0.0 if ordered else sort_penalty)
        consider(IndexScan(table, position, key_range), matching, ordered, total)

    # Walking an index in key order to satisfy ORDER BY, stopping early at LIMIT.
    # Only valid when every row is in the index, i.e. the column cannot be NULL.
    if (
        order_column is not None
        and order_column in table.indexes
        and order_column not in ranges
        and schema.columns[order_column].not_null
    ):
        needed = float(rows)
        if limit is not None:
            needed = min(float(rows), limit / max(selectivity, 1.0 / max(rows, 1)))
        height = table.indexes[order_column].height()
        consider(
            IndexScan(table, order_column, KeyRange()),
            needed,
            True,
            optimizer.index_scan_cost(needed, height),
        )

    if predicate is not None and where is not None:
        filtered = Filter(best, predicate, expressions.format_expr(where))
        filtered.est_rows = output_rows
        filtered.cost = best.cost
        return filtered, best_ordered
    return best, best_ordered


def plan_select(table: Table, statement: ast.Select) -> tuple[Operator, tuple[str, ...]]:
    """Build the operator tree for a SELECT and return it with its output column names."""
    schema = table.schema
    names: tuple[str, ...]
    items: list[int | DistanceSpec]
    if statement.columns is None:
        names = schema.column_names
        items = list(range(len(names)))
    else:
        names = tuple(
            "distance" if isinstance(column, ast.Distance) else column
            for column in statement.columns
        )
        items = [
            distance_spec(table, column)
            if isinstance(column, ast.Distance)
            else expressions.column_position(schema, column)
            for column in statement.columns
        ]

    if any(isinstance(item.column, ast.Distance) for item in statement.order_by):
        return _plan_nearest(table, statement, items, names), names

    keys: list[SortKey] = [
        (expressions.column_position(schema, str(item.column)), item.descending)
        for item in statement.order_by
    ]
    for position, _descending in keys:
        if schema.columns[position].type is ColumnType.VECTOR:
            raise TypeMismatchError(
                f"cannot ORDER BY {schema.columns[position].name}: vectors have no order"
            )
    order_column = keys[0][0] if len(keys) == 1 and not keys[0][1] else None

    plan, ordered = plan_access(
        table, statement.where, order_column=order_column, limit=statement.limit
    )
    limit = statement.limit

    if keys and not ordered:
        child = plan
        if limit is not None:
            plan = TopN(child, keys, limit, schema.column_names)
            plan.est_rows = min(child.est_rows, float(limit))
        else:
            plan = Sort(child, keys, schema.column_names)
            plan.est_rows = child.est_rows
        plan.cost = child.cost + optimizer.sort_cost(child.est_rows)
    elif limit is not None:
        child = plan
        plan = Limit(child, limit)
        plan.est_rows = min(child.est_rows, float(limit))
        plan.cost = child.cost

    child = plan
    plan = Project(child, items, names)
    plan.est_rows, plan.cost = child.est_rows, child.cost
    return plan, names


def distance_spec(table: Table, node: ast.Distance) -> DistanceSpec:
    """Check a `column <-> [vector]` expression against the table."""
    schema = table.schema
    position = expressions.column_position(schema, node.column)
    column = schema.columns[position]
    symbol = node.operator
    if column.type is not ColumnType.VECTOR:
        raise TypeMismatchError(
            f"{symbol} needs a VECTOR column, but {schema.name}.{column.name} is {column.type.name}"
        )
    if len(node.vector) != column.dimension:
        raise TypeMismatchError(
            f"{schema.name}.{column.name} is VECTOR({column.dimension}), but the vector "
            f"after {symbol} has {len(node.vector)} numbers"
        )
    try:
        query = distance.as_float32(node.vector)
    except (OverflowError, struct.error):
        raise TypeMismatchError(
            f"a number in the vector after {symbol} is too large for a 32-bit float"
        ) from None
    metric = distance.OPERATORS[symbol]
    if metric == distance.COSINE and distance.is_zero(query):
        raise TypeMismatchError(
            "cosine distance (<=>) to an all-zero vector is undefined: it has no direction"
        )
    return DistanceSpec(position, column.name, metric, query)


def _plan_nearest(
    table: Table, statement: ast.Select, items: list[int | DistanceSpec], names: tuple[str, ...]
) -> Operator:
    """ORDER BY a vector distance: scan the candidates, keep the k best.

    WHERE is applied first, so the search is over the rows that pass the filter.
    There is no vector index yet, which makes this exact and linear in the
    number of candidate rows.
    """
    if len(statement.order_by) != 1:
        raise SqlError("ORDER BY a vector distance cannot be combined with other sort keys")
    item = statement.order_by[0]
    assert isinstance(item.column, ast.Distance)
    spec = distance_spec(table, item.column)

    child, _ordered = plan_access(table, statement.where)
    # With no WHERE clause nothing needs the decoded rows, so the stored bytes
    # can go straight to NumPy and only the winners are decoded.
    raw_table = table if statement.where is None and distance.numpy_available() else None
    plan: Operator = VectorTopK(child, spec, statement.limit, item.descending, raw_table)
    kept = child.est_rows if statement.limit is None else min(child.est_rows, statement.limit)
    plan.est_rows = float(kept)
    plan.cost = child.cost + optimizer.vector_scan_cost(child.est_rows, len(spec.query))

    project = Project(plan, items, names)
    project.est_rows, project.cost = plan.est_rows, plan.cost
    return project
