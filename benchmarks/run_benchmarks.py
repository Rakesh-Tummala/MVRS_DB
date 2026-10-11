"""MVRS_DB benchmark suite.

    python benchmarks/run_benchmarks.py                 run everything, write the report
    python benchmarks/run_benchmarks.py --only lookup   run one section
    python benchmarks/run_benchmarks.py --quick         smaller datasets (a smoke run)

Results go to benchmarks/results/results.json and benchmarks/results/REPORT.md.
Every number in the report comes from the JSON written by the same run.

All database files are created under .tmp/bench inside the project.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
import time
import tracemalloc
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mvrs_db
from common import RESULTS_DIR, environment, scratch_dir, summarize
from mvrs_db.execution import expressions
from mvrs_db.execution.operators import Filter, IndexScan, KeyRange, Operator, SeqScan
from mvrs_db.sql.parser import parse

CREATE = "CREATE TABLE items (id INTEGER PRIMARY KEY, grp INTEGER, payload TEXT)"
CREATE_TWO_INDEXES = (
    "CREATE TABLE items (id INTEGER PRIMARY KEY, grp INTEGER, payload TEXT, code TEXT UNIQUE)"
)
PAYLOAD = "p" * 60
BATCH = 500


def insert_statement(start: int, stop: int, *, with_code: bool = False) -> str:
    if with_code:
        rows = (f"({i}, {i}, '{PAYLOAD}', 'code-{i:09d}')" for i in range(start, stop))
    else:
        rows = (f"({i}, {i}, '{PAYLOAD}')" for i in range(start, stop))
    return "INSERT INTO items VALUES " + ", ".join(rows)


def load(db: mvrs_db.Database, rows: int, *, with_code: bool = False) -> float:
    """Create and fill the table. Returns seconds for the inserts only."""
    db.execute(CREATE_TWO_INDEXES if with_code else CREATE)
    started = time.perf_counter()
    for start in range(0, rows, BATCH):
        db.execute(insert_statement(start, min(start + BATCH, rows), with_code=with_code))
    return time.perf_counter() - started


def timed(run: Callable[[], Any]) -> float:
    started = time.perf_counter()
    run()
    return time.perf_counter() - started


def per_call(db: mvrs_db.Database, statements: list[str]) -> list[float]:
    times = []
    execute = db.execute
    for statement in statements:
        started = time.perf_counter()
        execute(statement)
        times.append(time.perf_counter() - started)
    return times


def median_of(run: Callable[[], Any], repeats: int = 5) -> float:
    return sorted(timed(run) for _ in range(repeats))[repeats // 2]


# ---------------------------------------------------------------------- sections


def bench_load(sizes: list[int]) -> list[dict[str, Any]]:
    """Insert throughput, 500 rows per statement, one transaction per statement, no fsync."""
    out = []
    for rows in sizes:
        folder = scratch_dir(f"load-{rows}")
        with mvrs_db.connect(folder / "db.mvrs", synchronous=False) as db:
            seconds = load(db, rows)
            pages = db.page_count
        out.append(
            {
                "rows": rows,
                "seconds": seconds,
                "rows_per_second": rows / seconds,
                "file_bytes": (folder / "db.mvrs").stat().st_size,
                "pages": pages,
            }
        )
    return out


def bench_lookup(sizes: list[int]) -> list[dict[str, Any]]:
    """Point lookup by primary key (index) versus by an unindexed column (full scan)."""
    out = []
    rng = random.Random(1)
    for rows in sizes:
        folder = scratch_dir(f"lookup-{rows}")
        db = mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384)
        load(db, rows)
        keys = [rng.randrange(rows) for _ in range(2000)]
        by_index = [f"SELECT * FROM items WHERE id = {k}" for k in keys]
        by_scan = [f"SELECT * FROM items WHERE grp = {k}" for k in keys[:15]]
        per_call(db, by_index[:200])  # warm up
        warm = summarize(per_call(db, by_index))
        scan = summarize(per_call(db, by_scan))
        db.close()

        db = mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384)
        cold = summarize(per_call(db, by_index))  # buffer pool empty; OS cache still warm
        parse_only = summarize([timed(lambda s=s: parse(s)) for s in by_index[:500]])
        db.close()
        out.append(
            {
                "rows": rows,
                "index_warm": warm,
                "index_after_reopen": cold,
                "full_scan": scan,
                "parse_only": parse_only,
                "speedup_median": scan["median_us"] / warm["median_us"],
            }
        )
    return out


def bench_range(rows: int) -> dict[str, Any]:
    """Rows per second for a full scan and for index range scans of different widths."""
    folder = scratch_dir("range")
    out: dict[str, Any] = {"rows": rows, "ranges": []}
    with mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384) as db:
        load(db, rows)
        seconds = median_of(lambda: db.execute("SELECT * FROM items"))
        out["full_scan_rows_per_second"] = rows / seconds
        for width in (10, 100, 1000, 10000):
            if width > rows:
                continue
            low = rows // 3
            statement = f"SELECT * FROM items WHERE id >= {low} AND id < {low + width}"
            plan = db.execute(f"EXPLAIN {statement}").rows[-1][0].strip().split("(")[0]
            seconds = median_of(lambda statement=statement: db.execute(statement))
            out["ranges"].append(
                {
                    "width": width,
                    "plan": plan,
                    "milliseconds": seconds * 1e3,
                    "rows_per_second": width / seconds,
                }
            )
        ordered = "SELECT * FROM items ORDER BY id LIMIT 10"
        unordered = "SELECT * FROM items ORDER BY grp LIMIT 10"
        out["top10_by_indexed_column_ms"] = median_of(lambda: db.execute(ordered)) * 1e3
        out["top10_by_unindexed_column_ms"] = median_of(lambda: db.execute(unordered)) * 1e3
        out["sort_all_by_unindexed_column_ms"] = (
            median_of(lambda: db.execute("SELECT * FROM items ORDER BY grp"), 3) * 1e3
        )
    return out


def bench_commit(transactions: int) -> list[dict[str, Any]]:
    """Transactions per second with and without fsync at commit."""
    out = []
    configs = [
        ("fsync on, 1 row per transaction", True, 1),
        ("fsync off, 1 row per transaction", False, 1),
        ("fsync on, 100 rows per transaction", True, 100),
        ("fsync off, 100 rows per transaction", False, 100),
    ]
    for label, synchronous, rows_per_txn in configs:
        folder = scratch_dir("commit")
        with mvrs_db.connect(folder / "db.mvrs", synchronous=synchronous) as db:
            db.execute(CREATE)
            count = transactions if rows_per_txn == 1 else max(transactions // 10, 20)
            statements = [
                insert_statement(i * rows_per_txn, (i + 1) * rows_per_txn) for i in range(count)
            ]
            times = per_call(db, statements)
        total = sum(times)
        out.append(
            {
                "config": label,
                "transactions": count,
                "transactions_per_second": count / total,
                "rows_per_second": count * rows_per_txn / total,
                "latency": summarize(times),
            }
        )
    return out


def bench_buffer_pool(rows: int) -> list[dict[str, Any]]:
    """The same random lookups with different buffer pool sizes."""
    folder = scratch_dir("pool")
    with mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384) as db:
        load(db, rows)
        pages = db.page_count
    rng = random.Random(2)
    statements = [f"SELECT * FROM items WHERE id = {rng.randrange(rows)}" for _ in range(3000)]
    out = []
    for frames in (16, 64, 256, 1024, 4096):
        with mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=frames) as db:
            per_call(db, statements[:500])
            stats = db.pool.stats
            hits, misses = stats.hits, stats.misses
            times = per_call(db, statements)
            new_hits, new_misses = stats.hits - hits, stats.misses - misses
        out.append(
            {
                "frames": frames,
                "pool_bytes": frames * 4096,
                "database_pages": pages,
                "latency": summarize(times),
                "hit_ratio": new_hits / (new_hits + new_misses),
            }
        )
    return out


def bench_page_size(rows: int) -> list[dict[str, Any]]:
    out = []
    rng = random.Random(3)
    statements = [f"SELECT * FROM items WHERE id = {rng.randrange(rows)}" for _ in range(2000)]
    for page_size in (4096, 8192, 16384, 32768):
        folder = scratch_dir(f"page-{page_size}")
        with mvrs_db.connect(
            folder / "db.mvrs", synchronous=False, page_size=page_size, buffer_pages=4096
        ) as db:
            seconds = load(db, rows)
            per_call(db, statements[:200])
            lookups = summarize(per_call(db, statements))
            scan = median_of(lambda: db.execute("SELECT * FROM items"), 3)
            table = db._catalog.get("items")
            height = table.indexes[0].height()
            pages = db.page_count
        out.append(
            {
                "page_size": page_size,
                "load_rows_per_second": rows / seconds,
                "file_bytes": (folder / "db.mvrs").stat().st_size,
                "pages": pages,
                "index_height": height,
                "lookup": lookups,
                "full_scan_ms": scan * 1e3,
            }
        )
    return out


def bench_index_maintenance(rows: int) -> list[dict[str, Any]]:
    """Insert cost with one index (primary key) versus two (plus a UNIQUE text column)."""
    out = []
    for label, with_code in (("primary key only", False), ("primary key + unique text", True)):
        folder = scratch_dir("indexes")
        with mvrs_db.connect(folder / "db.mvrs", synchronous=False) as db:
            seconds = load(db, rows, with_code=with_code)
        out.append({"indexes": label, "rows": rows, "rows_per_second": rows / seconds})
    return out


def bench_recovery(commit_counts: list[int]) -> list[dict[str, Any]]:
    """Time to open a database whose changes are all still in the log.

    The files are copied while the database is open and nothing has been
    checkpointed. That copy is exactly what a crash at that moment leaves behind.
    """
    out = []
    for commits in commit_counts:
        folder = scratch_dir(f"recovery-{commits}")
        db = mvrs_db.connect(folder / "db.mvrs", synchronous=False, checkpoint_bytes=1 << 40)
        db.execute(CREATE)
        for i in range(commits):
            db.execute(insert_statement(i * 10, (i + 1) * 10))
        crashed = folder / "crashed"
        crashed.mkdir()
        shutil.copy(folder / "db.mvrs", crashed / "db.mvrs")
        shutil.copy(folder / "db.mvrs.wal", crashed / "db.mvrs.wal")
        log_bytes = (crashed / "db.mvrs.wal").stat().st_size
        db.close()

        started = time.perf_counter()
        recovered = mvrs_db.connect(crashed / "db.mvrs")
        seconds = time.perf_counter() - started
        stats = recovered.recovery
        assert recovered.check().tables == {"items": commits * 10}
        recovered.close()
        clean_open = timed(lambda: mvrs_db.connect(crashed / "db.mvrs").close())
        out.append(
            {
                "commits": commits,
                "log_bytes": log_bytes,
                "records": stats.records_scanned,
                "pages_restored": stats.pages_restored,
                "recovery_ms": seconds * 1e3,
                "clean_open_and_close_ms": clean_open * 1e3,
            }
        )
    return out


def bench_memory(rows: int) -> list[dict[str, Any]]:
    """Peak Python memory (tracemalloc) for loading and for materialising a full result."""
    out = []
    for frames in (64, 1024, 8192):
        folder = scratch_dir("memory")
        tracemalloc.start()
        db = mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=frames)
        load(db, rows)
        _, load_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        result = db.execute("SELECT * FROM items")
        _, select_peak = tracemalloc.get_traced_memory()
        del result
        db.close()
        tracemalloc.stop()
        out.append(
            {
                "frames": frames,
                "pool_bytes": frames * 4096,
                "rows": rows,
                "peak_bytes_during_load": load_peak,
                "peak_bytes_during_select_all": select_peak,
            }
        )
    return out


def bench_crossover(rows: int) -> dict[str, Any]:
    """Index range scan versus sequential scan as the matching fraction grows.

    Both plans are built by hand and timed, then compared with what the planner
    would have chosen for the same query.
    """
    folder = scratch_dir("crossover")
    out: dict[str, Any] = {"rows": rows, "points": []}
    with mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384) as db:
        load(db, rows)
        table = db._catalog.get("items")
        for fraction in (0.001, 0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0):
            high = max(1, int(rows * fraction))
            where_text = f"id < {high}"
            statement = parse(f"SELECT * FROM items WHERE {where_text}")
            predicate = expressions.compile_predicate(statement.where, table.schema)  # type: ignore[union-attr,arg-type]

            def run(scan: Operator) -> int:
                return sum(1 for _ in Filter(scan, predicate, where_text).rows())

            index_plan = IndexScan(table, 0, KeyRange(high=high, high_inclusive=False))
            index_ms = median_of(lambda p=index_plan: run(p)) * 1e3
            seq_ms = median_of(lambda: run(SeqScan(table))) * 1e3
            chosen = db.execute(f"EXPLAIN SELECT * FROM items WHERE {where_text}").rows[-1][0]
            planner_choice = "index" if "IndexScan" in chosen else "seq"
            faster = "index" if index_ms < seq_ms else "seq"
            out["points"].append(
                {
                    "fraction": fraction,
                    "index_ms": index_ms,
                    "seq_ms": seq_ms,
                    "faster": faster,
                    "planner": planner_choice,
                    "planner_correct": planner_choice == faster,
                    "cost_of_wrong_choice": max(index_ms, seq_ms) / min(index_ms, seq_ms),
                }
            )
    return out


def bench_sqlite(rows: int) -> dict[str, Any]:
    """The same SQL statements against SQLite (C) and MVRS_DB (Python).

    Both: one transaction per 500-row INSERT statement, no fsync at commit
    (MVRS_DB synchronous=False; SQLite PRAGMA synchronous=OFF, default
    rollback journal), then 2000 point lookups by primary key and a full scan.
    """
    rng = random.Random(4)
    lookups = [f"SELECT * FROM items WHERE id = {rng.randrange(rows)}" for _ in range(2000)]
    inserts = [insert_statement(s, min(s + BATCH, rows)) for s in range(0, rows, BATCH)]
    out: dict[str, Any] = {"rows": rows, "sqlite_version": sqlite3.sqlite_version}

    folder = scratch_dir("sqlite")
    connection = sqlite3.connect(folder / "db.sqlite", isolation_level=None)
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(CREATE)
    seconds = timed(lambda: [connection.execute(s) for s in inserts])
    times = []
    for statement in lookups:
        started = time.perf_counter()
        connection.execute(statement).fetchall()
        times.append(time.perf_counter() - started)
    scan = median_of(lambda: connection.execute("SELECT * FROM items").fetchall())
    connection.close()
    out["sqlite"] = {
        "load_rows_per_second": rows / seconds,
        "lookup": summarize(times),
        "full_scan_ms": scan * 1e3,
    }

    with mvrs_db.connect(folder / "db.mvrs", synchronous=False, buffer_pages=16384) as db:
        db.execute(CREATE)
        seconds = timed(lambda: [db.execute(s) for s in inserts])
        per_call(db, lookups[:200])
        lookup = summarize(per_call(db, lookups))
        scan = median_of(lambda: db.execute("SELECT * FROM items"))
    out["mvrs_db"] = {
        "load_rows_per_second": rows / seconds,
        "lookup": lookup,
        "full_scan_ms": scan * 1e3,
    }
    return out


SECTIONS: dict[str, Callable[[bool], Any]] = {
    "load": lambda quick: bench_load([1000, 5000] if quick else [1000, 10000, 100000]),
    "lookup": lambda quick: bench_lookup([1000, 5000] if quick else [1000, 10000, 100000]),
    "range": lambda quick: bench_range(5000 if quick else 100000),
    "commit": lambda quick: bench_commit(50 if quick else 300),
    "buffer_pool": lambda quick: bench_buffer_pool(5000 if quick else 100000),
    "page_size": lambda quick: bench_page_size(3000 if quick else 30000),
    "index_maintenance": lambda quick: bench_index_maintenance(3000 if quick else 30000),
    "recovery": lambda quick: bench_recovery([50, 200] if quick else [100, 1000, 5000]),
    "memory": lambda quick: bench_memory(3000 if quick else 30000),
    "crossover": lambda quick: bench_crossover(5000 if quick else 50000),
    "sqlite": lambda quick: bench_sqlite(5000 if quick else 100000),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=sorted(SECTIONS), action="append")
    parser.add_argument("--quick", action="store_true", help="small datasets, for a smoke run")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(exist_ok=True)
    results_path = RESULTS_DIR / ("results-quick.json" if args.quick else "results.json")
    results: dict[str, Any] = {}
    if args.only and results_path.exists():
        results = json.loads(results_path.read_text(encoding="utf-8"))
    results["environment"] = environment()
    results["generated"] = datetime.now().isoformat(timespec="seconds")
    results["quick"] = args.quick
    results["mvrs_db_version"] = mvrs_db.__version__

    for name in args.only or list(SECTIONS):
        print(f"running {name} ...", flush=True)
        started = time.perf_counter()
        results[name] = SECTIONS[name](args.quick)
        print(f"  done in {time.perf_counter() - started:.1f}s", flush=True)
        results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    if not args.quick:
        from report import write_report

        write_report(results)
        print(f"report written to {RESULTS_DIR / 'REPORT.md'}")


if __name__ == "__main__":
    main()
