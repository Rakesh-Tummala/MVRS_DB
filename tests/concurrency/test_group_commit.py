"""Group commit: many commits sharing one fsync, without losing durability or isolation.

These tests slow or gate os.fsync so that the interleavings they need are
certain, instead of depending on timing luck.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import EngineFailedError, StorageIOError

REAL_FSYNC = os.fsync


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "gc.mvrs", checkpoint_bytes=1 << 40)
    database.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who INTEGER)")
    yield database
    database.close()


def run_threads(
    count: int, work: Callable[[int], None], timeout: float = 60
) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrapper(index: int) -> None:
        try:
            work(index)
        except BaseException as exc:  # noqa: BLE001 - collected for the test to inspect
            errors.append(exc)

    threads = [threading.Thread(target=wrapper, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        assert not thread.is_alive(), "a thread never finished"
    return errors


def test_one_thread_still_syncs_once_per_commit(db: mvrs_db.Database) -> None:
    before = db._wal.sync_count

    for i in range(10):
        db.execute(f"INSERT INTO t VALUES ({i}, 0)")

    assert db._wal.sync_count - before == 10


def test_commits_that_overlap_share_fsyncs(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_fsync(fd: int) -> None:
        time.sleep(0.004)  # long enough for the other threads to queue up behind it
        REAL_FSYNC(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    threads, each = 8, 12
    syncs_before, commits_before = db._wal.sync_count, db.commits

    def work(index: int) -> None:
        connection = db.connect()
        for i in range(each):
            connection.execute(f"INSERT INTO t VALUES ({index * 1000 + i}, {index})")

    assert run_threads(threads, work) == []

    commits = db.commits - commits_before
    syncs = db._wal.sync_count - syncs_before
    assert commits == threads * each
    assert syncs < commits / 2, f"{commits} commits took {syncs} fsyncs"
    assert db.execute("SELECT COUNT(*) FROM t").rows == [(threads * each,)]
    assert db.check().tables == {"t": threads * each}


def test_every_acknowledged_commit_is_durable_when_the_call_returns(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute() may only return once that commit's own records are durable, however batched."""

    def slow_fsync(fd: int) -> None:
        time.sleep(0.002)
        REAL_FSYNC(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    seen = threading.local()
    real_log_and_commit = db._log_and_commit

    def recording(txn_id: int) -> int | None:
        end = real_log_and_commit(txn_id)
        seen.end = end  # this thread's commit ends at this log position
        return end

    monkeypatch.setattr(db, "_log_and_commit", recording)
    violations: list[str] = []

    def work(index: int) -> None:
        connection = db.connect()
        for i in range(15):
            seen.end = None
            connection.execute(f"INSERT INTO t VALUES ({index * 1000 + i}, {index})")
            end = seen.end
            if end is None:
                violations.append("a write logged nothing")
            elif db._wal._synced_lsn < end:
                violations.append(f"returned with the log durable only to {db._wal._synced_lsn}")

    assert run_threads(6, work) == []
    assert violations == []
    assert db._wal._synced_lsn == db._wal.next_lsn  # and nothing is left pending at the end


def test_a_reader_sees_a_commit_early_but_is_not_answered_before_it_is_durable(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, entered = threading.Event(), threading.Event()
    armed = threading.Event()

    def gated_fsync(fd: int) -> None:
        if armed.is_set():
            entered.set()
            assert gate.wait(10)
        REAL_FSYNC(fd)

    monkeypatch.setattr(os, "fsync", gated_fsync)
    armed.set()
    writer_done = threading.Event()
    reader_result: list[object] = []

    def write() -> None:
        db.connect().execute("INSERT INTO t VALUES (1, 1)")
        writer_done.set()

    def read() -> None:
        reader_result.append(db.connect().execute("SELECT id FROM t").rows)

    threading.Thread(target=write, daemon=True).start()
    assert entered.wait(10), "the writer never reached fsync"
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(0.5)

    # The writer's locks are already released, so the reader ran; but it must
    # not return a row that a crash could still take back.
    assert reader.is_alive(), "a reader returned data that was not durable yet"
    assert not writer_done.is_set(), "the writer was acknowledged before its commit was durable"

    gate.set()
    reader.join(10)
    assert not reader.is_alive() and writer_done.wait(10)
    assert reader_result == [[(1,)]]  # it did see the committed row


def test_a_writer_is_not_held_up_by_anothers_fsync(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Early lock release: B can execute and append its commit while A waits for the disk."""
    gate, entered = threading.Event(), threading.Event()
    armed = threading.Event()

    def gated_fsync(fd: int) -> None:
        if armed.is_set():
            entered.set()
            assert gate.wait(10)
        REAL_FSYNC(fd)

    monkeypatch.setattr(os, "fsync", gated_fsync)
    armed.set()
    done: list[int] = []

    def insert(row: int) -> None:
        db.connect().execute(f"INSERT INTO t VALUES ({row}, {row})")
        done.append(row)

    first = threading.Thread(target=insert, args=(1,), daemon=True)
    first.start()
    assert entered.wait(10)
    position_while_blocked = db._wal.next_lsn
    second = threading.Thread(target=insert, args=(2,), daemon=True)
    second.start()
    deadline = time.monotonic() + 5
    while db._wal.next_lsn == position_while_blocked and time.monotonic() < deadline:
        time.sleep(0.01)

    # Both inserts hit the same table. B got its X lock and appended its records
    # although A's commit is still waiting for the disk.
    assert db._wal.next_lsn > position_while_blocked
    assert done == []  # neither is acknowledged yet

    gate.set()
    first.join(10)
    second.join(10)
    assert sorted(done) == [1, 2]
    assert db.execute("SELECT id FROM t ORDER BY id").rows == [(1,), (2,)]


def test_an_fsync_failure_fails_every_commit_waiting_on_it(
    db: mvrs_db.Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Barrier(5, timeout=10)
    armed = threading.Event()

    def failing_fsync(fd: int) -> None:
        if armed.is_set():
            time.sleep(0.05)  # let the other threads append and start waiting
            raise OSError(5, "simulated disk failure")
        REAL_FSYNC(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    armed.set()
    outcomes: dict[int, str] = {}

    def work(index: int) -> None:
        connection = db.connect()
        started.wait()
        try:
            connection.execute(f"INSERT INTO t VALUES ({index}, {index})")
            outcomes[index] = "acknowledged"
        except StorageIOError:
            outcomes[index] = "failed"
        except EngineFailedError:
            outcomes[index] = "refused"

    assert run_threads(5, work) == []

    # Nobody was told their commit succeeded, and the engine has stopped.
    assert "acknowledged" not in outcomes.values()
    assert set(outcomes) == set(range(5))
    with pytest.raises(EngineFailedError):
        db.execute("SELECT * FROM t")
    monkeypatch.undo()
    db.close()


def test_group_commit_with_fsync_off_still_batches_writes(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "async.mvrs", synchronous=False) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who INTEGER)")

        def work(index: int) -> None:
            connection = db.connect()
            for i in range(20):
                connection.execute(f"INSERT INTO t VALUES ({index * 100 + i}, {index})")

        assert run_threads(6, work) == []

        assert db._wal.sync_count == 0  # no commit asked for an fsync
        assert db.execute("SELECT COUNT(*) FROM t").rows == [(120,)]
        assert db.check().tables == {"t": 120}


def test_a_page_waits_for_its_commit_record_before_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WAL rule under early release, forced rather than hoped for.

    One commit is held open at its fsync. Its pages are already visible and
    evictable. A second thread then churns through enough pages to evict them.
    The eviction must wait until the commit record is durable: otherwise a crash
    would leave the transaction half on disk, with no commit record to say so.
    """
    gate, entered, armed = threading.Event(), threading.Event(), threading.Event()
    breaches: list[str] = []

    def gated_fsync(fd: int) -> None:
        if armed.is_set():
            entered.set()
            assert gate.wait(10)
        REAL_FSYNC(fd)

    with mvrs_db.connect(tmp_path / "rule.mvrs", buffer_pages=8, checkpoint_bytes=1 << 40) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        for i in range(60):
            db.execute(f"INSERT INTO t VALUES ({i}, '{'x' * 700}')")
        pages = db._catalog.get("t").heap.pages()  # read before anything is pending
        assert len(pages) > 10

        real_write_page = db._disk.write_page

        def checking_write_page(page_id: int, data: bytearray) -> None:
            needed = db.pool._frames[page_id].durable_after
            if db._wal._synced_lsn < needed:
                breaches.append(
                    f"page {page_id} written with the log durable to "
                    f"{db._wal._synced_lsn}, commit ends at {needed}"
                )
            real_write_page(page_id, data)

        monkeypatch.setattr(db._disk, "write_page", checking_write_page)
        monkeypatch.setattr(os, "fsync", gated_fsync)
        armed.set()

        writer = threading.Thread(
            target=lambda: db.connect().execute("INSERT INTO t VALUES (1000, 'new')"), daemon=True
        )
        writer.start()
        assert entered.wait(10), "the commit never reached its fsync"
        assert db._wal._synced_lsn < db._wal.next_lsn  # a commit really is pending

        def churn() -> None:
            for page_id in pages[:-1] * 2:
                with db.pool.page(page_id):
                    pass

        churner = threading.Thread(target=churn, daemon=True)
        churner.start()
        churner.join(0.6)
        assert churner.is_alive(), "pages of a commit that was not durable were evicted freely"
        assert breaches == []

        gate.set()
        churner.join(10)
        writer.join(10)
        assert not churner.is_alive() and not writer.is_alive()
        assert breaches == []
        assert db.execute("SELECT COUNT(*) FROM t").rows == [(61,)]
        assert db.check().tables == {"t": 61}


def test_concurrent_commits_and_evictions_leave_a_consistent_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stress run: threads commit while a tiny pool evicts constantly."""

    def slow_fsync(fd: int) -> None:
        time.sleep(0.002)
        REAL_FSYNC(fd)

    breaches: list[str] = []
    writes = [0]

    with mvrs_db.connect(tmp_path / "stress.mvrs", buffer_pages=8, checkpoint_bytes=1 << 40) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        real_write_page = db._disk.write_page

        def checking_write_page(page_id: int, data: bytearray) -> None:
            if db._wal._synced_lsn < db.pool._frames[page_id].durable_after:
                breaches.append(f"page {page_id}")
            writes[0] += 1
            real_write_page(page_id, data)

        monkeypatch.setattr(db._disk, "write_page", checking_write_page)
        monkeypatch.setattr(os, "fsync", slow_fsync)

        def work(index: int) -> None:
            connection = db.connect()
            for i in range(100):
                connection.execute(f"INSERT INTO t VALUES ({index * 1000 + i}, '{'x' * 700}')")

        assert run_threads(4, work) == []

        assert writes[0] > 20, f"only {writes[0]} pages were written"
        assert breaches == []
        assert db.check().tables == {"t": 400}
