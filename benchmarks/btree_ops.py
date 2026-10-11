"""B+ tree insert and delete: CPU per operation, and size of the log record each makes.

    python benchmarks/btree_ops.py

For each page size, a tree is built from random keys in memory (the buffer pool
holds everything). Then three things are measured:

* microseconds per insert, per lookup and per delete;
* for a sample of inserts and deletes, how many bytes of the tree's pages
  change, as the commit path would log them (the byte ranges that differ, plus
  their headers), and how many of those operations changed more than half a page
  and would therefore be logged as a whole page;
* the same, for appending in ascending key order.

Results are printed and saved to benchmarks/results/btree_ops.json (or the file
named by --out).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import RESULTS_DIR, environment, scratch_dir
from mvrs_db.index.btree import BTree
from mvrs_db.index.keys import encode_key
from mvrs_db.recovery.delta import changed_ranges, delta_size
from mvrs_db.storage.buffer_pool import BufferPool
from mvrs_db.storage.disk_manager import DiskManager
from mvrs_db.storage.heap_file import RecordId
from mvrs_db.storage.record import ColumnType

KEYS = 30_000
SAMPLE = 2_000


def key(value: int) -> bytes:
    return encode_key(ColumnType.INTEGER, value)


def logged_size(pool: BufferPool, operation: Any, page_size: int) -> tuple[int, bool]:
    """Run `operation` as a transaction and return the bytes it would log, and
    whether it would be logged as a whole page (change over half a page)."""
    pool.begin()
    operation()
    total, whole_page = 0, False
    for _page_id, before, data in pool.transaction_changes():
        if before is None:
            total += page_size  # a new page is logged whole
            whole_page = True
            continue
        ranges = changed_ranges(before, data)
        if not ranges:
            continue
        size = delta_size(ranges)
        if size > page_size // 2:
            total += page_size
            whole_page = True
        else:
            total += size
    pool.commit()
    return total, whole_page


def summarise(samples: list[tuple[int, bool]]) -> dict[str, float]:
    sizes = [size for size, _ in samples]
    return {
        "median_bytes": statistics.median(sizes),
        "mean_bytes": statistics.fmean(sizes),
        "p95_bytes": sorted(sizes)[int(0.95 * (len(sizes) - 1))],
        "whole_page_share": sum(1 for _, whole in samples if whole) / len(samples),
    }


def run(page_size: int) -> dict[str, Any]:
    folder = scratch_dir(f"btree-ops-{page_size}")
    disk = DiskManager.create(folder / "db.mvrs", page_size)
    pool = BufferPool(disk, 16384)
    tree = BTree(pool, BTree.create(pool))
    rng = random.Random(42)
    values = list(range(KEYS))
    rng.shuffle(values)

    started = time.perf_counter()
    for value in values:
        tree.insert(key(value), RecordId(1 + value % 1000, value % 50))
    insert_us = (time.perf_counter() - started) / KEYS * 1e6

    probes = [rng.randrange(KEYS) for _ in range(KEYS)]
    started = time.perf_counter()
    for value in probes:
        tree.get(key(value))
    lookup_us = (time.perf_counter() - started) / KEYS * 1e6

    victims = values[: KEYS // 2]
    started = time.perf_counter()
    for value in victims:
        tree.delete(key(value))
    delete_us = (time.perf_counter() - started) / len(victims) * 1e6
    tree.check_invariants()

    # Log sizes: random inserts into a populated tree, deletes from it, and appends.
    inserts = [
        logged_size(
            pool,
            lambda v=value: tree.insert(key(v), RecordId(1, v % 50)),
            page_size,
        )
        for value in victims[:SAMPLE]
    ]
    remaining = values[KEYS // 2 :]
    deletes = [
        logged_size(pool, lambda v=value: tree.delete(key(v)), page_size)
        for value in remaining[:SAMPLE]
    ]
    appends = [
        logged_size(
            pool,
            lambda v=value: tree.insert(key(v), RecordId(1, v % 50)),
            page_size,
        )
        for value in range(KEYS + 10, KEYS + 10 + SAMPLE)
    ]
    tree.check_invariants()
    disk.close()
    return {
        "page_size": page_size,
        "insert_us": insert_us,
        "lookup_us": lookup_us,
        "delete_us": delete_us,
        "logged_random_insert": summarise(inserts),
        "logged_delete": summarise(deletes),
        "logged_append": summarise(appends),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="btree_ops.json")
    args = parser.parse_args()

    results = []
    print(f"{KEYS:,} random keys; {SAMPLE:,} sampled operations for log sizes\n")
    for page_size in (4096, 32768):
        r = run(page_size)
        results.append(r)
        print(
            f"page {page_size:>5}: insert {r['insert_us']:6.1f} us"
            f"   lookup {r['lookup_us']:5.1f} us   delete {r['delete_us']:6.1f} us"
        )
        for label, name in (
            ("random insert", "logged_random_insert"),
            ("delete", "logged_delete"),
            ("append", "logged_append"),
        ):
            s = r[name]
            print(
                f"            {label:14} logs median {s['median_bytes']:7.0f} B  "
                f"mean {s['mean_bytes']:7.0f} B  p95 {s['p95_bytes']:7.0f} B  "
                f"whole page {s['whole_page_share'] * 100:4.0f}%"
            )
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / args.out
    path.write_text(
        json.dumps({"environment": environment(), "results": results}, indent=2), encoding="utf-8"
    )
    print(f"\nsaved to {path}")


if __name__ == "__main__":
    main()
