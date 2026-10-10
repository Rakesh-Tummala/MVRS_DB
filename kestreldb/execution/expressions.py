"""WHERE expressions: type checking, compilation to Python closures, and formatting.

SQL uses three-valued logic. A comparison involving NULL is neither true nor
false but UNKNOWN, represented here by None. The compiled predicate returns
True, False or None, and a WHERE clause keeps a row only when it is True.

    AND: False if either side is False; otherwise UNKNOWN if either is UNKNOWN.
    OR:  True if either side is True; otherwise UNKNOWN if either is UNKNOWN.
    NOT: UNKNOWN stays UNKNOWN.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

from kestreldb.catalog.schema import TableSchema
from kestreldb.errors import ColumnNotFoundError, TypeMismatchError
from kestreldb.sql import ast
from kestreldb.storage.record import ColumnType, Value

Row = tuple[Value, ...]
Predicate = Callable[[Row], bool | None]

_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}

_NUMERIC = "a number"
_TEXT = "text"


def column_position(schema: TableSchema, name: str) -> int:
    position = schema.column_index(name)
    if position is None:
        raise ColumnNotFoundError(f"table {schema.name} has no column named {name}")
    return position


def _kind(operand: ast.Operand, schema: TableSchema) -> str | None:
    """'a number', 'text', or None for a NULL literal (which fits anything)."""
    if isinstance(operand, ast.ColumnRef):
        column_type = schema.columns[column_position(schema, operand.name)].type
        return _TEXT if column_type is ColumnType.TEXT else _NUMERIC
    if operand.value is None:
        return None
    return _TEXT if isinstance(operand.value, str) else _NUMERIC


def check(expr: ast.Expr, schema: TableSchema) -> None:
    """Reject unknown columns and comparisons between text and numbers."""
    if isinstance(expr, ast.Compare):
        left, right = _kind(expr.left, schema), _kind(expr.right, schema)
        if left is not None and right is not None and left != right:
            raise TypeMismatchError(f"cannot compare {left} with {right} in: {format_expr(expr)}")
    elif isinstance(expr, ast.IsNull):
        _kind(expr.operand, schema)
    elif isinstance(expr, ast.Not):
        check(expr.operand, schema)
    else:
        check(expr.left, schema)
        check(expr.right, schema)


def compile_predicate(expr: ast.Expr, schema: TableSchema) -> Predicate:
    """Turn a checked expression into a function of a row."""
    if isinstance(expr, ast.Compare):
        return _compile_compare(expr, schema)
    if isinstance(expr, ast.IsNull):
        if isinstance(expr.operand, ast.ColumnRef):
            position = column_position(schema, expr.operand.name)
            if expr.negated:
                return lambda row: row[position] is not None
            return lambda row: row[position] is None
        constant = (expr.operand.value is None) != expr.negated
        return lambda row: constant
    if isinstance(expr, ast.Not):
        inner = compile_predicate(expr.operand, schema)

        def negate(row: Row) -> bool | None:
            value = inner(row)
            return None if value is None else not value

        return negate

    left = compile_predicate(expr.left, schema)
    right = compile_predicate(expr.right, schema)
    if isinstance(expr, ast.And):

        def conjunction(row: Row) -> bool | None:
            a = left(row)
            if a is False:
                return False
            b = right(row)
            if b is False:
                return False
            return None if a is None or b is None else True

        return conjunction

    def disjunction(row: Row) -> bool | None:
        a = left(row)
        if a is True:
            return True
        b = right(row)
        if b is True:
            return True
        return None if a is None or b is None else False

    return disjunction


def _compile_compare(expr: ast.Compare, schema: TableSchema) -> Predicate:
    compare = _OPERATORS[expr.op]
    left, right = expr.left, expr.right
    left_is_column = isinstance(left, ast.ColumnRef)
    right_is_column = isinstance(right, ast.ColumnRef)

    if left_is_column and right_is_column:
        a = column_position(schema, left.name)  # type: ignore[union-attr]
        b = column_position(schema, right.name)  # type: ignore[union-attr]

        def column_column(row: Row) -> bool | None:
            x, y = row[a], row[b]
            return None if x is None or y is None else compare(x, y)

        return column_column

    if left_is_column or right_is_column:
        column = left if left_is_column else right
        literal = right if left_is_column else left
        position = column_position(schema, column.name)  # type: ignore[union-attr]
        constant = literal.value  # type: ignore[union-attr]
        if constant is None:
            return lambda row: None
        if left_is_column:

            def column_literal(row: Row) -> bool | None:
                x = row[position]
                return None if x is None else compare(x, constant)

            return column_literal

        def literal_column(row: Row) -> bool | None:
            x = row[position]
            return None if x is None else compare(constant, x)

        return literal_column

    x, y = left.value, right.value  # type: ignore[union-attr]
    result = None if x is None or y is None else compare(x, y)
    return lambda row: result


def conjuncts(expr: ast.Expr | None) -> list[ast.Expr]:
    """Split `a AND b AND c` into [a, b, c]. Anything else is a single conjunct."""
    if expr is None:
        return []
    if isinstance(expr, ast.And):
        return conjuncts(expr.left) + conjuncts(expr.right)
    return [expr]


def format_value(value: Value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def _format_operand(operand: ast.Operand) -> str:
    return operand.name if isinstance(operand, ast.ColumnRef) else format_value(operand.value)


def format_expr(expr: ast.Expr) -> str:
    """Render an expression as SQL text, for EXPLAIN and error messages."""
    if isinstance(expr, ast.Compare):
        return f"{_format_operand(expr.left)} {expr.op} {_format_operand(expr.right)}"
    if isinstance(expr, ast.IsNull):
        suffix = "IS NOT NULL" if expr.negated else "IS NULL"
        return f"{_format_operand(expr.operand)} {suffix}"
    if isinstance(expr, ast.Not):
        return f"NOT ({format_expr(expr.operand)})"
    joiner = " AND " if isinstance(expr, ast.And) else " OR "
    parts = []
    for side in (expr.left, expr.right):
        text = format_expr(side)
        # Parenthesise OR inside AND so the printed text means the same thing.
        if isinstance(expr, ast.And) and isinstance(side, ast.Or):
            text = f"({text})"
        parts.append(text)
    return joiner.join(parts)
