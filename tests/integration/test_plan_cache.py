"""The plan cache: reused when safe, never wrong, and thrown away when stale."""

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
    database = mvrs_db.connect(tmp_path / "pc.mvrs", synchronous=False)
    database.execute(
        "CREATE TABLE t "
        "(id INTEGER PRIMARY KEY, g INTEGER, name TEXT, score REAL, u INTEGER UNIQUE)"
    )
    rows = [
        (i, None if i % 7 == 0 else i % 5, None if i % 11 == 0 else f"n{i % 9}", i / 4, i * 10)
        for i in range(1, 61)
    ]
    for row in rows:
        database.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?)", row)
    yield database
    database.close()


class PlanSpy:
    """Counts how many times the planner ran, so a reused plan can be told from a rebuilt one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.selects = 0
        self.access = 0
        select, access = executor_module.plan_select, executor_module.plan_access

        def counting_select(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.selects += 1
            return select(*args, **kwargs)

        def counting_access(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.access += 1
            return access(*args, **kwargs)

        monkeypatch.setattr(executor_module, "plan_select", counting_select)
        monkeypatch.setattr(executor_module, "plan_access", counting_access)

    @property
    def total(self) -> int:
        return self.selects + self.access


def plan_text(db: mvrs_db.Database) -> str:
    lines: list[str] = []
    for entry in db._executor.plans._entries.values():
        if entry.plan is not None:
            stack = [entry.plan]
            while stack:
                node = stack.pop()
                lines.append(node.describe())
                stack.extend(node.children)
    return "\n".join(lines)


# ---------------------------------------------------------------------- reuse


def test_a_point_lookup_is_planned_once(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PlanSpy(monkeypatch)
    sql = "SELECT name FROM t WHERE id = ?"
    for i in (1, 2, 3, 40, 59, 1):
        assert db.execute(sql, (i,)).rows == [(f"n{i % 9}" if i % 11 else None,)]
    assert spy.selects == 1
    assert db._executor.plans.hits == 5
    assert "IndexScan(t.id: id = ?1)" in plan_text(db)


def test_the_text_of_a_statement_is_the_key_not_its_values(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PlanSpy(monkeypatch)
    db.execute("SELECT name FROM t WHERE id = 1")
    db.execute("SELECT name FROM t WHERE id = 2")  # a different statement: planned again
    db.execute("SELECT name FROM t WHERE id = 1")  # the same text as the first: reused
    assert spy.selects == 2


def test_filters_on_unindexed_columns_limit_and_update_and_delete_reuse_plans(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PlanSpy(monkeypatch)
    for g in (1, 2, 3):
        db.execute("SELECT id FROM t WHERE g = ? LIMIT ?", (g, 4))
    for i in (5, 6, 8):
        db.execute("UPDATE t SET name = ? WHERE id = ?", (f"u{i}", i))
    for i in (9, 10, 12):
        db.execute("DELETE FROM t WHERE u = ?", (i * 10,))
    for name in ("n1", "n2"):
        db.execute("UPDATE t SET score = 0.5 WHERE name = ?", (name,))
    assert spy.selects == 1
    assert spy.access == 3  # one UPDATE by id, one DELETE by u, one UPDATE by name


def test_ranges_on_an_indexed_column_are_not_cached_but_still_correct(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PlanSpy(monkeypatch)
    sql = "SELECT id FROM t WHERE id > ? AND id <= ? ORDER BY id"
    for low, high in ((0, 5), (50, 60), (10, 12)):
        expected = [(i,) for i in range(low + 1, high + 1)]
        assert db.execute(sql, (low, high)).rows == expected
    # One refused attempt, then a fresh plan per execution: the best plan depends on the range.
    assert spy.selects == 4
    assert db._executor.plans.not_reusable == 1  # the refusal was recorded once, not retried
    assert db._executor.plans.hits == 0


def test_order_by_with_a_limit_parameter_is_not_cached(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PlanSpy(monkeypatch)
    sql = "SELECT id FROM t ORDER BY id LIMIT ?"
    assert db.execute(sql, (3,)).rows == [(1,), (2,), (3,)]
    assert db.execute(sql, (0,)).rows == []
    assert spy.selects == 3  # one refused attempt, then each execution planned on its own


def test_joins_aggregates_and_vectors_are_planned_each_time(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in (1, 2, 3):
        db.execute("SELECT g, COUNT(*) FROM t WHERE id > ? GROUP BY g", (i,))
        db.execute("SELECT a.id FROM t a JOIN t b ON a.id = b.id WHERE a.id = ?", (i,))
    assert len(db._executor.plans) == 0  # none of these kinds goes through the cache
    assert db._executor.plans.misses == 0


# ---------------------------------------------------------------------- equal to planning afresh


def uncached(db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db._executor, "_reusable", lambda *args, **kwargs: None)


def random_value(rng: random.Random) -> object:
    return rng.choice(
        [None, 0, 1, 2, 5, 7, 11, 33, 59, 60, 61, -3, 2.0, 2.5, 7.0, 1e9, "n3", "x", ""]
    )


QUERIES = [
    "SELECT id FROM t WHERE id = ?",
    "SELECT id, name FROM t WHERE u = ?",
    "SELECT id FROM t WHERE score = ?",
    "SELECT id FROM t WHERE g = ? AND name = ?",
    "SELECT id FROM t WHERE g != ? OR name IS NULL ORDER BY id",
    "SELECT id FROM t WHERE ? < g ORDER BY id DESC",
    "SELECT id FROM t WHERE id = ? OR g = ?",
    "SELECT id FROM t WHERE id = ? AND g = ?",
    "SELECT id FROM t WHERE g = ? LIMIT ?",
    "SELECT name FROM t WHERE name = ? ORDER BY id LIMIT 3",
    "SELECT id FROM t WHERE NOT (g = ?) AND id >= 0 ORDER BY id",
    "SELECT * FROM t WHERE u = ? AND id = ?",
]


@pytest.mark.parametrize("sql", QUERIES)
def test_cached_plans_give_the_answers_of_fresh_plans(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    rng = random.Random(sql)
    count = sql.count("?")
    cases = [tuple(random_value(rng) for _ in range(count)) for _ in range(60)]

    def run() -> list[object]:
        out: list[object] = []
        for params in cases:
            try:
                out.append(db.execute(sql, params).rows)
            except (TypeMismatchError, ParameterError) as exc:
                out.append(type(exc).__name__)
        return out

    with_cache = run()
    uncached(db, monkeypatch)
    assert with_cache == run()


def test_cached_update_and_delete_equal_fresh_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def scenario(cached: bool) -> list[tuple[object, ...]]:
        database = mvrs_db.connect(tmp_path / f"s{cached}.mvrs", synchronous=False)
        try:
            if not cached:
                monkeypatch.setattr(database._executor, "_reusable", lambda *a, **k: None)
            database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, g INTEGER, u INTEGER UNIQUE)")
            for i in range(40):
                database.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i % 4, i * 2))
            rng = random.Random(5)
            log: list[tuple[object, ...]] = []
            for _ in range(80):
                kind = rng.randrange(4)
                try:
                    if kind == 0:
                        r = database.execute(
                            "UPDATE t SET g = ? WHERE id = ?", (rng.randrange(5), rng.randrange(45))
                        )
                    elif kind == 1:
                        r = database.execute(
                            "UPDATE t SET g = ? WHERE g = ?", (rng.randrange(5), rng.randrange(5))
                        )
                    elif kind == 2:
                        r = database.execute("DELETE FROM t WHERE u = ?", (rng.randrange(90),))
                    else:
                        r = database.execute(
                            "INSERT INTO t VALUES (?, ?, ?)",
                            (100 + rng.randrange(60), 1, 1000 + rng.randrange(60)),
                        )
                    log.append((kind, r.rowcount))
                except mvrs_db.MvrsDbError as exc:
                    log.append((kind, type(exc).__name__))
            log.append(tuple(database.execute("SELECT id, g, u FROM t ORDER BY id").rows))
            return log
        finally:
            database.close()

    assert scenario(True) == scenario(False)


# ------------------------------------------------------------------ values the index cannot hold


def test_values_that_are_not_index_keys_still_match_like_literals(db: mvrs_db.Database) -> None:
    sql = "SELECT id FROM t WHERE id = ?"
    assert db.execute(sql, (3,)).rows == [(3,)]  # builds the cached plan
    assert db.execute(sql, (3.0,)).rows == [(3,)]  # a float equal to an integer key
    assert db.execute(sql, (3.5,)).rows == []
    assert db.execute(sql, (None,)).rows == []  # NULL equals nothing
    assert db.execute(sql, (10**15,)).rows == []
    with pytest.raises(TypeMismatchError):
        db.execute(sql, ("3",))  # the type check runs on every execution, not just the first
    assert db.execute(sql, (4,)).rows == [(4,)]
    assert db.execute("SELECT id FROM t WHERE id = 3.0").rows == [(3,)]  # as a literal


def test_a_real_key_and_a_text_key(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "keys.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE r (k REAL PRIMARY KEY, v TEXT)")
        database.execute("CREATE TABLE s (k TEXT PRIMARY KEY, v INTEGER)")
        database.execute("INSERT INTO r VALUES (1.5, 'a'), (2.0, 'b')")
        database.execute("INSERT INTO s VALUES ('x', 1), ('y', 2)")
        for _ in range(2):
            assert database.execute("SELECT v FROM r WHERE k = ?", (2,)).rows == [("b",)]
            assert database.execute("SELECT v FROM r WHERE k = ?", (1.5,)).rows == [("a",)]
            assert database.execute("SELECT v FROM s WHERE k = ?", ("y",)).rows == [(2,)]
            assert database.execute("SELECT v FROM s WHERE k = ?", ("z" * 300,)).rows == []
    finally:
        database.close()


def test_errors_are_raised_on_every_execution(db: mvrs_db.Database) -> None:
    with pytest.raises(mvrs_db.errors.ColumnNotFoundError):
        db.execute("SELECT nope FROM t WHERE id = ?", (1,))
    with pytest.raises(mvrs_db.errors.ColumnNotFoundError):
        db.execute("SELECT nope FROM t WHERE id = ?", (1,))  # nothing wrong was cached
    sql = "SELECT id FROM t WHERE g = ? LIMIT ?"
    assert db.execute(sql, (1, 2)).rows
    for bad in (-1, "2", 1.5, True):
        with pytest.raises(ParameterError):
            db.execute(sql, (1, bad))
    assert len(db.execute(sql, (1, 3)).rows) == 3


# ---------------------------------------------------------------------- staleness


def test_dropping_and_recreating_a_table_replans(db: mvrs_db.Database) -> None:
    sql = "SELECT id FROM t WHERE id = ?"
    assert db.execute(sql, (5,)).rows == [(5,)]
    db.execute("DROP TABLE t")
    db.execute("CREATE TABLE t (other TEXT, id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES ('x', 5)")
    assert db.execute(sql, (5,)).rows == [(5,)]
    assert db.execute("SELECT other FROM t WHERE id = ?", (5,)).rows == [("x",)]
    db.execute("DROP TABLE t")
    with pytest.raises(mvrs_db.errors.TableNotFoundError):
        db.execute(sql, (5,))


def test_a_rollback_invalidates_plans(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql = "SELECT name FROM t WHERE id = ?"
    db.execute(sql, (5,))
    spy = PlanSpy(monkeypatch)
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (500, 1, 'z', 1.0, 5000)")
    db.execute("ROLLBACK")
    assert db.execute(sql, (500,)).rows == []
    assert db.execute(sql, (5,)).rows == [("n5",)]
    assert spy.selects == 1  # rebuilt once after the reload, then reused


def test_a_dropped_table_rolled_back_is_usable_again(db: mvrs_db.Database) -> None:
    sql = "SELECT name FROM t WHERE id = ?"
    assert db.execute(sql, (5,)).rows == [("n5",)]
    db.execute("BEGIN")
    db.execute("DROP TABLE t")
    db.execute("ROLLBACK")
    assert db.execute(sql, (5,)).rows == [("n5",)]


def test_growth_triggers_a_new_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = mvrs_db.connect(tmp_path / "grow.mvrs", synchronous=False)
    try:
        database.execute("CREATE TABLE g (id INTEGER PRIMARY KEY, v INTEGER)")
        database.execute("INSERT INTO g VALUES (1, 1), (2, 2)")
        sql = "SELECT id FROM g WHERE v = ?"
        spy = PlanSpy(monkeypatch)
        database.execute(sql, (1,))
        database.execute(sql, (1,))
        assert spy.selects == 1
        database.execute(
            "INSERT INTO g VALUES " + ", ".join(f"({i}, {i % 7})" for i in range(10, 80))
        )
        database.execute(sql, (1,))  # 2 rows became 72: planned again
        assert spy.selects == 2
        database.execute(sql, (1,))
        assert spy.selects == 2
        database.execute("DELETE FROM g WHERE id > 0")
        database.execute(sql, (1,))  # and back to nothing
        assert spy.selects == 3
    finally:
        database.close()


# ---------------------------------------------------------------------- other contexts


def test_threads_share_cached_plans_with_their_own_values(db: mvrs_db.Database) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def work(seed: int) -> None:
        conn = db.connect()
        rng = random.Random(seed)
        barrier.wait()
        try:
            for _ in range(300):
                i = rng.randrange(1, 61)
                assert conn.execute("SELECT u FROM t WHERE id = ?", (i,)).rows == [(i * 10,)]
                g = rng.randrange(5)
                got = conn.execute("SELECT id FROM t WHERE g = ? ORDER BY id", (g,)).rows
                assert got == [(k,) for k in range(1, 61) if k % 7 and k % 5 == g]
        except (AssertionError, mvrs_db.MvrsDbError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s,)) for s in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []


def test_snapshot_isolation_reuses_plans_only_within_a_commit(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "snap.mvrs", isolation="snapshot", synchronous=False)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        database.execute("INSERT INTO t VALUES (1, 10), (2, 20)")
        reader, writer = database.connect(), database.connect()
        sql = "SELECT v FROM t WHERE id = ?"
        reader.execute("BEGIN")
        assert reader.execute(sql, (1,)).rows == [(10,)]
        writer.execute("UPDATE t SET v = ? WHERE id = ?", (11, 1))
        assert reader.execute(sql, (1,)).rows == [(10,)]  # still its snapshot
        assert writer.execute(sql, (1,)).rows == [(11,)]
        # the reader's snapshot and the writer's latest state each get correct answers
        assert reader.execute(sql, (1,)).rows == [(10,)]
        reader.execute("COMMIT")
        assert reader.execute(sql, (1,)).rows == [(11,)]
    finally:
        database.close()


def test_explain_shows_the_values_not_the_template(db: mvrs_db.Database) -> None:
    db.execute("SELECT name FROM t WHERE id = ?", (3,))
    plan = "\n".join(r[0] for r in db.execute("EXPLAIN SELECT name FROM t WHERE id = ?", (3,)).rows)  # type: ignore[misc]
    assert "id = 3" in plan and "?" not in plan


def test_the_cache_is_bounded(db: mvrs_db.Database) -> None:
    db._executor.plans._size = 10
    for i in range(1, 40):
        db.execute(f"SELECT name FROM t WHERE id = {i}")
    assert len(db._executor.plans) <= 10
