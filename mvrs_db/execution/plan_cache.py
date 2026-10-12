"""A cache of query plans, so repeated statements are not planned again.

Planning a point query costs about as much as running it. A cached plan is the
operator tree built once for a statement *template* (the parsed text, with `?`
placeholders still unbound); each execution runs the same tree with that
execution's values (execution/params.py).

When is a plan reusable?
    Only if its shape cannot depend on the values. The planner decides that
    while it plans: `column = ?` on an indexed column is a point lookup whatever
    the value (the index is unique), and a condition on an unindexed column is
    just a filter, so those are reusable. A range such as `id > ?` on an indexed
    column is not: whether the index or a scan is better depends on how much of
    the table the value selects. The planner refuses (PlanNotReusable) and that
    statement is planned afresh each time, which is what happened before this
    cache existed. The refusal is itself remembered, so the attempt is not
    repeated.

When is a cached plan thrown away?
    * The catalog changed: a table was created or dropped, or a rollback made the
      in-memory catalog reload (`Catalog.epoch` changes). The plan holds table
      objects from the old catalog, so it must go.
    * A different catalog is being used. In snapshot isolation each commit has its
      own catalog view, so a plan is reused only between commits.
    * A table's row count has moved a long way from what the plan was built for
      (more than doubled, or less than halved). The planner's choices rest on the
      row count, so a plan for a 10-row table should not outlive its growth to a
      million.

Safe to share: plan operators keep no state between runs (every `rows()` call
starts from scratch), and per-execution values live in a context variable, so
two threads can run the same cached plan at once.

Entries are found by the identity of the template object, which the parse cache
hands out, not by hashing the tree. An entry keeps its template and catalog
alive, so neither object's identity can be reused while the entry exists.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from mvrs_db.catalog.catalog import Catalog, Table
from mvrs_db.execution.operators import Operator
from mvrs_db.sql import ast


@dataclass(slots=True)
class CachedPlan:
    template: ast.Statement
    catalog: Catalog
    epoch: int
    # None records that the statement was tried and cannot be reused.
    plan: Operator | None
    names: tuple[str, ...]
    # Each table the plan reads, with its row count when the plan was made.
    tables: list[tuple[Table, int | None]]

    def current(self, template: ast.Statement, catalog: Catalog) -> bool:
        if self.template is not template or self.catalog is not catalog:
            return False
        if self.epoch != catalog.epoch:
            return False
        for table, then in self.tables:
            now = table.row_count
            if now is None or then is None:
                continue
            if now > 2 * then + 16 or 2 * now + 16 < then:
                return False
        return True


class PlanCache:
    def __init__(self, size: int = 256) -> None:
        self._size = size
        self._entries: OrderedDict[int, CachedPlan] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.not_reusable = 0

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, template: ast.Statement, catalog: Catalog) -> CachedPlan | None:
        """A current entry for this template and catalog, if there is one."""
        with self._lock:
            entry = self._entries.get(id(template))
            if entry is None or not entry.current(template, catalog):
                return None
            self._entries.move_to_end(id(template))
            if entry.plan is not None:
                self.hits += 1
            return entry

    def store(self, entry: CachedPlan) -> None:
        with self._lock:
            self.misses += 1
            if entry.plan is None:
                self.not_reusable += 1
            self._entries[id(entry.template)] = entry
            self._entries.move_to_end(id(entry.template))
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
