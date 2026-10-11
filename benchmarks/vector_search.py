"""Time exact nearest-neighbour search, with NumPy and with the pure-Python fallback.

    python benchmarks/vector_search.py

Prints a table. Nothing is written to the benchmark report; this is the
measurement behind the numbers quoted in docs/11-vector-search.md.
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
from mvrs_db.vector import distance

ROWS = 20_000
QUERIES = 5


def build(path: Path, dimension: int, rng: random.Random) -> mvrs_db.Database:
    db = mvrs_db.connect(path, synchronous=False, buffer_pages=16384, page_size=8192)
    db.execute(f"CREATE TABLE e (id INTEGER PRIMARY KEY, v VECTOR({dimension}))")
    for start in range(0, ROWS, 100):
        values = ", ".join(
            f"({i}, [{', '.join(f'{rng.uniform(-1, 1):.6f}' for _ in range(dimension))}])"
            for i in range(start, start + 100)
        )
        db.execute(f"INSERT INTO e VALUES {values}")
    return db


def time_queries(db: mvrs_db.Database, statements: list[str]) -> float:
    times = []
    for statement in statements:
        started = time.perf_counter()
        db.execute(statement)
        times.append(time.perf_counter() - started)
    return statistics.median(times)


def main() -> None:
    folder = scratch_dir("vector-search")
    rng = random.Random(11)
    print(f"{ROWS:,} rows, top 10 by Euclidean distance, median of {QUERIES} queries\n")
    header = ("dims", "scan only ms", "NumPy ms", "pure Python ms", "speedup")
    print("{:>5} {:>13} {:>10} {:>15} {:>8}".format(*header))
    for dimension in (16, 128, 768):
        db = build(folder / f"d{dimension}.mvrs", dimension, rng)
        queries = [
            "[" + ", ".join(f"{rng.uniform(-1, 1):.6f}" for _ in range(dimension)) + "]"
            for _ in range(QUERIES)
        ]
        search = [f"SELECT id FROM e ORDER BY v <-> {q} LIMIT 10" for q in queries]
        scan_only = time_queries(db, ["SELECT id FROM e"] * QUERIES)
        with_numpy = time_queries(db, search)
        saved = distance._np
        distance._np = None
        try:
            pure = time_queries(db, search[:2])
        finally:
            distance._np = saved
        table = db._catalog.get("e")
        rows, pages = table.ensure_stats()
        db.close()
        print(
            f"{dimension:>5} {scan_only * 1e3:>13.1f} {with_numpy * 1e3:>10.1f} "
            f"{pure * 1e3:>15.1f} {pure / with_numpy:>7.1f}x   ({pages:,} pages)"
        )


if __name__ == "__main__":
    main()
