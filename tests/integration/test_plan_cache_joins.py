"""Cached plans for joins and aggregates."""

# The statements under test are written on one line each so they can be read as SQL.
# ruff: noqa: E501

from __future__ import annotations

import random
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import ParameterError, TypeMismatchError
from mvrs_db.execution import executor as executor_module


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "pcj.mvrs", synchronous=False)
    database.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, score REAL)"
    )
    database.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, cid INTEGER, total REAL, city TEXT)"
    )
    database.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, oid INTEGER, qty INTEGER)")
    rng = random.Random(1)
    cities = ["pune", "delhi", "goa", None]
    for i in range(1, 41):
        database.execute(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            (i, None if i % 9 == 0 else f"c{i}", cities[i % 4], rng.choice([None, 0.5, 1.5, 2.5])),
        )
    for i in range(1, 121):
        database.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            (i, rng.choice([None, *range(1, 46)]), float(rng.randrange(0, 50)), cities[i % 4]),
        )
    for i in range(1, 241):
        database.execute(
            "INSERT INTO items VALUES (?, ?, ?)", (i, rng.choice([None, *range(1, 130)]), i % 6)
        )
    yield database
    database.close()


class PlanSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.selects = 0
        original = executor_module.plan_select

        def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.selects += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(executor_module, "plan_select", counting)


def uncached(db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db._executor, "_reusable", lambda *args, **kwargs: None)


# ---------------------------------------------------------------------- reuse


def test_a_join_is_planned_once(db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = PlanSpy(monkeypatch)
    sql = (
        "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id "
        "WHERE o.total > ? AND c.city = ?"
    )
    for total in (5.0, 20.0, 45.0, 5.0):
        db.execute(sql, (total, "pune"))
    assert spy.selects == 1
    assert db._executor.plans.hits == 3


def test_aggregates_group_by_and_limit_parameters_are_planned_once(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PlanSpy(monkeypatch)
    for total in (1.0, 10.0, 30.0):
        db.execute(
            "SELECT city, COUNT(*), SUM(total) FROM orders WHERE total > ? GROUP BY city", (total,)
        )
        db.execute(
            "SELECT city, COUNT(*) FROM orders WHERE total < ? GROUP BY city LIMIT ?", (total, 2)
        )
        db.execute("SELECT COUNT(*), MIN(id), MAX(id) FROM orders")
    assert spy.selects == 3


def test_joins_with_indexed_parameters_are_cached_or_refused_as_before(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PlanSpy(monkeypatch)
    # equality on a primary key: always one row, so reusable
    for i in (1, 2, 3):
        db.execute(
            "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id WHERE o.id = ?", (i,)
        )
    assert spy.selects == 1
    # a range on a primary key depends on the value: refused, planned each time
    spy.selects = 0
    for i in (1, 2, 3):
        db.execute(
            "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id WHERE o.id > ?", (i,)
        )
    assert spy.selects == 1 + 3  # one refused attempt, then a plan per execution


# ---------------------------------------------------------------------- equal to planning afresh


JOIN_QUERIES = [
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id WHERE c.id = ?",
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id WHERE o.total > ?",
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id WHERE c.city = ? AND o.total < ?",
    "SELECT o.id, c.city FROM orders o LEFT JOIN customers c ON o.cid = c.id WHERE c.city = ?",
    "SELECT o.id, c.city FROM orders o LEFT JOIN customers c ON o.cid = c.id WHERE o.total >= ?",
    "SELECT o.id, c.city FROM orders o LEFT JOIN customers c ON o.cid = c.id WHERE c.city IS NULL AND o.total > ?",
    "SELECT o.id, c.id FROM orders o RIGHT JOIN customers c ON o.cid = c.id WHERE c.score = ?",
    "SELECT o.id, c.id FROM orders o FULL JOIN customers c ON o.cid = c.id WHERE o.total > ? OR c.score > ?",
    "SELECT o.id, c.id FROM orders o JOIN customers c ON o.cid = c.id WHERE o.total > ? OR c.score > ?",
    "SELECT o.id, c.id FROM orders o JOIN customers c ON o.cid = c.id WHERE o.city = c.city AND o.total > ?",
    "SELECT o.id, c.name, i.qty FROM orders o JOIN customers c ON o.cid = c.id JOIN items i ON i.oid = o.id WHERE i.qty >= ? AND c.city = ?",
    "SELECT c.name, i.qty FROM customers c LEFT JOIN orders o ON o.cid = c.id LEFT JOIN items i ON i.oid = o.id WHERE c.id = ?",
    "SELECT c.name, i.qty FROM customers c LEFT JOIN orders o ON o.cid = c.id LEFT JOIN items i ON i.oid = o.id WHERE i.qty = ?",
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id WHERE o.id = ? AND c.name = ?",
    "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id ORDER BY o.id LIMIT ?",
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id WHERE o.total > ? ORDER BY o.total DESC, o.id LIMIT ?",
    "SELECT c.city, COUNT(*), SUM(o.total) FROM orders o JOIN customers c ON o.cid = c.id WHERE o.total > ? GROUP BY c.city",
    "SELECT c.city, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.cid = c.id WHERE c.score != ? GROUP BY c.city",
    "SELECT city, COUNT(*), MIN(total), MAX(total), AVG(total) FROM orders WHERE total > ? GROUP BY city ORDER BY city LIMIT ?",
    "SELECT COUNT(*) FROM orders WHERE total = ? OR city = ?",
]


def random_value(rng: random.Random) -> object:
    return rng.choice(
        [
            None,
            0,
            1,
            3,
            17,
            44,
            130,
            2.5,
            0.5,
            20.0,
            49.0,
            100.0,
            "pune",
            "goa",
            "c3",
            "nope",
            "",
            -1,
        ]
    )


@pytest.mark.parametrize("sql", JOIN_QUERIES)
def test_cached_join_plans_give_the_answers_of_fresh_plans(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    rng = random.Random(sql)
    count = sql.count("?")
    cases = []
    for _ in range(40):
        params = [random_value(rng) for _ in range(count)]
        if "LIMIT ?" in sql:
            params[-1] = rng.choice([0, 1, 3, 10, 500])
        cases.append(tuple(params))

    def run() -> list[object]:
        out: list[object] = []
        for params in cases:
            try:
                out.append(sorted(map(repr, db.execute(sql, params).rows)) if "ORDER BY" not in sql
                           else db.execute(sql, params).rows)  # fmt: skip
            except (TypeMismatchError, ParameterError) as exc:
                out.append(type(exc).__name__)
        return out

    with_cache = run()
    uncached(db, monkeypatch)
    assert with_cache == run()


def test_the_inner_condition_of_an_index_join_takes_its_parameter(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One order joined to its customer through the primary key, with a condition on the customer."""
    sql = "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id WHERE o.id = ? AND c.city = ?"
    plan = "\n".join(r[0] for r in db.execute("EXPLAIN " + sql, (5, "goa")).rows)  # type: ignore[misc]
    assert "IndexJoin" in plan, plan
    cases = [(i, city) for i in range(1, 60) for city in ("pune", "goa", None)]
    first = [db.execute(sql, case).rows for case in cases]
    uncached(db, monkeypatch)
    assert first == [db.execute(sql, case).rows for case in cases]
    with pytest.raises(TypeMismatchError):
        db.execute(sql, (5, 7))


def test_type_errors_in_a_join_are_raised_on_every_execution(db: mvrs_db.Database) -> None:
    sql = "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id WHERE o.total > ?"
    assert db.execute(sql, (10.0,)).rows
    for _ in range(3):
        with pytest.raises(TypeMismatchError):
            db.execute(sql, ("ten",))
    assert db.execute(sql, (10.0,)).rows


# ---------------------------------------------------------------------- staleness


def test_growth_of_a_joined_table_replans(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql = "SELECT o.id FROM orders o JOIN items i ON i.oid = o.id WHERE i.qty = ?"
    spy = PlanSpy(monkeypatch)
    db.execute(sql, (1,))
    db.execute(sql, (2,))
    assert spy.selects == 1
    # items is the second table of the join: growing it must matter too
    db.execute(
        "INSERT INTO items VALUES "
        + ", ".join(f"({i}, {i % 100 + 1}, 1)" for i in range(1000, 1800))
    )
    db.execute(sql, (1,))
    assert spy.selects == 2


def test_ddl_and_rollback_replan_joins(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql = "SELECT o.id, c.name FROM orders o JOIN customers c ON o.cid = c.id WHERE o.total > ?"
    before = db.execute(sql, (10.0,)).rows
    db.execute(sql, (10.0,))
    spy = PlanSpy(monkeypatch)
    db.execute("BEGIN")
    db.execute("DELETE FROM customers WHERE id > 0")
    db.execute("ROLLBACK")
    assert db.execute(sql, (10.0,)).rows == before
    assert spy.selects == 1
    db.execute("DROP TABLE customers")
    with pytest.raises(mvrs_db.errors.TableNotFoundError):
        db.execute(sql, (10.0,))
    db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO customers VALUES (1, 'only')")
    want = [
        (i, "only")
        for (i,) in db.execute(
            "SELECT id FROM orders WHERE cid = 1 AND total > 10.0 ORDER BY id"
        ).rows
    ]
    assert sorted(db.execute(sql, (10.0,)).rows) == want


def test_creating_an_index_changes_the_plan_of_a_cached_join(db: mvrs_db.Database) -> None:
    sql = "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id WHERE c.city = ?"
    first = sorted(db.execute(sql, ("pune",)).rows)
    db.execute("CREATE INDEX by_city ON customers (city)")
    assert sorted(db.execute(sql, ("pune",)).rows) == first
    db.execute("DROP INDEX by_city")
    assert sorted(db.execute(sql, ("pune",)).rows) == first


# ---------------------------------------------------------------------- other contexts


def test_threads_share_a_cached_join_plan(db: mvrs_db.Database) -> None:
    sql = "SELECT COUNT(*) FROM orders o JOIN customers c ON o.cid = c.id WHERE o.total > ? AND c.city = ?"
    expected = {
        (t, city): db.execute(
            "SELECT COUNT(*) FROM orders o JOIN customers c ON o.cid = c.id "
            f"WHERE o.total > {t} AND c.city = '{city}'"
        ).rows
        for t in (5.0, 25.0, 45.0)
        for city in ("pune", "goa")
    }
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def work(seed: int) -> None:
        conn = db.connect()
        rng = random.Random(seed)
        barrier.wait()
        try:
            for _ in range(150):
                key = (rng.choice([5.0, 25.0, 45.0]), rng.choice(["pune", "goa"]))
                assert conn.execute(sql, key).rows == expected[key]
        except (AssertionError, mvrs_db.MvrsDbError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s,)) for s in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []


def test_joins_in_snapshot_mode(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "snap.mvrs", isolation="snapshot", synchronous=False)
    try:
        database.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, k INTEGER)")
        database.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, v INTEGER)")
        for i in range(1, 21):
            database.execute("INSERT INTO a VALUES (?, ?)", (i, i))
            database.execute("INSERT INTO b VALUES (?, ?)", (i, i * 10))
        reader, writer = database.connect(), database.connect()
        sql = "SELECT a.id, b.v FROM a JOIN b ON a.k = b.id WHERE b.v > ? ORDER BY a.id"
        reader.execute("BEGIN")
        old = reader.execute(sql, (150,)).rows
        assert old == [(i, i * 10) for i in range(16, 21)]
        writer.execute("DELETE FROM b WHERE id > 16")
        writer.execute("UPDATE b SET v = 999 WHERE id = 1")
        assert reader.execute(sql, (150,)).rows == old
        assert writer.execute(sql, (150,)).rows == [(1, 999), (16, 160)]
        reader.execute("COMMIT")
        assert reader.execute(sql, (150,)).rows == [(1, 999), (16, 160)]
    finally:
        database.close()


def test_explain_of_a_join_shows_the_values(db: mvrs_db.Database) -> None:
    sql = "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id WHERE c.city = ?"
    db.execute(sql, ("pune",))
    text = "\n".join(r[0] for r in db.execute("EXPLAIN " + sql, ("pune",)).rows)  # type: ignore[misc]
    assert "'pune'" in text and "?" not in text


def test_a_mistyped_parameter_is_an_error_even_if_its_operator_never_runs(
    db: mvrs_db.Database,
) -> None:
    """Regression: the type check used to wait for the operator to start. Under LIMIT 0, or on
    the probe side of a join whose build side is empty, it never did, and the query quietly
    returned nothing where planning the same statement with a literal raises."""
    first = "SELECT id FROM orders WHERE id = ? LIMIT ?"
    assert db.execute(first, (1, 5)).rows == [(1,)]  # builds the cached plan
    with pytest.raises(TypeMismatchError):
        db.execute(first, ("abc", 0))
    with pytest.raises(TypeMismatchError):
        db.execute("SELECT id FROM orders WHERE id = 'abc' LIMIT 0")  # the literal's behaviour

    join = (
        "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id "
        "WHERE o.total > ? AND c.city = ?"
    )
    assert db.execute(join, (1000.0, "pune")).rows == []  # nothing is that large: empty build side
    with pytest.raises(TypeMismatchError):
        db.execute(join, (1000.0, 7))  # the probe side would never start, but 7 is not a city
