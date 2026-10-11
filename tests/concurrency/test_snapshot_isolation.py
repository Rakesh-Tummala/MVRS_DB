"""Snapshot isolation: readers see one commit, take no locks, and never wait for writers."""

from __future__ import annotations

import copy
import random
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import LockTimeoutError, SerializationError, TableNotFoundError


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "snap.mvrs", isolation="snapshot", synchronous=False)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER, tag TEXT)")
    database.execute("INSERT INTO t VALUES (1, 10, 'a'), (2, 20, 'b'), (3, 30, 'c')")
    yield database
    database.close()


def select(conn: mvrs_db.Connection, sql: str = "SELECT id, v FROM t ORDER BY id") -> list[tuple]:  # type: ignore[type-arg]
    return conn.execute(sql).rows


# ---------------------------------------------------------------------- what a snapshot sees


def test_a_transaction_reads_one_commit_throughout(db: mvrs_db.Database) -> None:
    reader, writer = db.connect(), db.connect()
    reader.execute("BEGIN")
    before = select(reader)
    writer.execute("UPDATE t SET v = 99 WHERE id = 1")
    writer.execute("INSERT INTO t VALUES (4, 40, 'd')")
    writer.execute("DELETE FROM t WHERE id = 2")

    assert select(reader) == before  # repeatable: updates, inserts and deletes are all invisible
    assert select(reader, "SELECT COUNT(*) FROM t") == [(3,)]
    assert select(reader, "SELECT v FROM t WHERE id = 1") == [(10,)]  # through the primary key
    assert select(reader, "SELECT id FROM t WHERE id > 1 ORDER BY id") == [(2,), (3,)]  # a range
    reader.execute("COMMIT")
    assert select(reader) == [(1, 99), (3, 30), (4, 40)]  # the next statement sees the new state


def test_each_autocommit_statement_sees_the_latest_commit(db: mvrs_db.Database) -> None:
    reader, writer = db.connect(), db.connect()
    assert select(reader, "SELECT v FROM t WHERE id = 1") == [(10,)]
    writer.execute("UPDATE t SET v = 11 WHERE id = 1")
    assert select(reader, "SELECT v FROM t WHERE id = 1") == [(11,)]


def test_no_dirty_reads(db: mvrs_db.Database) -> None:
    reader, writer = db.connect(), db.connect()
    writer.execute("BEGIN")
    writer.execute("UPDATE t SET v = 0")
    writer.execute("INSERT INTO t VALUES (9, 90, 'z')")
    writer.execute("DELETE FROM t WHERE id = 3")
    assert select(reader) == [(1, 10), (2, 20), (3, 30)]
    assert select(writer) == [(1, 0), (2, 0), (9, 90)]  # the writer reads its own changes
    writer.execute("ROLLBACK")
    assert select(reader) == [(1, 10), (2, 20), (3, 30)]


def test_a_reader_started_while_a_writer_is_open_sees_the_state_before_it(
    db: mvrs_db.Database,
) -> None:
    reader, writer = db.connect(), db.connect()
    writer.execute("BEGIN")
    writer.execute("UPDATE t SET v = 7")
    reader.execute("BEGIN")
    assert select(reader) == [(1, 10), (2, 20), (3, 30)]
    writer.execute("COMMIT")
    assert select(reader) == [(1, 10), (2, 20), (3, 30)]  # still the old commit
    reader.execute("COMMIT")
    assert select(reader) == [(1, 7), (2, 7), (3, 7)]


def test_each_connection_has_its_own_snapshot(db: mvrs_db.Database) -> None:
    a, b, writer = db.connect(), db.connect(), db.connect()
    a.execute("BEGIN")
    select(a)
    writer.execute("UPDATE t SET v = 1 WHERE id = 1")
    b.execute("BEGIN")
    select(b)
    writer.execute("UPDATE t SET v = 2 WHERE id = 1")
    assert select(a, "SELECT v FROM t WHERE id = 1") == [(10,)]
    assert select(b, "SELECT v FROM t WHERE id = 1") == [(1,)]
    assert select(writer, "SELECT v FROM t WHERE id = 1") == [(2,)]


def test_aggregates_and_joins_read_the_snapshot(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER, n INTEGER)")
    db.execute("INSERT INTO u VALUES (1, 1, 5), (2, 1, 6), (3, 2, 7)")
    reader, writer = db.connect(), db.connect()
    reader.execute("BEGIN")
    join = "SELECT t.id, SUM(u.n) FROM t JOIN u ON u.t_id = t.id GROUP BY t.id ORDER BY t.id"
    outer = "SELECT t.id, u.n FROM t LEFT JOIN u ON u.t_id = t.id ORDER BY t.id, u.n"
    expected_join, expected_outer = select(reader, join), select(reader, outer)
    assert expected_join == [(1, 11), (2, 7)]

    writer.execute("INSERT INTO u VALUES (4, 3, 100)")
    writer.execute("UPDATE u SET n = 0 WHERE id = 1")
    writer.execute("DELETE FROM t WHERE id = 2")

    assert select(reader, join) == expected_join
    assert select(reader, outer) == expected_outer
    assert select(reader, "SELECT MAX(n), MIN(n), COUNT(*) FROM u") == [(7, 5, 3)]


def test_vector_search_in_a_snapshot_is_exact(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "vec.mvrs", isolation="snapshot", synchronous=False)
    try:
        database.execute("CREATE TABLE d (id INTEGER PRIMARY KEY, v VECTOR(2) HNSW)")
        database.execute("INSERT INTO d VALUES (1, [0, 0]), (2, [1, 1]), (3, [5, 5])")
        database.vector.min_rows = 0
        reader, writer = database.connect(), database.connect()
        reader.execute("BEGIN")
        sql = "SELECT id FROM d ORDER BY v <-> [0.9, 0.9] LIMIT 2"
        assert select(reader, sql) == [(2,), (1,)]
        writer.execute("INSERT INTO d VALUES (4, [0.9, 0.9])")
        assert select(reader, sql) == [(2,), (1,)]  # the new, nearest row is not in this snapshot
        reader.execute("COMMIT")
        assert select(reader, sql)[0] == (4,)
        plan = "\n".join(str(r[0]) for r in reader.execute(f"EXPLAIN {sql}").rows)
        assert "VectorIndexScan" not in plan  # approximate search is not used for snapshots
    finally:
        database.close()


# ---------------------------------------------------------------------- DDL


def test_a_snapshot_survives_drop_and_create_table(db: mvrs_db.Database) -> None:
    reader, writer = db.connect(), db.connect()
    reader.execute("BEGIN")
    assert select(reader, "SELECT COUNT(*) FROM t") == [(3,)]
    writer.execute("DROP TABLE t")
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, other TEXT)")
    writer.execute("INSERT INTO t VALUES (1, 'new')")
    # The old table's pages were freed and reused; the old snapshot still reads them.
    assert select(reader) == [(1, 10), (2, 20), (3, 30)]
    reader.execute("COMMIT")
    assert select(reader, "SELECT * FROM t") == [(1, "new")]


def test_a_table_created_after_the_snapshot_is_not_visible(db: mvrs_db.Database) -> None:
    reader, writer = db.connect(), db.connect()
    reader.execute("BEGIN")
    select(reader)
    writer.execute("CREATE TABLE later (id INTEGER PRIMARY KEY)")
    with pytest.raises(TableNotFoundError):
        reader.execute("SELECT * FROM later")
    assert reader.in_transaction  # a failed read does not end the transaction
    reader.execute("COMMIT")
    assert select(reader, "SELECT COUNT(*) FROM later") == [(0,)]


def test_a_dropped_table_stays_readable_in_old_snapshots_until_they_end(
    db: mvrs_db.Database,
) -> None:
    reader, writer = db.connect(), db.connect()
    reader.execute("BEGIN")
    select(reader)
    writer.execute("DROP TABLE t")
    assert db.pool.version_count > 0
    assert select(reader, "SELECT COUNT(*) FROM t") == [(3,)]
    reader.execute("ROLLBACK")
    assert db.pool.version_count == 0
    with pytest.raises(TableNotFoundError):
        reader.execute("SELECT * FROM t")


# ---------------------------------------------------------------------- writing


def test_a_stale_snapshot_cannot_write(db: mvrs_db.Database) -> None:
    a, b = db.connect(), db.connect()
    a.execute("BEGIN")
    select(a)
    b.execute("UPDATE t SET v = 0 WHERE id = 1")

    with pytest.raises(SerializationError, match="roll back and retry"):
        a.execute("UPDATE t SET v = 21 WHERE id = 2")
    assert not a.in_transaction  # the transaction ended
    assert select(b) == [(1, 0), (2, 20), (3, 30)]  # and wrote nothing
    # Retrying from scratch works.
    a.execute("BEGIN")
    select(a)
    a.execute("UPDATE t SET v = 21 WHERE id = 2")
    a.execute("COMMIT")
    assert select(b) == [(1, 0), (2, 21), (3, 30)]


def test_a_current_snapshot_can_become_a_writer(db: mvrs_db.Database) -> None:
    a = db.connect()
    a.execute("BEGIN")
    before = select(a)
    a.execute("UPDATE t SET v = 11 WHERE id = 1")  # nothing committed since the read
    assert select(a) == [(1, 11), *before[1:]]  # and it now reads its own writes
    a.execute("COMMIT")
    assert select(a) == [(1, 11), *before[1:]]
    assert db.pool.version_count == 0


def test_a_read_that_changed_nothing_does_not_make_a_snapshot_stale(db: mvrs_db.Database) -> None:
    a, b = db.connect(), db.connect()
    a.execute("BEGIN")
    select(a)
    b.execute("DELETE FROM t WHERE id = 99")  # matches nothing, so commits nothing
    a.execute("UPDATE t SET v = 0 WHERE id = 1")
    a.execute("COMMIT")


def test_lost_update_is_prevented(db: mvrs_db.Database) -> None:
    """Two transactions read v, both compute v + 1, both write: one must fail."""
    a, b = db.connect(), db.connect()
    for conn in (a, b):
        conn.execute("BEGIN")
    (va,) = select(a, "SELECT v FROM t WHERE id = 1")[0]
    (vb,) = select(b, "SELECT v FROM t WHERE id = 1")[0]
    a.execute(f"UPDATE t SET v = {va + 1} WHERE id = 1")
    a.execute("COMMIT")
    with pytest.raises(SerializationError):
        b.execute(f"UPDATE t SET v = {vb + 1} WHERE id = 1")
    assert select(db.connect(), "SELECT v FROM t WHERE id = 1") == [(11,)]


def test_a_writer_that_never_read_is_never_stale(db: mvrs_db.Database) -> None:
    a, b = db.connect(), db.connect()
    a.execute("BEGIN")
    b.execute("UPDATE t SET v = 0 WHERE id = 1")
    a.execute("UPDATE t SET v = 21 WHERE id = 2")  # no earlier read, so no snapshot to be stale
    a.execute("COMMIT")
    assert select(b) == [(1, 0), (2, 21), (3, 30)]


def test_a_failed_statement_in_a_snapshot_keeps_the_transaction(db: mvrs_db.Database) -> None:
    a = db.connect()
    a.execute("BEGIN")
    select(a)
    with pytest.raises(mvrs_db.errors.SqlError):
        a.execute("SELECT nope FROM t")
    assert a.in_transaction
    assert select(a, "SELECT COUNT(*) FROM t") == [(3,)]
    a.execute("ROLLBACK")


# ---------------------------------------------------------------------- never waiting


def test_a_reader_is_not_blocked_by_an_open_writer(db: mvrs_db.Database) -> None:
    writer = db.connect()
    writer.execute("BEGIN")
    writer.execute("UPDATE t SET v = 0")  # holds an exclusive lock on t

    answer: list[list[tuple]] = []  # type: ignore[type-arg]
    thread = threading.Thread(target=lambda: answer.append(select(db.connect())))
    thread.start()
    thread.join(10)

    assert not thread.is_alive(), "the reader waited for the writer"
    assert answer == [[(1, 10), (2, 20), (3, 30)]]
    writer.execute("ROLLBACK")


def test_the_same_scenario_blocks_in_locking_mode(tmp_path: Path) -> None:
    """The contrast that gives the test above its meaning."""
    database = mvrs_db.connect(tmp_path / "lock.mvrs", synchronous=False, lock_timeout=0.3)
    try:
        database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        database.execute("INSERT INTO t VALUES (1, 10)")
        writer = database.connect()
        writer.execute("BEGIN")
        writer.execute("UPDATE t SET v = 0")
        with pytest.raises(LockTimeoutError):
            database.connect().execute("SELECT * FROM t")
        writer.execute("ROLLBACK")
    finally:
        database.close()


def test_a_writer_is_not_blocked_by_open_readers(db: mvrs_db.Database) -> None:
    readers = [db.connect() for _ in range(3)]
    for r in readers:
        r.execute("BEGIN")
        select(r)
    finished = threading.Event()

    def write() -> None:
        db.connect().execute("UPDATE t SET v = 5")
        finished.set()

    thread = threading.Thread(target=write)
    thread.start()
    assert finished.wait(10), "the writer waited for readers"
    thread.join()
    for r in readers:
        assert select(r) == [(1, 10), (2, 20), (3, 30)]
        r.execute("COMMIT")


def test_old_versions_are_released_when_the_last_reader_ends(db: mvrs_db.Database) -> None:
    a, b, writer = db.connect(), db.connect(), db.connect()
    a.execute("BEGIN")
    select(a)
    for i in range(5):
        writer.execute(f"UPDATE t SET v = {i} WHERE id = 1")
    b.execute("BEGIN")
    select(b)
    writer.execute("UPDATE t SET v = 100 WHERE id = 1")
    held = db.pool.version_count
    assert held > 0
    a.execute("COMMIT")
    assert 0 < db.pool.version_count < held  # b still needs the last one
    b.execute("COMMIT")
    assert db.pool.version_count == 0


def test_closing_the_database_releases_open_snapshots(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "close.mvrs", isolation="snapshot", synchronous=False)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    reader = database.connect()
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t")
    database.close()  # must not hang or leak
    with mvrs_db.connect(tmp_path / "close.mvrs", isolation="snapshot") as again:
        assert again.execute("SELECT COUNT(*) FROM t").rows == [(0,)]


def test_isolation_must_be_one_of_the_two(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="isolation"):
        mvrs_db.connect(tmp_path / "x.mvrs", isolation="chaos")


# ---------------------------------------------------------------------- invariants under threads


def test_audits_always_see_a_consistent_total_while_money_moves(tmp_path: Path) -> None:
    database = mvrs_db.connect(tmp_path / "bank.mvrs", isolation="snapshot", synchronous=False)
    try:
        database.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance INTEGER NOT NULL)")
        database.execute(
            "INSERT INTO accounts VALUES " + ", ".join(f"({i}, 100)" for i in range(8))
        )
        total = 800
        stop = threading.Event()
        problems: list[str] = []
        audits = [0]
        transfers = [0]

        def transfer(seed: int) -> None:
            rng = random.Random(seed)
            conn = database.connect()
            while transfers[0] < 120:
                src, dst = rng.sample(range(8), 2)
                try:
                    conn.execute("BEGIN")
                    (a,) = conn.execute(f"SELECT balance FROM accounts WHERE id = {src}").rows[0]
                    (b,) = conn.execute(f"SELECT balance FROM accounts WHERE id = {dst}").rows[0]
                    amount = rng.randrange(1, 30)
                    conn.execute(f"UPDATE accounts SET balance = {a - amount} WHERE id = {src}")
                    conn.execute(f"UPDATE accounts SET balance = {b + amount} WHERE id = {dst}")
                    conn.execute("ROLLBACK" if rng.random() < 0.15 else "COMMIT")
                    transfers[0] += 1
                except SerializationError:
                    pass  # someone committed first; the transaction is over, so try again

        def audit() -> None:
            conn = database.connect()
            while not stop.is_set():
                balances = [r[0] for r in conn.execute("SELECT balance FROM accounts").rows]
                audits[0] += 1
                if sum(balances) != total:  # type: ignore[arg-type]
                    problems.append(f"saw {balances}")

        auditors = [threading.Thread(target=audit) for _ in range(3)]
        movers = [threading.Thread(target=transfer, args=(seed,)) for seed in range(4)]
        for t in auditors + movers:
            t.start()
        for t in movers:
            t.join(120)
            assert not t.is_alive()
        stop.set()
        for t in auditors:
            t.join(10)

        assert problems == []
        assert audits[0] > 20  # the auditors really ran alongside the writers
        final = [r[0] for r in database.execute("SELECT balance FROM accounts").rows]
        assert sum(final) == total  # type: ignore[arg-type]
        assert database.pool.version_count == 0
        database.check()
    finally:
        database.close()


# ---------------------------------------------------------------------- against a model

Model = dict[str, dict[int, tuple[int, str]]]  # table -> id -> (v, tag)


def model_rows(model: Model, table: str) -> list[tuple[int, int, str]]:
    return [(i, v, tag) for i, (v, tag) in sorted(model[table].items())]


def model_join(model: Model) -> list[tuple[int, int]]:
    """SELECT a.id, b.v FROM a LEFT JOIN b ON a.v = b.id ORDER BY a.id, b.v (NULL first)."""
    rows = []
    for i, (v, _tag) in model["a"].items():
        partner = model["b"].get(v)
        rows.append((i, partner[0] if partner is not None else None))
    return sorted(rows, key=lambda r: (r[0], r[1] is not None, r[1] or 0))  # type: ignore[arg-type]


@pytest.mark.parametrize("seed", range(12))
def test_random_interleavings_match_a_model(tmp_path: Path, seed: int) -> None:
    """A writer and three readers run random operations; every read must equal the model's
    state as of when that reader's snapshot began."""
    rng = random.Random(seed)
    database = mvrs_db.connect(
        tmp_path / "model.mvrs", isolation="snapshot", synchronous=False, buffer_pages=64
    )
    try:
        for name in ("a", "b"):
            database.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, v INTEGER, tag TEXT)")
        model: Model = {"a": {}, "b": {}}
        commits = 0  # commits that changed something, as the model counts them
        writer = database.connect()
        readers = [database.connect() for _ in range(3)]
        # per reader: None (no open transaction), or [snapshot of the model, commit count then]
        snap: list[list[object] | None] = [None, None, None]

        def write_op() -> None:
            nonlocal commits
            table = rng.choice(["a", "b"])
            kind = rng.random()
            ident = rng.randrange(40)
            if kind < 0.4:
                if ident in model[table]:
                    return
                v, tag = rng.randrange(10), rng.choice("xyz")
                writer.execute(f"INSERT INTO {table} VALUES ({ident}, {v}, '{tag}')")
                model[table][ident] = (v, tag)
                commits += 1
            elif kind < 0.65:
                v = rng.randrange(10)
                changed = ident in model[table]
                writer.execute(f"UPDATE {table} SET v = {v} WHERE id = {ident}")
                if changed:
                    model[table][ident] = (v, model[table][ident][1])
                    commits += 1
            elif kind < 0.85:
                changed = ident in model[table]
                writer.execute(f"DELETE FROM {table} WHERE id = {ident}")
                if changed:
                    del model[table][ident]
                    commits += 1
            elif kind < 0.93:
                # a multi-statement transaction, sometimes rolled back
                writer.execute("BEGIN")
                saved = copy.deepcopy(model)
                n = 0
                for _ in range(3):
                    new_id = 100 + rng.randrange(1000)
                    if new_id not in model[table]:
                        writer.execute(f"INSERT INTO {table} VALUES ({new_id}, 1, 'q')")
                        model[table][new_id] = (1, "q")
                        n += 1
                if rng.random() < 0.5:
                    writer.execute("ROLLBACK")
                    model.clear()
                    model.update(saved)
                else:
                    writer.execute("COMMIT")
                    commits += 1 if n else 0
            else:
                # DDL: replace table b by a fresh one holding a few rows
                writer.execute("DROP TABLE b")
                writer.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, v INTEGER, tag TEXT)")
                model["b"] = {}
                commits += 2
                for k in range(rng.randrange(4)):
                    writer.execute(f"INSERT INTO b VALUES ({k}, {k * 3}, 'n')")
                    model["b"][k] = (k * 3, "n")
                    commits += 1

        def read_op(r: int) -> None:
            nonlocal commits
            conn = readers[r]
            if snap[r] is None:
                conn.execute("BEGIN")
                snap[r] = [None, 0]
            state = snap[r]
            assert state is not None
            choice = rng.random()
            if choice >= 0.9:
                # Try to write. A transaction that has read something may write only
                # if nothing has committed since; one that has not read is never stale.
                stale = state[0] is not None and commits != state[1]
                try:
                    conn.execute(f"INSERT INTO a VALUES ({5000 + rng.randrange(10**6)}, 0, 'w')")
                except SerializationError:
                    assert stale, f"seed {seed}: refused a write although nothing had changed"
                    snap[r] = None
                else:
                    assert not stale, f"seed {seed}: allowed a stale write"
                    conn.execute("COMMIT")
                    snap[r] = None
                    commits += 1
                    rows = conn.execute("SELECT id, v, tag FROM a ORDER BY id").rows
                    model["a"] = {i: (v, tag) for i, v, tag in rows}  # type: ignore[misc]
                return
            if state[0] is None:
                state[0] = copy.deepcopy(model)  # the snapshot starts with the first read
                state[1] = commits
            seen: Model = state[0]  # type: ignore[assignment]
            if choice < 0.25:
                table = rng.choice(["a", "b"])
                assert select(conn, f"SELECT id, v, tag FROM {table} ORDER BY id") == model_rows(
                    seen, table
                ), f"seed {seed}"
            elif choice < 0.45:
                table = rng.choice(["a", "b"])
                ident = rng.randrange(40)
                expected = [(seen[table][ident][0],)] if ident in seen[table] else []
                assert select(conn, f"SELECT v FROM {table} WHERE id = {ident}") == expected
            elif choice < 0.6:
                table = rng.choice(["a", "b"])
                low = rng.randrange(40)
                expected = [(i,) for i in sorted(seen[table]) if i >= low]
                sql = f"SELECT id FROM {table} WHERE id >= {low} ORDER BY id"
                assert select(conn, sql) == expected
            elif choice < 0.75:
                total = sum(v for v, _ in seen["a"].values())
                got = select(conn, "SELECT COUNT(*), SUM(v) FROM a")
                assert got == [(len(seen["a"]), total if seen["a"] else None)]
            else:
                got = select(
                    conn,
                    "SELECT a.id, b.v FROM a LEFT JOIN b ON a.v = b.id ORDER BY a.id, b.v",
                )
                assert got == model_join(seen)

        for _ in range(250):
            if rng.random() < 0.45:
                write_op()
            else:
                read_op(rng.randrange(3))
            if rng.random() < 0.08:
                r = rng.randrange(3)
                if snap[r] is not None:
                    readers[r].execute(rng.choice(["COMMIT", "ROLLBACK"]))
                    snap[r] = None
        for r in range(3):
            if snap[r] is not None:
                readers[r].execute("ROLLBACK")
        assert database.pool.version_count == 0  # everything was released
        database.check()
    finally:
        database.close()


def test_scans_stay_whole_while_a_writer_inserts_and_the_pool_evicts(tmp_path: Path) -> None:
    """Readers scan whole tables (heap and index) while batches of 10 rows commit.

    Every answer must be a multiple of 10 rows, never less than the reader's
    previous answer, and the index and the heap must agree: a reader that caught
    a page mid-edit, or mixed two commits, would break one of these.
    """
    database = mvrs_db.connect(
        tmp_path / "scan.mvrs", isolation="snapshot", synchronous=False, buffer_pages=32
    )
    try:
        database.execute("CREATE TABLE r (id INTEGER PRIMARY KEY, pad TEXT)")
        stop = threading.Event()
        problems: list[str] = []
        scans = [0]

        def read() -> None:
            conn = database.connect()
            last = 0
            while not stop.is_set():
                conn.execute("BEGIN")
                heap = conn.execute("SELECT COUNT(*) FROM r").rows[0][0]
                ids = [row[0] for row in conn.execute("SELECT id FROM r ORDER BY id").rows]
                by_index = conn.execute("SELECT id FROM r WHERE id >= 0 ORDER BY id").rows
                conn.execute("COMMIT")
                scans[0] += 1
                if heap % 10 or heap < last or len(ids) != heap or len(by_index) != heap:
                    problems.append(f"count {heap}, ids {len(ids)}, via index {len(by_index)}")
                if ids != list(range(heap)):
                    problems.append(f"ids are not 0..{heap - 1}")
                last = heap

        readers = [threading.Thread(target=read) for _ in range(3)]
        for t in readers:
            t.start()
        writer = database.connect()
        for batch in range(60):
            values = ", ".join(f"({batch * 10 + i}, '{'x' * 200}')" for i in range(10))
            writer.execute(f"INSERT INTO r VALUES {values}")
        stop.set()
        for t in readers:
            t.join(30)
            assert not t.is_alive()

        assert problems == []
        assert scans[0] > 10
        assert database.pool.version_count == 0
        assert database.check().tables == {"r": 600}
    finally:
        database.close()
