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
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType

from mvrs_db import config, faults
from mvrs_db.catalog.ann_store import AnnStore
from mvrs_db.catalog.catalog import Catalog
from mvrs_db.errors import (
    CorruptFileError,
    CorruptIndexError,
    CorruptRecordError,
    DatabaseClosedError,
    DatabaseLockedError,
    DatabaseNotFoundError,
    DeadlockError,
    EngineFailedError,
    LockTimeoutError,
    MvrsDbError,
    ParameterError,
    SerializationError,
    StorageIOError,
    TransactionError,
    TransactionTooLargeError,
)
from mvrs_db.execution.executor import Executor, Result
from mvrs_db.recovery.delta import changed_ranges, delta_size, encode_delta
from mvrs_db.recovery.recovery_manager import RecoveryStats, recover
from mvrs_db.recovery.wal import RecordType, WalWriter
from mvrs_db.sql import ast
from mvrs_db.sql.binding import bind, coerce_params
from mvrs_db.sql.parser import parse_template
from mvrs_db.storage.buffer_pool import BufferPool, SnapshotPool
from mvrs_db.storage.disk_manager import DiskManager
from mvrs_db.storage.page import page_lsn, set_page_lsn
from mvrs_db.storage.record import decode_record
from mvrs_db.transactions.lock_manager import EXCLUSIVE, SHARED, LockManager
from mvrs_db.transactions.transaction import Transaction
from mvrs_db.vector.hnsw import SearchSettings

_PAGE_ID = struct.Struct("<I")
_WRITER = "#writer"  # the single writer slot; '#' cannot appear in a table name


# Parsed statements kept for reuse, and the longest statement text worth keeping.
_PARSE_CACHE_SIZE = 256
_MAX_CACHED_SQL = 4096


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
    """An open MVRS_DB database. Use Database.open(), then execute() or connect()."""

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
        log_deltas: bool,
        snapshot_reads: bool,
    ) -> None:
        self._path = path
        self._file_lock = file_lock
        self._disk = disk
        self._wal = wal
        self.recovery = recovery
        # The WAL rule: before any page reaches the data file, the log records
        # describing it must be durable. wal.sync is a no-op when they already are.
        self._pool = BufferPool(disk, buffer_pages, before_page_write=self._make_log_durable)
        self._catalog = Catalog(self._pool, AnnStore(path, disk.header.db_id))
        self._executor = Executor(self._catalog)
        self._locks = LockManager(lock_timeout)
        # Snapshot isolation: readers see the database as of one commit and take
        # no locks. Catalog views are built per commit number and shared by every
        # reader at that number; _views_lock guards the dictionary.
        self._snapshot_reads = snapshot_reads
        self._views: dict[int, Catalog] = {}
        self._views_lock = threading.Lock()
        # Parsed statements by SQL text. A statement tree is immutable, so one
        # entry can be shared by every connection and thread.
        self._parsed: OrderedDict[str, tuple[ast.Statement, int]] = OrderedDict()
        self._parsed_lock = threading.Lock()
        self._synchronous = synchronous
        self._log_deltas = log_deltas
        # Pages that have a committed full image in the log since the last
        # checkpoint. Only these may be logged as deltas.
        self._imaged: set[int] = set()
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
        log_deltas: bool = True,
        isolation: str = "locking",
    ) -> Database:
        """Open a database, creating it if it does not exist and `create` is true.

        synchronous=False skips the fsync at commit. Commits are then much
        faster and still survive a crash of this process, but can be lost if the
        operating system crashes or the power fails.

        log_deltas=False logs a whole page for every change, as the engine did
        before deltas existed. It exists to measure what deltas save.

        isolation="locking" (the default) is strict two-phase locking: a reader
        takes shared table locks and waits for any writer on those tables.
        isolation="snapshot" is snapshot isolation: a SELECT reads the database as
        of the last commit when it started, takes no locks, and never waits for
        or blocks a writer. See docs/13-snapshot-isolation.md.
        """
        if isolation not in ("locking", "snapshot"):
            raise ValueError(f"isolation must be 'locking' or 'snapshot', not {isolation!r}")
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
                log_deltas=log_deltas,
                snapshot_reads=isolation == "snapshot",
            )
            database._load_catalog()
            return database
        except BaseException:
            if wal is not None:
                wal.close()
            if disk is not None:
                with contextlib.suppress(MvrsDbError):
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
                end = self._log_and_commit(next(self._txn_ids))
            except BaseException:
                if self._pool.in_transaction:
                    self._pool.rollback()
                raise
            self._await_durable(end)
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
                self._save_vector_indexes_quietly()
        finally:
            self._closed = True
            self._wal.close()
            try:
                self._disk.close()
            finally:
                self._file_lock.release()

    def save_vector_indexes(self) -> int:
        """Save every changed vector index, so the next open need not rebuild it.

        Returns how many were written. Close does this automatically; calling it
        earlier protects against a crash, which would otherwise lose the saved
        copy (the next open then rebuilds from the table, which is always correct).
        """
        self._require_open()
        if self._pool.in_transaction:
            return 0
        return self._catalog.save_vector_indexes()

    def _save_vector_indexes_quietly(self) -> None:
        # The snapshot is a cache. Failing to write it (disk full, read-only
        # directory) must not stop the database from closing.
        with contextlib.suppress(OSError):
            self._catalog.save_vector_indexes()

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

    @property
    def log_bytes_written(self) -> int:
        """Log record bytes written since the database was opened, across checkpoints."""
        return self._wal.bytes_written

    @property
    def vector(self) -> SearchSettings:
        """Vector search switches: use_index, ef_search, min_rows. Change them at any time."""
        return self._executor.vector_settings

    def connect(self) -> Connection:
        """A new connection. Give each thread its own."""
        self._require_open()
        return Connection(self)

    def execute(self, sql: str, params: Sequence[object] = ()) -> Result:
        """Run one statement on the database's default connection.

        Write `?` where a value belongs and pass the values in `params`:
        `db.execute("SELECT * FROM t WHERE id = ?", (5,))`.
        """
        return self._default.execute(sql, params)

    def _parse(self, sql: str) -> tuple[ast.Statement, int]:
        """The statement for this text and how many `?` it has, parsed at most once."""
        with self._parsed_lock:
            cached = self._parsed.get(sql)
            if cached is not None:
                self._parsed.move_to_end(sql)
                return cached
        parsed = parse_template(sql)  # outside the lock: parsing can be slow and may raise
        if len(sql) <= _MAX_CACHED_SQL:
            with self._parsed_lock:
                self._parsed[sql] = parsed
                while len(self._parsed) > _PARSE_CACHE_SIZE:
                    self._parsed.popitem(last=False)
        return parsed

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

    def _lock_for_reading(self, txn: Transaction, statement: ast.Select) -> None:
        """Shared locks on every table a SELECT reads.

        A join locks every table it names, in name order. Taking locks in one fixed
        order means two joins of the same tables can never each hold one lock
        while waiting for the other.
        """
        tables = {statement.table}
        tables.update(join.table for join in statement.joins)
        for name in sorted(tables):
            self._locks.acquire(txn.txn_id, _table_lock(name), SHARED)

    def _run(
        self,
        txn: Transaction,
        statement: ast.Statement,
        template: ast.Statement | None = None,
        params: Sequence[ast.Value] = (),
    ) -> Result:
        """Lock what the statement needs and execute it.

        `statement` has its values filled in. `template` and `params` are the same
        statement before binding and the values; the executor uses them to reuse
        a cached plan.
        """
        if isinstance(statement, (ast.Begin, ast.Commit, ast.Rollback)):
            raise TransactionError("transaction control is handled by the connection")
        # Once the log cannot be made durable, memory may hold commits that
        # will not survive a restart. Nothing can be trusted, reads included.
        failure = self._failed or self._wal.failure
        if failure is not None:
            raise EngineFailedError(
                f"an earlier durability step failed ({failure}); close and reopen the database"
            )
        if isinstance(statement, ast.Select):
            if self._snapshot_reads and not txn.writing:
                catalog = self._snapshot_catalog(txn)
                result = self._executor.select(
                    statement,
                    catalog,
                    self._snapshot_settings(),
                    template=template,
                    params=params,
                )
                self._wait_for_visible_commits()
                return result
            if not self._snapshot_reads:
                self._lock_for_reading(txn, statement)
            result = self._executor.select(statement, template=template, params=params)
            self._wait_for_visible_commits()
            return result
        if isinstance(statement, ast.Explain):
            inner = statement.statement
            if isinstance(inner, ast.Select) and self._snapshot_reads and not txn.writing:
                catalog = self._snapshot_catalog(txn)
                return self._executor.explain(statement, catalog, self._snapshot_settings())
            if not self._snapshot_reads:
                if isinstance(inner, ast.Select):
                    self._lock_for_reading(txn, inner)
                else:
                    self._locks.acquire(txn.txn_id, _table_lock(inner.table), SHARED)
            elif not isinstance(inner, ast.Select) and not txn.writing:
                self._locks.acquire(txn.txn_id, _table_lock(inner.table), SHARED)
            return self._executor.explain(statement)

        if not txn.writing:
            # One writer at a time: page-level logging and rollback are only
            # correct if a page never holds two transactions' uncommitted changes.
            self._locks.acquire(txn.txn_id, _WRITER, EXCLUSIVE)
            if txn.snapshot is not None:
                # This transaction has been reading a snapshot. Nobody else can
                # commit while it holds the writer slot, so if the database is
                # still at that commit, its reads are exactly what it would read
                # now and it may carry on as a writer. If not, whatever it
                # decides to write rests on data that has since changed.
                if txn.snapshot != self._pool.commit_seq:
                    raise SerializationError(
                        "could not serialize access: the database changed after this "
                        "transaction's snapshot was taken; roll back and retry"
                    )
                self._end_snapshot(txn)
            self._pool.begin()
            txn.writing = True
        if isinstance(statement, ast.DropIndex):
            lock_name = self._catalog.index_owner(statement.name)[0].schema.name
        else:
            lock_name = statement.table
        self._locks.acquire(txn.txn_id, _table_lock(lock_name), EXCLUSIVE)

        changes_before = self._pool.change_count
        try:
            if isinstance(statement, ast.Insert):
                return self._executor.insert(statement)
            if isinstance(statement, ast.Update):
                return self._executor.update(statement, template=template, params=params)
            if isinstance(statement, ast.Delete):
                return self._executor.delete(statement, template=template, params=params)
            if isinstance(statement, ast.CreateTable):
                return self._executor.create_table(statement)
            if isinstance(statement, ast.CreateIndex):
                return self._executor.create_index(statement)
            if isinstance(statement, ast.DropIndex):
                return self._executor.drop_index(statement)
            return self._executor.drop_table(statement)
        except BaseException:
            txn.must_abort = self._pool.change_count != changes_before
            raise

    # ------------------------------------------------------------------ snapshots

    def _snapshot_catalog(self, txn: Transaction) -> Catalog:
        """The catalog as of the transaction's snapshot, taking the snapshot if it has none."""
        if txn.snapshot is None:
            txn.snapshot = self._pool.begin_snapshot()
        seq = txn.snapshot
        with self._views_lock:
            view = self._views.get(seq)
            if view is None:
                view = self._catalog.view(SnapshotPool(self._pool, seq))
                # Views for commits no snapshot can use any more are dropped.
                keep = set(self._pool.active_snapshots()) | {seq}
                for old in [number for number in self._views if number not in keep]:
                    del self._views[old]
                self._views[seq] = view
            return view

    def _snapshot_settings(self) -> SearchSettings:
        # The approximate index describes the newest rows, which this reader may
        # not be allowed to see, so a snapshot always searches exactly.
        return replace(self._executor.vector_settings, use_index=False)

    def _end_snapshot(self, txn: Transaction) -> None:
        if txn.snapshot is not None:
            seq, txn.snapshot = txn.snapshot, None
            self._pool.end_snapshot(seq)

    def _finish(self, txn: Transaction, *, commit: bool) -> None:
        """End a transaction.

        A commit has two moments. The commit record is appended to the log while
        the transaction still holds its locks, which fixes its place in the
        order of commits. Then the locks are released (early lock release) and
        the transaction waits for the log to reach the disk. Other transactions
        proceed during that wait, and their commit records land behind this one,
        so one fsync can make a whole group of them durable.
        """
        durable_at: int | None = None
        was_writer = txn.writing
        self._end_snapshot(txn)
        try:
            if txn.writing:
                txn.writing = False
                if commit:
                    try:
                        durable_at = self._log_and_commit(txn.txn_id)
                    except BaseException:
                        self._rollback_pages()
                        raise
                    self._catalog.transaction_committed()
                else:
                    self._rollback_pages()
        finally:
            self._locks.release_all(txn.txn_id)
        if commit:
            self._await_durable(durable_at)
            if was_writer:
                self._maybe_checkpoint()

    def _await_durable(self, position: int | None) -> None:
        """Wait until the log is durable up to `position` (None: nothing to wait for)."""
        if position is None:
            return
        try:
            self._wal.flush_through(position, sync=self._synchronous)
        except StorageIOError as exc:
            # The commit record may or may not have reached the disk; reopening
            # runs recovery, which settles it. Until then the engine is stopped.
            self._failed = str(exc)
            raise
        faults.crash_point("commit.after_durable")

    def _make_log_durable(self, position: int | None) -> None:
        """The WAL rule, called by the buffer pool before it writes a page.

        `position` is the end of the commit record of the transaction that last
        changed the page; None means everything appended so far. A page that
        was committed but whose commit record is still waiting for its fsync
        holds the write until the log catches up.
        """
        if position is None:
            self._wal.sync()
        elif position:
            self._wal.flush_through(position, sync=True)

    def _wait_for_visible_commits(self) -> None:
        """Do not hand data to the caller that a crash could still take back.

        A transaction's changes become visible to others when it appends its
        commit record, slightly before the record is durable. A reader that saw
        such changes must not return them until they are durable. For a writer
        that is automatic: its own commit record lies after the one it read, so
        it cannot be acknowledged first. A reader has no record of its own, so it
        waits for everything appended so far. With nothing pending this is a
        comparison and returns at once.
        """
        self._await_durable(self._wal.next_lsn)

    def _maybe_checkpoint(self) -> None:
        """Checkpoint if the log has grown past its limit. Takes the writer slot."""
        if self._wal.size <= self._checkpoint_bytes or self._failed is not None:
            return
        owner = next(self._txn_ids)
        self._locks.acquire(owner, _WRITER, EXCLUSIVE)
        try:
            # Another committer may have checkpointed while this one waited.
            if self._wal.size > self._checkpoint_bytes and self._failed is None:
                self._checkpoint()
        finally:
            self._locks.release_all(owner)

    def _rollback_pages(self) -> None:
        if self._pool.in_transaction:
            self._pool.rollback()
        # The in-memory catalog, free-space maps and row counts may describe
        # changes that no longer exist. Rebuild them from the restored pages.
        self._catalog.load()

    def _log_and_commit(self, txn_id: int) -> int | None:
        """Append the active transaction's log records and mark it committed in memory.

        Returns the log position after its commit record, which the caller waits
        on for durability, or None if the transaction changed nothing. Nothing
        here touches the disk.
        """
        pool, wal = self._pool, self._wal
        # Decide, per changed page, between a delta and a full image.
        # (page ID, page buffer, ranges that changed or None for a full image)
        to_log: list[tuple[int, bytearray, bytes | None, list[tuple[int, int]] | None]] = []
        half_page = self._disk.page_size // 2
        for page_id, before, data in pool.transaction_changes():
            if before is None:
                to_log.append((page_id, data, None, None))  # a new page
                continue
            ranges = changed_ranges(before, data)
            if not ranges:
                continue  # fetched for writing but left as it was
            needs_image = (
                not self._log_deltas
                # The first change since the last checkpoint must be a full
                # image: it is what repairs the page if a later write is torn.
                or page_id not in self._imaged
                or delta_size(ranges) > half_page
            )
            to_log.append((page_id, data, before, None if needs_image else ranges))

        if not to_log and not pool.header_changed():
            pool.commit()  # the transaction changed nothing
            return None
        faults.crash_point("commit.before_log")
        # All of this transaction's records go into the buffer as one unit. The
        # pool's latch is not taken inside the block: the pool calls into the log
        # while holding its latch (before it writes a page), so the other order
        # could deadlock.
        with wal.atomic_append():
            if pool.header_changed():
                wal.append(RecordType.HEADER, txn_id, self._disk.header.pack())
            for page_id, data, before, delta_ranges in to_log:
                set_page_lsn(data, wal.next_lsn)
                if delta_ranges is None or before is None:
                    wal.append(RecordType.PAGE_IMAGE, txn_id, _PAGE_ID.pack(page_id) + bytes(data))
                else:
                    wal.append(
                        RecordType.PAGE_DELTA,
                        txn_id,
                        encode_delta(page_id, page_lsn(before), data, delta_ranges),
                    )
            wal.append(RecordType.COMMIT, txn_id)
            end = wal.next_lsn
        # These pages now have a full image in the log (possibly not on disk
        # yet; any write of the page waits for the log first, so that is enough).
        self._imaged.update(page_id for page_id, _d, _b, planned in to_log if planned is None)
        pool.commit(end)
        self.commits += 1
        faults.crash_point("commit.before_flush")  # buffered, not yet written
        return end

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
        # The log is empty again, so no page has an image in it any more.
        self._imaged.clear()
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
                    key = table.index_key(position, value, rid)
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

    def prepare(self, sql: str) -> PreparedStatement:
        """Parse a statement now, to run it later with different parameters."""
        self._db._require_open()
        statement, count = self._db._parse(sql)
        return PreparedStatement(self, statement, count)

    def execute(self, sql: str, params: Sequence[object] = ()) -> Result:
        """Parse (or reuse a parsed copy of) one SQL statement, bind `params`, and run it."""
        db = self._db
        db._require_open()
        template, count = db._parse(sql)
        return self._execute(template, count, params)

    def _execute(self, template: ast.Statement, count: int, params: Sequence[object]) -> Result:
        if isinstance(params, (str, bytes)) or not isinstance(params, Sequence):
            raise ParameterError("parameters must be given as a list or tuple of values")
        if len(params) != count:
            raise ParameterError(
                f"the statement has {count} `?` placeholder(s) but {len(params)} "
                f"parameter(s) were given"
            )
        values = coerce_params(params)
        statement = bind(template, values) if count else template
        db = self._db
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
                result = db._run(txn, statement, template, values)
            except (
                DeadlockError,
                LockTimeoutError,
                SerializationError,
                TransactionTooLargeError,
            ):
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


class PreparedStatement:
    """A statement parsed once and run many times: `stmt.execute((value, ...))`.

    Preparing saves the parsing, not the planning: each run is planned afresh,
    because the best plan depends on the bound values and on the table's current
    size. A prepared statement belongs to the connection that made it.
    """

    def __init__(self, connection: Connection, statement: ast.Statement, count: int) -> None:
        self._connection = connection
        self._statement = statement
        self.param_count = count

    def execute(self, params: Sequence[object] = ()) -> Result:
        self._connection._db._require_open()
        return self._connection._execute(self._statement, self.param_count, params)
