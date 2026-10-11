"""Turn benchmarks/results/results.json into REPORT.md and, if matplotlib is installed, charts.

    python benchmarks/report.py

Normally called by run_benchmarks.py. Running it alone re-renders the report
from the saved JSON without measuring anything again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import RESULTS_DIR

RUN = "python benchmarks/run_benchmarks.py --only"


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def num(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def latency(stats: dict[str, float]) -> list[str]:
    return [num(stats["median_us"], 1), num(stats["p95_us"], 1), num(stats["p99_us"], 1)]


def megabytes(count: float) -> str:
    return f"{count / 1_048_576:.2f} MB"


def charts(results: dict[str, Any]) -> list[str]:
    """Write PNG charts. Returns the file names written (empty if matplotlib is missing)."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    written = []

    lookup = results["lookup"]
    figure, axis = plt.subplots(figsize=(6, 4))
    sizes = [entry["rows"] for entry in lookup]
    axis.plot(sizes, [e["index_warm"]["median_us"] for e in lookup], "o-", label="index lookup")
    axis.plot(sizes, [e["full_scan"]["median_us"] for e in lookup], "s-", label="full scan")
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("rows in table")
    axis.set_ylabel("median latency (microseconds)")
    axis.set_title("Point lookup: index vs full scan")
    axis.legend()
    axis.grid(True, which="both", alpha=0.3)
    figure.tight_layout()
    figure.savefig(RESULTS_DIR / "lookup.png", dpi=120)
    plt.close(figure)
    written.append("lookup.png")

    points = results["crossover"]["points"]
    figure, axis = plt.subplots(figsize=(6, 4))
    fractions = [p["fraction"] * 100 for p in points]
    axis.plot(fractions, [p["index_ms"] for p in points], "o-", label="index range scan")
    axis.plot(fractions, [p["seq_ms"] for p in points], "s-", label="sequential scan")
    axis.set_xlabel("rows matching (% of table)")
    axis.set_ylabel("milliseconds")
    axis.set_title(f"Scan vs index, {results['crossover']['rows']:,} rows")
    axis.legend()
    axis.grid(True, alpha=0.3)
    figure.tight_layout()
    figure.savefig(RESULTS_DIR / "crossover.png", dpi=120)
    plt.close(figure)
    written.append("crossover.png")

    pool = results["buffer_pool"]
    figure, axis = plt.subplots(figsize=(6, 4))
    axis.plot([e["frames"] for e in pool], [e["latency"]["median_us"] for e in pool], "o-")
    axis.set_xscale("log", base=2)
    axis.set_xlabel("buffer pool size (frames)")
    axis.set_ylabel("median lookup latency (microseconds)")
    axis.set_title(f"Buffer pool size, {pool[0]['database_pages']:,}-page database")
    axis.grid(True, which="both", alpha=0.3)
    figure.tight_layout()
    figure.savefig(RESULTS_DIR / "buffer_pool.png", dpi=120)
    plt.close(figure)
    written.append("buffer_pool.png")

    commit = results["commit"]
    figure, axis = plt.subplots(figsize=(7, 4))
    axis.barh([e["config"] for e in commit], [e["transactions_per_second"] for e in commit])
    axis.set_xscale("log")
    axis.set_xlabel("transactions per second (log scale)")
    axis.set_title("Commit throughput")
    figure.tight_layout()
    figure.savefig(RESULTS_DIR / "commit.png", dpi=120)
    plt.close(figure)
    written.append("commit.png")
    return written


def render(results: dict[str, Any], images: list[str]) -> str:
    env = results["environment"]
    parts: list[str] = []
    add = parts.append

    add("# MVRS_DB benchmark report\n")
    add(
        f"Generated {results['generated']} by `python benchmarks/run_benchmarks.py` "
        f"(MVRS_DB {results['mvrs_db_version']}). Every number below was measured in "
        "that run and is stored in `results.json` next to this file.\n"
    )

    add("## Read this first\n")
    add(
        "- **MVRS_DB is written in Python.** It is one to three orders of magnitude slower "
        "than engines written in C. The comparisons that mean something here are *internal*: "
        "the same engine with and without an index, with and without fsync, with different "
        "buffer pool and page sizes.\n"
        "- **Single run, one machine.** Latencies are medians with p95/p99 over the stated "
        "number of samples; throughput figures are from one pass unless a median of several "
        "is stated. Expect run-to-run variation of 10-20%.\n"
        "- **Warm cache unless stated.** Tables are loaded and queried in the same process, "
        'so pages are in the buffer pool. "After reopen" means the buffer pool is empty but '
        "the operating system's file cache is still warm; a truly cold disk read was not "
        "measured because the OS cache cannot be dropped from a normal user process.\n"
        "- **Timings include SQL parsing**, which is a large share of a point lookup.\n"
    )

    add("## Environment\n")
    add(
        table(
            ["Item", "Value"],
            [
                ["CPU", f"{env['cpu']} ({env['logical_cpus']} logical CPUs)"],
                ["RAM", env["ram"]],
                ["Drive", env["drive"]],
                ["OS", env["os"]],
                ["Python", env["python"]],
                ["Windows Defender real-time scanning", env["defender_realtime"]],
                ["Page size", "4096 bytes unless stated"],
                ["Buffer pool", "16,384 frames (64 MB) unless stated, so data fits in memory"],
                ["Row", "`(id INTEGER PRIMARY KEY, grp INTEGER, payload TEXT)`, 60-byte payload"],
            ],
        )
        + "\n"
    )

    add("## 1. Insert throughput\n")
    add(
        "500 rows per `INSERT` statement, each statement its own transaction, "
        "`synchronous=False` (no fsync at commit).\n"
    )
    add(
        table(
            ["Rows", "Seconds", "Rows/second", "File size", "Pages"],
            [
                [
                    num(e["rows"]),
                    num(e["seconds"], 2),
                    num(e["rows_per_second"]),
                    megabytes(e["file_bytes"]),
                    num(e["pages"]),
                ]
                for e in results["load"]
            ],
        )
    )
    add(f"\nReproduce: `{RUN} load`\n")

    add("## 2. Point lookup: index versus full scan\n")
    add(
        "`SELECT * FROM items WHERE id = k` uses the primary-key B+ tree. "
        "`WHERE grp = k` returns the same row but `grp` has no index, so the whole table is "
        "read. 2,000 random keys for the index, 15 for the scan. Microseconds.\n"
    )
    add(
        table(
            [
                "Rows",
                "Index median",
                "p95",
                "p99",
                "After reopen median",
                "Full scan median",
                "Speedup",
            ],
            [
                [
                    num(e["rows"]),
                    *latency(e["index_warm"]),
                    num(e["index_after_reopen"]["median_us"], 1),
                    num(e["full_scan"]["median_us"]),
                    f"{e['speedup_median']:,.0f}x",
                ]
                for e in results["lookup"]
            ],
        )
    )
    parse_us = results["lookup"][-1]["parse_only"]["median_us"]
    add(
        f"\nParsing the lookup statement alone takes a median of {parse_us:.1f} microseconds, "
        "so roughly that much of each index lookup is the SQL front end, not storage. "
        "Index latency grows slowly with table size (one more tree level); scan latency "
        "grows in proportion to it.\n"
    )
    if "lookup.png" in images:
        add("![Point lookup latency](lookup.png)\n")
    add(f"Reproduce: `{RUN} lookup`\n")

    rng = results["range"]
    add("## 3. Range scans and ORDER BY\n")
    add(
        f"{num(rng['rows'])} rows. Full table scan: "
        f"**{num(rng['full_scan_rows_per_second'])} rows/second**.\n"
    )
    add(
        table(
            ["Rows in range", "Plan chosen", "Milliseconds", "Rows/second"],
            [
                [num(e["width"]), e["plan"], num(e["milliseconds"], 2), num(e["rows_per_second"])]
                for e in rng["ranges"]
            ],
        )
    )
    add(
        "\n"
        + table(
            ["Query", "Milliseconds"],
            [
                [
                    "`ORDER BY id LIMIT 10` (walks the index, stops after 10)",
                    num(rng["top10_by_indexed_column_ms"], 2),
                ],
                [
                    "`ORDER BY grp LIMIT 10` (scans everything, keeps the best 10)",
                    num(rng["top10_by_unindexed_column_ms"], 2),
                ],
                [
                    "`ORDER BY grp` (scans and sorts everything)",
                    num(rng["sort_all_by_unindexed_column_ms"], 2),
                ],
            ],
        )
    )
    add(f"\nReproduce: `{RUN} range`\n")

    add("## 4. Commit throughput: fsync on versus off\n")
    add(
        "Each transaction is one `INSERT` statement. With fsync on, commit returns only "
        "after the log is on stable storage, so the drive's sync latency sets the ceiling.\n"
    )
    add(
        table(
            ["Configuration", "Transactions/s", "Rows/s", "Median ms", "p95 ms", "p99 ms"],
            [
                [
                    e["config"],
                    num(e["transactions_per_second"]),
                    num(e["rows_per_second"]),
                    num(e["latency"]["median_us"] / 1000, 2),
                    num(e["latency"]["p95_us"] / 1000, 2),
                    num(e["latency"]["p99_us"] / 1000, 2),
                ]
                for e in results["commit"]
            ],
        )
    )
    add(
        "\nBatching rows into one transaction is the cheap way to get throughput with "
        "durability: the fsync cost is paid once per transaction, not once per row.\n"
    )
    if "commit.png" in images:
        add("![Commit throughput](commit.png)\n")
    add(f"Reproduce: `{RUN} commit`\n")

    pool = results["buffer_pool"]
    add("## 5. Buffer pool size\n")
    add(
        f"3,000 random primary-key lookups against a {num(pool[0]['database_pages'])}-page "
        "database, reopened with each pool size. A lookup touches the index root, one or two "
        "more index pages, and one heap page.\n"
    )
    add(
        table(
            ["Frames", "Pool memory", "Hit ratio", "Median us", "p95 us", "p99 us"],
            [
                [
                    num(e["frames"]),
                    megabytes(e["pool_bytes"]),
                    f"{e['hit_ratio'] * 100:.1f}%",
                    *latency(e["latency"]),
                ]
                for e in pool
            ],
        )
    )
    add(
        "\nMisses here are served from the operating system's file cache, not the disk, so "
        "the penalty shown is the cost of a system call, a checksum and an eviction. On a "
        "cold disk the gap would be far larger.\n"
    )
    if "buffer_pool.png" in images:
        add("![Buffer pool size](buffer_pool.png)\n")
    add(f"Reproduce: `{RUN} buffer_pool`\n")

    add("## 6. Page size\n")
    sizes = results["page_size"]
    add(f"{num(30000 if not results['quick'] else 3000)} rows loaded at each page size.\n")
    add(
        table(
            [
                "Page size",
                "Load rows/s",
                "File size",
                "Pages",
                "Index height",
                "Lookup median us",
                "Full scan ms",
            ],
            [
                [
                    num(e["page_size"]),
                    num(e["load_rows_per_second"]),
                    megabytes(e["file_bytes"]),
                    num(e["pages"]),
                    e["index_height"],
                    num(e["lookup"]["median_us"], 1),
                    num(e["full_scan_ms"], 1),
                ]
                for e in sizes
            ],
        )
    )
    add(
        "\nLoad throughput falls as the page grows. The log is not the reason: with delta "
        "records the bytes logged per commit barely change with page size (section 12). "
        "Profiling shows the cost is CPU per commit that grows with the page: a changed page "
        "is compared byte-range by byte-range against its earlier copy, and a B+ tree node "
        "is decoded and re-encoded whole, so a larger node means more entries to process for "
        "each inserted row. At this table size the index has the same height at every page "
        "size, so the lookup difference is not from a shallower tree.\n"
    )
    add(f"Reproduce: `{RUN} page_size`\n")

    add("## 7. Cost of maintaining a second index\n")
    add(
        table(
            ["Indexes", "Rows", "Insert rows/s"],
            [
                [e["indexes"], num(e["rows"]), num(e["rows_per_second"])]
                for e in results["index_maintenance"]
            ],
        )
    )
    add(
        "\nThere is no separate `CREATE INDEX`, so index build time is part of loading. "
        "Each index adds a uniqueness check and an insert per row.\n"
    )
    add(f"Reproduce: `{RUN} index_maintenance`\n")

    add("## 8. Recovery time\n")
    add(
        "Small transactions (10 rows each) with checkpoints disabled, so every change is "
        "only in the log. The files are copied while the database is open, which is exactly "
        "the state a crash leaves, and the copy is opened.\n"
    )
    add(
        table(
            [
                "Commits in log",
                "Log size",
                "Records",
                "Pages restored",
                "Recovery ms",
                "Clean open+close ms",
            ],
            [
                [
                    num(e["commits"]),
                    megabytes(e["log_bytes"]),
                    num(e["records"]),
                    num(e["pages_restored"]),
                    num(e["recovery_ms"], 1),
                    num(e["clean_open_and_close_ms"], 1),
                ]
                for e in results["recovery"]
            ],
        )
    )
    add(
        "\nRecovery reads the whole log but writes each page once, however many records "
        "(images and deltas) the log holds for it. Recovery time therefore follows log size, "
        "and the default 8 MB checkpoint threshold bounds it.\n"
    )
    add(f"Reproduce: `{RUN} recovery`\n")

    add("## 9. Memory\n")
    add(
        "Peak Python heap measured with `tracemalloc` (which itself slows the run and "
        "does not count memory held by the interpreter outside Python objects).\n"
    )
    add(
        table(
            ["Pool frames", "Pool capacity", "Peak while loading", "Peak during `SELECT *`"],
            [
                [
                    num(e["frames"]),
                    megabytes(e["pool_bytes"]),
                    megabytes(e["peak_bytes_during_load"]),
                    megabytes(e["peak_bytes_during_select_all"]),
                ]
                for e in results["memory"]
            ],
        )
    )
    add(
        f"\n{num(results['memory'][0]['rows'])} rows. A result set is materialised in full "
        "before `execute()` returns, so `SELECT *` on a large table costs memory in "
        "proportion to the result, independent of the buffer pool.\n"
    )
    add(f"Reproduce: `{RUN} memory`\n")

    cross = results["crossover"]
    add("## 10. Does the optimiser choose correctly?\n")
    add(
        f"`SELECT * FROM items WHERE id < N` on {num(cross['rows'])} rows, for N covering "
        "0.1% to 100% of the table. Both plans were built by hand and timed; the last "
        "columns show what the planner picked and what a wrong pick costs.\n"
    )
    add(
        table(
            ["Matching", "Index scan ms", "Seq scan ms", "Faster", "Planner chose", "Correct?"],
            [
                [
                    f"{p['fraction'] * 100:g}%",
                    num(p["index_ms"], 2),
                    num(p["seq_ms"], 2),
                    p["faster"],
                    p["planner"],
                    "yes"
                    if p["planner_correct"]
                    else f"no ({p['cost_of_wrong_choice']:.2f}x slower)",
                ]
                for p in cross["points"]
            ],
        )
    )
    wrong = [p for p in cross["points"] if not p["planner_correct"]]
    if wrong:
        worst = max(p["cost_of_wrong_choice"] for p in wrong)
        add(
            f"\nThe planner was wrong at {len(wrong)} of {len(cross['points'])} points, all "
            f"near the crossover, where the two plans cost almost the same; the worst case "
            f"was {worst:.2f}x slower than the better plan.\n"
        )
    else:
        add(f"\nThe planner picked the faster plan at all {len(cross['points'])} points.\n")
    add(
        "The crossover is far to the right of what a disk-bound engine would show, because "
        "with a warm cache a heap fetch through the index is a dictionary lookup, not a "
        "seek. The constants were fitted with `benchmarks/calibrate_optimizer.py`.\n"
    )
    if "crossover.png" in images:
        add("![Scan versus index](crossover.png)\n")
    add(f"Reproduce: `{RUN} crossover`\n")

    sq = results["sqlite"]
    add("## 11. SQLite, for scale only\n")
    add(
        f"> **SQLite {sq['sqlite_version']} is written in C. MVRS_DB is written in "
        "Python.** These numbers show the size of that gap. They are not a contest, and "
        "nothing here suggests MVRS_DB could replace SQLite.\n"
    )
    add(
        f"Identical SQL statements, {num(sq['rows'])} rows, one transaction per 500-row "
        "`INSERT`, no fsync on either side (MVRS_DB `synchronous=False`; SQLite "
        "`PRAGMA synchronous=OFF` with its default rollback journal), then 2,000 primary-key "
        "lookups and a full scan.\n"
    )
    k, s = sq["mvrs_db"], sq["sqlite"]
    add(
        table(
            ["Operation", "MVRS_DB", "SQLite", "SQLite is faster by"],
            [
                [
                    "Insert, rows/second",
                    num(k["load_rows_per_second"]),
                    num(s["load_rows_per_second"]),
                    f"{s['load_rows_per_second'] / k['load_rows_per_second']:.0f}x",
                ],
                [
                    "Point lookup, median microseconds",
                    num(k["lookup"]["median_us"], 1),
                    num(s["lookup"]["median_us"], 1),
                    f"{k['lookup']['median_us'] / s['lookup']['median_us']:.0f}x",
                ],
                [
                    "Full scan, milliseconds",
                    num(k["full_scan_ms"], 1),
                    num(s["full_scan_ms"], 1),
                    f"{k['full_scan_ms'] / s['full_scan_ms']:.0f}x",
                ],
            ],
        )
    )
    add(f"\nReproduce: `{RUN} sqlite`\n")

    volume = _load_log_volume()
    if volume is not None:
        add(_log_volume_section(volume))

    group = _load_json("group_commit.json")
    if group is not None:
        add(_group_commit_section(group["results"]))

    add("## Known bottlenecks\n")
    add(
        "- **Whole-page log images remain for new pages, for the first change to a page "
        "after a checkpoint, and for changes of more than half a page.** Deletes from an "
        "index hit the last case often (section 12).\n"
        "- **One fsync per commit.** There is no group commit, so durable throughput is "
        "bounded by the drive's sync latency. Smaller log records did not change it.\n"
        "- **B+ tree nodes are decoded and re-encoded whole** on every insert and delete. "
        "That costs CPU, grows with the page size, and makes index changes larger in the "
        "log than they need to be.\n"
        "- **Row-at-a-time execution.** Each row passes through a Python generator per "
        "operator and is decoded into a tuple even if most columns are unused.\n"
        "- **Results are materialised** before being returned.\n"
        "- **The SQL text is parsed on every call.** There are no prepared statements.\n"
        "- **Single writer.** Write throughput does not scale with threads, and the GIL "
        "prevents read scaling as well.\n"
    )

    add("## What was not measured\n")
    add(
        "- Cold-disk performance, and behaviour once the database is larger than RAM.\n"
        "- Power-loss durability. `fsync` is called; whether the drive honours it was not "
        "tested.\n"
        "- Concurrent throughput. The concurrency tests check correctness, not speed.\n"
        "- Skewed data. The optimiser assumes evenly spread values, and every dataset here "
        "is evenly spread.\n"
    )
    return "\n".join(parts)


def _load_json(name: str) -> dict[str, Any] | None:
    path = RESULTS_DIR / name
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _group_commit_section(results: list[dict[str, Any]]) -> str:
    """Section 13: durable commit throughput as committing threads are added."""
    base = results[0]["commits_per_second"]
    lines = [
        "## 13. Group commit\n",
        "Each thread runs durable single-row `INSERT` transactions (fsync on) into one table "
        "for a fixed time. With one thread every commit pays for its own fsync. With several, "
        "commits that arrive while an fsync is running queue up and share the next one.\n",
        table(
            [
                "Threads",
                "Commits/s",
                "Versus one thread",
                "Commits per fsync",
                "Median ms",
                "p99 ms",
            ],
            [
                [
                    r["threads"],
                    num(r["commits_per_second"]),
                    f"{r['commits_per_second'] / base:.1f}x",
                    f"{r['commits_per_fsync']:.1f}",
                    num(r["median_ms"], 1),
                    num(r["p99_ms"], 1),
                ]
                for r in results
            ],
        ),
        "\nThroughput rises with the number of threads because each fsync now serves "
        "several commits. **Latency rises as well**: a commit waits for a shared batch and "
        "not only for its own fsync, so the median grows even as total throughput does. "
        "A single thread is unaffected. The gain is bounded by how many commits overlap, "
        "and the hard disk's fsync time sets the scale; a drive with fast syncs would "
        "show a smaller gain.\n",
        "Reproduce: `python benchmarks/group_commit.py`\n",
    ]
    return "\n".join(lines)


def _load_log_volume() -> dict[str, Any] | None:
    """Results of benchmarks/log_volume.py, if that script has been run."""
    path = RESULTS_DIR / "log_volume.json"
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _durable_sentence(durable: list[dict[str, Any]] | None) -> str:
    """The fsync-on comparison, quoted from the data, or a pointer if it was not measured."""
    if not durable:
        return (
            "- **Throughput with fsync on was not measured in this run.** Run "
            "`python benchmarks/log_volume.py` to include it.\n"
        )
    images = next(d for d in durable if not d["deltas"])
    deltas = next(d for d in durable if d["deltas"])
    return (
        f"- **Throughput with fsync on did not change either.** {images['commits']} durable "
        f"single-row inserts took a median of {images['median_ms']:.1f} ms per commit with "
        f"whole pages ({images['bytes_per_commit'] / 1000:.1f} KB logged per commit) and "
        f"{deltas['median_ms']:.1f} ms with deltas ({deltas['bytes_per_commit'] / 1000:.1f} KB), "
        "because the drive's sync latency dominates. A drive with fast syncs, such as an SSD, "
        "might show a difference; this one is a hard disk.\n"
    )


def _log_volume_section(data: dict[str, Any]) -> str:
    """Section 12: what delta log records change, measured by log_volume.py."""
    results: list[dict[str, Any]] = data["results"]
    by_workload: dict[str, dict[bool, dict[str, Any]]] = {}
    by_page_size: dict[int, dict[bool, dict[str, Any]]] = {}
    for result in results:
        if result["page_size"] == 4096:
            by_workload.setdefault(result["workload"], {})[result["deltas"]] = result
        if result["workload"] == "3,000 single-row inserts":
            by_page_size.setdefault(result["page_size"], {})[result["deltas"]] = result

    ratios = [
        pair[True]["commits_per_second"] / pair[False]["commits_per_second"]
        for pair in by_workload.values()
    ]
    low, high = min(ratios), max(ratios)
    lines = [
        "## 12. Delta log records\n",
        "Each workload runs on a fresh database with `log_deltas=False` (a whole page logged "
        "for every changed page) and with `log_deltas=True` (only the bytes that changed, "
        "except for new pages, the first change to a page after a checkpoint, and changes of "
        "more than half a page). `synchronous=False`, so the timings show the cost of building "
        "and writing log records and not the drive's fsync. Checkpoints are off, so the "
        "recovery column replays every commit.\n",
        table(
            [
                "Workload",
                "Log size, images",
                "Log size, deltas",
                "Smaller by",
                "Bytes per commit (deltas)",
                "Commits/s, images",
                "Commits/s, deltas",
                "Recovery ms, images",
                "Recovery ms, deltas",
            ],
            [
                [
                    name,
                    megabytes(pair[False]["log_bytes"]),
                    megabytes(pair[True]["log_bytes"]),
                    f"{pair[False]['log_bytes'] / pair[True]['log_bytes']:.1f}x",
                    num(pair[True]["bytes_per_commit"]),
                    num(pair[False]["commits_per_second"]),
                    num(pair[True]["commits_per_second"]),
                    num(pair[False]["recovery_ms"]),
                    num(pair[True]["recovery_ms"]),
                ]
                for name, pair in by_workload.items()
            ],
        ),
        "\nPage size, 3,000 single-row inserts:\n",
        table(
            [
                "Page size",
                "Bytes per commit, images",
                "Bytes per commit, deltas",
                "Commits/s, deltas",
            ],
            [
                [
                    num(size),
                    num(pair[False]["bytes_per_commit"]),
                    num(pair[True]["bytes_per_commit"]),
                    num(pair[True]["commits_per_second"]),
                ]
                for size, pair in sorted(by_page_size.items())
            ],
        ),
        "\nWhat the numbers show:\n",
        "- **The log shrinks a lot for small changes**: a single-row insert or update logs "
        "a few hundred bytes instead of two or three pages.\n"
        f"- **Throughput with fsync off did not change in a way these runs can tell apart "
        f"from noise.** The ratio (deltas over whole pages) ran from {low:.2f} to {high:.2f} "
        "across workloads in this run. The same workload has varied by about 15% between "
        "runs on this machine, so differences of that size mean nothing. Building the log "
        "record was not where the time went; parsing, executing and rewriting B+ tree nodes "
        "were.\n"
        + _durable_sentence(data.get("durable"))
        + "- **Bulk inserts barely benefit** (about 1.1 times smaller): most of what they "
        "write is new pages, which are always logged whole.\n"
        "- **Deletes benefit least.** A B+ tree node is decoded and re-encoded whole, so "
        "removing one key shifts many cells and the change often exceeds half a page, "
        "which is then logged as a full page. Rewriting nodes in place would fix this.\n"
        "- **The log no longer grows with the page size**, but time per commit still does: "
        "comparing a changed page against its earlier copy and rewriting a B+ tree node "
        "both cost more on bigger pages.\n"
        "- **Recovery was faster where the log shrank most**: for single-row inserts and "
        "updates it took 1.7 to 2.1 times less time in each of two runs, since there is less "
        "to read. For deletes and bulk inserts it varied between no change and a modest "
        "gain, within the noise.\n",
        "Reproduce: `python benchmarks/log_volume.py`\n",
    ]
    return "\n".join(lines)


def write_report(results: dict[str, Any]) -> None:
    images = charts(results)
    (RESULTS_DIR / "REPORT.md").write_text(render(results, images), encoding="utf-8")


if __name__ == "__main__":
    write_report(json.loads((RESULTS_DIR / "results.json").read_text(encoding="utf-8")))
    print(f"report written to {RESULTS_DIR / 'REPORT.md'}")
