"""Log volume and speed with delta records on and off.

    python benchmarks/log_volume.py

Every workload runs twice on a fresh database, once with log_deltas=False (a
whole page logged for every changed page) and once with log_deltas=True. Both
use synchronous=False so the timing shows the cost of building and writing log
records and not of the drive's fsync. Checkpoints are disabled so that every
commit's records stay in the log, and the recovery rows measure replaying all
of them.

Results are printed and saved to benchmarks/results/log_volume.json.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mvrs_db
from common import RESULTS_DIR, environment, scratch_dir

ROW = "p" * 60


def workload_single_inserts(db: mvrs_db.Database) -> int:
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER, body TEXT)")
    for i in range(3000):
        db.execute(f"INSERT INTO t VALUES ({i}, {i % 50}, '{ROW}')")
    return 3000


def workload_bulk_inserts(db: mvrs_db.Database) -> int:
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER, body TEXT)")
    commits = 0
    for start in range(0, 30_000, 500):
        values = ", ".join(f"({i}, {i % 50}, '{ROW}')" for i in range(start, start + 500))
        db.execute(f"INSERT INTO t VALUES {values}")
        commits += 1
    return commits


def workload_updates(db: mvrs_db.Database) -> int:
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER, body TEXT)")
    for start in range(0, 3000, 500):
        values = ", ".join(f"({i}, {i % 50}, '{ROW}')" for i in range(start, start + 500))
        db.execute(f"INSERT INTO t VALUES {values}")
    started = db.commits
    for i in range(0, 3000, 2):  # one small in-place change per commit
        db.execute(f"UPDATE t SET grp = {i % 7} WHERE id = {i}")
    return db.commits - started


def workload_deletes(db: mvrs_db.Database) -> int:
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER, body TEXT)")
    for start in range(0, 3000, 500):
        values = ", ".join(f"({i}, {i % 50}, '{ROW}')" for i in range(start, start + 500))
        db.execute(f"INSERT INTO t VALUES {values}")
    started = db.commits
    for i in range(0, 3000, 2):
        db.execute(f"DELETE FROM t WHERE id = {i}")
    return db.commits - started


WORKLOADS = {
    "3,000 single-row inserts": workload_single_inserts,
    "60 inserts of 500 rows": workload_bulk_inserts,
    "1,500 single-row updates": workload_updates,
    "1,500 single-row deletes": workload_deletes,
}


def run(name: str, workload: Any, deltas: bool, page_size: int) -> dict[str, Any]:
    folder = scratch_dir("log-volume")
    path = folder / "db.mvrs"
    db = mvrs_db.connect(
        path,
        synchronous=False,
        checkpoint_bytes=1 << 40,
        buffer_pages=16384,
        log_deltas=deltas,
        page_size=page_size,
    )
    started = time.perf_counter()
    commits = workload(db)
    seconds = time.perf_counter() - started
    written = db.log_bytes_written
    # Copy the files while the database is open: that is the state a crash leaves.
    crashed = folder / "crashed"
    crashed.mkdir()
    for suffix in ("", ".wal"):
        (crashed / f"db.mvrs{suffix}").write_bytes(Path(str(path) + suffix).read_bytes())
    db.close()

    started = time.perf_counter()
    recovered = mvrs_db.connect(crashed / "db.mvrs", page_size=page_size)
    recovery = time.perf_counter() - started
    stats = recovered.recovery
    recovered.check()
    recovered.close()
    return {
        "workload": name,
        "deltas": deltas,
        "page_size": page_size,
        "commits": commits,
        "log_bytes": written,
        "bytes_per_commit": written / commits,
        "seconds": seconds,
        "commits_per_second": commits / seconds,
        "recovery_ms": recovery * 1e3,
        "recovery_deltas_applied": stats.deltas_applied,
    }


def run_durable(deltas: bool) -> dict[str, Any]:
    """Single-row inserts with fsync at every commit, timed one commit at a time."""
    folder = scratch_dir("log-volume-durable")
    db = mvrs_db.connect(
        folder / "db.mvrs", synchronous=True, checkpoint_bytes=1 << 40, log_deltas=deltas
    )
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER, body TEXT)")
    baseline = db.log_bytes_written
    times = []
    for i in range(150):
        started = time.perf_counter()
        db.execute(f"INSERT INTO t VALUES ({i}, {i % 50}, '{ROW}')")
        times.append(time.perf_counter() - started)
    written = db.log_bytes_written - baseline
    db.close()
    times.sort()
    return {
        "deltas": deltas,
        "commits": len(times),
        "median_ms": times[len(times) // 2] * 1e3,
        "commits_per_second": len(times) / sum(times),
        "bytes_per_commit": written / len(times),
    }


def main() -> None:
    results = []
    print(
        f"{'workload':28} {'':8} {'log MB':>8} {'B/commit':>9} {'commits/s':>10} {'recover ms':>11}"
    )
    for name, workload in WORKLOADS.items():
        pair = []
        for deltas in (False, True):
            result = run(name, workload, deltas, 4096)
            results.append(result)
            pair.append(result)
            print(
                f"{name:28} {'deltas' if deltas else 'images':8} "
                f"{result['log_bytes'] / 1e6:>8.2f} {result['bytes_per_commit']:>9.0f} "
                f"{result['commits_per_second']:>10.0f} {result['recovery_ms']:>11.0f}"
            )
        off, on = pair
        print(
            f"{'':28} {'ratio':8} {off['log_bytes'] / on['log_bytes']:>7.1f}x "
            f"{'':>9} {on['commits_per_second'] / off['commits_per_second']:>9.2f}x "
            f"{off['recovery_ms'] / on['recovery_ms']:>10.2f}x\n"
        )

    print("Page size, 3,000 single-row inserts:")
    print(f"{'page size':>10} {'':8} {'log MB':>8} {'B/commit':>9} {'commits/s':>10}")
    for page_size in (4096, 16384, 32768):
        for deltas in (False, True):
            result = run("3,000 single-row inserts", workload_single_inserts, deltas, page_size)
            results.append(result)
            print(
                f"{page_size:>10} {'deltas' if deltas else 'images':8} "
                f"{result['log_bytes'] / 1e6:>8.2f} {result['bytes_per_commit']:>9.0f} "
                f"{result['commits_per_second']:>10.0f}"
            )

    print("\nDurable commits (fsync on), 150 single-row inserts:")
    durable = []
    for deltas in (False, True):
        entry = run_durable(deltas)
        durable.append(entry)
        print(
            f"  {'deltas' if deltas else 'images':8} median {entry['median_ms']:.2f} ms/commit, "
            f"{entry['commits_per_second']:.0f} commits/s, {entry['bytes_per_commit']:.0f} B/commit"
        )

    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / "log_volume.json"
    path.write_text(
        json.dumps(
            {"environment": environment(), "results": results, "durable": durable}, indent=2
        ),
        encoding="utf-8",
    )
    print(f"\nsaved to {path}")


if __name__ == "__main__":
    main()
