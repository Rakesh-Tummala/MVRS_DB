"""The public entry point: Database and Connection.

This module ties the layers together and owns the two things that cut across
all of them: transactions and durability.

Opening
    1. Take the lock file, so only one process uses the database.
    2. Open the data file and validate its header.
    3. Recover: redo committed changes from the log, sync, empty the log.
    4. Load the catalog.

Running a statement
    Every statement runs inside a transaction. With no BEGIN, the statement is
    its own transaction and commits as soon as it finishes.

    Reads take a shared lock on their table. Writes take the single writer slot
    and an exclusive lock on their table. Locks are held until the transaction
    ends (strict two-phase locking).

Committing a write
    1. Give each changed page the LSN of its log record and append the page's
       after-image to the log, plus the file header if it changed.
    2. Append a COMMIT record.
    3. Flush the log and fsync it.
    Only after step 3 returns is the transaction committed. The changed pages
    stay in the buffer pool and are written to the data file later.

Checkpoint
    Write every dirty page and the header to the data file, fsync it, then
    empty the log. Runs when the log passes a size threshold and on close.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import struct
import sys
import threading
import weakref
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType

from kestreldb import config, faults
from kestreldb.catalog.catalog import Catalog
from kestreldb.errors import (
    CorruptFileError,
    CorruptIndexError,
    CorruptRecordError,
    DatabaseClosedError,
    DatabaseLockedError,
    DatabaseNotFoundError,
    DeadlockError,
    EngineFailedError,
    KestrelError,
    LockTimeoutError,
    StorageIOError,
    TransactionError,
    TransactionTooLargeError,
)
from kestreldb.execution.executor import Executor, Result
from kestreldb.index.keys import encode_key
from kestreldb.recovery.recovery_manager import RecoveryStats, recover
from kestreldb.recovery.wal import RecordType, WalWriter
from kestreldb.sql import ast
from kestreldb.sql.parser import parse
from kestreldb.storage.buffer_pool import BufferPool
from kestreldb.storage.disk_manager import DiskManager
from kestreldb.storage.page import set_page_lsn
from kestreldb.storage.record import decode_record
from kestreldb.transactions.lock_manager import EXCLUSIVE, SHARED, LockManager
from kestreldb.transactions.transaction import Transaction

_PAGE_ID = struct.Struct("<I")
_WRITER = "#writer"  # the single writer slot; '#' cannot appear in a table name


def _table_lock(name: str) -> str:
    return f"table:{name}"


class _FileLock:
    """An operating-system lock on `<database>.lock`.

    The OS releases it when the process dies, so a crash never leaves the
    database locked. The lock is per file handle, so it also stops the same
    process from opening the database twice.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            self._fd: int | None = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise StorageIOError(f"cannot open lock file {path}: {exc}") from exc
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise DatabaseLockedError(
                f"{path.with_suffix('')} is already open in another process or handle"
            ) from None

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)
        with contextlib.suppress(OSError):
            self._path.unlink()


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    pages: int
    free_pages: int
    tables: dict[str, int] = field(default_factory=dict)  # table name -> row count


class Database:
    """An open KestrelDB database. Use Database.open(), then execute() or connect()."""

    def __init__(
        self,
        path: Path,
        file_lock: _FileLock,
        disk: DiskManager,
        wal: WalWriter,
        recovery: RecoveryStats,
        *,
        buffer_pages: int,
        synchronous: bool,
        checkpoint_bytes: int,
        lock_timeout: float,
    ) -> None:
        self._path = path
        self._file_lock = file_lock
        self._disk = disk
        self._wal = wal
        self.recovery = recovery
        # The WAL rule: before any page reaches the data file, the log records
        # describing it must be durable. wal.sync is a no-op when they already are.
        self._pool = BufferPool(disk, buffer_pages, before_page_write=wal.sync)
        self._catalog = Catalog(self._pool)
        self._executor = Executor(self._catalog)
        self._locks = LockManager(lock_timeout)
        self._synchronous = synchronous
        self._checkpoint_bytes = checkpoint_bytes
        self._txn_ids = itertools.count(1)
        self._failed: str | None = None
        self._closed = False
        self._connections: weakref.WeakSet[Connection] = weakref.WeakSet()
        self._default = Connection(self)
        self.checkpoints = 0
        self.commits = 0

    # ------------------------------------------------------------------ open / close

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        create: bool = True,
        page_size: int = config.DEFAULT_PAGE_SIZE,
        buffer_pages: int = config.DEFAULT_BUFFER_PAGES,
        synchronous: bool = True,
        checkpoint_bytes: int = config.DEFAULT_CHECKPOINT_BYTES,
        lock_timeout: float = config.DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> Database:
        """Open a database, creating it if it does not exist and `create` is true.

        synchronous=False skips the fsync at commit. Commits are then much
        faster and still survive a crash of this process, but can be lost if the
        operating system crashes or the power fails.
        """
        db_path = Path(path)
        if not create and not db_path.exists():
            raise DatabaseNotFoundError(f"no such database file: {db_path}")
        file_lock = _FileLock(Path(str(db_path) + config.LOCK_SUFFIX))
        disk: DiskManager | None = None
        wal: WalWriter | None = None
        try:
            if db_path.exists():
                disk = DiskManager.open(db_path, check_length=False)
            else:
                disk = DiskManager.create(db_path, page_size)
            wal_path = Path(str(db_path) + config.WAL_SUFFIX)

            stats = recover(disk, wal_path)
            if disk.header.lsn != stats.last_lsn:
                disk.set_header(replace(disk.header, lsn=stats.last_lsn))
            disk.flush_header()
            faults.crash_point("recovery.before_sync")
            disk.sync()
            disk.verify_length()
            faults.crash_point("recovery.before_log_reset")
            wal = WalWriter.create(wal_path, disk.page_size, disk.header.db_id, stats.last_lsn + 1)

            database = cls(
                db_path,
                file_lock,
                disk,
                wal,
                stats,
                buffer_pages=buffer_pages,
                synchronous=synchronous,
                checkpoint_bytes=checkpoint_bytes,
                lock_timeout=lock_timeout,
            )
            database._load_catalog()
            return database
        except BaseException:
            if wal is not None:
                wal.close()
            if disk is not None:
                with contextlib.suppress(KestrelError):
                    disk.close()
            file_lock.release()
            raise

    def _load_catalog(self) -> None:
        if self._disk.header.catalog_root == 0:
            # A brand-new file. Creating the catalog is an ordinary logged
            # transaction, so a crash here simply leaves a file that is
            # bootstrapped again on the next open.
            self._pool.begin()
            try:
                self._catalog.bootstrap()
                self._log_and_commit(next(self._txn_ids))
            except BaseException:
                if self._pool.in_transaction:
                    self._pool.rollback()
                raise
        self._catalog.load()

    def close(self) -> None:
        """Roll back open transactions, checkpoint, and release the files."""
        if self._closed:
            return
        try:
            for connection in list(self._connections):
                connection._abandon()
            if self._failed is None:
                self._checkpoint()
        finally:
            self._closed = True
            self._wal.close()
            try:
                self._disk.close()
            finally:
                self._file_lock.release()

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ public API

    @property
    def path(self) -> Path:
        return self._path

    @property
    def page_size(self) -> int:
        return self._disk.page_size

    @property
    def page_count(self) -> int:
        return self._disk.page_count

    @property
    def pool(self) -> BufferPool:
        return self._pool

    @property
    def wal_size(self) -> int:
        return self._wal.size

    def connect(self) -> Connection:
        """A new connection. Give each thread its own."""
        self._require_open()
        return Connection(self)

    def execute(self, sql: str) -> Result:
        """Run one statement on the database's default connection."""
        return self._default.execute(sql)

    def table_names(self) -> list[str]:
        self._require_open()
        return self._catalog.table_names()

    def checkpoint(self) -> None:
        """Write all committed changes to the data file and empty the log."""
        self._require_open()
        owner = next(self._txn_ids)
        self._locks.acquire(owner, _WRITER, EXCLUSIVE)
        try:
            self._checkpoint()
        finally:
            self._locks.release_all(owner)

    # ------------------------------------------------------------------ internals

    def _require_open(self) -> None:
        if self._closed:
            raise DatabaseClosedError(f"{self._path} is closed")

    def _new_transaction(self, *, explicit: bool) -> Transaction:
        self._require_open()
        return Transaction(next(self._txn_ids), explicit)

    def _run(self, txn: Transaction, statement: ast.Statement) -> Result:
        """Lock what the statement needs and execute it."""
        if isinstance(statement, ast.Select):
            self._locks.acquire(txn.txn_id, _table_lock(statement.table), SHARED)
            return self._executor.select(statement)
        if isinstance(statement, ast.Explain):
            self._locks.acquire(txn.txn_id, _table_lock(statement.statement.table), SHARED)
            return self._executor.explain(statement)
        if isinstance(statement, (ast.Begin, ast.Commit, ast.Rollback)):
            raise TransactionError("transaction control is handled by the connection")

        if self._failed is not None:
            raise EngineFailedError(
                f"an earlier durability step failed ({self._failed}); "
                f"close and reopen the database before writing"
            )
        if not txn.writing:
            # One writer at a time: page-level logging and rollback are only
            # correct if a page never holds two transactions' uncommitted changes.
            self._locks.acquire(txn.txn_id, _WRITER, EXCLUSIVE)
            self._pool.begin()
            txn.writing = True
        self._locks.acquire(txn.txn_id, _table_lock(statement.table), EXCLUSIVE)

        changes_before = self._pool.change_count
        try:
            if isinstance(statement, ast.Insert):
                return self._executor.insert(statement)
            if isinstance(statement, ast.Update):
                return self._executor.update(statement)
            if isinstance(statement, ast.Delete):
                return self._executor.delete(statement)
            if isinstance(statement, ast.CreateTable):
                return self._executor.create_table(statement)
            return self._executor.drop_table(statement)
        except BaseException:
            txn.must_abort = self._pool.change_count != changes_before
            raise

    def _finish(self, txn: Transaction, *, commit: bool) -> None:
        """End a transaction and release its locks."""
        try:
            if txn.writing:
                txn.writing = False
                if commit:
                    try:
                        self._log_and_commit(txn.txn_id)
                    except BaseException:
                        self._wal.discard_buffer()
                        self._rollback_pages()
                        raise
                    if self._wal.size > self._checkpoint_bytes:
                        self._checkpoint()
                else:
                    self._rollback_pages()
        finally:
            self._locks.release_all(txn.txn_id)

    def _rollback_pages(self) -> None:
        if self._pool.in_transaction:
            self._pool.rollback()
        # The in-memory catalog, free-space maps and row counts may describe
        # changes that no longer exist. Rebuild them from the restored pages.
        self._catalog.load()

    def _log_and_commit(self, txn_id: int) -> None:
        """Make the active transaction durable (the commit protocol)."""
        pool, wal = self._pool, self._wal
        pages = pool.transaction_pages()
        if not pages and not pool.header_changed():
            pool.commit()  # the transaction changed nothing
            return
        faults.crash_point("commit.before_log")
        try:
            if pool.header_changed():
                wal.append(RecordType.HEADER, txn_id, self._disk.header.pack())
            for page_id, data in pages:
                set_page_lsn(data, wal.next_lsn)
                wal.append(RecordType.PAGE_IMAGE, txn_id, _PAGE_ID.pack(page_id) + bytes(data))
            wal.append(RecordType.COMMIT, txn_id)
            faults.crash_point("commit.before_flush")
            wal.flush(sync=self._synchronous)
        except StorageIOError as exc:
            # The log may or may not contain the commit record now. Refuse
            # further writes; reopening runs recovery, which settles the outcome.
            wal.discard_buffer()
            self._failed = str(exc)
            raise
        pool.commit()
        self.commits += 1
        faults.crash_point("commit.after_durable")

    def _checkpoint(self) -> None:
        """Caller must hold the writer slot (or be the only user, as in close())."""
        if self._pool.in_transaction:
            raise TransactionError("cannot checkpoint during a transaction")
        faults.crash_point("checkpoint.begin")
        try:
            last_lsn = self._wal.next_lsn - 1
            self._disk.set_header(replace(self._disk.header, lsn=last_lsn))
            self._pool.flush_all()
            faults.crash_point("checkpoint.after_flush")
            self._disk.sync()
            faults.crash_point("checkpoint.after_sync")
            self._wal.reset(last_lsn + 1)
        except StorageIOError as exc:
            self._failed = str(exc)
            raise
        self.checkpoints += 1

    # ------------------------------------------------------------------ integrity

    def check(self) -> IntegrityReport:
        """Verify every invariant that can be checked from the stored data.

        Raises a CorruptionError naming the first violation. Covers page
        checksums, heap page layout (H1-H6), B+ tree shape (T1-T6), that every
        record decodes (C2), that indexes and tables agree (C3, C4), the free
        list (A1), and that every page is owned by exactly one structure (A2).
        """
        self._require_open()
        owner = next(self._txn_ids)
        self._locks.acquire(owner, _WRITER, EXCLUSIVE)
        try:
            return self._check()
        finally:
            self._locks.release_all(owner)

    def _check(self) -> IntegrityReport:
        owners: dict[int, str] = {}

        def claim(page_id: int, who: str) -> None:
            if page_id in owners:
                raise CorruptFileError(
                    f"A2: page {page_id} belongs to both {owners[page_id]} and {who}"
                )
            owners[page_id] = who

        free_pages = self._pool.free_list()
        for page_id in free_pages:
            claim(page_id, "the free list")
        for page_id in self._catalog.heap.pages():
            claim(page_id, "the catalog")
        self._catalog.heap.validate()

        tables: dict[str, int] = {}
        for table in self._catalog.tables():
            schema = table.schema
            for page_id in table.heap.pages():
                claim(page_id, f"table {schema.name}")
            table.heap.validate()
            rows = {
                rid: decode_record(schema.column_types, record) for rid, record in table.heap.scan()
            }
            for position, column in enumerate(schema.columns):
                if column.not_null and any(row[position] is None for row in rows.values()):
                    raise CorruptRecordError(
                        f"C2: {schema.name}.{column.name} is NOT NULL but holds a NULL"
                    )
            for position, tree in table.indexes.items():
                column = schema.columns[position]
                label = f"index {schema.name}.{column.name}"
                for page_id in tree.pages():
                    claim(page_id, label)
                tree.check_invariants()
                expected = {}
                for rid, row in rows.items():
                    value = row[position]
                    if value is None:
                        continue
                    key = encode_key(column.type, value)
                    if key in expected:
                        raise CorruptIndexError(f"C4: {label} has a duplicate value {value!r}")
                    expected[key] = rid
                if dict(tree.scan()) != expected:
                    raise CorruptIndexError(f"C3: {label} does not match the table's rows")
            tables[schema.name] = len(rows)

        for page_id in range(1, self._disk.page_count):
            if page_id not in owners:
                raise CorruptFileError(
                    f"A2: page {page_id} is not reachable from anything (leaked)"
                )
        return IntegrityReport(self._disk.page_count, len(free_pages), tables)


class Connection:
    """A session: holds at most one open transaction. Not safe to share between threads."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._txn: Transaction | None = None
        self._guard = threading.Lock()
        database._connections.add(self)

    @property
    def in_transaction(self) -> bool:
        return self._txn is not None

    def execute(self, sql: str) -> Result:
        """Parse and run one SQL statement."""
        statement = parse(sql)
        db = self._db
        db._require_open()
        with self._guard:
            if isinstance(statement, ast.Begin):
                if self._txn is not None:
                    raise TransactionError("a transaction is already open on this connection")
                self._txn = db._new_transaction(explicit=True)
                return Result()
            if isinstance(statement, (ast.Commit, ast.Rollback)):
                if self._txn is None:
                    raise TransactionError("no transaction is open on this connection")
                txn, self._txn = self._txn, None
                db._finish(txn, commit=isinstance(statement, ast.Commit))
                return Result()

            implicit = self._txn is None
            txn = self._txn or db._new_transaction(explicit=False)
            try:
                result = db._run(txn, statement)
            except (DeadlockError, LockTimeoutError, TransactionTooLargeError):
                # None of these can be fixed by retrying the statement inside
                # the same transaction, so the transaction ends here.
                self._abort(txn)
                raise
            except BaseException:
                # A statement that failed before changing anything leaves an
                # explicit transaction usable. One that failed midway cannot be
                # undone alone, so the whole transaction is rolled back.
                if implicit or txn.must_abort:
                    self._abort(txn)
                raise
            if implicit:
                db._finish(txn, commit=True)
            return result

    def _abort(self, txn: Transaction) -> None:
        self._txn = None
        self._db._finish(txn, commit=False)

    def _abandon(self) -> None:
        """Roll back whatever is open (used when the database closes)."""
        txn, self._txn = self._txn, None
        if txn is not None:
            self._db._finish(txn, commit=False)

    def close(self) -> None:
        """Roll back any open transaction."""
        with self._guard:
            self._abandon()
