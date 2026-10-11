"""Crash recovery, tested by really killing a process.

Each test starts a child process that runs a workload and dies with os._exit at
an injected point: no cleanup, no flushing of Python buffers, no `finally`
blocks. The parent then opens the database, which runs recovery, and checks:

    durability   every transaction the child acknowledged is present
    atomicity    the database equals the model for a whole number of transactions
    consistency  Database.check() passes: checksums, page layouts, B+ tree
                 shape, indexes agreeing with tables, no page leaked or owned twice

What this does not test: the machine losing power. A killed process leaves the
operating system running, so bytes already handed to the OS still reach the
disk. Whether fsync truly survives power loss depends on the OS and the drive.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db import faults
from mvrs_db.recovery.wal import WAL_HEADER_SIZE, read_wal

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios  # noqa: E402

pytestmark = pytest.mark.crash

WORKER = Path(__file__).resolve().parent / "worker.py"
TRANSACTIONS = 30
SMALL_POOL = 24  # frames: forces committed pages to be written between checkpoints
# Bytes of log that trigger a checkpoint. This has to shrink as the log records do
# (deltas, then in-place index edits): at 96 KiB the "rows" workload made only three
# checkpoints, one of them the final one at close, and the crash points below that
# count checkpoints and page writes stopped meaning what their names say. At 32 KiB
# both workloads make about a dozen checkpoints and well over 100 page writes.
SMALL_LOG = 32 * 1024
HUGE_LOG = 1 << 30  # never checkpoint


def run_worker(
    path: Path,
    scenario: str,
    *,
    crash_at: str | None = None,
    transactions: int = TRANSACTIONS,
    buffer_pages: int = SMALL_POOL,
    checkpoint_bytes: int = SMALL_LOG,
    die_at_end: bool = False,
) -> int:
    env = dict(os.environ)
    env.pop(faults.ENV_VAR, None)
    if crash_at:
        env[faults.ENV_VAR] = crash_at
    command = [
        sys.executable,
        str(WORKER),
        str(path),
        scenario,
        str(transactions),
        str(buffer_pages),
        str(checkpoint_bytes),
    ]
    if die_at_end:
        command.append("exit")
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
    if completed.returncode not in (0, faults.CRASH_EXIT_CODE):
        raise AssertionError(f"worker failed unexpectedly:\n{completed.stderr}")
    return completed.returncode


def acknowledged(path: Path) -> tuple[bool, int]:
    """(setup acknowledged, number of acknowledged transactions)."""
    ack = Path(str(path) + ".ack")
    lines = ack.read_text().split() if ack.exists() else []
    return "setup" in lines, sum(1 for line in lines if line.isdigit())


def verify(path: Path, scenario: str, in_flight: bool | None = None) -> mvrs_db.Database:
    """Open the database (running recovery) and check it against the acknowledgements.

    `in_flight` says what must have happened to the one transaction that was
    running when the process died: True = it must be present, False = it must
    be absent, None = either is acceptable (but never half of it).
    """
    setup_done, acked = acknowledged(path)
    db = mvrs_db.connect(path, create=False)
    try:
        db.check()
        visible = scenarios.visible_transactions(db)
        if not setup_done:
            # Died before the workload started; any consistent database is correct.
            assert visible in (None, 0)
            return db
        assert visible is not None, "setup was acknowledged but its tables are missing"
        assert visible >= acked, f"{acked} commits were acknowledged but only {visible} survived"
        assert visible <= acked + 1, "a transaction that never started is visible"
        if in_flight is True:
            assert visible == acked + 1, "a commit that reached the log was lost"
        elif in_flight is False:
            assert visible == acked, "a transaction that never reached the log is visible"
        scenarios.SCENARIOS[scenario][2](db, visible)
        return db
    except BaseException:
        db.close()
        raise


# (crash point, what must be true of the transaction in flight)
CRASH_POINTS = [
    # --- before the log is written: the transaction must vanish
    ("commit.before_log:4", False),
    ("commit.before_log:17", False),
    # --- records are in the user-space buffer but never written
    ("commit.before_flush:9", False),
    ("wal.before_flush:6", False),
    ("wal.before_flush:21", False),
    # --- half of the commit's records reach the file: a truncated log tail
    ("wal.partial_flush:5", False),
    ("wal.partial_flush:19", False),
    # --- the commit record is in the file (written, not yet fsynced; a killed
    #     process cannot lose bytes the OS already has)
    ("wal.after_write:8", True),
    # --- the commit record is durable; commit has not returned yet
    ("wal.after_sync:7", True),
    ("commit.after_durable:12", True),
    # --- checkpoints, which run after a commit and before it is acknowledged
    ("checkpoint.begin:2", True),
    ("checkpoint.after_flush:2", True),
    ("checkpoint.after_sync:3", True),
    ("wal.reset_before_truncate:3", True),
    # --- half a page written, at different moments in the run. Depending on
    #     when it fires this is an eviction mid-transaction or a checkpoint.
    ("disk.torn_page_write:1", None),
    ("disk.torn_page_write:9", None),
    ("disk.torn_page_write:40", None),
    ("disk.torn_page_write:50", None),
    ("disk.torn_page_write:120", None),
]


@pytest.mark.parametrize("scenario", ["rows", "ddl"])
@pytest.mark.parametrize(("crash_at", "in_flight"), CRASH_POINTS)
def test_crash_then_recover(
    tmp_path: Path, scenario: str, crash_at: str, in_flight: bool | None
) -> None:
    path = tmp_path / "crash.mvrs"

    code = run_worker(path, scenario, crash_at=crash_at)

    assert code == faults.CRASH_EXIT_CODE, f"the worker never reached {crash_at}"
    verify(path, scenario, in_flight).close()
    # Recovery left a database that keeps working and stays consistent.
    with mvrs_db.connect(path) as db:
        db.execute("CREATE TABLE after_crash (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO after_crash VALUES (1)")
        db.check()


@pytest.mark.parametrize("scenario", ["rows", "ddl"])
def test_crash_while_creating_the_database(tmp_path: Path, scenario: str) -> None:
    """The very first log write of a new database is the catalog itself."""
    path = tmp_path / "crash.mvrs"

    for crash_at in ("commit.before_log:1", "wal.partial_flush:1", "wal.after_sync:1"):
        for suffix in ("", ".wal", ".ack", ".lock"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        assert run_worker(path, scenario, crash_at=crash_at) == faults.CRASH_EXIT_CODE
        verify(path, scenario).close()
        assert run_worker(path, scenario) == 0  # and the workload then runs normally
        verify(path, scenario).close()


@pytest.mark.parametrize("scenario", ["rows", "ddl"])
def test_killed_while_idle_with_nothing_checkpointed(tmp_path: Path, scenario: str) -> None:
    """After commit but before any dirty page is flushed: everything comes from the log."""
    path = tmp_path / "crash.mvrs"

    run_worker(path, scenario, buffer_pages=2000, checkpoint_bytes=HUGE_LOG, die_at_end=True)

    data_file_pages = path.stat().st_size // 4096
    assert data_file_pages <= 2, "the data file should hold only the header (and catalog) so far"
    db = verify(path, scenario, in_flight=False)
    assert acknowledged(path) == (True, TRANSACTIONS)
    assert db.recovery.committed_transactions > TRANSACTIONS
    assert db.recovery.pages_restored > 10
    # Not only whole-page images: pages were rebuilt by replaying deltas after
    # their first image, which is the path delta logging added to recovery.
    assert db.recovery.deltas_applied > 10
    db.close()


RECOVERY_CRASH_POINTS = [
    "recovery.apply:1",
    "recovery.apply:7",
    "recovery.before_sync:1",
    "recovery.before_log_reset:1",
    "wal.reset_before_truncate:1",
    "disk.torn_page_write:3",
]


@pytest.mark.parametrize("scenario", ["rows", "ddl"])
@pytest.mark.parametrize("crash_at", RECOVERY_CRASH_POINTS)
def test_crash_during_recovery_then_recover_again(
    tmp_path: Path, scenario: str, crash_at: str
) -> None:
    """Recovery must be repeatable: crash in the middle of it, then run it again."""
    path = tmp_path / "crash.mvrs"
    run_worker(path, scenario, buffer_pages=2000, checkpoint_bytes=HUGE_LOG, die_at_end=True)

    # This child only opens the database, so the crash lands inside recovery.
    code = run_worker(path, scenario, crash_at=crash_at, transactions=0)
    assert code == faults.CRASH_EXIT_CODE, f"recovery never reached {crash_at}"
    # ... and once more, so two interrupted recoveries precede the real one.
    run_worker(path, scenario, crash_at=crash_at, transactions=0)

    db = verify(path, scenario, in_flight=False)
    assert acknowledged(path)[1] == TRANSACTIONS
    db.close()


def test_recovering_twice_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "crash.mvrs"
    run_worker(path, "rows", buffer_pages=2000, checkpoint_bytes=HUGE_LOG, die_at_end=True)

    verify(path, "rows").close()
    first = path.read_bytes()
    db = verify(path, "rows")
    assert db.recovery.pages_restored == 0
    db.close()

    assert path.read_bytes() == first


@pytest.mark.parametrize("scenario", ["rows", "ddl"])
def test_truncated_log_tail(tmp_path: Path, scenario: str) -> None:
    """Cut the log at many points. Recovery must keep a whole number of transactions."""
    path = tmp_path / "crash.mvrs"
    wal_path = Path(str(path) + ".wal")
    run_worker(path, scenario, buffer_pages=2000, checkpoint_bytes=HUGE_LOG, die_at_end=True)
    data, log = path.read_bytes(), wal_path.read_bytes()
    assert len(log) > 100_000

    seen = set()
    for cut in [*range(WAL_HEADER_SIZE, len(log), len(log) // 24), len(log) - 1]:
        path.write_bytes(data)
        wal_path.write_bytes(log[:cut])
        with mvrs_db.connect(path, create=False) as db:
            db.check()
            visible = scenarios.visible_transactions(db)
            # visible 0 or None means the cut fell inside the workload's setup,
            # which is several separate commits (create a table, insert a row,
            # create the next table). Any prefix of those is a valid state and
            # check() above has already verified it. From visible 1 on, setup
            # is complete and the whole scenario must match its model.
            if visible:
                scenarios.SCENARIOS[scenario][2](db, visible)
            seen.add(visible)

    assert len(seen) > 10, "the cuts should land in many different transactions"
    assert TRANSACTIONS not in seen  # every cut removed at least the last commit record


@pytest.mark.parametrize("scenario", ["rows", "ddl"])
def test_corrupted_log_tail(tmp_path: Path, scenario: str) -> None:
    """Damage a byte in each of the last few records. Earlier transactions must survive."""
    path = tmp_path / "crash.mvrs"
    wal_path = Path(str(path) + ".wal")
    run_worker(path, scenario, buffer_pages=2000, checkpoint_bytes=HUGE_LOG, die_at_end=True)
    data, log = path.read_bytes(), wal_path.read_bytes()
    contents = read_wal(wal_path)
    assert contents is not None

    for record in contents.records[-12:]:
        offset = WAL_HEADER_SIZE + record.lsn - contents.base_lsn
        damaged = bytearray(log)
        damaged[offset + 30 if record.payload else offset + 5] ^= 0xFF
        path.write_bytes(data)
        wal_path.write_bytes(damaged)
        with mvrs_db.connect(path, create=False) as db:
            db.check()
            visible = scenarios.visible_transactions(db)
            assert visible is not None and visible >= TRANSACTIONS - 3
            assert db.recovery.discarded_tail_bytes > 0
            scenarios.SCENARIOS[scenario][2](db, visible)


def test_clean_run_needs_no_recovery(tmp_path: Path) -> None:
    path = tmp_path / "crash.mvrs"

    assert run_worker(path, "rows") == 0

    assert Path(str(path) + ".ack").read_text().split()[-1] == "closed"
    db = verify(path, "rows", in_flight=False)
    assert db.recovery.records_scanned == 0
    db.close()
