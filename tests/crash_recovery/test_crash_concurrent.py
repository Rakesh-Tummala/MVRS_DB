"""Crash recovery with several threads committing at once (group commit).

The process is killed at an injected point while threads are committing
two-statement transactions. After recovery:

    durability   every commit a thread was told about is present
    atomicity    each thread's counter equals the number of its rows, so no
                 transaction is half applied
    order        each thread's rows form an unbroken run 0..n-1
    bound        at most one extra transaction per thread survived (the one it
                 was waiting on), never more
    consistency  Database.check() passes

Commits are batched, so one kill can take several threads' pending commits with
it; they were never acknowledged, so that is allowed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db import faults

pytestmark = pytest.mark.crash

WORKER = Path(__file__).resolve().parent / "threaded_worker.py"
THREADS = 6
PER_THREAD = 12
SMALL_POOL = 16
SMALL_LOG = 64 * 1024
HUGE_LOG = 1 << 30


def run_worker(
    path: Path,
    *,
    crash_at: str | None,
    buffer_pages: int = 1024,
    checkpoint_bytes: int = HUGE_LOG,
    isolation: str = "locking",
) -> int:
    env = dict(os.environ)
    env.pop(faults.ENV_VAR, None)
    env["MVRS_ISOLATION"] = isolation
    if crash_at:
        env[faults.ENV_VAR] = crash_at
    command = [
        sys.executable,
        str(WORKER),
        str(path),
        str(THREADS),
        str(PER_THREAD),
        str(buffer_pages),
        str(checkpoint_bytes),
    ]
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
    if completed.returncode not in (0, faults.CRASH_EXIT_CODE):
        raise AssertionError(f"worker failed unexpectedly:\n{completed.stderr}")
    return completed.returncode


def acknowledged(path: Path) -> dict[int, int]:
    """thread -> number of commits it was told about."""
    ack = Path(str(path) + ".ack")
    counts: dict[int, int] = {}
    for line in ack.read_text().split("\n") if ack.exists() else []:
        parts = line.split()
        if len(parts) == 2:
            tid, seq = int(parts[0]), int(parts[1])
            assert seq == counts.get(tid, 0), "acknowledgements arrived out of order"
            counts[tid] = seq + 1
    return counts


def verify(path: Path) -> mvrs_db.Database:
    acked = acknowledged(path)
    setup_done = "setup" in Path(str(path) + ".ack").read_text().split()
    db = mvrs_db.connect(path, create=False)
    try:
        db.check()
        if not setup_done:
            return db  # died before the workload began; any consistent state will do
        assert {"log", "counters"} <= set(db.table_names()), (
            "setup was acknowledged but its tables did not survive"
        )
        for tid in range(THREADS):
            (counter,) = db.execute(f"SELECT n FROM counters WHERE tid = {tid}").rows[0]
            seqs = [
                row[0]
                for row in db.execute(f"SELECT seq FROM log WHERE tid = {tid} ORDER BY seq").rows
            ]
            assert seqs == list(range(len(seqs))), f"thread {tid} has a gap: {seqs}"
            assert counter == len(seqs), f"thread {tid}: counter {counter}, rows {len(seqs)}"
            told = acked.get(tid, 0)
            assert len(seqs) >= told, f"thread {tid}: told {told} commits, {len(seqs)} survived"
            assert len(seqs) <= told + 1, f"thread {tid}: {len(seqs)} survived, told {told}"
        return db
    except BaseException:
        db.close()
        raise


# (crash point and which hit to die at, pool size, checkpoint threshold)
CASES = [
    # buffered but not written: the commits queued behind the leader are lost
    ("commit.before_flush:15", 1024, HUGE_LOG),
    ("commit.before_flush:40", 1024, HUGE_LOG),
    # inside the leader's write
    ("wal.before_flush:6", 1024, HUGE_LOG),
    ("wal.before_flush:20", 1024, HUGE_LOG),
    ("wal.partial_flush:5", 1024, HUGE_LOG),
    ("wal.partial_flush:14", 1024, HUGE_LOG),
    # written but not yet fsynced
    ("wal.after_write:8", 1024, HUGE_LOG),
    # after an fsync, before the waiting threads have been released
    ("wal.after_sync:10", 1024, HUGE_LOG),
    ("commit.after_durable:30", 1024, HUGE_LOG),
    # pages being evicted and checkpoints running while commits are pending
    ("disk.torn_page_write:3", SMALL_POOL, SMALL_LOG),
    ("disk.torn_page_write:12", SMALL_POOL, SMALL_LOG),
    ("checkpoint.after_flush:1", SMALL_POOL, SMALL_LOG),
    ("checkpoint.after_sync:2", SMALL_POOL, SMALL_LOG),
    # Hit 1 of reset is the log being created with the database; 2 and 3 are
    # real checkpoints.
    ("wal.reset_before_truncate:2", SMALL_POOL, SMALL_LOG),
    ("wal.reset_before_truncate:3", SMALL_POOL, SMALL_LOG),
]


@pytest.mark.parametrize(("crash_at", "buffer_pages", "checkpoint_bytes"), CASES)
def test_kill_while_threads_are_committing(
    tmp_path: Path, crash_at: str, buffer_pages: int, checkpoint_bytes: int
) -> None:
    path = tmp_path / "threads.mvrs"

    code = run_worker(
        path, crash_at=crash_at, buffer_pages=buffer_pages, checkpoint_bytes=checkpoint_bytes
    )

    assert code == faults.CRASH_EXIT_CODE, f"the worker never reached {crash_at}"
    verify(path).close()
    # The recovered database keeps working and stays consistent.
    with mvrs_db.connect(path) as db:
        db.execute("CREATE TABLE after_crash (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO after_crash VALUES (1)")
        db.check()


# The same kills with snapshot isolation and a reader thread running throughout.
SNAPSHOT_CASES = [
    ("commit.before_flush:20", 1024, HUGE_LOG),
    ("wal.partial_flush:8", 1024, HUGE_LOG),
    ("wal.after_sync:10", 1024, HUGE_LOG),
    ("disk.torn_page_write:5", SMALL_POOL, SMALL_LOG),
    ("checkpoint.after_flush:1", SMALL_POOL, SMALL_LOG),
]


@pytest.mark.parametrize(("crash_at", "buffer_pages", "checkpoint_bytes"), SNAPSHOT_CASES)
def test_kill_during_snapshot_isolation(
    tmp_path: Path, crash_at: str, buffer_pages: int, checkpoint_bytes: int
) -> None:
    path = tmp_path / "snap.mvrs"

    code = run_worker(
        path,
        crash_at=crash_at,
        buffer_pages=buffer_pages,
        checkpoint_bytes=checkpoint_bytes,
        isolation="snapshot",
    )

    assert code == faults.CRASH_EXIT_CODE, f"the worker never reached {crash_at}"
    verify(path).close()


def test_a_clean_snapshot_run_loses_nothing(tmp_path: Path) -> None:
    path = tmp_path / "snap.mvrs"

    assert (
        run_worker(
            path,
            crash_at=None,
            buffer_pages=SMALL_POOL,
            checkpoint_bytes=SMALL_LOG,
            isolation="snapshot",
        )
        == 0
    )

    assert acknowledged(path) == dict.fromkeys(range(THREADS), PER_THREAD)
    db = verify(path)
    assert db.check().tables == {"log": THREADS * PER_THREAD, "counters": THREADS}
    db.close()


def test_a_clean_run_loses_nothing(tmp_path: Path) -> None:
    path = tmp_path / "threads.mvrs"

    assert run_worker(path, crash_at=None, buffer_pages=SMALL_POOL, checkpoint_bytes=SMALL_LOG) == 0

    acked = acknowledged(path)
    assert acked == dict.fromkeys(range(THREADS), PER_THREAD)
    db = verify(path)
    assert db.check().tables == {"log": THREADS * PER_THREAD, "counters": THREADS}
    db.close()
