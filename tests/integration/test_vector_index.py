"""Phase 8, part 3: the HNSW approximate index."""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.catalog.schema import Column, TableSchema
from mvrs_db.errors import CatalogError, ConstraintViolationError, CorruptCatalogError
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse
from mvrs_db.storage.record import ColumnType
from mvrs_db.vector import distance
from mvrs_db.vector.hnsw import HnswIndex

np = pytest.importorskip("numpy")

DIM = 8


def literal(vector: object) -> str:
    return "[" + ", ".join(repr(float(x)) for x in vector) + "]"  # type: ignore[attr-defined]


def clustered(rng: random.Random, count: int, dimension: int = DIM) -> list[tuple[float, ...]]:
    centres = [[rng.uniform(-5, 5) for _ in range(dimension)] for _ in range(12)]
    return [
        distance.as_float32([c + rng.gauss(0, 0.6) for c in rng.choice(centres)])
        for _ in range(count)
    ]


def brute_force(
    vectors: dict[int, tuple[float, ...]], query: object, metric: str, k: int
) -> list[int]:
    scored = []
    for key, vector in vectors.items():
        value = distance.distance(vector, query, metric)  # type: ignore[arg-type]
        if value is not None:
            scored.append((value, key))
    return [key for _value, key in sorted(scored)[:k]]


# ---------------------------------------------------------------------- the graph alone


@pytest.mark.parametrize("metric", [distance.L2, distance.COSINE])
def test_recall_against_brute_force(metric: str) -> None:
    rng = random.Random(1)
    vectors = dict(enumerate(clustered(rng, 1500)))
    index = HnswIndex(DIM, metric)
    for key, vector in vectors.items():
        assert index.add(key, vector)
    queries = clustered(rng, 40)

    def recall(ef: int) -> float:
        hits = 0
        for query in queries:
            want = set(brute_force(vectors, query, metric, 10))
            hits += len(want & set(index.search(query, 10, ef)))
        return hits / (10 * len(queries))

    # Keeping every node as a candidate makes the search exhaustive. A perfect
    # score here shows the graph is connected: every vector can be reached.
    assert recall(1500) == 1.0
    # At realistic settings the index is approximate, and how approximate
    # depends on the data. These floors are what this dataset measured, with a
    # margin. Cosine is the harder case here: in 8 dimensions some queries fall
    # between clusters, the search settles in one cluster, and the true
    # neighbours in another are never visited. docs/11-vector-search.md has more.
    floor_wide, floor_narrow = (0.99, 0.95) if metric == distance.L2 else (0.95, 0.85)
    assert recall(200) >= floor_wide
    assert recall(50) >= floor_narrow
    assert recall(200) >= recall(50) >= recall(10)  # a larger ef never hurts here
    assert len(index) == 1500


def test_results_are_ordered_nearest_first_and_measure_few_vectors() -> None:
    rng = random.Random(2)
    vectors = dict(enumerate(clustered(rng, 3000)))
    index = HnswIndex(DIM, distance.L2)
    for key, vector in vectors.items():
        index.add(key, vector)
    query = clustered(rng, 1)[0]

    index.distance_computations = 0
    found = index.search(query, 10, 50)

    distances = [distance.distance(vectors[key], query, distance.L2) for key in found]  # type: ignore[index]
    assert distances == sorted(distances)  # type: ignore[type-var]
    # The whole point: far fewer than 3,000 vectors were looked at.
    assert index.distance_computations < 1200


def test_building_is_deterministic() -> None:
    rng = random.Random(3)
    vectors = clustered(rng, 400)
    query = vectors[7]

    def build() -> list[object]:
        index = HnswIndex(DIM, distance.L2)
        for key, vector in enumerate(vectors):
            index.add(key, vector)
        return index.search(query, 10, 30)

    assert build() == build()
    assert build()[0] == 7  # a stored vector finds itself first


def test_empty_and_tiny_indexes() -> None:
    index = HnswIndex(2, distance.L2)
    assert index.search((0.0, 0.0), 5, 50) == []

    index.add("a", (1.0, 1.0))
    index.add("b", (5.0, 5.0))

    assert index.search((0.0, 0.0), 5, 50) == ["a", "b"]  # fewer than k is fine
    assert index.search((0.0, 0.0), 1, 1) == ["a"]
    assert index.search((0.0, 0.0), 0, 50) == []


def test_removed_vectors_are_never_returned() -> None:
    rng = random.Random(4)
    vectors = dict(enumerate(clustered(rng, 600)))
    index = HnswIndex(DIM, distance.L2)
    for key, vector in vectors.items():
        index.add(key, vector)
    query = vectors[5]
    first = index.search(query, 10, 100)

    for key in first[:5]:
        assert index.remove(key) is True
    assert index.remove(first[0]) is False  # already gone

    remaining = {k: v for k, v in vectors.items() if k not in first[:5]}
    assert index.search(query, 10, 200) == brute_force(remaining, query, distance.L2, 10)
    assert len(index) == 595
    assert index.dead_fraction == pytest.approx(5 / 600)


def test_a_reused_key_refers_to_the_new_vector() -> None:
    index = HnswIndex(2, distance.L2)
    index.add("row", (0.0, 0.0))
    index.add("other", (10.0, 10.0))
    index.remove("row")

    index.add("row", (9.0, 9.0))  # a new row stored where the deleted one was

    assert index.search((0.0, 0.0), 2, 10) == ["row", "other"]
    assert index.search((10.0, 10.0), 1, 10) == ["other"]
    assert len(index) == 2


def test_cosine_index_skips_zero_vectors_and_ignores_length() -> None:
    index = HnswIndex(2, distance.COSINE)

    assert index.add("zero", (0.0, 0.0)) is False  # no direction, nothing to index
    index.add("short", (1.0, 0.0))
    index.add("long", (50.0, 1.0))
    index.add("up", (0.0, 3.0))

    assert index.search((2.0, 0.0), 3, 10) == ["short", "long", "up"]
    assert index.search((0.0, 0.0), 3, 10) == []
    assert len(index) == 3


def test_index_grows_past_its_initial_capacity() -> None:
    index = HnswIndex(2, distance.L2, m=4, ef_construction=8)
    for i in range(2500):
        index.add(i, (float(i), 0.0))

    assert index.search((1999.7, 0.0), 2, 20) == [2000, 1999]
    assert len(index) == 2500


def test_accepts_stored_bytes_and_tuples_alike() -> None:
    import struct

    index = HnswIndex(3, distance.L2)
    index.add("bytes", struct.pack("<3f", 1.0, 2.0, 3.0))
    index.add("tuple", (4.0, 5.0, 6.0))

    assert index.search((1.0, 2.0, 3.1), 2, 10) == ["bytes", "tuple"]


# ---------------------------------------------------------------------- syntax and catalog


def test_hnsw_parses_as_a_column_option() -> None:
    statement = parse(
        "CREATE TABLE t (id INT PRIMARY KEY, a VECTOR(3) HNSW, "
        "b VECTOR(4) NOT NULL HNSW COSINE, c VECTOR(2))"
    )
    assert isinstance(statement, ast.CreateTable)
    assert [(c.name, c.hnsw) for c in statement.columns] == [
        ("id", ""),
        ("a", "l2"),
        ("b", "cosine"),
        ("c", ""),
    ]
    assert statement.columns[2].not_null


def test_hnsw_is_only_for_vector_columns(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "bad.mvrs") as db:
        with pytest.raises(CatalogError, match="HNSW can only index a VECTOR column"):
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT HNSW)")
        assert db.table_names() == []


def test_catalog_remembers_the_index_declaration() -> None:
    schema = TableSchema(
        3,
        "t",
        9,
        (
            Column("id", ColumnType.INTEGER, primary_key=True, not_null=True, index_root=4),
            Column("a", ColumnType.VECTOR, dimension=3, ann="l2"),
            Column("b", ColumnType.VECTOR, not_null=True, dimension=5, ann="cosine"),
            Column("c", ColumnType.VECTOR, dimension=2),
        ),
    )

    assert TableSchema.decode(schema.encode()) == schema


def test_catalog_rejects_index_flags_on_a_non_vector_column() -> None:
    schema = TableSchema(1, "t", 2, (Column("id", ColumnType.INTEGER, primary_key=True),))
    data = bytearray(schema.encode())
    data[-5] |= 8  # the flags byte of the only column

    with pytest.raises(CorruptCatalogError, match="vector index flags"):
        TableSchema.decode(bytes(data))


# ---------------------------------------------------------------------- through SQL


@pytest.fixture
def vectors() -> dict[int, tuple[float, ...]]:
    return dict(enumerate(clustered(random.Random(5), 700)))


@pytest.fixture
def db(tmp_path: Path, vectors: dict[int, tuple[float, ...]]) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "ann.mvrs", synchronous=False)
    database.vector.min_rows = 0  # use the index even on this small table
    database.vector.ef_search = 200
    database.execute(
        f"CREATE TABLE docs (id INTEGER PRIMARY KEY, grp INTEGER, v VECTOR({DIM}) HNSW)"
    )
    items = list(vectors.items())
    for start in range(0, len(items), 100):
        values = ", ".join(f"({i}, {i % 3}, {literal(v)})" for i, v in items[start : start + 100])
        database.execute(f"INSERT INTO docs VALUES {values}")
    yield database
    database.close()


def nearest(db: mvrs_db.Database, query: object, k: int = 10, tail: str = "") -> list[int]:
    sql = f"SELECT id FROM docs {tail} ORDER BY v <-> {literal(query)} LIMIT {k}"
    return [row[0] for row in db.execute(sql).rows]  # type: ignore[misc]


def plan(db: mvrs_db.Database, sql: str) -> str:
    return "\n".join(row[0] for row in db.execute(f"EXPLAIN {sql}").rows)  # type: ignore[misc]


def test_index_is_used_and_agrees_with_exact_search(
    db: mvrs_db.Database, vectors: dict[int, tuple[float, ...]]
) -> None:
    query = clustered(random.Random(6), 1)[0]
    sql = f"SELECT id FROM docs ORDER BY v <-> {literal(query)} LIMIT 10"

    assert "index not built yet" in plan(db, sql)
    approximate = nearest(db, query)
    assert "VectorIndexScan(10 nearest by v <->" in plan(db, sql)
    assert "HNSW, approximate, ef=200)" in plan(db, sql)

    db.vector.use_index = False
    assert "VectorTopK" in plan(db, sql) and "VectorIndexScan" not in plan(db, sql)
    exact = nearest(db, query)

    assert exact == brute_force(vectors, query, distance.L2, 10)
    assert approximate == exact  # with ef=200 on 700 clustered rows nothing is missed


def test_recall_through_sql_over_many_queries(
    db: mvrs_db.Database, vectors: dict[int, tuple[float, ...]]
) -> None:
    db.vector.ef_search = 40
    hits = 0
    queries = clustered(random.Random(8), 30)
    for query in queries:
        hits += len(set(nearest(db, query)) & set(brute_force(vectors, query, distance.L2, 10)))

    assert hits / (10 * len(queries)) >= 0.95


def test_distance_column_is_exact_even_when_the_search_is_approximate(
    db: mvrs_db.Database, vectors: dict[int, tuple[float, ...]]
) -> None:
    query = vectors[42]
    rows = db.execute(
        f"SELECT id, v <-> {literal(query)} FROM docs ORDER BY v <-> {literal(query)} LIMIT 3"
    ).rows

    assert rows[0] == (42, 0.0)
    for row_id, value in rows:
        assert value == pytest.approx(distance.distance(vectors[row_id], query, distance.L2))  # type: ignore[index]


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT id FROM docs WHERE grp = 1 ORDER BY v <-> {q} LIMIT 5", "a WHERE clause"),
        ("SELECT id FROM docs ORDER BY v <-> {q} DESC LIMIT 5", "farthest-first"),
        ("SELECT id FROM docs ORDER BY v <-> {q}", "no LIMIT"),
        ("SELECT id FROM docs ORDER BY v <=> {q} LIMIT 5", "a different distance"),
    ],
)
def test_queries_the_index_cannot_serve_use_the_exact_scan(
    db: mvrs_db.Database, vectors: dict[int, tuple[float, ...]], sql: str, reason: str
) -> None:
    text = plan(db, sql.format(q=literal(vectors[0])))

    assert "VectorTopK" in text and "VectorIndexScan" not in text, reason


def test_small_tables_use_the_exact_scan_by_default(db: mvrs_db.Database) -> None:
    db.vector.min_rows = 1000  # the default

    text = plan(db, f"SELECT id FROM docs ORDER BY v <-> {literal([0.0] * DIM)} LIMIT 5")

    assert "VectorTopK" in text and "VectorIndexScan" not in text


def test_without_numpy_the_exact_scan_is_used(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(distance, "_np", None)

    text = plan(db, f"SELECT id FROM docs ORDER BY v <-> {literal([0.0] * DIM)} LIMIT 5")

    assert "VectorTopK" in text and "pure Python" in text


def test_ef_is_raised_to_the_limit(db: mvrs_db.Database) -> None:
    db.vector.ef_search = 5

    text = plan(db, f"SELECT id FROM docs ORDER BY v <-> {literal([0.0] * DIM)} LIMIT 50")

    assert "ef=50)" in text or "ef=50," in text


def test_index_follows_inserts_updates_and_deletes(
    db: mvrs_db.Database, vectors: dict[int, tuple[float, ...]]
) -> None:
    query = (40.0,) * DIM  # far from every cluster
    nearest(db, query)  # builds the index

    db.execute(f"INSERT INTO docs VALUES (9001, 0, {literal([39.9] * DIM)})")
    assert nearest(db, query, 1) == [9001]

    db.execute(f"UPDATE docs SET v = {literal([40.0] * DIM)} WHERE id = 3")
    assert nearest(db, query, 2) == [3, 9001]

    db.execute("DELETE FROM docs WHERE id = 3")
    assert nearest(db, query, 1) == [9001]

    db.execute("UPDATE docs SET v = NULL WHERE id = 9001")
    live = {k: v for k, v in vectors.items() if k != 3}
    assert nearest(db, query, 5) == brute_force(live, query, distance.L2, 5)
    db.check()


def test_rollback_does_not_leave_ghost_rows_in_the_index(
    db: mvrs_db.Database, vectors: dict[int, tuple[float, ...]]
) -> None:
    query = (40.0,) * DIM
    before = nearest(db, query, 5)  # builds the index

    db.execute("BEGIN")
    db.execute(f"INSERT INTO docs VALUES (9001, 0, {literal([40.0] * DIM)})")
    db.execute("DELETE FROM docs WHERE id = 0")
    assert nearest(db, query, 1) == [9001]  # a transaction sees its own changes
    db.execute("ROLLBACK")

    assert nearest(db, query, 5) == before
    assert 9001 not in nearest(db, query, 50)
    assert 0 in nearest(db, vectors[0], 1)


def test_index_built_inside_a_rolled_back_transaction_is_discarded(
    db: mvrs_db.Database,
) -> None:
    query = (40.0,) * DIM
    db.execute("BEGIN")
    db.execute(f"INSERT INTO docs VALUES (9001, 0, {literal([40.0] * DIM)})")
    assert nearest(db, query, 1) == [9001]  # the index is first built here, mid-transaction
    db.execute("ROLLBACK")

    assert 9001 not in nearest(db, query, 20)


def test_a_failed_statement_elsewhere_keeps_the_built_index(db: mvrs_db.Database) -> None:
    table = db._catalog.get("docs")
    nearest(db, (0.0,) * DIM)
    built = table._ann[2]
    db.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO other VALUES (1)")

    with pytest.raises(ConstraintViolationError):
        db.execute("INSERT INTO other VALUES (2), (1)")  # changes pages, then rolls back

    assert db._catalog.get("docs")._ann[2] is built  # not rebuilt for nothing


def test_index_is_rebuilt_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "reopen.mvrs"
    rng = random.Random(9)
    rows = dict(enumerate(clustered(rng, 300)))
    with mvrs_db.connect(path) as db:
        db.execute(f"CREATE TABLE docs (id INTEGER PRIMARY KEY, v VECTOR({DIM}) HNSW COSINE)")
        values = ", ".join(f"({i}, {literal(v)})" for i, v in rows.items())
        db.execute(f"INSERT INTO docs VALUES {values}")

    with mvrs_db.connect(path) as db:
        db.vector.min_rows = 0
        db.vector.ef_search = 300
        sql = f"SELECT id FROM docs ORDER BY v <=> {literal(rows[17])} LIMIT 5"
        assert "index not built yet" in plan(db, sql)  # nothing was stored on disk

        found = [row[0] for row in db.execute(sql).rows]

        assert found == brute_force(rows, rows[17], distance.COSINE, 5)
        assert "HNSW, approximate" in plan(db, sql) and "not built" not in plan(db, sql)
        assert db.check().tables == {"docs": 300}


def test_fewer_indexed_rows_than_the_limit_falls_back_to_exact(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "few.mvrs") as db:
        db.vector.min_rows = 0
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v VECTOR(2) HNSW)")
        db.execute("INSERT INTO t VALUES (1, [1, 1]), (2, NULL), (3, [5, 5])")

        rows = db.execute("SELECT id FROM t ORDER BY v <-> [0, 0] LIMIT 10").rows

        assert rows == [(1,), (3,), (2,)]  # includes the row with no distance, last


def test_index_is_rebuilt_once_many_rows_have_been_deleted(
    db: mvrs_db.Database, vectors: dict[int, tuple[float, ...]]
) -> None:
    nearest(db, vectors[0])
    table = db._catalog.get("docs")
    first = table._ann[2]

    db.execute("DELETE FROM docs WHERE id >= 100 AND id < 400")  # 300 of 700 rows
    assert first.dead_fraction > 0.3
    live = {k: v for k, v in vectors.items() if not 100 <= k < 400}
    found = nearest(db, vectors[0])

    rebuilt = db._catalog.get("docs")._ann[2]
    assert rebuilt is not first
    assert rebuilt.dead_fraction == 0.0
    assert found == brute_force(live, vectors[0], distance.L2, 10)
