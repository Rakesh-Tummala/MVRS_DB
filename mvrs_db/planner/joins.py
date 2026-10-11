"""The reasoning behind multi-table joins, kept apart from the operators it feeds.

Three pieces of logic live here because each can be wrong in a way that returns
wrong rows rather than an error, and each is easier to test on its own.

1. Which tables can have a WHERE condition pushed down to their scan?
   A condition applied below an outer join sees the table's rows before the
   join NULL-extends them; applied above it, after. The two differ, so a
   condition may only be pushed to a table that is never NULL-extended.

2. Can an outer join become an inner join? A WHERE condition that is never true
   when the NULL-extended side's columns are all NULL throws away exactly the
   rows the outer join added. The join can then be the cheaper inner kind, and
   the planner is free to reorder it. (This is the standard "outer join
   simplification" rule.)

3. In what order should an all-inner chain of tables be joined?

Join kinds are the strings "INNER", "LEFT", "RIGHT" and "FULL". Tables are
numbered 0.. in FROM order; kinds[k] is the join that brings in table k + 1.
In a chain ((t0 JOIN t1) JOIN t2) ..., the join that brings in table j has the
tables 0..j-1 on its left (all of them together) and table j on its right.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from mvrs_db.sql import ast

ColumnTest = Callable[[str], bool]


def rejects_null(expr: ast.Expr, in_side: ColumnTest) -> bool:
    """True if `expr` can never be TRUE when every column for which `in_side` holds is NULL.

    Conservative: when unsure, answer False. A wrong True would turn an outer
    join into an inner one and silently drop rows; a wrong False only costs a
    slower plan.

    * a comparison is never true if either operand is NULL (so one operand in
      the side is enough);
    * `col IS NOT NULL` is false for a NULL column; `col IS NULL` is true;
    * AND is not true if either side is not true; OR only if both are not;
    * NOT(x) is not true when x is NULL, but x being "not true" could mean
      false, which makes NOT(x) true. So NOT is never reported.
    """
    if isinstance(expr, ast.Compare):
        return any(
            isinstance(operand, ast.ColumnRef) and in_side(operand.name)
            for operand in (expr.left, expr.right)
        )
    if isinstance(expr, ast.IsNull):
        return (
            expr.negated and isinstance(expr.operand, ast.ColumnRef) and in_side(expr.operand.name)
        )
    if isinstance(expr, ast.And):
        return rejects_null(expr.left, in_side) or rejects_null(expr.right, in_side)
    if isinstance(expr, ast.Or):
        return rejects_null(expr.left, in_side) and rejects_null(expr.right, in_side)
    return False


def narrow_kinds(
    kinds: Sequence[str], conjuncts: Sequence[ast.Expr], table_of: Callable[[str], int]
) -> list[str]:
    """Replace outer joins by weaker joins wherever a WHERE condition allows it.

    Repeats until nothing changes, because narrowing one join does not change
    which rows another join NULL-extends, but is cheap to re-check and keeps the
    rule simple to state.
    """
    result = list(kinds)
    changed = True
    while changed:
        changed = False
        for k, kind in enumerate(result):
            if kind == "INNER":
                continue
            new_table = k + 1

            def on_right(name: str, new_table: int = new_table) -> bool:
                return table_of(name) == new_table

            def on_left(name: str, new_table: int = new_table) -> bool:
                return table_of(name) < new_table

            # Rows where the right side is NULL-extended are the left-only rows,
            # and vice versa. Rejecting NULLs on a side removes those rows.
            right_gone = any(rejects_null(c, on_right) for c in conjuncts)
            left_gone = any(rejects_null(c, on_left) for c in conjuncts)
            narrowed = kind
            if kind == "LEFT" and right_gone or kind == "RIGHT" and left_gone:
                narrowed = "INNER"
            elif kind == "FULL":
                if right_gone and left_gone:
                    narrowed = "INNER"
                elif right_gone:
                    narrowed = "RIGHT"
                elif left_gone:
                    narrowed = "LEFT"
            if narrowed != kind:
                result[k] = narrowed
                changed = True
    return result


def null_supplied(kinds: Sequence[str]) -> list[bool]:
    """For each table, can the joins NULL-extend its rows?

    Table t is NULL-extended by its own join if that join is LEFT or FULL, and by
    any later RIGHT or FULL join (which NULL-extends everything accumulated so far).
    """
    count = len(kinds) + 1
    flags = [False] * count
    for k, kind in enumerate(kinds):
        new_table = k + 1
        if kind in ("LEFT", "FULL"):
            flags[new_table] = True
        if kind in ("RIGHT", "FULL"):
            for earlier in range(new_table):
                flags[earlier] = True
    return flags


def join_order(
    rows: Sequence[float],
    edges: Sequence[tuple[int, int]],
    estimate: Callable[[float, Sequence[int], int], float],
) -> list[int]:
    """A left-deep join order for an all-inner chain: smallest table first, then greedily.

    `edges` are the (table, table) pairs that have an ON condition.
    `estimate(rows so far, tables so far, t)` is the estimated size of joining table
    t on next. Each step adds the connected table that gives the smallest estimate.
    Joining a table with no condition to the rest would be a cross product, which
    the grammar cannot express, so every candidate is connected. Ties go to the
    smaller table, then the earlier one, so the order is deterministic.

    Greedy is not optimal: it can pick a poor first step that a search over all
    orders would avoid. For the handful of tables a query here can name, the
    alternative (dynamic programming over subsets) is more code than this
    optimiser's row estimates can justify.
    """
    count = len(rows)
    chosen = [min(range(count), key=lambda t: (rows[t], t))]
    current = rows[chosen[0]]
    neighbours: dict[int, set[int]] = {t: set() for t in range(count)}
    for a, b in edges:
        neighbours[a].add(b)
        neighbours[b].add(a)
    while len(chosen) < count:
        candidates = [
            t
            for t in range(count)
            if t not in chosen and any(other in neighbours[t] for other in chosen)
        ]
        best = min(candidates, key=lambda t: (estimate(current, chosen, t), rows[t], t))
        current = estimate(current, chosen, best)
        chosen.append(best)
    return chosen
