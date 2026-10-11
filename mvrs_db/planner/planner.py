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
from collections.abc import Sequence
from dataclasses import dataclass, replace

from mvrs_db import config
from mvrs_db.catalog.catalog import Table
from mvrs_db.catalog.schema import TableSchema
from mvrs_db.errors import InternalError, SqlError, TypeMismatchError
from mvrs_db.execution import expressions
from mvrs_db.execution.operators import (
    AggregateSpec,
    DistanceSpec,
    Filter,
    HashAggregate,
    HashJoin,
    IndexJoin,
    IndexScan,
    KeyRange,
    Limit,
    MetadataAggregate,
    Operator,
    Project,
    SeqScan,
    Sort,
    SortKey,
    TopN,
    VectorIndexScan,
    VectorTopK,
)
from mvrs_db.planner import optimizer
from mvrs_db.planner.joins import join_order, narrow_kinds, null_supplied
from mvrs_db.planner.scope import (
    Scope,
    Source,
    columns_in,
    display_names,
    rename_expr,
    rename_select,
)
from mvrs_db.sql import ast
from mvrs_db.storage.record import ColumnType, Value
from mvrs_db.vector import distance
from mvrs_db.vector.hnsw import SearchSettings

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


def _is_aggregate_query(statement: ast.Select) -> bool:
    selected = statement.columns or ()
    return bool(statement.group_by) or any(
        isinstance(item, ast.Aggregate)
        for item in (*selected, *(order.column for order in statement.order_by))
    )


def plan_select(
    table: Table,
    statement: ast.Select,
    vector_settings: SearchSettings | None = None,
    joined: Sequence[Table] = (),
) -> tuple[Operator, tuple[str, ...]]:
    """Build the operator tree for a SELECT and return it with its output column names.

    Column names are resolved first (see scope.py). After that, a one-table
    query is planned with bare column names and a join with `alias.column`
    names, and neither path has to think about qualifiers again.
    """
    if statement.joins:
        if len(joined) != len(statement.joins):
            raise InternalError("a join was planned without all of its tables")
        return _plan_join([table, *joined], statement)
    scope = Scope([Source(statement.alias or table.schema.name, table)])
    names = display_names(statement, table.schema.column_names)
    plan, _plain_names = _plan_single(table, rename_select(statement, scope.plain), vector_settings)
    return plan, names


def unqualified_where(table: Table, where: ast.Expr | None) -> ast.Expr | None:
    """A WHERE clause for UPDATE or DELETE with any `table.column` reduced to `column`."""
    if where is None:
        return None
    return rename_expr(where, Scope([Source(table.schema.name, table)]).plain)


def _plan_single(
    table: Table, statement: ast.Select, vector_settings: SearchSettings | None
) -> tuple[Operator, tuple[str, ...]]:
    schema = table.schema
    if _is_aggregate_query(statement):
        return _plan_aggregate(table, schema, statement)

    names: tuple[str, ...]
    items: list[int | DistanceSpec]
    if statement.columns is None:
        names = schema.column_names
        items = list(range(len(names)))
    else:
        names = tuple(
            "distance" if isinstance(column, ast.Distance) else str(column)
            for column in statement.columns
        )
        items = [
            distance_spec(table, column)
            if isinstance(column, ast.Distance)
            else expressions.column_position(schema, str(column))
            for column in statement.columns
        ]

    if any(isinstance(item.column, ast.Distance) for item in statement.order_by):
        settings = vector_settings or SearchSettings()
        return _plan_nearest(table, statement, items, names, settings), names

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


def _aggregate_spec(schema: TableSchema, node: ast.Aggregate) -> AggregateSpec:
    """Check an aggregate against the input: the column exists and has a usable type."""
    if node.column is None:
        return AggregateSpec(node.function, None, node.label)
    position = expressions.column_position(schema, node.column)
    column_type = schema.columns[position].type
    if node.function in ("SUM", "AVG") and column_type not in (
        ColumnType.INTEGER,
        ColumnType.REAL,
    ):
        raise TypeMismatchError(
            f"{node.function} needs a numeric column, but {schema.name}.{node.column} "
            f"is {column_type.name}"
        )
    if node.function in ("MIN", "MAX") and column_type is ColumnType.VECTOR:
        raise TypeMismatchError(f"{node.function} cannot be applied to a vector column")
    return AggregateSpec(node.function, position, node.label)


def _plan_aggregate(
    table: Table | None,
    schema: TableSchema,
    statement: ast.Select,
    source: Operator | None = None,
) -> tuple[Operator, tuple[str, ...]]:
    """A SELECT with GROUP BY or aggregate functions.

    The input is either one table (`table` given: the WHERE clause is planned
    here, and the metadata shortcut is possible) or an already-built operator
    such as a join (`source` given; `schema` describes its rows).

    The aggregate operator turns the table's rows into rows shaped
    (group columns..., aggregate results...). ORDER BY and LIMIT then apply to
    those rows, and a final projection puts the columns in the order asked for.

    The rule SQL enforces, and this function checks: every selected column must
    either be listed in GROUP BY or be inside an aggregate. Otherwise a group of
    many rows would have to yield one value for that column, and there is no
    right way to pick it.
    """
    if statement.columns is None:
        raise SqlError(
            "SELECT * cannot be used with GROUP BY or aggregate functions; name the columns"
        )

    group_positions: list[int] = []
    for name in statement.group_by:
        position = expressions.column_position(schema, name)
        if schema.columns[position].type is ColumnType.VECTOR:
            raise TypeMismatchError(f"cannot GROUP BY {name}: vectors cannot be compared")
        if position in group_positions:
            raise SqlError(f"column {name} is listed more than once in GROUP BY")
        group_positions.append(position)

    specs: list[AggregateSpec] = []
    slot_of: dict[ast.Aggregate, int] = {}

    def output_position(item: ast.SelectItem) -> int:
        """Where an item sits in the aggregate operator's output row."""
        if isinstance(item, ast.Distance):
            raise SqlError(
                "a vector distance cannot be combined with GROUP BY or aggregate functions"
            )
        if isinstance(item, ast.Aggregate):
            if item not in slot_of:
                slot_of[item] = len(specs)
                specs.append(_aggregate_spec(schema, item))
            return len(group_positions) + slot_of[item]
        position = expressions.column_position(schema, item)
        if position not in group_positions:
            raise SqlError(
                f"column {item} must appear in GROUP BY or be used inside an aggregate function"
            )
        return group_positions.index(position)

    select_positions: list[int | DistanceSpec] = [
        output_position(item) for item in statement.columns
    ]
    names = tuple(
        item.label if isinstance(item, ast.Aggregate) else str(item) for item in statement.columns
    )
    keys: list[SortKey] = [
        (output_position(order.column), order.descending) for order in statement.order_by
    ]
    output_names = tuple(schema.columns[p].name for p in group_positions) + tuple(
        spec.label for spec in specs
    )

    plan: Operator
    answerable_from_metadata = table is not None and all(
        spec.position is None
        or (spec.function in ("MIN", "MAX") and spec.position in table.indexes)
        for spec in specs
    )
    if (
        table is not None
        and not group_positions
        and statement.where is None
        and specs
        and answerable_from_metadata
    ):
        plan = MetadataAggregate(table, specs)
        plan.est_rows = 1.0
        _rows, pages = table.ensure_stats()
        plan.cost = sum(
            float(pages) if spec.position is None else float(table.indexes[spec.position].height())
            for spec in specs
        )
    else:
        if source is not None:
            child = source
        elif table is not None:
            child, _ordered = plan_access(table, statement.where)
        else:
            raise InternalError("an aggregate was planned with nothing to read")
        plan = HashAggregate(child, group_positions, specs, output_names)
        # The number of groups is unknown without column statistics. The square
        # root of the input size is a common stand-in; it only affects EXPLAIN.
        plan.est_rows = max(1.0, child.est_rows**0.5) if group_positions else 1.0
        plan.cost = child.cost + child.est_rows * optimizer.ROW_CPU

    limit = statement.limit
    if keys:
        child = plan
        if limit is not None:
            plan = TopN(child, keys, limit, output_names)
            plan.est_rows = min(child.est_rows, float(limit))
        else:
            plan = Sort(child, keys, output_names)
            plan.est_rows = child.est_rows
        plan.cost = child.cost + optimizer.sort_cost(child.est_rows)
    elif limit is not None:
        child = plan
        plan = Limit(child, limit)
        plan.est_rows, plan.cost = min(child.est_rows, float(limit)), child.cost

    child = plan
    plan = Project(child, select_positions, names)
    plan.est_rows, plan.cost = child.est_rows, child.cost
    return plan, names


def _all_of(conditions: list[ast.Expr]) -> ast.Expr | None:
    """Join conditions with AND; None for an empty list."""
    combined: ast.Expr | None = None
    for condition in conditions:
        combined = condition if combined is None else ast.And(combined, condition)
    return combined


def _comparable(left: ColumnType, right: ColumnType) -> bool:
    numeric = (ColumnType.INTEGER, ColumnType.REAL)
    return (left in numeric and right in numeric) or (
        left is ColumnType.TEXT and right is ColumnType.TEXT
    )


def _plan_join(tables: list[Table], statement: ast.Select) -> tuple[Operator, tuple[str, ...]]:
    """A chain of joins: ((t0 JOIN t1) JOIN t2) ..., each on one equality.

    1. Resolve names. A row of the join is the columns of each table in turn,
       named `alias.column`.

    2. Settle the join kinds. A WHERE condition that rejects NULLs on the side an
       outer join NULL-extends turns it into a weaker join (see joins.py).

    3. Push filters down. A condition on one table is applied while reading that
       table, unless the joins could NULL-extend that table, in which case it must
       wait until after them. A condition on several tables is applied as soon as
       every table it mentions has been joined, unless a later RIGHT or FULL join
       would NULL-extend them afterwards.

    4. Choose the order and the methods. A chain of inner joins is ordered
       greedily, smallest table first. A chain with an outer join keeps the order
       written, because moving an outer join changes the result. For each step
       the hash join and, where a table has an index on its join column, the
       index join are compared by estimated cost.

    5. Aggregates, ORDER BY, LIMIT and the select list are applied to the joined
       rows by the same operators a one-table query uses.
    """
    joins = statement.joins
    count = len(tables)
    scope = Scope(
        [
            Source(statement.alias or tables[0].schema.name, tables[0]),
            *(
                Source(join.alias or tables[k + 1].schema.name, tables[k + 1])
                for k, join in enumerate(joins)
            ),
        ]
    )
    aliases = [source.alias for source in scope.sources]

    def schema_of(layout: Sequence[int]) -> TableSchema:
        """The row layout of these tables joined in this order."""
        return TableSchema(
            0,
            " JOIN ".join(aliases[t] for t in layout),
            0,
            tuple(
                replace(
                    column,
                    name=f"{aliases[t]}.{column.name}",
                    index_root=0,
                    primary_key=False,
                    unique=False,
                    ann="",
                )
                for t in layout
                for column in tables[t].schema.columns
            ),
        )

    names = display_names(statement, schema_of(range(count)).column_names)

    # --- the ON conditions. Join k adds table k+1 and compares one of its columns
    # with a column of an earlier table: links[k] = (earlier, its column, new column).
    links: list[tuple[int, int, int]] = []
    for k, join in enumerate(joins):
        new = k + 1
        side_a, position_a = scope.resolve(join.left)
        side_b, position_b = scope.resolve(join.right)
        if side_b == new and side_a < new:
            old, old_position, new_position = side_a, position_a, position_b
        elif side_a == new and side_b < new:
            old, old_position, new_position = side_b, position_b, position_a
        else:
            raise SqlError(
                f"the join condition must compare a column of {' or '.join(aliases[:new])} "
                f"with a column of {aliases[new]}"
            )
        type_old = tables[old].schema.columns[old_position].type
        type_new = tables[new].schema.columns[new_position].type
        if not _comparable(type_old, type_new):
            types = (type_old, type_new) if side_a == old else (type_new, type_old)
            raise TypeMismatchError(
                f"cannot join {scope.qualified(join.left)} ({types[0].name}) with "
                f"{scope.qualified(join.right)} ({types[1].name})"
            )
        links.append((old, old_position, new_position))
    pairs = [(old, k + 1) for k, (old, _a, _b) in enumerate(links)]

    def link_between(chosen: Sequence[int], t: int) -> tuple[int, int, int]:
        """(chosen table, its column, t's column) for the one ON condition joining t to them."""
        for k, (old, old_position, new_position) in enumerate(links):
            if k + 1 == t and old in chosen:
                return old, old_position, new_position
            if old == t and (k + 1) in chosen:
                return k + 1, new_position, old_position
        raise InternalError(f"table {aliases[t]} is not connected to the tables joined before it")

    # --- classify the WHERE conditions
    conjuncts = expressions.conjuncts(statement.where)
    used_by = [frozenset(scope.resolve(name)[0] for name in columns_in(c)) for c in conjuncts]
    kinds = narrow_kinds(
        [join.kind for join in joins], conjuncts, lambda name: scope.resolve(name)[0]
    )
    nullable = null_supplied(kinds)
    per_table: list[list[ast.Expr]] = [[] for _ in range(count)]
    pending: list[tuple[frozenset[int], ast.Expr]] = []
    for conjunct, used in zip(conjuncts, used_by, strict=True):
        if len(used) == 1 and not nullable[next(iter(used))]:
            per_table[next(iter(used))].append(rename_expr(conjunct, scope.plain))
        else:
            pending.append((used, rename_expr(conjunct, scope.qualified)))
    side_where = [_all_of(per_table[t]) for t in range(count)]
    scans = [plan_access(tables[t], side_where[t])[0] for t in range(count)]
    rows = [max(scan.est_rows, 0.0) for scan in scans]
    totals = [float(max(table.ensure_stats()[0], 1)) for table in tables]

    def unique(t: int, position: int) -> bool:
        return position in tables[t].indexes

    def joined_estimate(
        acc_rows: float, base: int | None, old: int, old_position: int, new: int, new_position: int
    ) -> float:
        """Rows from joining the accumulated rows with table `new`.

        Each row of one side matches the rows of the other with its key. If the
        other side's key is unique there is at most one, so the result is the
        product of the sizes divided by that table's size. `base` is the table
        the accumulated rows are, if they are still just one table.
        """
        if unique(new, new_position):
            return acc_rows * rows[new] / totals[new]
        if base is not None and unique(old, old_position):
            return acc_rows * rows[new] / totals[old]
        return min(acc_rows, rows[new])

    if count > 2 and all(kind == "INNER" for kind in kinds):

        def estimate(acc_rows: float, chosen: Sequence[int], t: int) -> float:
            old, old_position, new_position = link_between(chosen, t)
            base = chosen[0] if len(chosen) == 1 else None
            return joined_estimate(acc_rows, base, old, old_position, t, new_position)

        order = join_order(rows, pairs, estimate)
    else:
        order = list(range(count))

    layout: list[int] = [order[0]]
    plan: Operator = scans[order[0]]
    base: int | None = order[0]

    def apply_ready(remaining_kinds: Sequence[str]) -> None:
        """Apply every pending condition whose tables have all been joined."""
        nonlocal plan, base
        if any(kind in ("RIGHT", "FULL") for kind in remaining_kinds):
            return
        schema = schema_of(layout)
        for item in [item for item in pending if item[0] <= set(layout)]:
            pending.remove(item)
            expressions.check(item[1], schema)
            child = plan
            plan = Filter(
                child,
                expressions.compile_predicate(item[1], schema),
                expressions.format_expr(item[1]),
            )
            plan.cost = child.cost
            # No statistics relate columns of different tables, so this is a flat guess.
            plan.est_rows = child.est_rows * optimizer.DEFAULT_RANGE
            base = None

    def column_name(t: int, position: int) -> str:
        return f"{aliases[t]}.{tables[t].schema.columns[position].name}"

    def add_table(new: int, old: int, old_position: int, new_position: int, kind: str) -> None:
        nonlocal plan, base
        acc_rows = plan.est_rows
        offset = sum(len(tables[t].schema.columns) for t in layout[: layout.index(old)])
        acc_key = offset + old_position
        left_width = sum(len(tables[t].schema.columns) for t in layout)
        right_width = len(tables[new].schema.columns)
        condition = f"{column_name(old, old_position)} = {column_name(new, new_position)}"
        same_type = (
            tables[old].schema.columns[old_position].type
            is tables[new].schema.columns[new_position].type
        )

        estimate_rows = joined_estimate(acc_rows, base, old, old_position, new, new_position)
        if kind in ("LEFT", "FULL"):
            estimate_rows = max(estimate_rows, acc_rows)
        if kind in ("RIGHT", "FULL"):
            estimate_rows = max(estimate_rows, rows[new])

        build_acc = acc_rows <= rows[new]
        built_name = " JOIN ".join(aliases[t] for t in layout) if build_acc else aliases[new]
        if build_acc:
            build_op, probe_op, build_key, probe_key = plan, scans[new], acc_key, new_position
        else:
            build_op, probe_op, build_key, probe_key = scans[new], plan, new_position, acc_key
        best: Operator = HashJoin(
            build_op,
            probe_op,
            build_key,
            probe_key,
            build_is_left=build_acc,
            condition=condition,
            build_name=built_name,
            kind=kind,
            left_width=left_width,
            right_width=right_width,
        )
        best.cost = plan.cost + scans[new].cost + optimizer.hash_join_cost(acc_rows, rows[new])

        # The earlier table as the looked-up side: only while the accumulated
        # rows are still that one table, so its scan can be replaced by lookups.
        if base is not None and unique(old, old_position) and same_type and kind == "INNER":
            cost = scans[new].cost + optimizer.index_join_cost(
                rows[new], tables[old].indexes[old_position].height()
            )
            if cost < best.cost:
                inner_where = side_where[old]
                best = IndexJoin(
                    scans[new],
                    tables[old],
                    new_position,
                    old_position,
                    None
                    if inner_where is None
                    else expressions.compile_predicate(inner_where, tables[old].schema),
                    outer_is_left=False,
                    condition=condition,
                    inner_filter_text=""
                    if inner_where is None
                    else expressions.format_expr(inner_where),
                )
                best.cost = cost
        # The new table as the looked-up side.
        if unique(new, new_position) and same_type and kind in ("INNER", "LEFT"):
            cost = plan.cost + optimizer.index_join_cost(
                acc_rows, tables[new].indexes[new_position].height()
            )
            if cost < best.cost:
                inner_where = side_where[new]
                best = IndexJoin(
                    plan,
                    tables[new],
                    acc_key,
                    new_position,
                    None
                    if inner_where is None
                    else expressions.compile_predicate(inner_where, tables[new].schema),
                    outer_is_left=True,
                    condition=condition,
                    inner_filter_text=""
                    if inner_where is None
                    else expressions.format_expr(inner_where),
                    keep_outer=kind == "LEFT",
                )
                best.cost = cost
        best.est_rows = estimate_rows
        plan = best
        layout.append(new)
        base = None

    reordered = order != sorted(order)
    apply_ready([] if reordered else kinds)
    for new in order[1:]:
        if reordered:
            old, old_position, new_position = link_between(layout, new)
        else:
            old, old_position, new_position = links[new - 1]
        add_table(
            new, old, old_position, new_position, kinds[new - 1] if not reordered else "INNER"
        )
        apply_ready([] if reordered else kinds[new:])

    if pending:
        raise InternalError("a WHERE condition was never applied")
    combined = schema_of(layout)

    canonical = replace(rename_select(statement, scope.qualified), where=None)
    if _is_aggregate_query(canonical):
        plan, _labels = _plan_aggregate(None, combined, canonical, source=plan)
        return plan, names

    items: list[int | DistanceSpec] = []
    if canonical.columns is None:
        # SELECT * lists the tables in the order written, whatever order they were joined in.
        for t in range(count):
            for position in range(len(tables[t].schema.columns)):
                items.append(expressions.column_position(combined, column_name(t, position)))
    else:
        for item in canonical.columns:
            if isinstance(item, ast.Distance):
                raise SqlError("a vector distance cannot be used in a join query")
            items.append(expressions.column_position(combined, str(item)))
    keys: list[SortKey] = []
    for order_item in canonical.order_by:
        if isinstance(order_item.column, ast.Distance):
            raise SqlError("a vector distance cannot be used in a join query")
        position = expressions.column_position(combined, str(order_item.column))
        if combined.columns[position].type is ColumnType.VECTOR:
            raise TypeMismatchError(f"cannot ORDER BY {order_item.column}: vectors have no order")
        keys.append((position, order_item.descending))

    limit = statement.limit
    if keys:
        child = plan
        if limit is not None:
            plan = TopN(child, keys, limit, combined.column_names)
            plan.est_rows = min(child.est_rows, float(limit))
        else:
            plan = Sort(child, keys, combined.column_names)
            plan.est_rows = child.est_rows
        plan.cost = child.cost + optimizer.sort_cost(child.est_rows)
    elif limit is not None:
        child = plan
        plan = Limit(child, limit)
        plan.est_rows, plan.cost = min(child.est_rows, float(limit)), child.cost

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
    table: Table,
    statement: ast.Select,
    items: list[int | DistanceSpec],
    names: tuple[str, ...],
    settings: SearchSettings,
) -> Operator:
    """ORDER BY a vector distance.

    Two plans exist. The exact one scans the candidates and keeps the k best;
    it is always available and always right. The approximate one asks the
    column's HNSW index, and is used only when all of these hold:

    * the column was declared with HNSW for the same distance the query uses
    * there is a LIMIT, the order is nearest-first, and there is no WHERE
      clause (the index knows nothing about other columns)
    * the table has at least `min_rows` rows, since below that the exact scan
      is quick anyway
    * NumPy is installed and the index has not been switched off

    Anything else falls back to the exact plan, so adding an index can make a
    query approximate but never makes one fail.
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

    limit = statement.limit
    if (
        settings.use_index
        and table.schema.columns[spec.position].ann == spec.metric
        and statement.where is None
        and limit is not None
        and limit > 0
        and not item.descending
        and distance.numpy_available()
        and table.ensure_stats()[0] >= settings.min_rows
    ):
        ef = max(settings.ef_search, limit)
        assert isinstance(plan, VectorTopK)
        plan = VectorIndexScan(table, spec, limit, ef, fallback=plan)
        plan.est_rows = float(limit)
        plan.cost = optimizer.vector_index_cost(ef, len(spec.query))

    project = Project(plan, items, names)
    project.est_rows, project.cost = plan.est_rows, plan.cost
    return project
