"""Phase 8, part 2: exact nearest-neighbour search with ORDER BY <-> / <=>."""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import mvrs_db
from mvrs_db.errors import ColumnNotFoundError, SqlError, SqlSyntaxError, TypeMismatchError
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse
from mvrs_db.vector import distance

L2, COSINE = distance.L2, distance.COSINE


def literal(vector: tuple[float, ...] | list[float]) -> str:
    return "[" + ", ".join(repr(float(x)) for x in vector) + "]"


def reference(a: tuple[float, ...], b: tuple[float, ...], metric: str) -> float | None:
    """An independent implementation, written from the definitions."""
    if metric == L2:
        return math.dist(a, b)
    norm_a, norm_b = math.hypot(*a), math.hypot(*b)
    if norm_a == 0 or norm_b == 0:
        return None
    return 1 - sum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)


@pytest.fixture(params=["numpy", "pure-python"])
def engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test once with NumPy and once with the pure-Python fallback."""
    if request.param == "pure-python":
        monkeypatch.setattr(distance, "_np", None)
    elif not distance.numpy_available():
        pytest.skip("NumPy is not installed")
    return str(request.param)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "search.mvrs", synchronous=False)
    database.execute(
        "CREATE TABLE points (id INTEGER PRIMARY KEY, label TEXT, grp INTEGER, v VECTOR(2))"
    )
    database.execute(
        "INSERT INTO points VALUES "
        "(1, 'origin', 0, [0, 0]), "
        "(2, 'east', 0, [1, 0]), "
        "(3, 'north', 1, [0, 1]), "
        "(4, 'far east', 1, [10, 0]), "
        "(5, 'north-east', 0, [3, 3]), "
        "(6, 'west', 1, [-2, 0]), "
        "(7, 'unknown', 0, NULL)"
    )
    yield database
    database.close()


def ids(db: mvrs_db.Database, tail: str) -> list[int]:
    return [row[0] for row in db.execute(f"SELECT id FROM points {tail}").rows]  # type: ignore[misc]


# ---------------------------------------------------------------------- distance functions


def test_known_distances() -> None:
    assert distance.distance((0.0, 0.0), (3.0, 4.0), L2) == 5.0
    assert distance.distance((1.0, 2.0), (1.0, 2.0), L2) == 0.0
    assert distance.distance((1.0, 0.0), (5.0, 0.0), COSINE) == pytest.approx(0.0)  # same direction
    assert distance.distance((1.0, 0.0), (0.0, 7.0), COSINE) == pytest.approx(1.0)  # perpendicular
    assert distance.distance((1.0, 0.0), (-3.0, 0.0), COSINE) == pytest.approx(2.0)  # opposite
    assert distance.distance((0.0, 0.0), (1.0, 0.0), COSINE) is None  # no direction
    assert distance.distance((1.0, 0.0), (0.0, 0.0), COSINE) is None


VECTORS = st.lists(st.floats(-100, 100, width=32, allow_nan=False), min_size=3, max_size=3).map(
    tuple
)


@settings(max_examples=200)
@given(st.lists(VECTORS, max_size=20), VECTORS, st.sampled_from([L2, COSINE]))
def test_batch_matches_the_reference(
    vectors: list[tuple[float, ...]], query: tuple[float, ...], metric: str
) -> None:
    results = {"numpy": distance.batch_distances(vectors, query, metric)}
    saved = distance._np
    try:
        distance._np = None
        results["python"] = distance.batch_distances(vectors, query, metric)
    finally:
        distance._np = saved

    for computed in results.values():
        assert len(computed) == len(vectors)
        for vector, value in zip(vectors, computed, strict=True):
            expected = reference(vector, query, metric)
            if expected is None:
                assert value is None
            else:
                assert value == pytest.approx(expected, abs=1e-6)
                assert value >= 0.0


def test_as_float32_rounds_like_storage() -> None:
    assert distance.as_float32((0.1, 1.0)) == (0.10000000149011612, 1.0)


# ---------------------------------------------------------------------- parsing


def test_distance_parses_in_select_list_and_order_by() -> None:
    statement = parse(
        "SELECT id, v <-> [1, 2] FROM t WHERE id > 0 ORDER BY v <=> [3, 4] DESC LIMIT 5"
    )
    assert isinstance(statement, ast.Select)
    assert statement.columns == ("id", ast.Distance("v", "<->", (1.0, 2.0)))
    assert statement.order_by == (ast.OrderItem(ast.Distance("v", "<=>", (3.0, 4.0)), True),)
    assert statement.limit == 5


def test_comparison_operators_still_lex_correctly() -> None:
    statement = parse("SELECT * FROM t WHERE a <= 1 AND b <> 2 AND c < -3 AND d >= 4")
    assert isinstance(statement, ast.Select)
    operators = []
    node = statement.where
    while isinstance(node, ast.And):
        assert isinstance(node.right, ast.Compare)
        operators.append(node.right.op)
        node = node.left
    assert isinstance(node, ast.Compare)
    assert [node.op, *reversed(operators)] == ["<=", "!=", "<", ">="]


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("SELECT * FROM t ORDER BY v <-> 5", "a vector such as"),
        ("SELECT * FROM t ORDER BY v <-> other", "a vector such as"),
        ("SELECT * FROM t ORDER BY v <->", "a vector such as"),
        ("SELECT * FROM t ORDER BY [1, 2] <-> v", "expected column name"),
        ("SELECT * FROM t WHERE v <-> [1, 2] < 3", "not in WHERE"),
        ("SELECT * FROM t WHERE v <=> [1, 2]", "not in WHERE"),
        ("UPDATE t SET d = v <-> [1, 2]", "only literal values"),
    ],
)
def test_distance_syntax_errors(statement: str, message: str) -> None:
    with pytest.raises(SqlSyntaxError, match=message):
        parse(statement)


# ---------------------------------------------------------------------- queries


def test_nearest_by_euclidean_distance(db: mvrs_db.Database, engine: str) -> None:
    assert ids(db, "ORDER BY v <-> [0.9, 0.1] LIMIT 3") == [2, 1, 3]
    assert ids(db, "ORDER BY v <-> [9, 0] LIMIT 1") == [4]
    assert ids(db, "ORDER BY v <-> [0, 0] LIMIT 1") == [1]


def test_nearest_by_cosine_distance_ignores_length(db: mvrs_db.Database, engine: str) -> None:
    # "far east" [10, 0] points the same way as [1, 0]; by angle it ties with "east".
    assert ids(db, "ORDER BY v <=> [1, 0] LIMIT 2") == [2, 4]
    assert ids(db, "ORDER BY v <=> [1, 1] LIMIT 1") == [5]
    # By straight-line distance the long vector is the farthest instead.
    assert ids(db, "ORDER BY v <-> [1, 0] LIMIT 2") == [2, 1]


def test_descending_returns_the_farthest(db: mvrs_db.Database, engine: str) -> None:
    assert ids(db, "ORDER BY v <-> [0, 0] DESC LIMIT 2") == [4, 5]
    assert ids(db, "ORDER BY v <=> [1, 0] DESC LIMIT 1") == [6]  # pointing the opposite way


def test_rows_without_a_distance_come_last(db: mvrs_db.Database, engine: str) -> None:
    # Row 7 has a NULL vector. Under cosine, row 1 (all zeros) has no direction either.
    assert ids(db, "ORDER BY v <-> [0, 0]") == [1, 2, 3, 6, 5, 4, 7]
    assert ids(db, "ORDER BY v <-> [0, 0] DESC") == [4, 5, 6, 2, 3, 1, 7]
    assert ids(db, "ORDER BY v <=> [1, 0]")[-2:] == [1, 7]
    assert ids(db, "ORDER BY v <=> [1, 0] DESC")[-2:] == [1, 7]
    assert ids(db, "ORDER BY v <-> [0, 0] LIMIT 100") == [1, 2, 3, 6, 5, 4, 7]


def test_ties_keep_storage_order(db: mvrs_db.Database, engine: str) -> None:
    # Rows 2 and 3 are both at distance 1 from the origin.
    assert ids(db, "ORDER BY v <-> [0, 0] LIMIT 3") == [1, 2, 3]
    assert ids(db, "ORDER BY v <-> [0, 0] LIMIT 2") == [1, 2]


def test_limit_edge_cases(db: mvrs_db.Database, engine: str) -> None:
    assert ids(db, "ORDER BY v <-> [0, 0] LIMIT 0") == []
    assert len(ids(db, "ORDER BY v <-> [0, 0] LIMIT 6")) == 6
    assert len(ids(db, "ORDER BY v <-> [0, 0]")) == 7


def test_where_is_applied_before_the_search(db: mvrs_db.Database, engine: str) -> None:
    assert ids(db, "WHERE grp = 1 ORDER BY v <-> [1, 0] LIMIT 2") == [3, 6]
    assert ids(db, "WHERE id >= 4 AND id <= 6 ORDER BY v <-> [0, 0] LIMIT 1") == [6]
    assert ids(db, "WHERE grp = 99 ORDER BY v <-> [0, 0] LIMIT 5") == []
    assert ids(db, "WHERE v IS NULL ORDER BY v <-> [0, 0] LIMIT 5") == [7]


def test_distance_in_the_select_list(db: mvrs_db.Database, engine: str) -> None:
    result = db.execute(
        "SELECT id, v <-> [3, 4], v <=> [1, 0] FROM points WHERE id <= 2 OR id = 7 ORDER BY id"
    )

    assert result.columns == ("id", "distance", "distance")
    assert result.rows == [
        (1, 5.0, None),  # the zero vector has no cosine distance
        (2, pytest.approx(math.dist((1, 0), (3, 4))), pytest.approx(0.0)),
        (7, None, None),
    ]


def test_selected_distance_matches_the_ordering(db: mvrs_db.Database, engine: str) -> None:
    rows = db.execute("SELECT v <-> [2, 2], id FROM points ORDER BY v <-> [2, 2] LIMIT 6").rows

    values = [row[0] for row in rows]
    assert values == sorted(values)  # type: ignore[type-var]
    assert rows[0] == (pytest.approx(math.sqrt(2)), 5)


def test_searching_for_a_stored_vector_finds_it_at_distance_zero(
    tmp_path: Path, engine: str
) -> None:
    with mvrs_db.connect(tmp_path / "exact.mvrs") as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v VECTOR(3))")
        db.execute("INSERT INTO t VALUES (1, [0.1, 0.2, 0.3]), (2, [0.3, 0.2, 0.1])")

        # 0.1 is not exact in 32 bits; the query is rounded the same way as the stored value.
        rows = db.execute("SELECT id, v <-> [0.1, 0.2, 0.3] FROM t ORDER BY v <-> [0.1, 0.2, 0.3]")

        assert rows.rows[0] == (1, 0.0)


def test_explain_shows_an_exact_scan(db: mvrs_db.Database, engine: str) -> None:
    lines = [
        row[0]
        for row in db.execute(
            "EXPLAIN SELECT id FROM points WHERE grp = 1 ORDER BY v <=> [1, 0] LIMIT 3"
        ).rows
    ]

    label = "NumPy" if engine == "numpy" else "pure Python"  # a WHERE clause: decoded rows
    assert lines[0].startswith("Project(id)")
    assert (
        lines[1].strip().startswith(f"VectorTopK(3 nearest by v <=> [1, 0]; exact scan, {label})")
    )
    assert "Filter(grp = 1)" in lines[2]
    assert "SeqScan(points)" in lines[3]


@pytest.mark.parametrize(
    ("tail", "error", "message"),
    [
        ("ORDER BY v <-> [1, 2, 3]", TypeMismatchError, "VECTOR\\(2\\).*has 3 numbers"),
        ("ORDER BY v <-> [1]", TypeMismatchError, "has 1 numbers"),
        ("ORDER BY label <-> [1, 2]", TypeMismatchError, "needs a VECTOR column.*is TEXT"),
        ("ORDER BY id <=> [1, 2]", TypeMismatchError, "is INTEGER"),
        ("ORDER BY v <=> [0, 0]", TypeMismatchError, "all-zero vector is undefined"),
        ("ORDER BY v <-> [1e39, 0]", TypeMismatchError, "too large for a 32-bit float"),
        ("ORDER BY nope <-> [1, 2]", ColumnNotFoundError, "no column named nope"),
        ("ORDER BY v <-> [1, 2], id", SqlError, "cannot be combined"),
        ("ORDER BY id, v <-> [1, 2]", SqlError, "cannot be combined"),
        ("ORDER BY v <-> [1, 2], v <=> [1, 2]", SqlError, "cannot be combined"),
    ],
)
def test_invalid_searches_are_rejected(
    db: mvrs_db.Database, tail: str, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        db.execute(f"SELECT id FROM points {tail}")


def test_invalid_distance_in_the_select_list(db: mvrs_db.Database) -> None:
    with pytest.raises(TypeMismatchError, match="has 3 numbers"):
        db.execute("SELECT v <-> [1, 2, 3] FROM points")
    with pytest.raises(TypeMismatchError, match="needs a VECTOR column"):
        db.execute("SELECT grp <-> [1, 2] FROM points")


def test_the_raw_path_and_the_general_path_agree(db: mvrs_db.Database) -> None:
    """No WHERE uses the undecoded-bytes path; any WHERE forces the decoded path."""
    if not distance.numpy_available():
        pytest.skip("NumPy is not installed")
    for tail in (
        "ORDER BY v <-> [2, 1]",
        "ORDER BY v <-> [2, 1] DESC LIMIT 3",
        "ORDER BY v <=> [1, 1]",
        "ORDER BY v <=> [-1, 0.5] LIMIT 4",
    ):
        raw = db.execute(f"SELECT id, v <-> [2, 1] FROM points {tail}")
        general = db.execute(f"SELECT id, v <-> [2, 1] FROM points WHERE id > 0 {tail}")
        assert raw.rows == general.rows, tail

    plan = "\n".join(
        r[0] for r in db.execute("EXPLAIN SELECT id FROM points ORDER BY v <-> [2, 1] LIMIT 3").rows
    )
    assert "NumPy on undecoded rows" in plan


def test_raw_vector_extraction_skips_earlier_columns_of_every_type() -> None:
    from mvrs_db.errors import CorruptRecordError
    from mvrs_db.storage.record import ColumnType, encode_record, raw_vector

    types = [
        ColumnType.INTEGER,
        ColumnType.TEXT,
        ColumnType.VECTOR,
        ColumnType.REAL,
        ColumnType.TEXT,
        ColumnType.VECTOR,
    ]
    row = (7, "héllo", (1.0, 2.0, 3.0), 2.5, None, (9.0, 8.0))
    record = encode_record(types, row)

    import struct

    assert raw_vector(types, record, 2, 3) == struct.pack("<3f", 1.0, 2.0, 3.0)
    assert raw_vector(types, record, 5, 2) == struct.pack("<2f", 9.0, 8.0)
    with_nulls = encode_record(types, (None, None, None, None, None, (4.0, 5.0)))
    assert raw_vector(types, with_nulls, 2, 3) is None
    assert raw_vector(types, with_nulls, 5, 2) == struct.pack("<2f", 4.0, 5.0)

    with pytest.raises(CorruptRecordError):
        raw_vector(types, record, 5, 3)  # the stored vector has 2 elements, not 3
    with pytest.raises(CorruptRecordError):
        raw_vector(types, record[:-1], 5, 2)  # cut short
    with pytest.raises(CorruptRecordError):
        raw_vector(types, b"\x02" + record[1:], 2, 3)  # unknown record format


# ---------------------------------------------------------------------- against brute force


@pytest.mark.parametrize("metric", [L2, COSINE])
def test_top_k_equals_brute_force_over_many_rows(tmp_path: Path, engine: str, metric: str) -> None:
    """More rows than one processing chunk, random data, checked against sorting everything."""
    rng = random.Random(7)
    dimension, count = 16, 2600
    vectors = {
        i: distance.as_float32([rng.uniform(-1, 1) for _ in range(dimension)]) for i in range(count)
    }
    vectors[13] = (0.0,) * dimension  # one row with no cosine distance
    symbol = distance.SYMBOLS[metric]

    with mvrs_db.connect(tmp_path / "many.mvrs", synchronous=False) as db:
        db.execute(f"CREATE TABLE e (id INTEGER PRIMARY KEY, v VECTOR({dimension}))")
        items = list(vectors.items())
        for start in range(0, count, 100):
            values = ", ".join(f"({i}, {literal(v)})" for i, v in items[start : start + 100])
            db.execute(f"INSERT INTO e VALUES {values}")

        for _ in range(5):
            query = distance.as_float32([rng.uniform(-1, 1) for _ in range(dimension)])
            scored = [(reference(v, query, metric), i) for i, v in vectors.items()]
            defined = sorted((d, i) for d, i in scored if d is not None)
            expected = [i for _d, i in defined[:10]]

            found = db.execute(
                f"SELECT id, v {symbol} {literal(query)} FROM e "
                f"ORDER BY v {symbol} {literal(query)} LIMIT 10"
            ).rows

            assert [row[0] for row in found] == expected
            for (row_id, value), (wanted, _i) in zip(found, defined, strict=False):
                assert value == pytest.approx(wanted, abs=1e-6), row_id

        everything = db.execute(f"SELECT id FROM e ORDER BY v {symbol} {literal(query)}").rows
        assert len(everything) == count
        if metric == COSINE:
            assert everything[-1] == (13,)


def test_search_survives_reopen_and_sees_updates(tmp_path: Path, engine: str) -> None:
    path = tmp_path / "persist.mvrs"
    with mvrs_db.connect(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v VECTOR(2))")
        db.execute("INSERT INTO t VALUES (1, [0, 0]), (2, [5, 5]), (3, [9, 9])")

    with mvrs_db.connect(path) as db:
        nearest = "SELECT id FROM t ORDER BY v <-> [6, 6] LIMIT 1"
        assert db.execute(nearest).rows == [(2,)]
        db.execute("UPDATE t SET v = [6, 6] WHERE id = 1")
        assert db.execute(nearest).rows == [(1,)]
        db.execute("DELETE FROM t WHERE id = 1")
        assert db.execute(nearest).rows == [(2,)]
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (4, [6, 6])")
        assert db.execute(nearest).rows == [(4,)]
        db.execute("ROLLBACK")
        assert db.execute(nearest).rows == [(2,)]
