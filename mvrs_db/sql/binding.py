"""Filling in the `?` placeholders of a parsed statement.

A statement with placeholders is a template: the parser records each `?` as an
`ast.Param` holding its position. Two steps turn a template into something that
runs:

1. `coerce_params()` checks the Python values the caller gave and converts each
   to a SQL value (once, however many places use it).
2. `bind()` returns a copy of the tree with every Param replaced by its value.
   The copy is an ordinary statement, so nothing after this point knows
   parameters exist. A cached plan skips this step and reads the values
   (`execution/params.py`) when it runs; it still needs step 1.

Why this matters: a value that arrives as a Python object is never re-read as
SQL, so there is nothing to quote and nothing to inject; and a template is
parsed once however many times it runs.

What a parameter may be: None (NULL), int, float, str, or a list or tuple of
numbers (a vector). A bool is refused (it would silently become 1 or 0), as are
integers outside 64 bits and floats that are not finite, because the parser
refuses the same values written as literals.

Parameters can stand wherever a literal can: INSERT values, UPDATE SET values,
comparisons in WHERE, a vector after `<->` or `<=>` (the whole vector, or its
elements), and LIMIT. They cannot stand for names (tables, columns).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace

from mvrs_db.errors import ParameterError
from mvrs_db.sql import ast

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParameterError(f"{where}: a vector may contain only numbers, not {value!r}")
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise ParameterError(f"{where}: integer {value} does not fit in 64 bits")
    elif not math.isfinite(value):
        raise ParameterError(f"{where}: {value} is not a finite number")
    return float(value)


def _coerce(raw: object, name: str) -> ast.Value:
    if raw is None or isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        raise ParameterError(f"{name}: a bool is not a SQL value here; use 1 or 0")
    if isinstance(raw, int):
        if not _INT64_MIN <= raw <= _INT64_MAX:
            raise ParameterError(f"{name}: integer {raw} does not fit in 64 bits")
        return raw
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise ParameterError(f"{name}: {raw} is not a finite number")
        return raw
    if isinstance(raw, (list, tuple)):
        if not raw:
            raise ParameterError(f"{name}: a vector needs at least one number")
        return tuple(_number(element, name) for element in raw)
    raise ParameterError(f"{name}: cannot use a {type(raw).__name__} as a SQL value")


def coerce_params(params: Sequence[object]) -> tuple[ast.Value, ...]:
    """Check each parameter and convert it to a SQL value (a vector becomes a tuple of floats)."""
    return tuple(_coerce(raw, f"parameter {i + 1}") for i, raw in enumerate(params))


def _vector(value: Sequence[object], where: str, values: Sequence[ast.Value]) -> tuple[float, ...]:
    """A vector written as `[1.5, ?, 2]`: its elements may be Params."""
    if not value:
        raise ParameterError(f"{where}: a vector needs at least one number")
    return tuple(_number(bound(element, values, where), where) for element in value)


def bound(value: object, values: Sequence[ast.Value], where: str = "parameter") -> ast.Value:
    """The value a node stands for: a Param is looked up, a vector's elements are bound."""
    if isinstance(value, ast.Param):
        return values[value.index]
    if isinstance(value, tuple):
        return _vector(value, where, values)
    return value  # type: ignore[return-value]


def _operand(operand: ast.Operand, values: Sequence[ast.Value]) -> ast.Operand:
    if isinstance(operand, ast.Literal):
        return ast.Literal(bound(operand.value, values))
    return operand


def bind_expr(expr: ast.Expr, values: Sequence[ast.Value]) -> ast.Expr:
    """A copy of a WHERE expression with its Params replaced by (already checked) values."""
    if isinstance(expr, ast.Compare):
        return ast.Compare(expr.op, _operand(expr.left, values), _operand(expr.right, values))
    if isinstance(expr, ast.IsNull):
        return ast.IsNull(_operand(expr.operand, values), expr.negated)
    if isinstance(expr, ast.Not):
        return ast.Not(bind_expr(expr.operand, values))
    left, right = bind_expr(expr.left, values), bind_expr(expr.right, values)
    return ast.And(left, right) if isinstance(expr, ast.And) else ast.Or(left, right)


def has_params(expr: ast.Expr | None) -> bool:
    """True if any placeholder appears in the expression."""
    if expr is None:
        return False
    if isinstance(expr, ast.Compare):
        return any(
            isinstance(o, ast.Literal) and isinstance(o.value, ast.Param)
            for o in (expr.left, expr.right)
        )
    if isinstance(expr, ast.IsNull):
        return isinstance(expr.operand, ast.Literal) and isinstance(expr.operand.value, ast.Param)
    if isinstance(expr, ast.Not):
        return has_params(expr.operand)
    return has_params(expr.left) or has_params(expr.right)


def _item(item: ast.SelectItem, values: Sequence[ast.Value]) -> ast.SelectItem:
    if isinstance(item, ast.Distance):
        vector = bound(item.vector, values, "the vector after the distance operator")
        if not isinstance(vector, tuple):
            raise ParameterError("the vector after the distance operator must be a list of numbers")
        return replace(item, vector=vector)
    return item


def limit_value(limit: object, values: Sequence[ast.Value]) -> int | None:
    """A LIMIT that may be a Param, checked: a non-negative integer or None."""
    if isinstance(limit, ast.Param):
        value = values[limit.index]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ParameterError(f"parameter {limit.index + 1}: LIMIT needs a non-negative integer")
        return value
    return limit  # type: ignore[return-value]


def bind(statement: ast.Statement, values: Sequence[ast.Value]) -> ast.Statement:
    """A copy of `statement` with every placeholder replaced by its value.

    `values` come from coerce_params(), so they are already checked.
    """
    if isinstance(statement, ast.Insert):
        rows = tuple(tuple(bound(v, values) for v in row) for row in statement.rows)
        return replace(statement, rows=rows)
    if isinstance(statement, ast.Update):
        assignments = tuple((name, bound(v, values)) for name, v in statement.assignments)
        where = None if statement.where is None else bind_expr(statement.where, values)
        return replace(statement, assignments=assignments, where=where)
    if isinstance(statement, ast.Delete):
        where = None if statement.where is None else bind_expr(statement.where, values)
        return replace(statement, where=where)
    if isinstance(statement, ast.Select):
        return replace(
            statement,
            columns=None
            if statement.columns is None
            else tuple(_item(item, values) for item in statement.columns),
            where=None if statement.where is None else bind_expr(statement.where, values),
            order_by=tuple(
                replace(order, column=_item(order.column, values)) for order in statement.order_by
            ),
            limit=limit_value(statement.limit, values),
        )
    if isinstance(statement, ast.Explain):
        inner = bind(statement.statement, values)
        if not isinstance(inner, (ast.Select, ast.Update, ast.Delete)):
            raise ParameterError("EXPLAIN needs a SELECT, UPDATE or DELETE")
        return ast.Explain(inner)
    return statement
