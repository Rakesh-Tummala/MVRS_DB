"""Executes one parsed statement against the catalog.

This layer enforces types and constraints and keeps tables and their indexes in
step. It knows nothing about transactions or locking: the caller (database.py)
wraps each statement in a transaction, so an error raised halfway through a
statement is undone by rolling that transaction back.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from mvrs_db.catalog.catalog import Catalog, Table
from mvrs_db.catalog.schema import Column
from mvrs_db.errors import (
    ConstraintViolationError,
    PlanNotReusable,
    SqlError,
    TypeMismatchError,
)
from mvrs_db.execution import expressions
from mvrs_db.execution.operators import Operator, explain
from mvrs_db.execution.params import bound_values
from mvrs_db.execution.plan_cache import CachedPlan, PlanCache, templated_conditions
from mvrs_db.index.keys import encode_key, secondary_prefix
from mvrs_db.planner.planner import plan_access, plan_select, unqualified_where
from mvrs_db.sql import ast
from mvrs_db.storage.heap_file import RecordId
from mvrs_db.storage.record import ColumnType, Value, encode_record
from mvrs_db.vector.hnsw import SearchSettings


@dataclass(slots=True)
class Result:
    """What a statement produced.

    columns/rows are filled for SELECT and EXPLAIN. rowcount is the number of
    rows returned, inserted, updated or deleted.
    """

    columns: tuple[str, ...] = ()
    rows: list[tuple[Value, ...]] = field(default_factory=list)
    rowcount: int = 0


def coerce(table: str, column: Column, value: Value) -> Value:
    """Check a literal against a column's type, converting integer to REAL."""
    if value is None:
        return None
    if column.type is ColumnType.INTEGER:
        if type(value) is int:
            return value
    elif column.type is ColumnType.REAL:
        if type(value) is float:
            return value
        if type(value) is int:
            return float(value)
    elif column.type is ColumnType.TEXT:
        if type(value) is str:
            return value
    elif type(value) is tuple:
        if len(value) != column.dimension:
            raise TypeMismatchError(
                f"column {table}.{column.name} is VECTOR({column.dimension}) and cannot hold "
                f"a vector of {len(value)} numbers"
            )
        return value
    type_name = column.type.name
    if column.type is ColumnType.VECTOR:
        type_name = f"VECTOR({column.dimension})"
    shown = expressions.format_value(value)
    if len(shown) > 60:
        shown = shown[:57] + "..."
    raise TypeMismatchError(f"column {table}.{column.name} is {type_name} and cannot hold {shown}")


def _constraint_name(column: Column) -> str:
    return "PRIMARY KEY" if column.primary_key else "UNIQUE"


class Executor:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self.vector_settings = SearchSettings()
        self.plans = PlanCache()

    # ------------------------------------------------------------------ queries

    def _plan(
        self,
        statement: ast.Select,
        catalog: Catalog | None = None,
        settings: SearchSettings | None = None,
    ) -> tuple[Operator, tuple[str, ...]]:
        """Plan a SELECT, against `catalog` if given (a snapshot's view) instead of the live one."""
        source = catalog or self._catalog
        table = source.get(statement.table)
        joined = [source.get(join.table) for join in statement.joins]
        return plan_select(table, statement, settings or self.vector_settings, joined)

    def _reusable(
        self,
        template: ast.Statement,
        catalog: Catalog,
        table_names: Sequence[str],
        build: Callable[[list[Table]], tuple[Operator, tuple[str, ...]]],
    ) -> CachedPlan | None:
        """A plan for this template that can be run with any values, or None.

        Uses the cached one if it is still current; otherwise builds one. None
        means the planner found the plan would depend on the values (or the
        statement is of a kind that is not cached), so the caller plans the
        bound statement as it always has.
        """
        entry = self.plans.lookup(template, catalog)
        if entry is None:
            targets = [catalog.get(name) for name in table_names]
            sizes = [(target, target.ensure_stats()[0]) for target in targets]
            try:
                plan, names = build(targets)
            except PlanNotReusable:
                entry = CachedPlan(template, catalog, catalog.epoch, None, (), [])
            else:
                entry = CachedPlan(
                    template,
                    catalog,
                    catalog.epoch,
                    plan,
                    names,
                    sizes,
                    templated_conditions(plan),
                )
            self.plans.store(entry)
        return entry if entry.plan is not None else None

    @staticmethod
    def _plain_select(template: ast.Select) -> bool:
        """Anything but vector distances. Those are planned with their own rules each time."""
        items = [*(template.columns or ()), *(order.column for order in template.order_by)]
        return not any(isinstance(item, ast.Distance) for item in items)

    def select(
        self,
        statement: ast.Select,
        catalog: Catalog | None = None,
        settings: SearchSettings | None = None,
        *,
        template: ast.Statement | None = None,
        params: Sequence[ast.Value] = (),
    ) -> Result:
        if isinstance(template, ast.Select) and self._plain_select(template):
            source = catalog or self._catalog
            entry = self._reusable(
                template,
                source,
                [template.table, *(join.table for join in template.joins)],
                lambda tables: plan_select(
                    tables[0], template, settings or self.vector_settings, tables[1:]
                ),
            )
            if entry is not None:
                entry.validate(params)
                with bound_values(params):
                    cached_rows = [row for _rid, row in entry.plan.rows()]  # type: ignore[union-attr]
                return Result(entry.names, cached_rows, len(cached_rows))
        plan, names = self._plan(statement, catalog, settings)
        rows = [row for _rid, row in plan.rows()]
        return Result(names, rows, len(rows))

    def explain(
        self,
        statement: ast.Explain,
        catalog: Catalog | None = None,
        settings: SearchSettings | None = None,
    ) -> Result:
        inner = statement.statement
        if isinstance(inner, ast.Select):
            return self._explain_lines(explain(self._plan(inner, catalog, settings)[0]))
        table = self._catalog.get(inner.table)
        access, _ordered = plan_access(table, unqualified_where(table, inner.where))
        if isinstance(inner, ast.Update):
            self._check_assignments(table, inner)
            changes = ", ".join(
                f"{name} = {expressions.format_value(value)}" for name, value in inner.assignments
            )
            head = f"Update({table.schema.name}: {changes})"
        else:
            head = f"Delete({table.schema.name})"
        return self._explain_lines([head, *explain(access, 1)])

    @staticmethod
    def _explain_lines(lines: list[str]) -> Result:
        return Result(("plan",), [(line,) for line in lines], len(lines))

    # ------------------------------------------------------------------ DDL

    def create_table(self, statement: ast.CreateTable) -> Result:
        self._catalog.create_table(statement)
        return Result()

    def drop_table(self, statement: ast.DropTable) -> Result:
        self._catalog.drop_table(statement.table)
        return Result()

    def create_index(self, statement: ast.CreateIndex) -> Result:
        self._catalog.create_index(statement)
        return Result()

    def analyze(self, statement: ast.Analyze) -> Result:
        done = self._catalog.analyze(statement.table)
        return Result(rowcount=len(done))

    def drop_index(self, statement: ast.DropIndex) -> Result:
        self._catalog.drop_index(statement.name)
        return Result()

    # ------------------------------------------------------------------ INSERT

    def insert(self, statement: ast.Insert) -> Result:
        table = self._catalog.get(statement.table)
        schema = table.schema
        width = len(schema.columns)
        if statement.columns is None:
            targets = list(range(width))
        else:
            targets = [expressions.column_position(schema, name) for name in statement.columns]
            if len(set(targets)) != len(targets):
                raise SqlError("a column is listed more than once in INSERT")

        for values in statement.rows:
            if len(values) != len(targets):
                raise SqlError(
                    f"INSERT has {len(values)} values for {len(targets)} columns of {schema.name}"
                )
            row: list[Value] = [None] * width
            for position, value in zip(targets, values, strict=True):
                row[position] = coerce(schema.name, schema.columns[position], value)
            self._insert_row(table, tuple(row))
        return Result(rowcount=len(statement.rows))

    def _insert_row(self, table: Table, row: tuple[Value, ...]) -> RecordId:
        schema = table.schema
        for column, value in zip(schema.columns, row, strict=True):
            if value is None and column.not_null:
                raise ConstraintViolationError(
                    f"NOT NULL constraint failed: {schema.name}.{column.name}"
                )
        keys: list[tuple[int, bytes]] = []
        for position, index in table.indexes.items():
            value = row[position]
            if value is None:
                continue  # NULLs are not indexed, so UNIQUE allows many of them
            column = schema.columns[position]
            if column.secondary:
                # Repeated values are allowed. Check now that this one can be a key,
                # so a value that is too long fails before the row is stored.
                secondary_prefix(column.type, value)
                continue
            key = encode_key(column.type, value)
            if index.get(key) is not None:
                raise ConstraintViolationError(
                    f"{_constraint_name(column)} constraint failed: {schema.name}.{column.name} "
                    f"= {expressions.format_value(value)}"
                )
            keys.append((position, key))
        rid = table.heap.insert(encode_record(schema.column_types, row))
        for position, key in keys:
            table.indexes[position].insert(key, rid)
        for position, index in table.indexes.items():
            if schema.columns[position].secondary and row[position] is not None:
                index.insert(table.index_key(position, row[position], rid), rid)
        table.vector_row_added(rid, row)
        table.rows_changed(1)
        return rid

    # ------------------------------------------------------------------ UPDATE

    def _check_assignments(self, table: Table, statement: ast.Update) -> dict[int, Value]:
        schema = table.schema
        changes: dict[int, Value] = {}
        for name, value in statement.assignments:
            position = expressions.column_position(schema, name)
            if position in changes:
                raise SqlError(f"column {name} is assigned more than once")
            changes[position] = coerce(schema.name, schema.columns[position], value)
        return changes

    def _targets(
        self,
        table: Table,
        statement: ast.Update | ast.Delete,
        template: ast.Statement | None,
        params: Sequence[ast.Value],
    ) -> list[tuple[RecordId, tuple[Value, ...]]]:
        """Every row an UPDATE or DELETE applies to, found before anything is changed."""
        if isinstance(template, (ast.Update, ast.Delete)):
            entry = self._reusable(
                template,
                self._catalog,
                [template.table],
                lambda t: (plan_access(t[0], unqualified_where(t[0], template.where))[0], ()),
            )
            if entry is not None:
                entry.validate(params)
                with bound_values(params):
                    return list(entry.plan.rows())  # type: ignore[union-attr]
        access, _ordered = plan_access(table, unqualified_where(table, statement.where))
        return list(access.rows())

    def update(
        self,
        statement: ast.Update,
        *,
        template: ast.Statement | None = None,
        params: Sequence[ast.Value] = (),
    ) -> Result:
        table = self._catalog.get(statement.table)
        schema = table.schema
        changes = self._check_assignments(table, statement)
        # Find every target first. Updating while scanning could move a row ahead
        # of the scan and update it twice (the "Halloween problem").
        targets = self._targets(table, statement, template, params)
        types = schema.column_types

        for rid, old_row in targets:
            new_values = list(old_row)
            for position, value in changes.items():
                if value is None and schema.columns[position].not_null:
                    raise ConstraintViolationError(
                        f"NOT NULL constraint failed: {schema.name}.{schema.columns[position].name}"
                    )
                new_values[position] = value
            new_row = tuple(new_values)
            if new_row == old_row:
                continue

            # Check uniqueness before touching anything.
            for position, index in table.indexes.items():
                value = new_row[position]
                if value is None:
                    continue
                column = schema.columns[position]
                if column.secondary:
                    secondary_prefix(column.type, value)  # can it be a key at all?
                    continue
                key = encode_key(column.type, value)
                if value != old_row[position]:
                    holder = index.get(key)
                    if holder is not None and holder != rid:
                        raise ConstraintViolationError(
                            f"{_constraint_name(column)} constraint failed: "
                            f"{schema.name}.{column.name} = {expressions.format_value(value)}"
                        )

            new_rid = table.heap.update(rid, encode_record(types, new_row))
            table.vector_row_removed(rid)
            table.vector_row_added(new_rid, new_row)
            for position, index in table.indexes.items():
                old_value, new_value = old_row[position], new_row[position]
                if old_value == new_value and new_rid == rid:
                    continue  # this index entry is still correct
                if old_value is not None:
                    index.delete(table.index_key(position, old_value, rid))
                if new_value is not None:
                    index.insert(table.index_key(position, new_value, new_rid), new_rid)
        return Result(rowcount=len(targets))

    # ------------------------------------------------------------------ DELETE

    def delete(
        self,
        statement: ast.Delete,
        *,
        template: ast.Statement | None = None,
        params: Sequence[ast.Value] = (),
    ) -> Result:
        table = self._catalog.get(statement.table)
        targets = self._targets(table, statement, template, params)
        for rid, row in targets:
            for position, index in table.indexes.items():
                value = row[position]
                if value is not None:
                    index.delete(table.index_key(position, value, rid))
            table.heap.delete(rid)
            table.vector_row_removed(rid)
        table.rows_changed(-len(targets))
        return Result(rowcount=len(targets))
