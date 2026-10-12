"""The parameter values of the statement being executed.

A cached plan is built once and shared by every execution, so the values of one
execution cannot live in the plan. They are held here, in a context variable set
around the execution: a thread (or task) sees only its own values, and a plan
operator reads them when its `rows()` starts running.

Only operators that were built from a template (a statement with `?`) look here;
operators built from a statement with literals never do.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from mvrs_db.errors import InternalError
from mvrs_db.sql import ast

_VALUES: ContextVar[Sequence[ast.Value] | None] = ContextVar("mvrs_db_params", default=None)


@contextmanager
def bound_values(values: Sequence[ast.Value]) -> Iterator[None]:
    """Make `values` (checked, from sql.binding.coerce_params) current for this execution."""
    token = _VALUES.set(values)
    try:
        yield
    finally:
        _VALUES.reset(token)


def current() -> Sequence[ast.Value]:
    values = _VALUES.get()
    if values is None:
        raise InternalError("a plan with parameters ran without any parameter values")
    return values
