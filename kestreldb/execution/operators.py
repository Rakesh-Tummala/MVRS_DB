"""Query operators in the iterator (Volcano) model.

Every operator exposes rows(), a generator that pulls from its child only when
its own consumer asks for the next row. A query is a tree of operators, and
execution is the top operator being iterated. Nothing is computed until it is
needed, which is what lets LIMIT stop a scan early.

An entry flowing between operators is (record ID, row). The record ID is
carried so that UPDATE and DELETE can use the same operators as SELECT.

Each operator also carries the planner's estimates (rows, cost) for EXPLAIN.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from dataclasses import dataclass

from kestreldb.catalog.catalog import Table
from kestreldb.execution.expressions import Predicate, Row, format_value
from kestreldb.index.keys import encode_key
from kestreldb.storage.heap_file import RecordId
from kestreldb.storage.record import Value, decode_record

Entry = tuple[RecordId, Row]
SortKey = tuple[int, bool]  # (column position, descending)


@dataclass(frozen=True, slots=True)
class KeyRange:
    """A range of values on one indexed column. None means unbounded on that side."""

    low: Value = None
    high: Value = None
    low_inclusive: bool = True
    high_inclusive: bool = True

    @property
    def is_point(self) -> bool:
        return (
            self.low is not None
            and self.low == self.high
            and self.low_inclusive
            and self.high_inclusive
        )

    @property
    def is_unbounded(self) -> bool:
        return self.low is None and self.high is None

    def describe(self, column: str) -> str:
        if self.is_unbounded:
            return "all rows, in key order"
        if self.is_point:
            return f"{column} = {format_value(self.low)}"
        parts = []
        if self.low is not None:
            parts.append(f"{column} {'>=' if self.low_inclusive else '>'} {format_value(self.low)}")
        if self.high is not None:
            parts.append(
                f"{column} {'<=' if self.high_inclusive else '<'} {format_value(self.high)}"
            )
        return " AND ".join(parts)


class Operator:
    est_rows: float = 0.0
    cost: float = 0.0
    children: tuple[Operator, ...] = ()

    def rows(self) -> Iterator[Entry]:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class SeqScan(Operator):
    """Read every row of a table by walking its heap pages."""

    def __init__(self, table: Table) -> None:
        self.table = table

    def rows(self) -> Iterator[Entry]:
        types = self.table.schema.column_types
        for rid, record in self.table.heap.scan():
            yield rid, decode_record(types, record)

    def describe(self) -> str:
        return f"SeqScan({self.table.schema.name})"


class IndexScan(Operator):
    """Use a B+ tree to find matching record IDs, then fetch each row from the heap.

    Rows come out in key order, which the planner can use to skip a sort.
    """

    def __init__(self, table: Table, column: int, key_range: KeyRange) -> None:
        self.table = table
        self.column = column
        self.key_range = key_range

    def rows(self) -> Iterator[Entry]:
        schema = self.table.schema
        types = schema.column_types
        column_type = schema.columns[self.column].type
        tree = self.table.indexes[self.column]
        heap = self.table.heap
        key_range = self.key_range
        if key_range.is_point:
            assert key_range.low is not None
            rid = tree.get(encode_key(column_type, key_range.low))
            if rid is not None:
                yield rid, decode_record(types, heap.read(rid))
            return
        low = None if key_range.low is None else encode_key(column_type, key_range.low)
        high = None if key_range.high is None else encode_key(column_type, key_range.high)
        for _key, rid in tree.scan(
            low,
            high,
            low_inclusive=key_range.low_inclusive,
            high_inclusive=key_range.high_inclusive,
        ):
            yield rid, decode_record(types, heap.read(rid))

    def describe(self) -> str:
        schema = self.table.schema
        column = schema.columns[self.column].name
        return f"IndexScan({schema.name}.{column}: {self.key_range.describe(column)})"


class Filter(Operator):
    def __init__(self, child: Operator, predicate: Predicate, text: str) -> None:
        self.children = (child,)
        self.predicate = predicate
        self.text = text

    def rows(self) -> Iterator[Entry]:
        predicate = self.predicate
        for entry in self.children[0].rows():
            if predicate(entry[1]) is True:
                yield entry

    def describe(self) -> str:
        return f"Filter({self.text})"


def _sort_entries(entries: list[Entry], keys: list[SortKey]) -> list[Entry]:
    """Sort by several keys. NULL sorts first ascending and last descending.

    Python's sort is stable, so sorting by the last key first and the first key
    last gives the right multi-key order, with each pass free to use its own
    direction.
    """
    for position, descending in reversed(keys):
        entries.sort(key=lambda entry: _null_first(entry[1][position]), reverse=descending)
    return entries


def _null_first(value: Value) -> tuple[bool, Value]:
    # (False, None) sorts before every (True, value) and is never compared further.
    return (value is not None, value)


def _describe_keys(names: tuple[str, ...], keys: list[SortKey]) -> str:
    return ", ".join(f"{names[p]} {'DESC' if d else 'ASC'}" for p, d in keys)


class Sort(Operator):
    def __init__(self, child: Operator, keys: list[SortKey], names: tuple[str, ...]) -> None:
        self.children = (child,)
        self.keys = keys
        self.names = names

    def rows(self) -> Iterator[Entry]:
        yield from _sort_entries(list(self.children[0].rows()), self.keys)

    def describe(self) -> str:
        return f"Sort({_describe_keys(self.names, self.keys)})"


class TopN(Operator):
    """ORDER BY with LIMIT n: keep only the best n rows instead of sorting everything."""

    def __init__(
        self, child: Operator, keys: list[SortKey], limit: int, names: tuple[str, ...]
    ) -> None:
        self.children = (child,)
        self.keys = keys
        self.limit = limit
        self.names = names

    def rows(self) -> Iterator[Entry]:
        source = self.children[0].rows()
        directions = {descending for _position, descending in self.keys}
        if self.limit == 0:
            return
        if len(directions) == 1:
            positions = [position for position, _descending in self.keys]

            def sort_key(entry: Entry) -> tuple[tuple[bool, Value], ...]:
                row = entry[1]
                return tuple(_null_first(row[p]) for p in positions)

            # A heap of size n: O(rows * log n) time and O(n) memory.
            pick = heapq.nlargest if directions.pop() else heapq.nsmallest
            yield from pick(self.limit, source, key=sort_key)
        else:
            yield from _sort_entries(list(source), self.keys)[: self.limit]

    def describe(self) -> str:
        return f"TopN({self.limit}; {_describe_keys(self.names, self.keys)})"


class Limit(Operator):
    def __init__(self, child: Operator, limit: int) -> None:
        self.children = (child,)
        self.limit = limit

    def rows(self) -> Iterator[Entry]:
        remaining = self.limit
        if remaining <= 0:
            return
        for entry in self.children[0].rows():
            yield entry
            remaining -= 1
            if remaining == 0:
                return

    def describe(self) -> str:
        return f"Limit({self.limit})"


class Project(Operator):
    def __init__(self, child: Operator, positions: list[int], names: tuple[str, ...]) -> None:
        self.children = (child,)
        self.positions = positions
        self.names = names

    def rows(self) -> Iterator[Entry]:
        positions = self.positions
        for rid, row in self.children[0].rows():
            yield rid, tuple(row[p] for p in positions)

    def describe(self) -> str:
        return f"Project({', '.join(self.names)})"


def explain(operator: Operator, indent: int = 0) -> list[str]:
    """Render an operator tree, one line per operator, children indented."""
    line = f"{'  ' * indent}{operator.describe()}  [rows~{_round(operator.est_rows)}"
    line += f" cost~{operator.cost:.1f}]"
    lines = [line]
    for child in operator.children:
        lines.extend(explain(child, indent + 1))
    return lines


def _round(value: float) -> int:
    return max(0, round(value))
