"""BEGIN / COMMIT / ROLLBACK and atomicity, on a single connection."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import (
    ConstraintViolationError,
    EngineFailedError,
    SqlSyntaxError,
    StorageIOError,
    TableNotFoundError,
    TransactionError,
    TransactionTooLargeError,
    TypeMismatchError,
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "txn.mvrs", synchronous=False)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE, n INTEGER)")
    database.execute("INSERT INTO t VALUES (1, 'one', 10), (2, 'two', 20), (3, 'three', 30)")
    yield database
    database.close()


def rows(db: mvrs_db.Database) -> list[tuple[object, ...]]:
    return db.execute("SELECT * FROM t ORDER BY id").rows


ORIGINAL = [(1, "one", 10), (2, "two", 20), (3, "three", 30)]


def test_commit_makes_changes_permanent(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with mvrs_db.connect(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (1, 1), (2, 2)")
        db.execute("UPDATE t SET n = 5 WHERE id = 1")
        db.execute("COMMIT")

    with mvrs_db.connect(path) as db:
        assert db.execute("SELECT * FROM t ORDER BY id").rows == [(1, 5), (2, 2)]


def test_rollback_discards_every_kind_of_change(db: mvrs_db.Database) -> None:
    pages_before = db.page_count

    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (4, 'four', 40)")
    db.execute("UPDATE t SET v = 'uno', n = 11 WHERE id = 1")
    db.execute("DELETE FROM t WHERE id = 2")
    for i in range(100, 600):  # enough rows to allocate pages and split the indexes
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}', {i})")
    assert len(rows(db)) == 503  # a transaction sees its own changes
    db.execute("ROLLBACK")

    assert rows(db) == ORIGINAL
    assert db.page_count == pages_before
    assert db.execute("SELECT id FROM t WHERE v = 'one'").rows == [(1,)]  # index entries too
    assert db.execute("SELECT id FROM t WHERE v = 'uno'").rows == []
    assert db.execute("SELECT id FROM t WHERE v = 'two'").rows == [(2,)]
    db.check()
    db.execute("INSERT INTO t VALUES (4, 'four', 40)")  # and the table still works
    assert len(rows(db)) == 4


def test_rollback_of_create_and_drop_table(db: mvrs_db.Database) -> None:
    db.execute("BEGIN")
    db.execute("CREATE TABLE fresh (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO fresh VALUES (1), (2)")
    db.execute("DROP TABLE t")
    assert db.table_names() == ["fresh"]
    db.execute("ROLLBACK")

    assert db.table_names() == ["t"]
    assert rows(db) == ORIGINAL
    with pytest.raises(TableNotFoundError):
        db.execute("SELECT * FROM fresh")
    assert db.check().tables == {"t": 3}


def test_committed_create_and_drop_inside_a_transaction(db: mvrs_db.Database) -> None:
    db.execute("BEGIN")
    db.execute("CREATE TABLE fresh (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO fresh VALUES (1)")
    db.execute("DROP TABLE t")
    db.execute("COMMIT")

    assert db.table_names() == ["fresh"]
    assert db.check().tables == {"fresh": 1}


def test_rollback_restores_planner_statistics(db: mvrs_db.Database) -> None:
    def estimated_rows() -> str:
        return db.execute("EXPLAIN SELECT * FROM t").rows[-1][0]

    before = estimated_rows()
    db.execute("BEGIN")
    for i in range(100, 400):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}', {i})")
    assert "rows~303" in estimated_rows()
    db.execute("ROLLBACK")

    assert estimated_rows() == before


def test_transaction_control_misuse(db: mvrs_db.Database) -> None:
    with pytest.raises(TransactionError, match="no transaction"):
        db.execute("COMMIT")
    with pytest.raises(TransactionError, match="no transaction"):
        db.execute("ROLLBACK")
    db.execute("BEGIN")
    with pytest.raises(TransactionError, match="already open"):
        db.execute("BEGIN")
    db.execute("ROLLBACK")


def test_statement_that_fails_midway_aborts_the_whole_transaction(db: mvrs_db.Database) -> None:
    connection = db.connect()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO t VALUES (4, 'four', 40)")

    with pytest.raises(ConstraintViolationError):
        # The first row is inserted before the second one collides.
        connection.execute("INSERT INTO t VALUES (5, 'five', 50), (1, 'dup', 0)")

    assert not connection.in_transaction
    assert rows(db) == ORIGINAL  # row 4 from the same transaction is gone as well
    with pytest.raises(TransactionError):
        connection.execute("COMMIT")
    db.check()


@pytest.mark.parametrize(
    ("statement", "error"),
    [
        ("INSERT INTO t VALUES (1, 'dup', 0)", ConstraintViolationError),
        ("INSERT INTO missing VALUES (1)", TableNotFoundError),
        ("UPDATE t SET n = 'text'", TypeMismatchError),
        ("SELECT * FROM t WHERE v = 5", TypeMismatchError),
        ("SELEC * FROM t", SqlSyntaxError),
    ],
)
def test_statement_that_fails_before_changing_anything_keeps_the_transaction(
    db: mvrs_db.Database, statement: str, error: type[Exception]
) -> None:
    connection = db.connect()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO t VALUES (4, 'four', 40)")

    with pytest.raises(error):
        connection.execute(statement)

    assert connection.in_transaction
    connection.execute("INSERT INTO t VALUES (5, 'five', 50)")
    connection.execute("COMMIT")
    assert [row[0] for row in rows(db)] == [1, 2, 3, 4, 5]


def test_read_only_transaction(db: mvrs_db.Database) -> None:
    commits = db.commits
    db.execute("BEGIN")
    assert rows(db) == ORIGINAL
    assert db.execute("SELECT n FROM t WHERE id = 2").rows == [(20,)]
    db.execute("COMMIT")

    assert db.commits == commits  # nothing was written to the log


def test_empty_write_transaction_logs_nothing(db: mvrs_db.Database) -> None:
    size = db.wal_size
    assert db.execute("DELETE FROM t WHERE id = 999").rowcount == 0
    assert db.execute("UPDATE t SET n = 1 WHERE id = 999").rowcount == 0
    assert db.wal_size == size


def test_closing_a_connection_rolls_back_its_transaction(db: mvrs_db.Database) -> None:
    connection = db.connect()
    connection.execute("BEGIN")
    connection.execute("DELETE FROM t")

    connection.close()

    assert rows(db) == ORIGINAL


def test_closing_the_database_rolls_back_open_transactions(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    db = mvrs_db.connect(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (2)")
    db.close()

    with mvrs_db.connect(path) as db:
        assert db.execute("SELECT * FROM t").rows == [(1,)]
        db.check()


def test_each_statement_outside_a_transaction_commits_on_its_own(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    db = mvrs_db.connect(path, checkpoint_bytes=1 << 30)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    commits = db.commits
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("INSERT INTO t VALUES (2)")
    assert db.commits == commits + 2
    db.close()


def test_transaction_larger_than_the_buffer_pool(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "db.mvrs", buffer_pages=16, synchronous=False) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO t VALUES (0, 'kept')")
        connection = db.connect()
        connection.execute("BEGIN")

        with pytest.raises(TransactionTooLargeError):
            for i in range(1, 2000):
                connection.execute(f"INSERT INTO t VALUES ({i}, '{'x' * 500}')")

        assert not connection.in_transaction
        assert db.execute("SELECT * FROM t").rows == [(0, "kept")]
        db.check()
        # The same work in smaller transactions goes through.
        for start in range(1, 200, 10):
            db.execute("BEGIN")
            for i in range(start, start + 10):
                db.execute(f"INSERT INTO t VALUES ({i}, '{'x' * 500}')")
            db.execute("COMMIT")
        assert db.check().tables == {"t": 201}  # the kept row plus ids 1 to 200


def test_failed_log_write_fails_the_commit_and_stops_further_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "db.mvrs"
    db = mvrs_db.connect(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (1)")

    def failing_flush(*, sync: bool = True) -> None:
        raise StorageIOError("simulated: no space left on device")

    monkeypatch.setattr(db._wal, "flush", failing_flush)
    with pytest.raises(StorageIOError):
        db.execute("INSERT INTO t VALUES (2)")

    assert db.execute("SELECT * FROM t").rows == [(1,)]  # reads still work, without row 2
    with pytest.raises(EngineFailedError):
        db.execute("INSERT INTO t VALUES (3)")
    db.close()

    with mvrs_db.connect(path) as db:  # reopening clears the failed state
        assert db.execute("SELECT * FROM t").rows == [(1,)]
        db.execute("INSERT INTO t VALUES (2)")
        db.check()


def test_checkpoint_empties_the_log(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "db.mvrs", checkpoint_bytes=1 << 30) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        for i in range(20):
            db.execute(f"INSERT INTO t VALUES ({i}, '{'x' * 300}')")
        assert db.wal_size > 80_000  # every commit logged at least two whole pages
        assert db.pool.dirty_page_count() > 0

        db.checkpoint()

        assert db.wal_size == 40
        assert db.pool.dirty_page_count() == 0
        assert db.check().tables == {"t": 20}


def test_automatic_checkpoint_keeps_the_log_bounded(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "db.mvrs", checkpoint_bytes=64 * 1024, synchronous=False) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        for i in range(300):
            db.execute(f"INSERT INTO t VALUES ({i}, '{'x' * 300}')")

        assert db.checkpoints > 5
        assert db.wal_size < 64 * 1024 + 5 * 4200  # at most one commit past the threshold
