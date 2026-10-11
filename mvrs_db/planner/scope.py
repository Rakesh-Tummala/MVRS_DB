"""Name resolution: working out which table a column name refers to.

A query names columns as `price` or `orders.price` or, with an alias, `o.price`.
Before planning, every such name is resolved against the tables in the FROM
clause and rewritten into one fixed form, so the rest of the planner never has
to think about qualifiers:

* one table:   the bare column name, `price`
* two tables:  always qualified by alias, `o.price`

The errors this catches are the ones that depend on the whole query and so
cannot be found while parsing: an unknown qualifier, a column no table has, and
a bare name that exists in both tables (ambiguous).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from mvrs_db.catalog.catalog import Table
from mvrs_db.errors import ColumnNotFoundError, SqlError
from mvrs_db.sql import ast


@dataclass(frozen=True, slots=True)
class Source:
    """One table in the FROM clause and the name the query uses for it."""

    alias: str
    table: Table


class Scope:
    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources
        if len({source.alias for source in sources}) != len(sources):
            raise SqlError(
                f"table {sources[0].alias} appears twice; give each one an alias, "
                f"as in: FROM {sources[0].table.schema.name} a JOIN "
                f"{sources[0].table.schema.name} b"
            )

    def resolve(self, name: str) -> tuple[int, int]:
        """(index of the source, column position within it) for a column name."""
        if "." in name:
            qualifier, column = name.split(".", 1)
            for index, source in enumerate(self.sources):
                if source.alias == qualifier:
                    position = source.table.schema.column_index(column)
                    if position is None:
                        raise ColumnNotFoundError(
                            f"table {source.table.schema.name} has no column named {column}"
                        )
                    return index, position
            known = " or ".join(source.alias for source in self.sources)
            raise ColumnNotFoundError(
                f"{qualifier} is not a table in this query (expected {known}), in {name}"
            )
        matches = [
            (index, position)
            for index, source in enumerate(self.sources)
            if (position := source.table.schema.column_index(name)) is not None
        ]
        if not matches:
            tables = " or ".join(source.table.schema.name for source in self.sources)
            raise ColumnNotFoundError(f"table {tables} has no column named {name}")
        if len(matches) > 1:
            options = " or ".join(f"{source.alias}.{name}" for source in self.sources)
            raise SqlError(f"column {name} is ambiguous; write {options}")
        return matches[0]

    def plain(self, name: str) -> str:
        """The bare column name (for a query on one table)."""
        index, position = self.resolve(name)
        return self.sources[index].table.schema.columns[position].name

    def qualified(self, name: str) -> str:
        """`alias.column` (for a query on two tables)."""
        index, position = self.resolve(name)
        source = self.sources[index]
        return f"{source.alias}.{source.table.schema.columns[position].name}"


Rename = Callable[[str], str]


def _rename_operand(operand: ast.Operand, rename: Rename) -> ast.Operand:
    return ast.ColumnRef(rename(operand.name)) if isinstance(operand, ast.ColumnRef) else operand


def rename_expr(expr: ast.Expr, rename: Rename) -> ast.Expr:
    """A copy of a WHERE expression with every column name passed through `rename`."""
    if isinstance(expr, ast.Compare):
        return ast.Compare(
            expr.op, _rename_operand(expr.left, rename), _rename_operand(expr.right, rename)
        )
    if isinstance(expr, ast.IsNull):
        return ast.IsNull(_rename_operand(expr.operand, rename), expr.negated)
    if isinstance(expr, ast.Not):
        return ast.Not(rename_expr(expr.operand, rename))
    left, right = rename_expr(expr.left, rename), rename_expr(expr.right, rename)
    return ast.And(left, right) if isinstance(expr, ast.And) else ast.Or(left, right)


def rename_item(item: ast.SelectItem, rename: Rename) -> ast.SelectItem:
    if isinstance(item, ast.Distance):
        return replace(item, column=rename(item.column))
    if isinstance(item, ast.Aggregate):
        return item if item.column is None else replace(item, column=rename(item.column))
    return rename(item)


def rename_select(statement: ast.Select, rename: Rename) -> ast.Select:
    """A copy of a SELECT with every column name passed through `rename`."""
    return replace(
        statement,
        columns=(
            None
            if statement.columns is None
            else tuple(rename_item(item, rename) for item in statement.columns)
        ),
        where=None if statement.where is None else rename_expr(statement.where, rename),
        order_by=tuple(
            replace(order, column=rename_item(order.column, rename)) for order in statement.order_by
        ),
        group_by=tuple(rename(name) for name in statement.group_by),
    )


def columns_in(expr: ast.Expr) -> list[str]:
    """Every column name an expression mentions."""
    if isinstance(expr, ast.Compare):
        return [o.name for o in (expr.left, expr.right) if isinstance(o, ast.ColumnRef)]
    if isinstance(expr, ast.IsNull):
        return [expr.operand.name] if isinstance(expr.operand, ast.ColumnRef) else []
    if isinstance(expr, ast.Not):
        return columns_in(expr.operand)
    return columns_in(expr.left) + columns_in(expr.right)


def display_names(statement: ast.Select, star: tuple[str, ...]) -> tuple[str, ...]:
    """Result column names: each item as the query wrote it, or `star` for SELECT *."""
    if statement.columns is None:
        return star
    return tuple(
        "distance"
        if isinstance(item, ast.Distance)
        else item.label
        if isinstance(item, ast.Aggregate)
        else item
        for item in statement.columns
    )
