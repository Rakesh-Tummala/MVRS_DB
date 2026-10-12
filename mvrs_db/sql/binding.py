"""Filling in the `?` placeholders of a parsed statement.

A statement with placeholders is a template: the parser records each `?` as an
`ast.Param` holding its position, and `bind()` returns a copy of the tree in
which every one has been replaced by a value. The copy is an ordinary
statement, so nothing after this point knows parameters exist.

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


def _vector(value: Sequence[object], where: str, params: Sequence[object]) -> tuple[float, ...]:
    if not value:
        raise ParameterError(f"{where}: a vector needs at least one number")
    return tuple(_number(bound(element, params, where), where) for element in value)


def bound(value: object, params: Sequence[object], where: str = "parameter") -> ast.Value:
    """The value a node stands for: a Param is looked up, a vector's elements are bound."""
    if isinstance(value, ast.Param):
        name = f"parameter {value.index + 1}"
        raw = params[value.index]
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
            return _vector(raw, name, params)
        raise ParameterError(f"{name}: cannot use a {type(raw).__name__} as a SQL value")
    if isinstance(value, tuple):
        return _vector(value, where, params)
    return value  # type: ignore[return-value]


def _operand(operand: ast.Operand, params: Sequence[object]) -> ast.Operand:
    if isinstance(operand, ast.Literal):
        return ast.Literal(bound(operand.value, params))
    return operand


def _expr(expr: ast.Expr, params: Sequence[object]) -> ast.Expr:
    if isinstance(expr, ast.Compare):
        return ast.Compare(expr.op, _operand(expr.left, params), _operand(expr.right, params))
    if isinstance(expr, ast.IsNull):
        return ast.IsNull(_operand(expr.operand, params), expr.negated)
    if isinstance(expr, ast.Not):
        return ast.Not(_expr(expr.operand, params))
    left, right = _expr(expr.left, params), _expr(expr.right, params)
    return ast.And(left, right) if isinstance(expr, ast.And) else ast.Or(left, right)


def _item(item: ast.SelectItem, params: Sequence[object]) -> ast.SelectItem:
    if isinstance(item, ast.Distance):
        vector = bound(item.vector, params, "the vector after the distance operator")
        if not isinstance(vector, tuple):
            raise ParameterError("the vector after the distance operator must be a list of numbers")
        return replace(item, vector=vector)
    return item


def _limit(limit: object, params: Sequence[object]) -> int | None:
    if isinstance(limit, ast.Param):
        value = bound(limit, params)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ParameterError(f"parameter {limit.index + 1}: LIMIT needs a non-negative integer")
        return value
    return limit  # type: ignore[return-value]


def bind(statement: ast.Statement, params: Sequence[object]) -> ast.Statement:
    """A copy of `statement` with every placeholder replaced by its parameter."""
    if isinstance(statement, ast.Insert):
        rows = tuple(tuple(bound(v, params) for v in row) for row in statement.rows)
        return replace(statement, rows=rows)
    if isinstance(statement, ast.Update):
        assignments = tuple((name, bound(v, params)) for name, v in statement.assignments)
        where = None if statement.where is None else _expr(statement.where, params)
        return replace(statement, assignments=assignments, where=where)
    if isinstance(statement, ast.Delete):
        where = None if statement.where is None else _expr(statement.where, params)
        return replace(statement, where=where)
    if isinstance(statement, ast.Select):
        return replace(
            statement,
            columns=None
            if statement.columns is None
            else tuple(_item(item, params) for item in statement.columns),
            where=None if statement.where is None else _expr(statement.where, params),
            order_by=tuple(
                replace(order, column=_item(order.column, params)) for order in statement.order_by
            ),
            limit=_limit(statement.limit, params),
        )
    if isinstance(statement, ast.Explain):
        inner = bind(statement.statement, params)
        assert isinstance(inner, (ast.Select, ast.Update, ast.Delete))
        return ast.Explain(inner)
    return statement
