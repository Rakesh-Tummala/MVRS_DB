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
from typing import Any

from mvrs_db.catalog.catalog import Table
from mvrs_db.execution.expressions import Predicate, Row, format_value
from mvrs_db.index.keys import encode_key
from mvrs_db.storage.heap_file import RecordId
from mvrs_db.storage.record import Value, decode_record, raw_vector
from mvrs_db.vector import distance

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


@dataclass(frozen=True, slots=True)
class DistanceSpec:
    """A checked `column <-> [vector]` expression: which column, which measure, to what."""

    position: int
    column: str
    metric: str
    query: tuple[float, ...]

    def describe(self) -> str:
        shown = ", ".join(f"{x:g}" for x in self.query[:4])
        if len(self.query) > 4:
            shown += f", ... {len(self.query)} numbers"
        return f"{self.column} {distance.SYMBOLS[self.metric]} [{shown}]"

    def of(self, row: Row) -> float | None:
        vector = row[self.position]
        if vector is None:
            return None
        return distance.distance(vector, self.query, self.metric)  # type: ignore[arg-type]


class VectorTopK(Operator):
    """Exact nearest-neighbour search: measure every candidate row, keep the best k.

    Candidates are processed in chunks. The distances for a chunk are computed
    in one batch and merged with the best k found so far, so memory stays at one
    chunk plus k rows however large the table is.

    It is exact because every row is measured. That makes it the baseline any
    approximate index has to be compared against, and it costs time
    proportional to (rows x dimensions).

    Two ways of getting the candidates:

    * General path: pull decoded rows from the child operator. Needed when a
      WHERE clause has to be evaluated, and when NumPy is not installed.
    * Raw path (`raw_table` set): read records straight from the heap and hand
      the stored 32-bit floats to NumPy without turning them into Python
      objects. Only the k winning rows are decoded. Turning a 768-number vector
      into a Python tuple costs far more than measuring its distance, so this
      path is several times faster. The planner uses it when there is no WHERE
      clause and NumPy is available.

    Rows with no distance (a NULL vector, or a zero vector under cosine
    distance) come last, whichever direction is asked for. Ties keep scan order.
    """

    CHUNK = 1024

    def __init__(
        self,
        child: Operator,
        spec: DistanceSpec,
        limit: int | None,
        descending: bool,
        raw_table: Table | None = None,
    ) -> None:
        self.children = (child,)
        self.spec = spec
        self.limit = limit
        self.descending = descending
        self.raw_table = raw_table

    def rows(self) -> Iterator[Entry]:
        limit = self.limit
        if limit == 0:
            return
        sign = -1.0 if self.descending else 1.0
        # (sort key, arrival number, candidate); the arrival number breaks ties stably.
        best: list[tuple[float, int, Any]] = []
        undefined: list[Any] = []
        arrival = 0

        chunks = self._raw_chunks() if self.raw_table is not None else self._decoded_chunks()
        for candidates, distances in chunks:
            for candidate, value in zip(candidates, distances, strict=True):
                if value is None:
                    if limit is None or len(undefined) < limit:
                        undefined.append(candidate)
                else:
                    best.append((sign * value, arrival, candidate))
                arrival += 1
            if limit is not None and len(best) > limit:
                best = heapq.nsmallest(limit, best, key=_distance_order)

        best.sort(key=_distance_order)
        ordered = [candidate for _key, _arrival, candidate in best] + undefined
        if limit is not None:
            ordered = ordered[:limit]
        if self.raw_table is None:
            yield from ordered
            return
        # The raw path carried record IDs only; decode just the winners.
        types = self.raw_table.schema.column_types
        heap = self.raw_table.heap
        for rid in ordered:
            yield rid, decode_record(types, heap.read(rid))

    def _decoded_chunks(self) -> Iterator[tuple[list[Entry], list[float | None]]]:
        spec, position = self.spec, self.spec.position
        chunk: list[Entry] = []

        def measure() -> tuple[list[Entry], list[float | None]]:
            vectors = [entry[1][position] for entry in chunk if entry[1][position] is not None]
            measured = iter(
                distance.batch_distances(vectors, spec.query, spec.metric)  # type: ignore[arg-type]
            )
            values = [None if entry[1][position] is None else next(measured) for entry in chunk]
            return list(chunk), values

        for entry in self.children[0].rows():
            chunk.append(entry)
            if len(chunk) >= self.CHUNK:
                yield measure()
                chunk.clear()
        if chunk:
            yield measure()

    def _raw_chunks(self) -> Iterator[tuple[list[RecordId], list[float | None]]]:
        assert self.raw_table is not None
        spec = self.spec
        types = self.raw_table.schema.column_types
        dimension = len(spec.query)
        rids: list[RecordId] = []
        buffers: list[bytes | None] = []

        def measure() -> tuple[list[RecordId], list[float | None]]:
            present = [raw for raw in buffers if raw is not None]
            measured = iter(
                distance.batch_distances_raw(b"".join(present), dimension, spec.query, spec.metric)
            )
            values = [None if raw is None else next(measured) for raw in buffers]
            return list(rids), values

        for rid, record in self.raw_table.heap.scan():
            rids.append(rid)
            buffers.append(raw_vector(types, record, spec.position, dimension))
            if len(rids) >= self.CHUNK:
                yield measure()
                rids.clear()
                buffers.clear()
        if rids:
            yield measure()

    def describe(self) -> str:
        which = "farthest" if self.descending else "nearest"
        count = "all rows" if self.limit is None else f"{self.limit} {which}"
        if self.raw_table is not None:
            engine = "NumPy on undecoded rows"
        else:
            engine = "NumPy" if distance.numpy_available() else "pure Python"
        return f"VectorTopK({count} by {self.spec.describe()}; exact scan, {engine})"


def _distance_order(item: tuple[float, int, Any]) -> tuple[float, int]:
    return item[0], item[1]


class VectorIndexScan(Operator):
    """Approximate nearest-neighbour search through the column's HNSW index.

    The index returns record IDs, nearest first; only those rows are read. The
    result is approximate: it usually contains the true k nearest but can miss
    some. `ef` is how many candidates the search keeps; raising it finds more
    of the true neighbours and takes longer.

    If the index holds fewer than k vectors, the exact scan runs instead. That
    keeps small and sparse tables right, including the rule that rows with no
    distance are returned last.
    """

    def __init__(
        self, table: Table, spec: DistanceSpec, limit: int, ef: int, fallback: VectorTopK
    ) -> None:
        self.table = table
        self.spec = spec
        self.limit = limit
        self.ef = ef
        self.fallback = fallback

    def rows(self) -> Iterator[Entry]:
        index = self.table.ann_index(self.spec.position)
        # The index stores whatever key it was given; here that is always a record ID.
        rids: list[Any] = index.search(self.spec.query, self.limit, self.ef)
        if len(rids) < self.limit:
            yield from self.fallback.rows()
            return
        types = self.table.schema.column_types
        heap = self.table.heap
        for rid in rids:
            yield rid, decode_record(types, heap.read(rid))

    def describe(self) -> str:
        built = "" if self.table.ann_built(self.spec.position) else ", index not built yet"
        return (
            f"VectorIndexScan({self.limit} nearest by {self.spec.describe()}; "
            f"HNSW, approximate, ef={self.ef}{built})"
        )


class Project(Operator):
    """Keep the requested columns. An item is a column position or a distance to compute."""

    def __init__(
        self, child: Operator, items: list[int | DistanceSpec], names: tuple[str, ...]
    ) -> None:
        self.children = (child,)
        self.items = items
        self.names = names

    def rows(self) -> Iterator[Entry]:
        items = self.items
        if all(isinstance(item, int) for item in items):
            positions: list[int] = items  # type: ignore[assignment]
            for rid, row in self.children[0].rows():
                yield rid, tuple(row[p] for p in positions)
            return
        for rid, row in self.children[0].rows():
            yield (
                rid,
                tuple(row[item] if isinstance(item, int) else item.of(row) for item in items),
            )

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
