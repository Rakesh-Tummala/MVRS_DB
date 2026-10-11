"""Durable commit throughput as the number of committing threads grows.

    python benchmarks/group_commit.py

Every thread runs single-row INSERT transactions against one table with fsync on
(synchronous=True), for a fixed time. With one thread each commit pays for its
own fsync. With several, commits that arrive while an fsync is running queue up
and share the next one.

Reported per thread count: commits per second, fsyncs per commit, and the
median and 99th-percentile latency of one commit.

Results are printed and saved to benchmarks/results/group_commit.json.
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mvrs_db
from common import RESULTS_DIR, environment, percentile, scratch_dir

SECONDS = 4.0
ROW = "p" * 60


def run(threads: int) -> dict[str, Any]:
    folder = scratch_dir("group-commit")
    db = mvrs_db.connect(folder / "db.mvrs", synchronous=True, checkpoint_bytes=1 << 40)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who INTEGER, body TEXT)")
    latencies: list[list[float]] = [[] for _ in range(threads)]
    stop = threading.Event()
    start = threading.Barrier(threads + 1)

    def work(index: int) -> None:
        connection = db.connect()
        sequence = 0
        start.wait()
        while not stop.is_set():
            key = index * 10_000_000 + sequence
            started = time.perf_counter()
            connection.execute(f"INSERT INTO t VALUES ({key}, {index}, '{ROW}')")
            latencies[index].append(time.perf_counter() - started)
            sequence += 1

    workers = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    syncs_before = db._wal.sync_count
    start.wait()
    began = time.perf_counter()
    time.sleep(SECONDS)
    stop.set()
    for worker in workers:
        worker.join()
    elapsed = time.perf_counter() - began
    every = [x for per_thread in latencies for x in per_thread]
    syncs = db._wal.sync_count - syncs_before
    db.check()
    db.close()
    return {
        "threads": threads,
        "commits": len(every),
        "commits_per_second": len(every) / elapsed,
        "fsyncs": syncs,
        "commits_per_fsync": len(every) / max(syncs, 1),
        "median_ms": statistics.median(every) * 1e3,
        "p99_ms": percentile(every, 0.99) * 1e3,
    }


def main() -> None:
    print(f"durable single-row inserts into one table, {SECONDS:.0f} seconds each\n")
    print(f"{'threads':>8} {'commits/s':>10} {'commits/fsync':>14} {'median ms':>10} {'p99 ms':>8}")
    results = []
    for threads in (1, 2, 4, 8, 16):
        result = run(threads)
        results.append(result)
        print(
            f"{threads:>8} {result['commits_per_second']:>10.0f} "
            f"{result['commits_per_fsync']:>14.1f} {result['median_ms']:>10.1f} "
            f"{result['p99_ms']:>8.1f}"
        )
    base = results[0]["commits_per_second"]
    print(
        "\nthroughput versus one thread: "
        + ", ".join(
            f"{r['threads']} threads x{r['commits_per_second'] / base:.1f}" for r in results[1:]
        )
    )
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / "group_commit.json"
    path.write_text(
        json.dumps({"environment": environment(), "results": results}, indent=2), encoding="utf-8"
    )
    print(f"saved to {path}")


if __name__ == "__main__":
    main()
