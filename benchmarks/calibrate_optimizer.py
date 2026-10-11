"""Measure the three costs the optimiser's model is built from.

    python benchmarks/calibrate_optimizer.py

The cost model in mvrs_db/planner/optimizer.py says:

    sequential scan = pages * SEQ_PAGE + rows * ROW_CPU
    index scan      = matching_rows * INDEX_ROW   (plus a few pages for the descent)

This script times full scans of a narrow table (many rows per page) and a wide
table (few rows per page). Two tables with different rows-per-page ratios give
two equations, which separate the per-page cost from the per-row cost. It then
times index scans to get the cost per row fetched through the index.

The printed ratios, with SEQ_PAGE fixed at 1.0, are the constants to put in
optimizer.py. Absolute times depend on the machine; the ratios are what the
planner uses.
"""

from __future__ import annotations

import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mvrs_db
from common import scratch_dir
from mvrs_db.execution.operators import IndexScan, KeyRange, SeqScan, Sort

ROWS = 30_000
REPEATS = 5


def build(path: Path, payload_bytes: int) -> mvrs_db.Database:
    db = mvrs_db.connect(path, synchronous=False, buffer_pages=8192)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, payload TEXT)")
    payload = "x" * payload_bytes
    for start in range(0, ROWS, 500):
        values = ", ".join(f"({i}, '{payload}')" for i in range(start, start + 500))
        db.execute(f"INSERT INTO t VALUES {values}")
    return db


def best_time(run: object) -> float:
    times = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        run()  # type: ignore[operator]
        times.append(time.perf_counter() - started)
    return statistics.median(times)


def main() -> None:
    folder = scratch_dir("calibrate")
    measurements = []
    sort_seconds = 0.0
    for name, payload_bytes in (("narrow", 8), ("wide", 600)):
        db = build(folder / f"{name}.mvrs", payload_bytes)
        table = db._catalog.get("t")
        rows, pages = table.ensure_stats()

        seq = best_time(lambda table=table: sum(1 for _ in SeqScan(table).rows()))
        index_all = best_time(
            lambda table=table: sum(1 for _ in IndexScan(table, 0, KeyRange()).rows())
        )
        measurements.append((name, rows, pages, seq, index_all))
        print(
            f"{name:6}  rows={rows}  pages={pages}  rows/page={rows / pages:6.1f}  "
            f"seq scan={seq * 1e3:7.1f} ms  full index scan={index_all * 1e3:7.1f} ms"
        )
        if name == "narrow":
            # Sort by the payload-independent id descending, so the input is in
            # the worst order for the sort rather than already sorted.
            names = table.schema.column_names
            sorted_scan = best_time(
                lambda table=table: sum(1 for _ in Sort(SeqScan(table), [(0, True)], names).rows())
            )
            sort_seconds = max(sorted_scan - seq, 0.0)
        db.close()

    (_, r1, p1, s1, i1), (_, r2, p2, s2, i2) = measurements
    # Solve  s = page_cost * pages + row_cost * rows  for the two tables.
    determinant = p1 * r2 - p2 * r1
    page_cost = (s1 * r2 - s2 * r1) / determinant
    row_cost = (p1 * s2 - p2 * s1) / determinant
    index_row_cost = statistics.mean([i1 / r1, i2 / r2])

    print()
    print(f"per page (sequential)    {page_cost * 1e6:8.2f} microseconds")
    print(f"per row  (sequential)    {row_cost * 1e6:8.2f} microseconds")
    print(f"per row  (via index)     {index_row_cost * 1e6:8.2f} microseconds")
    print()
    print("Constants for optimizer.py, in units of one sequential page:")
    print("    SEQ_PAGE  = 1.0")
    print(f"    ROW_CPU   = {row_cost / page_cost:.3f}")
    print(f"    INDEX_ROW = {index_row_cost / page_cost:.3f}")
    sort_row = sort_seconds / ROWS / math.log2(ROWS + 2) / page_cost
    print(
        f"    SORT_ROW  = {sort_row:.4f}   (sorting {ROWS} rows took {sort_seconds * 1e3:.1f} ms)"
    )


if __name__ == "__main__":
    main()
