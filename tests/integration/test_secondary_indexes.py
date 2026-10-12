"""Secondary (non-unique) indexes: CREATE INDEX, DROP INDEX, maintenance, planning."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import (
    CatalogError,
    KeyTooLargeError,
    SqlSyntaxError,
    TableNotFoundError,
)
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    # auto_analyze=False: whether a plan with a parameter is cacheable depends on whether the
    # column has statistics, which these tests want to control with explicit ANALYZE
    database = mvrs_db.connect(tmp_path / "sec.mvrs", synchronous=False, auto_analyze=False)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER, name TEXT, score REAL)")
    rows = [
        (i, None if i % 13 == 0 else i % 10, None if i % 17 == 0 else f"n{i % 7}", (i % 5) / 2)
        for i in range(1, 201)
    ]
    for row in rows:
        database.execute("INSERT INTO t VALUES (?, ?, ?, ?)", row)
    yield database
    database.close()


def plan(db: mvrs_db.Database, sql: str, params: tuple[object, ...] = ()) -> str:
    return "\n".join(str(r[0]) for r in db.execute(f"EXPLAIN {sql}", params).rows)


def index_scan_rows(db: mvrs_db.Database, sql: str, params: tuple[object, ...]) -> int:
    """How many rows the IndexScan in the statement's plan returns on its own."""
    from mvrs_db.execution.operators import IndexScan
    from mvrs_db.execution.params import bound_values
    from mvrs_db.sql.binding import bind, coerce_params
    from mvrs_db.sql.parser import parse_template

    values = coerce_params(params)
    statement, _count = parse_template(sql)
    assert isinstance(statement, ast.Select)
    plan, _names = db._executor._plan(bind(statement, values))  # type: ignore[arg-type]
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, IndexScan):
            with bound_values(values):
                return len(list(node.rows()))
        stack.extend(node.children)
    raise AssertionError("the plan has no IndexScan")


# ---------------------------------------------------------------------- the syntax


def test_create_and_drop_index_parse() -> None:
    assert parse("CREATE INDEX by_g ON t (g)") == ast.CreateIndex("by_g", "t", "g")
    assert parse("DROP INDEX by_g;") == ast.DropIndex("by_g")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX ON t (g)",
        "CREATE INDEX i t (g)",
        "CREATE INDEX i ON t",
        "CREATE INDEX i ON t ()",
        "CREATE INDEX i ON t (a, b)",
        "CREATE UNIQUE INDEX i ON t (a)",
        "DROP INDEX",
        "CREATE INDEX i ON t (g) WHERE g > 1",
    ],
)
def test_malformed_index_statements(sql: str) -> None:
    with pytest.raises(SqlSyntaxError):
        parse(sql)


# ---------------------------------------------------------------------- the same answers


QUERIES = [
    "SELECT id FROM t WHERE g = 3 ORDER BY id",
    "SELECT id FROM t WHERE g = 99",
    "SELECT id FROM t WHERE g > 7 ORDER BY id",
    "SELECT id FROM t WHERE g >= 7 AND g < 9 ORDER BY id",
    "SELECT id FROM t WHERE g <= 0 ORDER BY id",
    "SELECT id FROM t WHERE 4 = g ORDER BY id",
    "SELECT id FROM t WHERE g != 3 ORDER BY id",
    "SELECT id FROM t WHERE g = 3 AND name = 'n3' ORDER BY id",
    "SELECT id FROM t WHERE g IS NULL ORDER BY id",
    "SELECT id FROM t WHERE g = 3 OR g = 4 ORDER BY id",
    "SELECT id FROM t WHERE name = 'n2' ORDER BY id",
    "SELECT id FROM t WHERE name > 'n4' ORDER BY id",
    "SELECT id FROM t WHERE name >= 'n2' AND name <= 'n3' ORDER BY id",
    "SELECT id FROM t WHERE name < 'n1' ORDER BY id",
    "SELECT id FROM t WHERE name = '' ORDER BY id",
    "SELECT id FROM t WHERE score = 1.0 ORDER BY id",
    "SELECT id FROM t WHERE score > 0.5 AND score <= 1.5 ORDER BY id",
    "SELECT id FROM t WHERE g = 3.0 ORDER BY id",
    "SELECT id FROM t WHERE g = 3 ORDER BY id DESC LIMIT 4",
    "SELECT g, id FROM t WHERE g > 2 ORDER BY g, id LIMIT 7",
    "SELECT g, COUNT(*) FROM t WHERE g >= 5 GROUP BY g ORDER BY g",
    "SELECT MIN(g), MAX(g), MIN(name), MAX(name), MIN(score), MAX(score) FROM t",
    "SELECT COUNT(*) FROM t WHERE g = 6",
]


def indexed_copy(db: mvrs_db.Database) -> mvrs_db.Database:
    for column in ("g", "name", "score"):
        db.execute(f"CREATE INDEX ix_{column} ON t ({column})")
    return db


@pytest.mark.parametrize("sql", QUERIES)
def test_answers_do_not_change_when_an_index_is_added(db: mvrs_db.Database, sql: str) -> None:
    before = db.execute(sql).rows
    indexed_copy(db)
    assert db.execute(sql).rows == before
    db.check()


def test_the_index_is_used(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    assert "IndexScan(t.g: g = 99)" in plan(db, "SELECT id FROM t WHERE g = 99")
    text = plan(db, "SELECT id FROM t WHERE g = 99")
    assert "SeqScan" not in text


def test_a_common_value_is_scanned_not_looked_up(db: mvrs_db.Database) -> None:
    """One value in ten matches 20 rows of 200; the planner counts them and prefers the scan
    only when the value is common enough that an index would not pay."""
    db.execute("CREATE INDEX by_g ON t (g)")
    rare = plan(db, "SELECT id FROM t WHERE g = 99")
    assert "IndexScan(t.g" in rare
    # a range covering most of the table is not worth the index
    wide = plan(db, "SELECT id FROM t WHERE g >= 0")
    assert "SeqScan" in wide


def test_min_and_max_come_from_the_index_edge(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_name ON t (name)")
    text = plan(db, "SELECT MIN(name), MAX(name) FROM t")
    assert "MetadataAggregate" in text
    assert db.execute("SELECT MIN(name), MAX(name) FROM t").rows == [("n0", "n6")]


# ---------------------------------------------------------------------- maintenance


def lookup(db: mvrs_db.Database, column: str, value: object) -> list[object]:
    return [
        r[0] for r in db.execute(f"SELECT id FROM t WHERE {column} = ? ORDER BY id", (value,)).rows
    ]


def test_the_index_follows_inserts_updates_and_deletes(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    db.execute("INSERT INTO t VALUES (500, 3, 'x', 0.0), (501, 3, 'y', 0.0), (502, NULL, 'z', 0.0)")
    assert 500 in lookup(db, "g", 3) and 501 in lookup(db, "g", 3)
    assert db.execute("UPDATE t SET g = 8 WHERE id = 500").rowcount == 1
    assert 500 not in lookup(db, "g", 3) and 500 in lookup(db, "g", 8)
    assert db.execute("UPDATE t SET g = NULL WHERE id = 501").rowcount == 1
    assert 501 not in lookup(db, "g", 3)
    assert db.execute("UPDATE t SET g = 3 WHERE id = 502").rowcount == 1
    assert 502 in lookup(db, "g", 3)
    assert db.execute("DELETE FROM t WHERE g = 3").rowcount > 0
    assert lookup(db, "g", 3) == []
    db.check()


def test_updating_a_different_column_leaves_the_index_alone(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    before = lookup(db, "g", 4)
    db.execute("UPDATE t SET name = 'moved', score = 9.5 WHERE g = 4")
    assert lookup(db, "g", 4) == before
    db.check()


def test_a_row_that_moves_keeps_its_index_entry(tmp_path: Path) -> None:
    """Growing a record can move it to another page; its index entries must follow it."""
    database = mvrs_db.connect(tmp_path / "move.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, k INTEGER, pad TEXT)")
        database.execute("CREATE INDEX by_k ON m (k)")
        for i in range(60):
            database.execute("INSERT INTO m VALUES (?, ?, ?)", (i, i % 3, "x"))
        for i in range(60):
            database.execute("UPDATE m SET pad = ? WHERE id = ?", ("y" * 300, i))
        assert database.execute("SELECT COUNT(*) FROM m WHERE k = 1").rows == [(20,)]
        database.check()
    finally:
        database.close()


def test_a_rolled_back_change_leaves_the_index_as_it_was(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    before = lookup(db, "g", 5)
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (900, 5, 'q', 0.0)")
    db.execute("UPDATE t SET g = 5 WHERE id = 1")
    db.execute("DELETE FROM t WHERE g = 5 AND id > 100")
    db.execute("ROLLBACK")
    assert lookup(db, "g", 5) == before
    db.check()


def test_text_values_that_start_each_other_and_odd_characters(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "text.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE s (id INTEGER PRIMARY KEY, v TEXT)")
        database.execute("CREATE INDEX by_v ON s (v)")
        values = ["", "a", "ab", "abc", "ab\x00", "ab\x00c", "b", "é", "日本", "a", "ab", "ab"]
        for i, v in enumerate(values):
            database.execute("INSERT INTO s VALUES (?, ?)", (i, v))
        for v in set(values):
            want = [i for i, x in enumerate(values) if x == v]
            got = [
                r[0]
                for r in database.execute("SELECT id FROM s WHERE v = ? ORDER BY id", (v,)).rows
            ]
            assert got == want, repr(v)
        got = [
            r[0]
            for r in database.execute(
                "SELECT id FROM s WHERE v > 'ab' AND v < 'b' ORDER BY id"
            ).rows
        ]
        assert got == sorted(i for i, x in enumerate(values) if "ab" < x < "b")
        database.check()
    finally:
        database.close()


def test_a_value_too_long_for_a_key_is_refused_and_changes_nothing(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_name ON t (name)")
    count = db.execute("SELECT COUNT(*) FROM t").rows
    with pytest.raises(KeyTooLargeError):
        db.execute("INSERT INTO t VALUES (?, ?, ?, ?)", (700, 1, "x" * 250, 0.0))
    with pytest.raises(KeyTooLargeError):
        db.execute("UPDATE t SET name = ? WHERE id = 1", ("y" * 250,))
    assert db.execute("SELECT COUNT(*) FROM t").rows == count
    assert db.execute("SELECT name FROM t WHERE id = 1").rows == [("n1",)]
    db.check()
    # Looking such a value up is fine: nothing matches.
    assert db.execute("SELECT id FROM t WHERE name = ?", ("z" * 250,)).rows == []


# ---------------------------------------------------------------------- DDL


def test_index_names_and_columns_are_checked(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    with pytest.raises(CatalogError, match="already exists"):
        db.execute("CREATE INDEX by_g ON t (name)")
    with pytest.raises(CatalogError, match="already has an index"):
        db.execute("CREATE INDEX other ON t (g)")
    with pytest.raises(CatalogError, match="already has an index"):
        db.execute("CREATE INDEX on_pk ON t (id)")
    with pytest.raises(CatalogError, match="no column"):
        db.execute("CREATE INDEX nope ON t (missing)")
    with pytest.raises(TableNotFoundError):
        db.execute("CREATE INDEX nope ON missing (g)")
    with pytest.raises(CatalogError, match="no such index"):
        db.execute("DROP INDEX missing")
    db.execute("CREATE TABLE v (id INTEGER PRIMARY KEY, e VECTOR(2))")
    with pytest.raises(CatalogError, match="VECTOR"):
        db.execute("CREATE INDEX on_vec ON v (e)")
    db.check()


def test_an_index_name_is_global_across_tables(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, g INTEGER)")
    db.execute("CREATE INDEX shared ON t (g)")
    with pytest.raises(CatalogError, match="already exists"):
        db.execute("CREATE INDEX shared ON u (g)")


def test_drop_index_frees_its_pages_and_the_query_still_works(db: mvrs_db.Database) -> None:
    before = db.execute("SELECT id FROM t WHERE g = 4 ORDER BY id").rows
    pages = db.check()
    db.execute("CREATE INDEX by_g ON t (g)")
    assert "IndexScan(t.g" in plan(db, "SELECT id FROM t WHERE g = 99")
    db.execute("DROP INDEX by_g")
    assert "IndexScan(t.g" not in plan(db, "SELECT id FROM t WHERE g = 99")
    assert db.execute("SELECT id FROM t WHERE g = 4 ORDER BY id").rows == before
    after = db.check()  # also proves no page leaked
    assert after.tables == pages.tables
    db.execute("CREATE INDEX by_g ON t (g)")  # the name can be used again


def test_drop_table_takes_its_secondary_indexes_with_it(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    db.execute("CREATE INDEX by_name ON t (name)")
    db.execute("DROP TABLE t")
    db.check()  # no leaked index pages
    with pytest.raises(CatalogError, match="no such index"):
        db.execute("DROP INDEX by_g")


def test_create_index_can_be_rolled_back(db: mvrs_db.Database) -> None:
    clean = db.check()
    db.execute("BEGIN")
    db.execute("CREATE INDEX by_g ON t (g)")
    assert "IndexScan(t.g" in plan(db, "SELECT id FROM t WHERE g = 99")
    db.execute("ROLLBACK")
    assert "IndexScan(t.g" not in plan(db, "SELECT id FROM t WHERE g = 99")
    assert db.check().tables == clean.tables
    db.execute("CREATE INDEX by_g ON t (g)")  # and can be made again


def test_a_failed_create_index_leaves_nothing_behind(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO t VALUES (800, 1, 'k', 0.0)")
    db.execute("UPDATE t SET name = ? WHERE id = 800", ("w" * 250,))  # fine without an index
    with pytest.raises(KeyTooLargeError):
        db.execute("CREATE INDEX by_name ON t (name)")
    assert "by_name" not in str(db.execute("EXPLAIN SELECT id FROM t WHERE name = 'a'").rows)
    db.check()
    db.execute("CREATE INDEX by_g ON t (g)")  # the connection and catalog are still healthy


def test_the_index_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "persist.mvrs"
    with mvrs_db.connect(path, synchronous=False) as database:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER)")
        for i in range(100):
            database.execute("INSERT INTO t VALUES (?, ?)", (i, i % 4))
        database.execute("CREATE INDEX by_g ON t (g)")
        database.execute("INSERT INTO t VALUES (200, 2)")
    with mvrs_db.connect(path) as database:
        assert "IndexScan(t.g" in plan(database, "SELECT id FROM t WHERE g = 99")
        assert database.execute("SELECT COUNT(*) FROM t WHERE g = 2").rows == [(26,)]
        database.check()
        database.execute("DROP INDEX by_g")
    with mvrs_db.connect(path) as database:
        assert "IndexScan(t.g" not in plan(database, "SELECT id FROM t WHERE g = 99")
        database.check()


# ---------------------------------------------------------------------- planning and the plan cache


def test_parameters_on_a_secondary_index_are_planned_each_time(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    sql = "SELECT id FROM t WHERE g = ? ORDER BY id"
    before = db._executor.plans.not_reusable
    for g in (1, 2, 99):
        want = [(i,) for i in range(1, 201) if i % 13 and i % 10 == g]
        assert db.execute(sql, (g,)).rows == want
    assert db._executor.plans.not_reusable == before + 1  # refused once, not retried
    assert db._executor.plans.hits == 0
    # the primary key's lookups are still cached
    for i in (1, 2, 3):
        db.execute("SELECT g FROM t WHERE id = ?", (i,))
    assert db._executor.plans.hits == 2


def test_a_cached_plan_does_not_outlive_an_index(db: mvrs_db.Database) -> None:
    sql = "SELECT id FROM t WHERE name = ?"
    for _ in range(2):
        db.execute(sql, ("n1",))
    db.execute("CREATE INDEX by_name ON t (name)")
    assert db.execute(sql, ("n1",)).rows == db.execute("SELECT id FROM t WHERE name = 'n1'").rows
    db.execute("DROP INDEX by_name")
    assert db.execute(sql, ("n1",)).rows == db.execute("SELECT id FROM t WHERE name = 'n1'").rows


def test_snapshot_readers_see_the_index_as_of_their_snapshot(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "snap.mvrs", isolation="snapshot", synchronous=False)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER)")
        for i in range(30):
            database.execute("INSERT INTO t VALUES (?, ?)", (i, i % 3))
        reader, writer = database.connect(), database.connect()
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM t WHERE g = 1").rows == [(10,)]
        writer.execute("CREATE INDEX by_g ON t (g)")
        writer.execute("INSERT INTO t VALUES (100, 1)")
        writer.execute("DELETE FROM t WHERE g = 1 AND id < 10")
        assert reader.execute("SELECT COUNT(*) FROM t WHERE g = 1").rows == [(10,)]
        assert "IndexScan" not in "\n".join(
            str(r[0]) for r in reader.execute("EXPLAIN SELECT id FROM t WHERE g = 99").rows
        )
        reader.execute("COMMIT")
        assert reader.execute("SELECT COUNT(*) FROM t WHERE g = 1").rows == [(8,)]
        assert "IndexScan(t.g" in "\n".join(
            str(r[0]) for r in reader.execute("EXPLAIN SELECT id FROM t WHERE g = 99").rows
        )
    finally:
        database.close()


# ------------------------------------------------------ against a table without the index


@pytest.mark.parametrize("seed", range(8))
def test_random_workloads_agree_with_an_unindexed_copy(tmp_path: Path, seed: int) -> None:
    """The same random statements run on two tables, one with secondary indexes; every
    query must return the same rows, and the indexed table must pass the integrity check."""
    rng = random.Random(seed)
    database = mvrs_db.connect(tmp_path / "rand.mvrs", synchronous=False, buffer_pages=128)
    try:
        for name in ("plain", "fast"):
            database.execute(
                f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c REAL)"
            )
        database.execute("CREATE INDEX fast_a ON fast (a)")
        database.execute("CREATE INDEX fast_b ON fast (b)")
        database.execute("CREATE INDEX fast_c ON fast (c)")

        def value_a() -> object:
            return rng.choice([None, *range(-3, 8)])

        def value_b() -> object:
            return rng.choice([None, "", "a", "ab", "abc", "b", "ab\x00", "z" * 30, "é"])

        def value_c() -> object:
            return rng.choice([None, 0.0, -0.0, 0.5, 1.5, -2.0])

        def run(sql_template: str, params: tuple[object, ...]) -> tuple[object, ...]:
            results = []
            for name in ("plain", "fast"):
                sql = sql_template.replace("TBL", name)
                try:
                    r = database.execute(sql, params)
                    results.append(r.rows if r.rows else r.rowcount)
                except mvrs_db.MvrsDbError as exc:
                    results.append(type(exc).__name__)
            return tuple(results)

        for _ in range(220):
            op = rng.random()
            if op < 0.3:
                params = (rng.randrange(80), value_a(), value_b(), value_c())
                a, b = run("INSERT INTO TBL VALUES (?, ?, ?, ?)", params)
                assert a == b
            elif op < 0.45:
                column = rng.choice(["a", "b", "c"])
                value = {"a": value_a, "b": value_b, "c": value_c}[column]()
                a, b = run(f"UPDATE TBL SET {column} = ? WHERE id = ?", (value, rng.randrange(80)))
                assert a == b
            elif op < 0.55:
                column = rng.choice(["a", "b", "c"])
                value = {"a": value_a, "b": value_b, "c": value_c}[column]()
                a, b = run(
                    f"UPDATE TBL SET {column} = ? WHERE {column} = ?",
                    (value_a() if column == "a" else value, value),
                )
                assert a == b
            elif op < 0.65:
                column = rng.choice(["a", "b", "c"])
                value = {"a": value_a, "b": value_b, "c": value_c}[column]()
                a, b = run(f"DELETE FROM TBL WHERE {column} = ?", (value,))
                assert a == b
            elif op < 0.7:
                a, b = run(
                    "DELETE FROM TBL WHERE a > ? AND a <= ?",
                    (rng.randrange(-3, 4), rng.randrange(4, 9)),
                )
                assert a == b
            else:
                column = rng.choice(["a", "b", "c"])
                value = {"a": value_a, "b": value_b, "c": value_c}[column]()
                comparison = rng.choice(["=", "<", "<=", ">", ">=", "!="])
                extra = rng.choice(["", " AND id < 40", " OR id = 3"])
                sql = (
                    f"SELECT id, a, b, c FROM TBL WHERE {column} {comparison} ?{extra} ORDER BY id"
                )
                a, b = run(sql, (value,))
                assert a == b, (sql, value)
                sql = f"SELECT id FROM TBL WHERE {column} {comparison} 3 ORDER BY {column}, id"
                if column == "b":
                    sql = (
                        f"SELECT id FROM TBL WHERE {column} {comparison} 'ab' ORDER BY {column}, id"
                    )
                a, b = run(sql, ())
                assert a == b, sql
        for q in (
            "SELECT MIN(a), MAX(a), MIN(b), MAX(b), MIN(c), MAX(c) FROM TBL",
            "SELECT COUNT(*) FROM TBL",
        ):
            a, b = run(q, ())
            assert a == b
        database.check()
        # the comparison above is only worth anything if the index really was used
        assert "IndexScan(fast.a" in plan(database, "SELECT id FROM fast WHERE a = 99")
        assert "IndexScan(fast.b" in plan(database, "SELECT id FROM fast WHERE b = 'nope'")
    finally:
        database.close()


def test_duplicate_values_are_counted_once_each(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    by_value = Counter(r[0] for r in db.execute("SELECT g FROM t").rows)
    for value, count in by_value.items():
        if value is not None:
            assert db.execute("SELECT COUNT(*) FROM t WHERE g = ?", (value,)).rows == [(count,)]


def test_every_comparison_operator_scans_the_index_and_is_exact(tmp_path: Path) -> None:
    """A selective range on a big enough table makes the planner use the index. Checks each
    operator at the edges (the first and last value, a value between two, one not present)."""
    database = mvrs_db.connect(tmp_path / "ops.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE r (id INTEGER PRIMARY KEY, k INTEGER, s TEXT)")
        values = [(i, (i * 7) % 400, f"s{(i * 7) % 400:04d}") for i in range(1, 801)]
        database.execute(
            "INSERT INTO r VALUES " + ", ".join(f"({i}, {k}, '{s}')" for i, k, s in values)
        )
        database.execute("CREATE INDEX by_k ON r (k)")
        database.execute("CREATE INDEX by_s ON r (s)")
        for column, edge, pick in (("k", 395, lambda v: v[1]), ("s", "s0395", lambda v: v[2])):
            low = 3 if column == "k" else "s0003"
            probes = [low, edge, 0 if column == "k" else "s0000", 399 if column == "k" else "s0399",
                      1000 if column == "k" else "s9999", -5 if column == "k" else "a"]  # fmt: skip
            for probe in probes:
                for op, test in (
                    ("=", lambda a, b: a == b),
                    ("<", lambda a, b: a < b),
                    ("<=", lambda a, b: a <= b),
                    (">", lambda a, b: a > b),
                    (">=", lambda a, b: a >= b),
                ):
                    text = f"SELECT id FROM r WHERE {column} {op} ? ORDER BY id"
                    want = [(v[0],) for v in values if test(pick(v), probe)]
                    assert database.execute(text, (probe,)).rows == want, (column, op, probe)
                    uses_index = "IndexScan(r." + column in plan(database, text, (probe,))
                    selective = len(want) < 150
                    if selective:
                        assert uses_index, (column, op, probe, len(want))
                        # The Filter above an index scan hides a range that is too wide, so
                        # count what the scan itself returns: exactly the matching rows.
                        assert index_scan_rows(database, text, (probe,)) == len(want), (
                            column, op, probe)  # fmt: skip
        lo, hi = 100, 103
        between = f"SELECT id FROM r WHERE k > {lo} AND k <= {hi} ORDER BY id"
        assert "IndexScan(r.k" in plan(database, between)
        assert database.execute(between).rows == [(v[0],) for v in values if lo < v[1] <= hi]
        database.check()
    finally:
        database.close()
