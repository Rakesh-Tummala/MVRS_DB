"""Hash join against index join, as the outer input grows.

    python benchmarks/join_methods.py

Joins `emp` (50,000 rows) to `dept` (20,000 rows, primary key `id`) on
emp.dept_id = dept.id, restricting emp to its first N rows. Both join methods
are built by hand and timed, and compared with what the planner would choose.

It also prints the two per-row costs the planner's model needs, in units of one
sequential page (the unit used throughout mvrs_db/planner/optimizer.py).
"""

from __future__ import annotations

import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mvrs_db
from common import scratch_dir
from mvrs_db.execution.operators import HashJoin, IndexJoin, IndexScan, KeyRange, SeqScan

EMP_ROWS = 50_000
DEPT_ROWS = 20_000
PAGE_MICROSECONDS = 5.4  # one sequential page, from benchmarks/calibrate_optimizer.py


def median_ms(run: object, repeats: int = 5) -> float:
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        run()  # type: ignore[operator]
        times.append(time.perf_counter() - started)
    return statistics.median(times) * 1e3


def main() -> None:
    folder = scratch_dir("join-methods")
    rng = random.Random(5)
    db = mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384)
    db.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, dept_id INTEGER, pay INTEGER)")
    for start in range(0, DEPT_ROWS, 500):
        values = ", ".join(f"({i}, 'dept-{i}')" for i in range(start, start + 500))
        db.execute(f"INSERT INTO dept VALUES {values}")
    for start in range(0, EMP_ROWS, 500):
        values = ", ".join(
            f"({i}, {rng.randrange(DEPT_ROWS)}, {rng.randrange(100)})"
            for i in range(start, start + 500)
        )
        db.execute(f"INSERT INTO emp VALUES {values}")
    emp, dept = db._catalog.get("emp"), db._catalog.get("dept")

    dept_scan = median_ms(lambda: sum(1 for _ in SeqScan(dept).rows()))
    print(f"scanning dept alone: {dept_scan:.1f} ms\n")
    print(f"{'outer rows':>10} {'hash ms':>9} {'index ms':>9} {'faster':>7} {'planner':>8}")

    samples = []
    for outer_rows in (10, 100, 1000, 3000, 10_000, 25_000, 50_000):

        def outer() -> object:
            if outer_rows == EMP_ROWS:
                return SeqScan(emp)
            return IndexScan(emp, 0, KeyRange(high=outer_rows, high_inclusive=False))

        outer_alone = median_ms(lambda: sum(1 for _ in outer().rows()))  # type: ignore[attr-defined]
        hash_ms = median_ms(
            lambda: sum(
                1
                for _ in HashJoin(
                    outer() if outer_rows <= DEPT_ROWS else SeqScan(dept),  # type: ignore[arg-type]
                    SeqScan(dept) if outer_rows <= DEPT_ROWS else outer(),  # type: ignore[arg-type]
                    1 if outer_rows <= DEPT_ROWS else 0,
                    0 if outer_rows <= DEPT_ROWS else 1,
                    build_is_left=outer_rows <= DEPT_ROWS,
                    condition="",
                    build_name="",
                ).rows()
            )
        )
        index_ms = median_ms(
            lambda: sum(
                1
                for _ in IndexJoin(
                    outer(),  # type: ignore[arg-type]
                    dept,
                    1,
                    0,
                    None,
                    outer_is_left=True,
                    condition="",
                    inner_filter_text="",
                ).rows()
            )
        )
        where = "" if outer_rows == EMP_ROWS else f"WHERE emp.id < {outer_rows}"
        plan = "\n".join(
            str(r[0])
            for r in db.execute(
                f"EXPLAIN SELECT emp.id FROM emp JOIN dept ON emp.dept_id = dept.id {where}"
            ).rows
        )
        chosen = "index" if "IndexJoin" in plan else "hash"
        faster = "index" if index_ms < hash_ms else "hash"
        mark = "" if chosen == faster else "  <- wrong"
        print(f"{outer_rows:>10} {hash_ms:>9.2f} {index_ms:>9.2f} {faster:>7} {chosen:>8}{mark}")
        samples.append((outer_rows, outer_alone, hash_ms, index_ms))

    # Per-row costs, taken from the largest sample where both sides are substantial.
    outer_rows, outer_alone, hash_ms, index_ms = samples[-2]
    hash_row = (hash_ms - outer_alone - dept_scan) * 1e3 / (outer_rows + DEPT_ROWS)
    lookup = (index_ms - outer_alone) * 1e3 / outer_rows
    height = dept.indexes[0].height()
    print(f"\nhash join:  {hash_row:.2f} microseconds per row built or probed")
    print(f"index join: {lookup:.2f} microseconds per outer row (index height {height})")
    print("\nIn units of one sequential page:")
    print(f"    HASH_ROW                              = {hash_row / PAGE_MICROSECONDS:.3f}")
    print(f"    INDEX_LOOKUP + {height} * INDEX_LEVEL          = {lookup / PAGE_MICROSECONDS:.3f}")
    db.close()


if __name__ == "__main__":
    main()
