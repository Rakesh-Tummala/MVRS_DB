"""ANALYZE and the planner decisions that rest on column statistics."""

from __future__ import annotations

import random
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import TableNotFoundError
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse


def build(path: Path) -> mvrs_db.Database:
    """5,000 orders: status is very skewed, region mildly, amount roughly bell-shaped."""
    # auto_analyze=False: these tests start from a table with no statistics and call ANALYZE
    db = mvrs_db.connect(path, synchronous=False, auto_analyze=False)
    db.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, region INTEGER, amount REAL, "
        "note TEXT)"
    )
    rng = random.Random(8)
    rows = []
    for i in range(1, 5001):
        roll = rng.random()
        status = "done" if roll < 0.93 else rng.choice(["new", "late", "refunded", "lost"])
        region = min(int(rng.paretovariate(1.5)), 40)
        amount = None if rng.random() < 0.05 else round(rng.gauss(100, 30), 1)
        note = None if i % 4 else f"n{i % 50}"
        rows.append((i, status, region, amount, note))
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        db.execute(
            "INSERT INTO orders VALUES " + ", ".join("(?, ?, ?, ?, ?)" for _ in chunk),
            tuple(v for row in chunk for v in row),
        )
    return db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = build(tmp_path / "stats.mvrs")
    yield database
    database.close()


def explain(db: mvrs_db.Database, sql: str, params: tuple[object, ...] = ()) -> list[str]:
    return [str(r[0]) for r in db.execute(f"EXPLAIN {sql}", params).rows]


def estimate(db: mvrs_db.Database, sql: str, node: str) -> int:
    """The planner's row estimate for the first plan line starting with `node`."""
    for line in explain(db, sql):
        if line.strip().startswith(node):
            return int(re.search(r"rows~(\d+)", line).group(1))  # type: ignore[union-attr]
    raise AssertionError(f"no {node} in {explain(db, sql)}")


def actual(db: mvrs_db.Database, sql: str) -> int:
    return len(db.execute(sql).rows)


# ---------------------------------------------------------------------- the statement


def test_analyze_parses() -> None:
    assert parse("ANALYZE") == ast.Analyze(None)
    assert parse("ANALYZE orders;") == ast.Analyze("orders")


def test_analyze_one_table_and_all(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE other (id INTEGER PRIMARY KEY, v INTEGER)")
    db.execute("INSERT INTO other VALUES (1, 5), (2, 5), (3, NULL)")
    assert db.execute("ANALYZE orders").rowcount == 1
    assert set(db._catalog.get("orders").stats) == {0, 1, 2, 3, 4}
    assert db._catalog.get("other").stats == {}
    assert db.execute("ANALYZE").rowcount == 2
    assert db._catalog.get("other").stats[1].nulls == 1
    with pytest.raises(TableNotFoundError):
        db.execute("ANALYZE missing")


def test_what_is_collected(db: mvrs_db.Database) -> None:
    db.execute("ANALYZE orders")
    stats = db._catalog.get("orders").stats
    assert (stats[0].rows, stats[0].distinct, stats[0].nulls) == (5000, 5000, 0)
    done = dict(stats[1].common)["done"]
    assert done == db.execute("SELECT COUNT(*) FROM orders WHERE status = 'done'").rows[0][0]
    assert stats[1].distinct == 5
    assert (
        stats[3].nulls == db.execute("SELECT COUNT(*) FROM orders WHERE amount IS NULL").rows[0][0]
    )
    assert stats[4].nulls == 3750


def test_vector_columns_are_skipped(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "vec.mvrs", synchronous=False, auto_analyze=False)
    try:
        database.execute("CREATE TABLE v (id INTEGER PRIMARY KEY, e VECTOR(2), n INTEGER)")
        database.execute("INSERT INTO v VALUES (1, [1, 2], 5), (2, [3, 4], 6)")
        database.execute("ANALYZE v")
        assert set(database._catalog.get("v").stats) == {0, 2}
        database.check()
    finally:
        database.close()


# ---------------------------------------------------------------------- persistence


def test_statistics_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "persist.mvrs"
    database = build(path)
    database.execute("ANALYZE orders")
    before = database._catalog.get("orders").stats
    database.close()
    with mvrs_db.connect(path, auto_analyze=False) as again:
        assert again._catalog.get("orders").stats == before
        again.check()


def test_analyze_again_replaces_the_old_statistics(db: mvrs_db.Database) -> None:
    db.execute("ANALYZE orders")
    db.execute("DELETE FROM orders WHERE id > 1000")
    assert db._catalog.get("orders").stats[0].rows == 5000  # a snapshot: nothing updates it
    db.execute("ANALYZE orders")
    assert db._catalog.get("orders").stats[0].rows == 1000
    db.check()  # no leftover duplicate record: C5 is checked on load, and pages are accounted for
    assert (
        sum(1 for k in db._catalog._stats_rids if k[0] == db._catalog.get("orders").schema.table_id)
        == 5
    )


def test_analyze_is_undone_by_rollback(db: mvrs_db.Database) -> None:
    db.execute("BEGIN")
    db.execute("ANALYZE orders")
    assert db._catalog.get("orders").stats
    db.execute("ROLLBACK")
    assert db._catalog.get("orders").stats == {}
    db.check()


def test_dropping_a_table_removes_its_statistics(db: mvrs_db.Database) -> None:
    clean = db.check()
    db.execute("ANALYZE")
    db.execute("DROP TABLE orders")
    assert db._catalog._stats_rids == {}
    assert db.check().tables == {k: v for k, v in clean.tables.items() if k != "orders"}


def test_creating_and_dropping_an_index_keeps_the_statistics(db: mvrs_db.Database) -> None:
    db.execute("ANALYZE orders")
    before = db._catalog.get("orders").stats
    db.execute("CREATE INDEX by_status ON orders (status)")
    assert db._catalog.get("orders").stats == before
    db.execute("DROP INDEX by_status")
    assert db._catalog.get("orders").stats == before


def test_snapshot_readers_plan_with_the_statistics_of_their_snapshot(tmp_path: Path) -> None:
    database = build(tmp_path / "snap.mvrs")
    database.close()
    database = mvrs_db.connect(
        tmp_path / "snap.mvrs", isolation="snapshot", synchronous=False, auto_analyze=False
    )
    try:
        database.execute("CREATE INDEX by_status ON orders (status)")
        reader, writer = database.connect(), database.connect()
        reader.execute("BEGIN")
        sql = "EXPLAIN SELECT id FROM orders WHERE status = 'done'"
        before = [r[0] for r in reader.execute(sql).rows]
        writer.execute("ANALYZE orders")
        assert [r[0] for r in reader.execute(sql).rows] == before  # its snapshot has none
        reader.execute("COMMIT")
        after = [r[0] for r in reader.execute(sql).rows]
        assert after != before
    finally:
        database.close()


# ---------------------------------------------------------------------- the planner


def test_estimates_move_toward_the_truth_after_analyze(db: mvrs_db.Database) -> None:
    queries = [
        ("SELECT id FROM orders WHERE status = 'done'", "Filter"),
        ("SELECT id FROM orders WHERE status = 'refunded'", "Filter"),
        ("SELECT id FROM orders WHERE region = 1", "Filter"),
        ("SELECT id FROM orders WHERE region > 5", "Filter"),
        ("SELECT id FROM orders WHERE amount > 130", "Filter"),
        ("SELECT id FROM orders WHERE amount IS NULL", "Filter"),
        ("SELECT id FROM orders WHERE note IS NULL", "Filter"),
        ("SELECT id FROM orders WHERE status != 'done'", "Filter"),
    ]

    def error(sql: str, node: str) -> float:
        guess, truth = estimate(db, sql, node), max(actual(db, sql), 1)
        return max(guess, truth) / min(max(guess, 1), truth) if guess else float("inf")

    before = {sql: error(sql, node) for sql, node in queries}
    db.execute("ANALYZE orders")
    after = {sql: error(sql, node) for sql, node in queries}
    for sql, _node in queries:
        assert after[sql] <= before[sql] + 1e-9 or after[sql] < 2.0, (sql, before[sql], after[sql])
    assert max(after.values()) < 2.0, after  # within a factor of two for each of them
    assert max(before.values()) > 4.0  # and the guesses without statistics were clearly worse


def test_a_common_value_scans_and_a_rare_one_uses_the_index(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_status ON orders (status)")
    # Without statistics the planner cannot tell: it counts 64 index entries, sees "a lot",
    # and assumes a tenth of the table, which an index scan still wins.
    assert "IndexScan(orders.status" in "\n".join(
        explain(db, "SELECT id FROM orders WHERE status = 'done'")
    )
    db.execute("ANALYZE orders")
    common = "\n".join(explain(db, "SELECT id FROM orders WHERE status = 'done'"))
    rare = "\n".join(explain(db, "SELECT id FROM orders WHERE status = 'refunded'"))
    assert "SeqScan" in common and "IndexScan" not in common  # 93% of the table
    assert "IndexScan(orders.status" in rare  # well under 1%


def test_group_by_estimates_the_number_of_groups(db: mvrs_db.Database) -> None:
    sql = "SELECT status, COUNT(*) FROM orders GROUP BY status"
    before = estimate(db, sql, "HashAggregate")
    db.execute("ANALYZE orders")
    after = estimate(db, sql, "HashAggregate")
    assert after == 5  # five distinct statuses
    assert before != 5
    nulls = estimate(db, "SELECT note, COUNT(*) FROM orders GROUP BY note", "HashAggregate")
    # notes exist only for multiples of 4, so i % 50 takes the 25 even values: 25 groups + NULL
    groups = len(db.execute("SELECT note, COUNT(*) FROM orders GROUP BY note").rows)
    assert groups == 26 and nulls == groups


def test_join_estimates_use_distinct_counts(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "join.mvrs", synchronous=False, auto_analyze=False)
    try:
        database.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, k INTEGER)")
        database.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, k INTEGER)")
        for start in range(0, 2000, 500):
            database.execute(
                "INSERT INTO a VALUES "
                + ", ".join(f"({i}, {i % 100})" for i in range(start, start + 500))
            )
        for start in range(0, 1000, 500):
            database.execute(
                "INSERT INTO b VALUES "
                + ", ".join(f"({i}, {i % 100})" for i in range(start, start + 500))
            )
        sql = "SELECT a.id FROM a JOIN b ON a.k = b.k"
        truth = len(database.execute(sql).rows)  # 2000 * 1000 / 100 = 20,000
        assert truth == 20000
        before = estimate(database, sql, "HashJoin")
        database.execute("ANALYZE")
        after = estimate(database, sql, "HashJoin")
        assert abs(after - truth) / truth < 0.05
        assert abs(before - truth) > abs(after - truth) * 10
    finally:
        database.close()


# ---------------------------------------------------------------------- plan cache and answers


def test_a_parameter_on_a_repeated_value_index_becomes_cacheable(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_region ON orders (region)")
    sql = "SELECT COUNT(*) FROM orders WHERE region = ?"
    plans = db._executor.plans
    for region in (1, 2, 3):
        db.execute(sql, (region,))
    assert plans.not_reusable == 1 and plans.hits == 0  # no statistics: refused, planned each time
    db.execute("ANALYZE orders")
    for region in (1, 2, 3, 40, 99, None):
        got = db.execute(sql, (region,)).rows
        literal = "NULL" if region is None else region
        assert got == db.execute(f"SELECT COUNT(*) FROM orders WHERE region = {literal}").rows
    assert plans.hits >= 4  # one plan, built after ANALYZE, serves every value
    assert plans.not_reusable == 1


def test_the_same_answers_with_and_without_statistics(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_status ON orders (status)")
    db.execute("CREATE INDEX by_region ON orders (region)")
    db.execute("CREATE INDEX by_amount ON orders (amount)")
    rng = random.Random(3)
    queries = []
    for _ in range(60):
        col = rng.choice(["status", "region", "amount", "note"])
        value = {
            "status": rng.choice(["done", "new", "late", "zzz", "refunded"]),
            "region": rng.choice([1, 2, 5, 17, 40, 99]),
            "amount": rng.choice([50.0, 100.0, 130.5, 400.0]),
            "note": rng.choice(["n1", "n13", "none"]),
        }[col]
        op = rng.choice(["=", "<", ">=", "!=", "<=", ">"])
        queries.append((f"SELECT id FROM orders WHERE {col} {op} ? ORDER BY id", (value,)))
        queries.append(
            (
                f"SELECT status, COUNT(*) FROM orders WHERE {col} {op} ? "
                "GROUP BY status ORDER BY status",
                (value,),
            )
        )
    without = [db.execute(sql, params).rows for sql, params in queries]
    db.execute("ANALYZE")
    with_stats = [db.execute(sql, params).rows for sql, params in queries]
    assert with_stats == without


def test_analyze_replans_cached_statements(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_status ON orders (status)")
    sql = "SELECT COUNT(*) FROM orders WHERE status = 'done'"
    first = db.execute(sql).rows
    plan_before = "\n".join(explain(db, sql))
    db.execute("ANALYZE")
    assert db.execute(sql).rows == first
    assert "\n".join(explain(db, sql)) != plan_before
