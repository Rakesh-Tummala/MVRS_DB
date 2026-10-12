"""Automatic ANALYZE: when it runs, what it counts, and what it must never do."""

from __future__ import annotations

import math
import random
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db import config
from mvrs_db.errors import ConstraintViolationError, LockTimeoutError


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "auto.mvrs", synchronous=False)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER, name TEXT)")
    yield database
    database.close()


def add(db: mvrs_db.Database, first: int, count: int) -> None:
    """Insert `count` rows, one statement (and so one commit) per row."""
    for i in range(first, first + count):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i % 7, f"n{i % 5}"))


def stats_rows(db: mvrs_db.Database) -> int | None:
    stats = db._catalog.get("t").stats
    return None if not stats else stats[0].rows


# ---------------------------------------------------------------------- the threshold


def test_a_new_table_is_analysed_after_fifty_changes(db: mvrs_db.Database) -> None:
    add(db, 1, config.AUTO_ANALYZE_MIN_CHANGES - 1)
    assert db.auto_analyzes == 0 and stats_rows(db) is None
    add(db, 100, 1)
    assert db.auto_analyzes == 1
    assert stats_rows(db) == config.AUTO_ANALYZE_MIN_CHANGES  # taken after the commit: includes it


def test_an_analysed_table_needs_fifty_plus_ten_percent_more(db: mvrs_db.Database) -> None:
    add(db, 1, 500)
    before = db.auto_analyzes
    assert before >= 1
    analysed_rows = stats_rows(db)
    assert analysed_rows is not None
    needed = math.ceil(
        config.AUTO_ANALYZE_MIN_CHANGES + config.AUTO_ANALYZE_FRACTION * analysed_rows
    )
    since = 500 - analysed_rows  # rows added after the last automatic analysis
    add(db, 1000, needed - since - 1)
    assert db.auto_analyzes == before  # one change short
    add(db, 5000, 1)
    assert db.auto_analyzes == before + 1


def test_the_statistics_describe_the_data_when_they_were_taken(db: mvrs_db.Database) -> None:
    add(db, 1, 50)
    stats = db._catalog.get("t").stats
    assert (stats[0].rows, stats[0].distinct) == (50, 50)
    assert stats[1].distinct == 7
    assert db._catalog.get("t").row_count == 50


def test_it_can_be_switched_off(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "off.mvrs", synchronous=False, auto_analyze=False)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER)")
        for i in range(300):
            database.execute("INSERT INTO t VALUES (?, ?)", (i, i % 3))
        assert database.auto_analyzes == 0 and database._catalog.get("t").stats == {}
        database.execute("ANALYZE t")  # explicit ANALYZE still works
        assert database._catalog.get("t").stats
    finally:
        database.close()


# ---------------------------------------------------------------------- what counts


def test_every_kind_of_change_counts_once_per_row(db: mvrs_db.Database) -> None:
    add(db, 1, 49)  # 49 changes: nothing yet
    assert db.auto_analyzes == 0
    db.execute("UPDATE t SET name = 'x' WHERE id = 1")  # the 50th
    assert db.auto_analyzes == 1

    db.execute("DELETE FROM t WHERE id > 40")  # 9 rows
    db.execute("UPDATE t SET g = 100 WHERE g < 3")  # many rows
    count = db.execute("SELECT COUNT(*) FROM t WHERE g = 100").rows[0][0]
    assert count > 0


def test_a_multi_row_statement_counts_all_its_rows(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, 1, 'a')" for i in range(60)))
    assert db.auto_analyzes == 1 and stats_rows(db) == 60


def test_an_update_that_changes_nothing_counts_nothing(db: mvrs_db.Database) -> None:
    add(db, 1, 10)
    for _ in range(100):
        db.execute("UPDATE t SET name = 'n1' WHERE id = 1")  # id 1 already has 'n1'
    for _ in range(100):
        db.execute("UPDATE t SET name = 'n1' WHERE id = 999")  # matches no row
    assert db.auto_analyzes == 0


def test_failed_statements_count_nothing(db: mvrs_db.Database) -> None:
    add(db, 1, 10)
    for _ in range(100):
        with pytest.raises(ConstraintViolationError):
            db.execute("INSERT INTO t VALUES (1, 1, 'dup')")
    assert db.auto_analyzes == 0


def test_rolled_back_changes_count_nothing_and_earlier_commits_are_not_forgotten(
    db: mvrs_db.Database,
) -> None:
    add(db, 1, 30)  # 30 committed changes
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, 1, 'r')" for i in range(1000, 1200)))
    db.execute("ROLLBACK")  # 200 changes that never happened (and the catalog reloads)
    assert db.auto_analyzes == 0
    add(db, 2000, 19)  # 49
    assert db.auto_analyzes == 0
    add(db, 3000, 1)  # 50 committed in all
    assert db.auto_analyzes == 1


def test_a_transaction_counts_when_it_commits_not_before(db: mvrs_db.Database) -> None:
    db.execute("BEGIN")
    for i in range(80):
        db.execute("INSERT INTO t VALUES (?, 1, 'a')", (i,))
        assert db.auto_analyzes == 0  # nothing runs inside the transaction
    db.execute("COMMIT")
    assert db.auto_analyzes == 1 and stats_rows(db) == 80


def test_dropping_a_table_forgets_its_count(db: mvrs_db.Database) -> None:
    add(db, 1, 40)
    db.execute("DROP TABLE t")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER, name TEXT)")
    add(db, 1, 49)
    assert db.auto_analyzes == 0  # the new table starts from zero, not from 40


# ---------------------------------------------------------------------- never harm the statement


def test_a_failed_analysis_leaves_the_statement_committed_and_is_retried(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = db._run
    refusals = [0]

    def refusing(txn, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        if type(statement).__name__ == "Analyze" and refusals[0] < 2:
            refusals[0] += 1
            raise LockTimeoutError("busy")
        return real(txn, statement, *args, **kwargs)

    monkeypatch.setattr(db, "_run", refusing)
    add(db, 1, 50)  # the 50th insert commits; its analysis is refused
    assert db.execute("SELECT COUNT(*) FROM t").rows == [(50,)]
    assert db.auto_analyzes == 0 and stats_rows(db) is None
    db.execute("INSERT INTO t VALUES (500, 1, 'x')")  # refused again
    assert db.auto_analyzes == 0
    db.execute("INSERT INTO t VALUES (501, 1, 'x')")  # now allowed
    assert db.auto_analyzes == 1 and stats_rows(db) == 52
    db.check()


def test_it_does_not_leave_a_transaction_open(db: mvrs_db.Database) -> None:
    add(db, 1, 60)
    assert db.auto_analyzes >= 1
    assert not db._pool.in_transaction
    db.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")  # the writer slot is free
    db.check()


def test_an_explicit_commit_triggers_it_after_releasing_its_locks(db: mvrs_db.Database) -> None:
    other = db.connect()
    other.execute("BEGIN")
    other.execute("SELECT COUNT(*) FROM t")  # a reader on t, in another connection...
    other.execute("COMMIT")  # ...which has finished before the writer commits
    conn = db.connect()
    conn.execute("BEGIN")
    for i in range(70):
        conn.execute("INSERT INTO t VALUES (?, 1, 'a')", (i,))
    conn.execute("COMMIT")
    assert db.auto_analyzes == 1


# ---------------------------------------------------------------------- the effect


def test_the_planner_gets_statistics_without_anyone_asking(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "plan.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE o (id INTEGER PRIMARY KEY, status TEXT)")
        database.execute("CREATE INDEX by_status ON o (status)")
        rng = random.Random(1)
        for start in range(0, 3000, 100):
            database.execute(
                "INSERT INTO o VALUES "
                + ", ".join(
                    f"({i}, '{'done' if rng.random() < 0.95 else 'late'}')"
                    for i in range(start, start + 100)
                )
            )
        assert database.auto_analyzes >= 1
        plan = "\n".join(
            r[0]  # type: ignore[misc]
            for r in database.execute("EXPLAIN SELECT id FROM o WHERE status = 'done'").rows
        )
        assert "SeqScan" in plan  # a common value: no index scan
    finally:
        database.close()


def test_statistics_taken_automatically_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "persist.mvrs"
    database = mvrs_db.connect(path, synchronous=False)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER)")
    for i in range(80):
        database.execute("INSERT INTO t VALUES (?, ?)", (i, i % 4))
    assert database.auto_analyzes >= 1
    stats = database._catalog.get("t").stats
    database.close()
    with mvrs_db.connect(path, auto_analyze=False) as again:
        assert again._catalog.get("t").stats == stats
        again.check()


def test_a_cached_plan_is_replaced_when_statistics_arrive(db: mvrs_db.Database) -> None:
    db.execute("CREATE INDEX by_g ON t (g)")
    sql = "SELECT COUNT(*) FROM t WHERE g = ?"
    for i in range(1, 30):
        db.execute("INSERT INTO t VALUES (?, ?, 'a')", (i, i % 3))
        db.execute(sql, (1,))
    assert db.auto_analyzes == 0
    plans = db._executor.plans
    refused = plans.not_reusable
    add(db, 1000, 40)  # crosses the threshold
    assert db.auto_analyzes >= 1
    for g in (0, 1, 2, 5, None):
        literal = "NULL" if g is None else g
        assert (
            db.execute(sql, (g,)).rows
            == db.execute(f"SELECT COUNT(*) FROM t WHERE g = {literal}").rows
        )
    assert plans.not_reusable == refused  # with statistics the plan is reusable now
    assert plans.hits >= 3


def test_it_works_under_snapshot_isolation(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "snap.mvrs", isolation="snapshot", synchronous=False)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER)")
        reader = database.connect()
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM t").rows == [(0,)]
        for i in range(70):
            database.execute("INSERT INTO t VALUES (?, ?)", (i, i % 5))
        assert database.auto_analyzes >= 1
        assert reader.execute("SELECT COUNT(*) FROM t").rows == [(0,)]  # its snapshot is unchanged
        reader.execute("COMMIT")
        assert reader.execute("SELECT COUNT(*) FROM t").rows == [(70,)]
        assert database.pool.version_count == 0
        database.check()
    finally:
        database.close()


# ---------------------------------------------------------------------- how often


def test_a_large_load_is_analysed_a_few_dozen_times_not_on_every_commit(
    tmp_path: Path,
) -> None:
    database = mvrs_db.connect(tmp_path / "load.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER)")
        commits_before = database.commits
        for start in range(0, 20000, 100):
            database.execute(
                "INSERT INTO t VALUES "
                + ", ".join(f"({i}, {i % 50})" for i in range(start, start + 100))
            )
        loads = database.commits - commits_before - database.auto_analyzes
        assert loads == 200
        assert 5 <= database.auto_analyzes <= 60, database.auto_analyzes
        assert database._catalog.get("t").stats[0].rows >= 20000 * 0.85  # not far behind
    finally:
        database.close()


# ---------------------------------------------------------------------- threads


def test_concurrent_writers_do_not_deadlock_or_corrupt(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "threads.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER)")
        database.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, g INTEGER)")
        errors: list[BaseException] = []

        def work(index: int) -> None:
            conn = database.connect()
            table = "t" if index % 2 else "u"
            try:
                for i in range(120):
                    conn.execute(f"INSERT INTO {table} VALUES (?, ?)", (index * 1000 + i, i % 9))
            except (AssertionError, mvrs_db.MvrsDbError) as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(k,)) for k in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
            assert not t.is_alive()
        assert errors == []
        assert database.auto_analyzes >= 2
        for name in ("t", "u"):
            stats = database._catalog.get(name).stats
            assert stats and stats[0].rows <= 360
        report = database.check()
        assert report.tables == {"t": 360, "u": 360}
    finally:
        database.close()
