"""Recall against latency for the HNSW index, measured through SQL.

    python benchmarks/vector_recall.py

For each dataset the script loads a table, runs every query with the exact
search to get the ground truth, then runs the same queries through the index at
several ef_search settings. It reports

    recall@10   the share of the true 10 nearest rows that the index returned
    latency     median time per query, including SQL parsing

Results are printed and saved to benchmarks/results/vector_recall.json.
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mvrs_db
from common import RESULTS_DIR, environment, scratch_dir

ROWS = 10_000
DIMENSIONS = 64
QUERIES = 100
K = 10
EF_VALUES = (10, 20, 50, 100, 200, 400)


def literal(vector: list[float]) -> str:
    return "[" + ", ".join(f"{x:.6f}" for x in vector) + "]"


def clustered(rng: random.Random, count: int) -> list[list[float]]:
    """Points gathered around 100 centres, like embeddings of items in topics."""
    centres = [[rng.gauss(0, 1) for _ in range(DIMENSIONS)] for _ in range(100)]
    return [[c + rng.gauss(0, 0.35) for c in rng.choice(centres)] for _ in range(count)]


def uniform(rng: random.Random, count: int) -> list[list[float]]:
    """Points with no structure at all: the hardest case for any such index."""
    return [[rng.uniform(-1, 1) for _ in range(DIMENSIONS)] for _ in range(count)]


def run(db: mvrs_db.Database, statements: list[str]) -> tuple[list[list[int]], float]:
    results, times = [], []
    for statement in statements:
        started = time.perf_counter()
        rows = db.execute(statement).rows
        times.append(time.perf_counter() - started)
        results.append([row[0] for row in rows])
    return results, statistics.median(times)


def evaluate(name: str, make: Any, operator: str, option: str) -> dict[str, Any]:
    rng = random.Random(21)
    folder = scratch_dir(f"recall-{name}")
    db = mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384)
    db.execute(f"CREATE TABLE e (id INTEGER PRIMARY KEY, v VECTOR({DIMENSIONS}) {option})")
    data = make(rng, ROWS)
    for start in range(0, ROWS, 100):
        values = ", ".join(f"({i}, {literal(data[i])})" for i in range(start, start + 100))
        db.execute(f"INSERT INTO e VALUES {values}")
    queries = make(rng, QUERIES)
    statements = [f"SELECT id FROM e ORDER BY v {operator} {literal(q)} LIMIT {K}" for q in queries]

    db.vector.use_index = False
    truth, exact_seconds = run(db, statements)

    db.vector.use_index = True
    started = time.perf_counter()
    db.execute(statements[0])  # the first approximate query builds the index
    build_seconds = time.perf_counter() - started

    points = []
    for ef in EF_VALUES:
        db.vector.ef_search = ef
        found, seconds = run(db, statements)
        hits = sum(len(set(a) & set(b)) for a, b in zip(found, truth, strict=True))
        points.append(
            {
                "ef_search": ef,
                "recall": hits / (K * QUERIES),
                "median_ms": seconds * 1e3,
                "speedup": exact_seconds / seconds,
            }
        )
    db.close()
    return {
        "dataset": name,
        "operator": operator,
        "rows": ROWS,
        "dimensions": DIMENSIONS,
        "queries": QUERIES,
        "k": K,
        "exact_median_ms": exact_seconds * 1e3,
        "index_build_seconds": build_seconds,
        "points": points,
    }


def main() -> None:
    cases = [
        ("clustered, Euclidean", clustered, "<->", "HNSW"),
        ("clustered, cosine", clustered, "<=>", "HNSW COSINE"),
        ("uniform random, Euclidean", uniform, "<->", "HNSW"),
    ]
    results = []
    for name, make, operator, option in cases:
        print(f"\n{name}: {ROWS:,} rows x {DIMENSIONS} dimensions, {QUERIES} queries, top {K}")
        result = evaluate(name.replace(", ", "-").replace(" ", "-"), make, operator, option)
        result["dataset"] = name
        results.append(result)
        print(f"  exact search      {result['exact_median_ms']:7.2f} ms per query (recall 1.000)")
        print(f"  building the index took {result['index_build_seconds']:.1f} s")
        for point in result["points"]:
            print(
                f"  ef_search={point['ef_search']:<4} recall@{K}={point['recall']:.3f}  "
                f"{point['median_ms']:6.2f} ms  ({point['speedup']:.0f}x faster than exact)"
            )
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / "vector_recall.json"
    path.write_text(
        json.dumps({"environment": environment(), "results": results}, indent=2), encoding="utf-8"
    )
    print(f"\nsaved to {path}")


if __name__ == "__main__":
    main()
