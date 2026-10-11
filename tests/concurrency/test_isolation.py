"""Isolation between concurrent transactions.

MVRS_DB uses strict two-phase locking at table granularity with a single
writer. These tests run real threads and check that the anomalies this design
claims to prevent do not happen: dirty reads, lost updates, non-repeatable
reads, and phantoms. They demonstrate the absence of those anomalies in the
schedules exercised; they are not a proof of serializability.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import DeadlockError

WAIT = 0.3  # long enough to tell "blocked" from "slow"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "iso.mvrs", synchronous=False, lock_timeout=20)
    database.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance INTEGER NOT NULL)")
    database.execute("INSERT INTO accounts VALUES (1, 100), (2, 100), (3, 100), (4, 100)")
    database.execute("CREATE TABLE other (id INTEGER PRIMARY KEY, v INTEGER)")
    database.execute("INSERT INTO other VALUES (1, 1)")
    yield database
    database.close()


class Background:
    """Runs a function in a thread and lets the test see whether it has finished."""

    def __init__(self, function: Callable[[], object]) -> None:
        self.result: object = None
        self.error: BaseException | None = None
        self._done = threading.Event()

        def run() -> None:
            try:
                self.result = function()
            except BaseException as exc:  # noqa: BLE001 - reported to the test below
                self.error = exc
            finally:
                self._done.set()

        threading.Thread(target=run, daemon=True).start()

    def blocked(self) -> bool:
        return not self._done.wait(WAIT)

    def finish(self) -> object:
        assert self._done.wait(10), "the background statement never finished"
        if self.error is not None:
            raise self.error
        return self.result


def balance(connection: mvrs_db.Connection, account: int) -> int:
    value = connection.execute(f"SELECT balance FROM accounts WHERE id = {account}").rows[0][0]
    assert isinstance(value, int)
    return value


def test_no_dirty_read_of_a_change_that_is_rolled_back(db: mvrs_db.Database) -> None:
    writer, reader = db.connect(), db.connect()
    writer.execute("BEGIN")
    writer.execute("UPDATE accounts SET balance = 999 WHERE id = 1")

    read = Background(lambda: balance(reader, 1))
    assert read.blocked(), "the reader must wait for the uncommitted change to be resolved"
    writer.execute("ROLLBACK")

    assert read.finish() == 100  # it never saw 999


def test_no_dirty_read_of_a_change_that_commits(db: mvrs_db.Database) -> None:
    writer, reader = db.connect(), db.connect()
    writer.execute("BEGIN")
    writer.execute("UPDATE accounts SET balance = 999 WHERE id = 1")

    read = Background(lambda: balance(reader, 1))
    assert read.blocked()
    writer.execute("COMMIT")

    assert read.finish() == 999


def test_repeatable_read(db: mvrs_db.Database) -> None:
    reader, writer = db.connect(), db.connect()
    reader.execute("BEGIN")
    first = balance(reader, 1)

    update = Background(
        lambda: writer.execute("UPDATE accounts SET balance = 0 WHERE id = 1").rowcount
    )
    assert update.blocked(), "the writer must wait while a transaction has read the table"
    assert balance(reader, 1) == first
    reader.execute("COMMIT")

    assert update.finish() == 1
    assert balance(reader, 1) == 0


def test_no_phantom_rows(db: mvrs_db.Database) -> None:
    reader, writer = db.connect(), db.connect()
    reader.execute("BEGIN")
    count = len(reader.execute("SELECT id FROM accounts WHERE balance >= 100").rows)

    insert = Background(lambda: writer.execute("INSERT INTO accounts VALUES (5, 500)").rowcount)
    assert insert.blocked()
    assert len(reader.execute("SELECT id FROM accounts WHERE balance >= 100").rows) == count
    reader.execute("COMMIT")

    assert insert.finish() == 1


def test_readers_do_not_block_each_other(db: mvrs_db.Database) -> None:
    first, second = db.connect(), db.connect()
    first.execute("BEGIN")
    second.execute("BEGIN")

    assert balance(first, 1) == 100
    assert balance(second, 1) == 100  # returns at once although `first` still holds its lock

    first.execute("COMMIT")
    second.execute("COMMIT")


def test_reader_of_one_table_is_not_blocked_by_a_writer_of_another(db: mvrs_db.Database) -> None:
    writer, reader = db.connect(), db.connect()
    writer.execute("BEGIN")
    writer.execute("UPDATE other SET v = 2 WHERE id = 1")

    assert balance(reader, 1) == 100

    writer.execute("COMMIT")


def test_only_one_writer_at_a_time(db: mvrs_db.Database) -> None:
    first, second = db.connect(), db.connect()
    first.execute("BEGIN")
    first.execute("UPDATE other SET v = 2 WHERE id = 1")

    # A different table, but there is only one writer slot.
    update = Background(
        lambda: second.execute("UPDATE accounts SET balance = 1 WHERE id = 1").rowcount
    )
    assert update.blocked()
    first.execute("COMMIT")

    assert update.finish() == 1


def test_deadlock_is_broken_by_rolling_one_transaction_back(db: mvrs_db.Database) -> None:
    first, second = db.connect(), db.connect()
    first.execute("BEGIN")
    second.execute("BEGIN")
    assert balance(first, 1) == 100  # both hold a shared lock on accounts
    assert balance(second, 1) == 100

    upgrade = Background(lambda: first.execute("UPDATE accounts SET balance = 1 WHERE id = 1"))
    assert upgrade.blocked()  # first waits for second to let go
    with pytest.raises(DeadlockError):
        second.execute("UPDATE accounts SET balance = 2 WHERE id = 1")

    assert not second.in_transaction  # the victim was rolled back
    upgrade.finish()
    first.execute("COMMIT")
    assert balance(second, 1) == 1


def test_no_lost_updates(db: mvrs_db.Database) -> None:
    """Read-modify-write from many threads. Every successful increment must count."""
    threads, increments_each = 6, 15
    succeeded = [0] * threads
    retries = [0] * threads

    def work(index: int) -> None:
        connection = db.connect()
        while succeeded[index] < increments_each:
            try:
                connection.execute("BEGIN")
                current = balance(connection, 1)
                # Give other threads time to read the same value. Without this,
                # each transaction finishes within one thread time slice and the
                # threads never actually overlap.
                time.sleep(0.002)
                connection.execute(f"UPDATE accounts SET balance = {current + 1} WHERE id = 1")
                connection.execute("COMMIT")
                succeeded[index] += 1
            except DeadlockError:
                retries[index] += 1  # rolled back by the engine; try again

    workers = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(120)
        assert not worker.is_alive()

    assert balance(db.connect(), 1) == 100 + threads * increments_each
    assert sum(retries) > 0, "the workload should have provoked at least one deadlock"
    db.check()


def test_money_is_conserved_under_concurrent_transfers(db: mvrs_db.Database) -> None:
    """Writers move money between accounts while readers add up the total."""
    total = 400
    stop = threading.Event()
    problems: list[str] = []
    transfers = [0]

    def transfer(seed: int) -> None:
        rng = random.Random(seed)
        connection = db.connect()
        for _ in range(40):
            source, target = rng.sample([1, 2, 3, 4], 2)
            amount = rng.randint(1, 30)
            try:
                connection.execute("BEGIN")
                available = balance(connection, source)
                if available < amount:
                    connection.execute("ROLLBACK")
                    continue
                connection.execute(
                    f"UPDATE accounts SET balance = {available - amount} WHERE id = {source}"
                )
                other = balance(connection, target)
                connection.execute(
                    f"UPDATE accounts SET balance = {other + amount} WHERE id = {target}"
                )
                # Abort some transfers halfway on purpose.
                connection.execute("ROLLBACK" if rng.random() < 0.2 else "COMMIT")
                transfers[0] += 1
            except DeadlockError:
                pass

    def audit() -> None:
        connection = db.connect()
        while not stop.is_set():
            balances = [row[0] for row in connection.execute("SELECT balance FROM accounts").rows]
            if sum(balances) != total or min(balances) < 0:  # type: ignore[arg-type,type-var]
                problems.append(f"saw balances {balances}")

    auditors = [threading.Thread(target=audit) for _ in range(2)]
    movers = [threading.Thread(target=transfer, args=(seed,)) for seed in range(4)]
    for thread in auditors + movers:
        thread.start()
    for thread in movers:
        thread.join(120)
        assert not thread.is_alive()
    stop.set()
    for thread in auditors:
        thread.join(10)

    assert problems == []
    assert transfers[0] > 20
    final = [row[0] for row in db.execute("SELECT balance FROM accounts").rows]
    assert sum(final) == total  # type: ignore[arg-type]
    db.check()


def test_concurrent_readers_and_a_writer_on_many_tables(tmp_path: Path) -> None:
    """Stress the shared buffer pool: a small pool, several readers, one writer."""
    with mvrs_db.connect(
        tmp_path / "stress.mvrs", buffer_pages=32, synchronous=False, lock_timeout=30
    ) as db:
        for name in ("a", "b", "c"):
            db.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, body TEXT)")
            db.execute("BEGIN")
            for i in range(300):
                db.execute(f"INSERT INTO {name} VALUES ({i}, '{name}-{i}-{'x' * 200}')")
            db.execute("COMMIT")
        errors: list[BaseException] = []
        stop = threading.Event()

        def read(name: str) -> None:
            connection = db.connect()
            rng = random.Random(name)
            try:
                while not stop.is_set():
                    i = rng.randrange(300)
                    rows = connection.execute(f"SELECT body FROM {name} WHERE id = {i}").rows
                    assert rows and str(rows[0][0]).startswith(f"{name}-{i}-")
                    assert len(connection.execute(f"SELECT id FROM {name}").rows) >= 300
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        def write() -> None:
            connection = db.connect()
            try:
                for i in range(300, 500):
                    name = "abc"[i % 3]
                    connection.execute(f"INSERT INTO {name} VALUES ({i}, '{name}-{i}-new')")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=read, args=(name,)) for name in "abcab"]
        writer = threading.Thread(target=write)
        for thread in [*readers, writer]:
            thread.start()
        writer.join(120)
        stop.set()
        for thread in readers:
            thread.join(30)

        assert errors == []
        assert sum(db.check().tables.values()) == 900 + 200
