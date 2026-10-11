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
from mvrs_db.errors import KeyTooLargeError
from mvrs_db.execution.expressions import Predicate, Row, format_value
from mvrs_db.index.keys import decode_key, encode_key
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
        if self.table.ann_built(self.spec.position):
            built = ""
        elif self.table.ann_saved(self.spec.position):
            built = ", index not in memory yet (saved copy on disk)"
        else:
            built = ", index not built yet"
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


class HashJoin(Operator):
    """Join on equality, by hashing. INNER, LEFT, RIGHT or FULL.

    Build: read one input completely and file its rows in a dictionary under
    their join value. Probe: read the other input a row at a time and look its
    join value up. Each lookup is constant time, so the whole join costs about
    (rows on one side + rows on the other), where comparing every pair would
    cost their product.

    The build side is held in memory, so the planner builds on the smaller
    input. A NULL join value matches nothing, not even another NULL, so those
    rows are dropped from both sides.

    Output rows are always (left columns..., right columns...), whichever side
    was built. "Left" is the rows joined so far and "right" is the table being
    added, so a chain of joins is a left-deep tree of these operators.

    Outer joins keep the rows of a side that found no partner, padded with NULLs
    for the other side's columns. A row whose key is NULL never has a partner but
    is still kept if its side is preserved. If the preserved side is the probe
    side, its unmatched rows are emitted as they stream by. If it is the build
    side, whether a row was matched is only known at the end, so each build row
    carries a flag and the unmatched ones are emitted after the probe finishes.
    """

    def __init__(
        self,
        build: Operator,
        probe: Operator,
        build_key: int,
        probe_key: int,
        *,
        build_is_left: bool,
        condition: str,
        build_name: str,
        kind: str = "INNER",
        left_width: int = 0,
        right_width: int = 0,
    ) -> None:
        # Children are listed left table first, to match the output column order.
        self.children = (build, probe) if build_is_left else (probe, build)
        self.kind = kind
        self.left_width = left_width
        self.right_width = right_width
        self.build = build
        self.probe = probe
        self.build_key = build_key
        self.probe_key = probe_key
        self.build_is_left = build_is_left
        self.condition = condition
        self.build_name = build_name

    def rows(self) -> Iterator[Entry]:
        if self.kind != "INNER":
            yield from self._outer_rows()
            return
        build_key, probe_key, build_is_left = self.build_key, self.probe_key, self.build_is_left
        buckets: dict[Value, list[Row]] = {}
        for _rid, row in self.build.rows():
            key = row[build_key]
            if key is not None:
                buckets.setdefault(key, []).append(row)
        if not buckets:
            return
        for _rid, row in self.probe.rows():
            key = row[probe_key]
            if key is None:
                continue
            for match in buckets.get(key, ()):
                yield _NO_RECORD, (match + row) if build_is_left else (row + match)

    def _outer_rows(self) -> Iterator[Entry]:
        build_is_left = self.build_is_left
        keep_left = self.kind in ("LEFT", "FULL")
        keep_right = self.kind in ("RIGHT", "FULL")
        keep_build = keep_left if build_is_left else keep_right
        keep_probe = keep_right if build_is_left else keep_left
        # What stands in for the columns of the side that found no partner.
        build_nulls: Row = (None,) * (self.left_width if build_is_left else self.right_width)
        probe_nulls: Row = (None,) * (self.right_width if build_is_left else self.left_width)

        # Each bucket entry is [row, matched]; keyless rows never match anything.
        buckets: dict[Value, list[list[Any]]] = {}
        keyless: list[Row] = []
        for _rid, row in self.build.rows():
            key = row[self.build_key]
            if key is None:
                keyless.append(row)
            else:
                buckets.setdefault(key, []).append([row, False])

        for _rid, row in self.probe.rows():
            key = row[self.probe_key]
            entries = None if key is None else buckets.get(key)
            if entries:
                for entry in entries:
                    entry[1] = True
                    match = entry[0]
                    yield _NO_RECORD, (match + row) if build_is_left else (row + match)
            elif keep_probe:
                yield _NO_RECORD, (build_nulls + row) if build_is_left else (row + build_nulls)

        if keep_build:
            for entries in buckets.values():
                for match, matched in entries:
                    if not matched:
                        padded = match + probe_nulls if build_is_left else probe_nulls + match
                        yield _NO_RECORD, padded
            for match in keyless:
                padded = match + probe_nulls if build_is_left else probe_nulls + match
                yield _NO_RECORD, padded

    def describe(self) -> str:
        kind = "" if self.kind == "INNER" else f"{self.kind}, "
        return f"HashJoin({kind}{self.condition}; hash table on {self.build_name})"


class IndexJoin(Operator):
    """Join on equality, using an index on one table's join column.

    For each row of the outer input, look its join value up in the inner
    table's B+ tree and fetch the matching row. Nothing is built in memory and
    the inner table is never scanned, so this wins when the outer input is
    small. Each lookup walks the tree, though, so with many outer rows the hash
    join's single pass over both tables is cheaper. The planner compares the
    two estimates.

    The inner column is a primary key or UNIQUE column, so each outer row
    matches at most one inner row. Conditions on the inner table alone
    (`inner_predicate`) are checked on the fetched row.

    With keep_outer (a LEFT JOIN whose new table is the inner side), an outer row
    with no partner is still emitted, padded with NULLs for the inner columns.
    """

    def __init__(
        self,
        outer: Operator,
        inner: Table,
        outer_key: int,
        inner_column: int,
        inner_predicate: Predicate | None,
        *,
        outer_is_left: bool,
        condition: str,
        inner_filter_text: str,
        keep_outer: bool = False,
    ) -> None:
        self.children = (outer,)
        self.keep_outer = keep_outer
        self.inner = inner
        self.outer_key = outer_key
        self.inner_column = inner_column
        self.inner_predicate = inner_predicate
        self.outer_is_left = outer_is_left
        self.condition = condition
        self.inner_filter_text = inner_filter_text

    def rows(self) -> Iterator[Entry]:
        inner = self.inner
        tree = inner.indexes[self.inner_column]
        column_type = inner.schema.columns[self.inner_column].type
        types = inner.schema.column_types
        heap = inner.heap
        predicate = self.inner_predicate
        outer_key, outer_is_left = self.outer_key, self.outer_is_left
        keep_outer = self.keep_outer
        nulls: Row = (None,) * len(types)
        for _rid, row in self.children[0].rows():
            value = row[outer_key]
            match: Row | None = None
            if value is not None:
                try:
                    key: bytes | None = encode_key(column_type, value)
                except KeyTooLargeError:
                    key = None  # longer than any key the index can hold, so it matches nothing
                rid = None if key is None else tree.get(key)
                if rid is not None:
                    match = decode_record(types, heap.read(rid))
                    if predicate is not None and predicate(match) is not True:
                        match = None
            if match is not None:
                yield _NO_RECORD, (row + match) if outer_is_left else (match + row)
            elif keep_outer:
                yield _NO_RECORD, (row + nulls) if outer_is_left else (nulls + row)

    def describe(self) -> str:
        schema = self.inner.schema
        column = schema.columns[self.inner_column].name
        extra = f"; then {self.inner_filter_text}" if self.inner_filter_text else ""
        kind = "LEFT, " if self.keep_outer else ""
        return f"IndexJoin({kind}{self.condition}; look up {schema.name}.{column}{extra})"


@dataclass(frozen=True, slots=True)
class AggregateSpec:
    """One checked aggregate: the function and the column position it reads."""

    function: str  # COUNT, SUM, AVG, MIN or MAX
    position: int | None  # None only for COUNT(*)
    label: str  # for example "sum(price)"


# The row every aggregate operator emits carries no record ID of its own.
_NO_RECORD = RecordId(0, 0)


class HashAggregate(Operator):
    """GROUP BY and aggregate functions.

    Rows are read once. Each row's group is found in a dictionary keyed by its
    GROUP BY values, and that group's running state is updated: a count, a
    total, a smallest and largest value. Nothing but one small state per group
    is kept, so memory grows with the number of groups, not the number of rows.

    Output rows are (group columns..., aggregate results...). With no GROUP BY
    there is exactly one group, and it produces a row even when no input row
    arrives: COUNT gives 0 and the other functions give NULL.

    SQL's NULL rules: COUNT(*) counts rows; every other aggregate ignores NULL
    inputs, and gives NULL if it saw no non-NULL value. In GROUP BY, NULLs form
    one group together.

    The alternative is sort-based aggregation: sort by the group columns, then
    aggregate runs of equal rows. It needs no dictionary and produces sorted
    output, but pays for a sort. Hashing is the usual choice when groups fit in
    memory, which is the only case this engine handles.
    """

    def __init__(
        self,
        child: Operator,
        group_positions: list[int],
        specs: list[AggregateSpec],
        names: tuple[str, ...],
    ) -> None:
        self.children = (child,)
        self.group_positions = group_positions
        self.specs = specs
        self.names = names

    def rows(self) -> Iterator[Entry]:
        group_positions, specs = self.group_positions, self.specs
        width = len(specs)
        # Per aggregate, per group: [count of non-NULL inputs, total, smallest, largest].
        groups: dict[tuple[Value, ...], list[list[Any]]] = {}
        for _rid, row in self.children[0].rows():
            key = tuple(row[p] for p in group_positions)
            states = groups.get(key)
            if states is None:
                states = groups[key] = [[0, 0, None, None] for _ in range(width)]
            for spec, state in zip(specs, states, strict=True):
                if spec.position is None:
                    state[0] += 1
                    continue
                value = row[spec.position]
                if value is None:
                    continue
                state[0] += 1
                function = spec.function
                if function in ("SUM", "AVG"):
                    state[1] += value
                elif function == "MIN":
                    if state[2] is None or value < state[2]:
                        state[2] = value
                elif function == "MAX" and (state[3] is None or value > state[3]):
                    state[3] = value
        if not groups and not group_positions:
            groups[()] = [[0, 0, None, None] for _ in range(width)]
        for key, states in groups.items():
            yield (
                _NO_RECORD,
                key + tuple(_finish(spec, s) for spec, s in zip(specs, states, strict=True)),
            )

    def describe(self) -> str:
        functions = ", ".join(spec.label for spec in self.specs)
        if not self.group_positions:
            return f"Aggregate({functions})"
        columns = ", ".join(self.names[: len(self.group_positions)])
        return f"HashAggregate(group by {columns}{'; ' + functions if functions else ''})"


def _finish(spec: AggregateSpec, state: list[Any]) -> Value:
    count, total, smallest, largest = state
    function = spec.function
    if function == "COUNT":
        return int(count)
    if count == 0:
        return None
    if function == "SUM":
        result: Value = total
        return result
    if function == "AVG":
        return float(total / count)
    return smallest if function == "MIN" else largest  # type: ignore[no-any-return]


class MetadataAggregate(Operator):
    """Answer COUNT(*), MIN and MAX without reading any rows.

    COUNT(*) over a whole table only needs the number of live slots in each
    heap page, so no record is decoded. MIN and MAX of an indexed column are the
    first and last keys of its B+ tree, found by walking down one edge of the
    tree. The planner uses this only when the query has no WHERE and no GROUP
    BY and every aggregate can be answered this way.
    """

    def __init__(self, table: Table, specs: list[AggregateSpec]) -> None:
        self.table = table
        self.specs = specs

    def rows(self) -> Iterator[Entry]:
        table = self.table
        values: list[Value] = []
        for spec in self.specs:
            if spec.position is None:
                values.append(table.heap.count()[0])
                continue
            tree = table.indexes[spec.position]
            key = tree.first_key() if spec.function == "MIN" else tree.last_key()
            column_type = table.schema.columns[spec.position].type
            values.append(None if key is None else decode_key(column_type, key))
        yield _NO_RECORD, tuple(values)

    def describe(self) -> str:
        parts = []
        for spec in self.specs:
            source = "heap page slot counts" if spec.position is None else "index edge"
            parts.append(f"{spec.label} from {source}")
        return f"MetadataAggregate({self.table.schema.name}: {'; '.join(parts)})"


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
