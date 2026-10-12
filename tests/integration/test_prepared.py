"""Parameters (`?`) and prepared statements."""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db import database as database_module
from mvrs_db.errors import (
    ParameterError,
    SqlSyntaxError,
    TypeMismatchError,
)
from mvrs_db.sql import ast
from mvrs_db.sql.binding import bind
from mvrs_db.sql.parser import parse, parse_template


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "prep.mvrs", synchronous=False)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, score REAL, n INTEGER)")
    database.execute(
        "INSERT INTO t VALUES (1, 'ann', 1.5, 10), (2, 'bob', 2.5, NULL), (3, 'cy', 3.5, 30)"
    )
    yield database
    database.close()


# ---------------------------------------------------------------------- the parser


def test_placeholders_are_counted_in_order() -> None:
    statement, count = parse_template("SELECT * FROM t WHERE a = ? AND b < ? ORDER BY a LIMIT ?")
    assert count == 3
    assert isinstance(statement, ast.Select)
    assert statement.limit == ast.Param(2)
    assert statement.where == ast.And(
        ast.Compare("=", ast.ColumnRef("a"), ast.Literal(ast.Param(0))),  # type: ignore[arg-type]
        ast.Compare("<", ast.ColumnRef("b"), ast.Literal(ast.Param(1))),  # type: ignore[arg-type]
    )


def test_parse_refuses_placeholders() -> None:
    with pytest.raises(SqlSyntaxError, match="placeholder"):
        parse("SELECT * FROM t WHERE a = ?")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ? FROM t",
        "SELECT * FROM ?",
        "SELECT * FROM t WHERE ? = 1 AND",
        "INSERT INTO ? VALUES (1)",
        "UPDATE t SET ? = 1",
        "SELECT * FROM t ORDER BY ?",
        "SELECT * FROM t GROUP BY ?",
        "SELECT * FROM t WHERE a IS ?",
    ],
)
def test_placeholders_only_stand_for_values(sql: str) -> None:
    with pytest.raises(SqlSyntaxError):
        parse_template(sql)


def test_a_question_mark_in_a_string_is_just_text(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO t VALUES (9, 'what?', 0.0, 0)")
    assert db.execute("SELECT name FROM t WHERE id = ?", (9,)).rows == [("what?",)]
    assert db.execute("SELECT id FROM t WHERE name = 'what?'").rows == [(9,)]


# ---------------------------------------------------------------------- running


def test_select_insert_update_delete(db: mvrs_db.Database) -> None:
    assert db.execute("SELECT name FROM t WHERE id = ?", (2,)).rows == [("bob",)]
    assert db.execute("INSERT INTO t VALUES (?, ?, ?, ?)", (4, "di", 4.5, 40)).rowcount == 1
    assert db.execute("UPDATE t SET name = ?, n = ? WHERE id = ?", ("DI", None, 4)).rowcount == 1
    assert db.execute("SELECT name, n FROM t WHERE id = ?", (4,)).rows == [("DI", None)]
    assert db.execute("DELETE FROM t WHERE score > ?", (3.0,)).rowcount == 2
    assert db.execute("SELECT id FROM t ORDER BY id").rows == [(1,), (2,)]


def test_multi_row_insert_and_limit(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO t VALUES (?, ?, ?, ?), (?, ?, ?, ?)", (5, "e", 0.5, 1, 6, "f", 0.6, 2))
    assert db.execute("SELECT id FROM t ORDER BY id DESC LIMIT ?", (2,)).rows == [(6,), (5,)]
    assert db.execute("SELECT id FROM t LIMIT ?", (0,)).rows == []


def test_null_is_a_value_but_does_not_equal_anything(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO t VALUES (?, ?, ?, ?)", (7, None, None, None))
    assert db.execute("SELECT id FROM t WHERE n = ?", (None,)).rows == []  # NULL = NULL is unknown
    assert db.execute("SELECT id FROM t WHERE n IS NULL ORDER BY id").rows == [(2,), (7,)]


def test_a_parameter_uses_an_index_like_a_literal(db: mvrs_db.Database) -> None:
    plan = "\n".join(r[0] for r in db.execute("EXPLAIN SELECT * FROM t WHERE id = ?", (2,)).rows)  # type: ignore[misc]
    assert "IndexScan(t.id: id = 2)" in plan


def test_joins_aggregates_and_explain_take_parameters(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER, w INTEGER)")
    db.execute("INSERT INTO u VALUES (1, 1, 5), (2, 1, 6), (3, 2, 7)")
    sql = (
        "SELECT t.name, SUM(u.w) FROM t LEFT JOIN u ON u.t_id = t.id "
        "WHERE t.id < ? GROUP BY t.name ORDER BY t.name"
    )
    assert db.execute(sql, (3,)).rows == [("ann", 11), ("bob", 7)]
    assert db.execute(sql, (2,)).rows == [("ann", 11)]


def test_vector_parameters(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "vec.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE d (id INTEGER PRIMARY KEY, v VECTOR(2))")
        database.execute("INSERT INTO d VALUES (?, ?)", (1, [0.0, 0.0]))
        database.execute("INSERT INTO d VALUES (?, ?)", (2, (1, 1)))  # integers are fine
        database.execute("INSERT INTO d VALUES (3, [?, ?])", (5.0, 5.0))
        nearest = "SELECT id FROM d ORDER BY v <-> ? LIMIT ?"
        assert database.execute(nearest, ([0.9, 0.9], 2)).rows == [(2,), (1,)]
        elements = "SELECT id FROM d ORDER BY v <=> [?, 0.0] LIMIT 1"
        assert database.execute(elements, (1.0,)).rows[0] in [(1,), (2,)]
        with pytest.raises(TypeMismatchError):
            database.execute(nearest, ([1.0, 2.0, 3.0], 1))  # wrong dimension
    finally:
        database.close()


# ---------------------------------------------------------------------- refusing bad input


@pytest.mark.parametrize(
    ("sql", "params", "message"),
    [
        ("SELECT * FROM t WHERE id = ?", (), r"has 1 `\?` placeholder"),
        ("SELECT * FROM t WHERE id = ?", (1, 2), r"has 1 `\?` placeholder"),
        ("SELECT * FROM t", (1,), r"has 0 `\?` placeholder"),
        ("SELECT * FROM t WHERE id = ?", "5", "list or tuple"),
        ("SELECT * FROM t WHERE id = ?", (True,), "bool"),
        ("SELECT * FROM t WHERE id = ?", (2**63,), "64 bits"),
        ("SELECT * FROM t WHERE id = ?", (float("nan"),), "finite"),
        ("SELECT * FROM t WHERE score = ?", (float("inf"),), "finite"),
        ("SELECT * FROM t WHERE id = ?", (object(),), "cannot use a object"),
        ("SELECT * FROM t WHERE id = ?", (b"x",), "cannot use a bytes"),
        ("SELECT * FROM t LIMIT ?", (-1,), "non-negative"),
        ("SELECT * FROM t LIMIT ?", ("3",), "non-negative"),
        ("SELECT * FROM t LIMIT ?", (2.5,), "non-negative"),
        ("INSERT INTO t VALUES (1, 'a', ?, 1)", ([1.0, "x"],), "only numbers"),
        ("INSERT INTO t VALUES (1, 'a', ?, 1)", ([],), "at least one"),
    ],
)
def test_bad_parameters_are_refused(
    db: mvrs_db.Database, sql: str, params: object, message: str
) -> None:
    with pytest.raises(ParameterError, match=message):
        db.execute(sql, params)  # type: ignore[arg-type]
    assert db.execute("SELECT COUNT(*) FROM t").rows == [(3,)]  # nothing ran


def test_the_wrong_type_is_the_same_error_as_for_a_literal(db: mvrs_db.Database) -> None:
    with pytest.raises(TypeMismatchError):
        db.execute("SELECT * FROM t WHERE id = ?", ("abc",))
    with pytest.raises(TypeMismatchError):
        db.execute("SELECT * FROM t WHERE id = 'abc'")


def test_values_are_never_read_as_sql(db: mvrs_db.Database) -> None:
    evil = "x'); DROP TABLE t; --"
    db.execute("INSERT INTO t VALUES (?, ?, ?, ?)", (50, evil, 0.0, 0))
    assert db.execute("SELECT name FROM t WHERE id = ?", (50,)).rows == [(evil,)]
    assert db.execute("SELECT id FROM t WHERE name = ?", (evil,)).rows == [(50,)]
    assert db.table_names() == ["t"]
    # An embedded quote or newline needs no escaping.
    odd = 'it\'s\na "test"\\'
    db.execute("UPDATE t SET name = ? WHERE id = ?", (odd, 1))
    assert db.execute("SELECT name FROM t WHERE id = 1").rows == [(odd,)]


# ---------------------------------------------------------------------- equivalence


def literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def test_bound_statements_equal_the_same_statements_with_literals(db: mvrs_db.Database) -> None:
    rng = random.Random(3)
    db.execute("DELETE FROM t WHERE id > 0")
    for i in range(30):
        row = (
            i,
            rng.choice(["a", "b", None, "c'd"]),
            rng.choice([0.5, 1.5, None]),
            rng.choice([1, 2, None]),
        )
        db.execute("INSERT INTO t VALUES (?, ?, ?, ?)", row)
    for _ in range(80):
        low, high = sorted(rng.sample(range(30), 2))
        name = rng.choice(["a", "b", "c'd", None])
        limit = rng.randrange(0, 12)
        template = (
            "SELECT id, name FROM t WHERE id >= ? AND id <= ? AND (name = ? OR n IS NULL) "
            "ORDER BY id LIMIT ?"
        )
        inline = (
            f"SELECT id, name FROM t WHERE id >= {low} AND id <= {high} "
            f"AND (name = {literal(name)} OR n IS NULL) ORDER BY id LIMIT {limit}"
        )
        assert db.execute(template, (low, high, name, limit)).rows == db.execute(inline).rows


def test_bind_leaves_the_template_alone() -> None:
    template, count = parse_template("SELECT * FROM t WHERE a = ?")
    first = bind(template, (1,))
    second = bind(template, (2,))
    assert first != second
    assert bind(template, (1,)) == first
    assert template == parse_template("SELECT * FROM t WHERE a = ?")[0]
    assert count == 1


# ---------------------------------------------------------------------- prepared statements


def test_prepare_parses_once_and_runs_many_times(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    original = database_module.parse_template
    monkeypatch.setattr(
        database_module, "parse_template", lambda text: calls.append(text) or original(text)
    )
    lookup = db.connect().prepare("SELECT name FROM t WHERE id = ?")
    assert lookup.param_count == 1
    for i in (1, 2, 3, 1, 2, 3):
        assert lookup.execute((i,)).rows == [(["ann", "bob", "cy"][i - 1],)]
    assert len(calls) == 1


def test_prepare_reports_syntax_errors_early(db: mvrs_db.Database) -> None:
    with pytest.raises(SqlSyntaxError):
        db.connect().prepare("SELEKT 1")


def test_prepared_statements_run_inside_transactions(db: mvrs_db.Database) -> None:
    conn = db.connect()
    insert = conn.prepare("INSERT INTO t VALUES (?, ?, ?, ?)")
    conn.execute("BEGIN")
    insert.execute((10, "x", 0.0, 0))
    insert.execute((11, "y", 0.0, 0))
    conn.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM t").rows == [(3,)]
    conn.execute("BEGIN")
    insert.execute((10, "x", 0.0, 0))
    conn.execute("COMMIT")
    assert db.execute("SELECT COUNT(*) FROM t").rows == [(4,)]


def test_a_prepared_statement_sees_schema_changes(db: mvrs_db.Database) -> None:
    conn = db.connect()
    count = conn.prepare("SELECT COUNT(*) FROM t WHERE id > ?")
    assert count.execute((0,)).rows == [(3,)]
    db.execute("DROP TABLE t")
    with pytest.raises(mvrs_db.errors.TableNotFoundError):
        count.execute((0,))
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (5)")
    assert count.execute((0,)).rows == [(1,)]  # planned afresh against the new table


def test_prepared_statements_work_in_snapshot_mode(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "s.mvrs", isolation="snapshot", synchronous=False)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        database.execute("INSERT INTO t VALUES (?, ?)", (1, 10))
        reader, writer = database.connect(), database.connect()
        get = reader.prepare("SELECT v FROM t WHERE id = ?")
        reader.execute("BEGIN")
        assert get.execute((1,)).rows == [(10,)]
        writer.execute("UPDATE t SET v = ? WHERE id = ?", (11, 1))
        assert get.execute((1,)).rows == [(10,)]
    finally:
        database.close()


# ---------------------------------------------------------------------- the parse cache


def test_the_parse_cache_is_bounded_and_does_not_keep_failures(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database_module, "_PARSE_CACHE_SIZE", 8)
    for i in range(30):
        db.execute(f"SELECT {i} FROM t") if False else db.execute(
            f"SELECT id FROM t WHERE id = {i}"
        )
    assert len(db._parsed) == 8
    with pytest.raises(SqlSyntaxError):
        db.execute("SELEKT")
    assert "SELEKT" not in db._parsed
    huge = "SELECT id FROM t WHERE name = '" + "x" * 5000 + "'"
    db.execute(huge)
    assert huge not in db._parsed


def test_threads_can_share_cached_statements(db: mvrs_db.Database) -> None:
    errors: list[BaseException] = []

    def work(seed: int) -> None:
        conn = db.connect()
        rng = random.Random(seed)
        try:
            for _ in range(200):
                i = rng.choice([1, 2, 3])
                rows = conn.execute("SELECT name FROM t WHERE id = ?", (i,)).rows
                assert rows == [(["ann", "bob", "cy"][i - 1],)]
        except (AssertionError, mvrs_db.MvrsDbError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s,)) for s in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
