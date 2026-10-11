# MVRS_DB benchmark report

Generated 2026-10-11T23:25:27 by `python benchmarks/run_benchmarks.py` (MVRS_DB 0.1.0). Every number below was measured in that run and is stored in `results.json` next to this file.

## Read this first

- **MVRS_DB is written in Python.** It is one to three orders of magnitude slower than engines written in C. The comparisons that mean something here are *internal*: the same engine with and without an index, with and without fsync, with different buffer pool and page sizes.
- **Single run, one machine.** Latencies are medians with p95/p99 over the stated number of samples; throughput figures are from one pass unless a median of several is stated. Expect run-to-run variation of 10-20%.
- **Warm cache unless stated.** Tables are loaded and queried in the same process, so pages are in the buffer pool. "After reopen" means the buffer pool is empty but the operating system's file cache is still warm; a truly cold disk read was not measured because the OS cache cannot be dropped from a normal user process.
- **Timings include SQL parsing**, which is a large share of a point lookup.

## Environment

| Item | Value |
|---|---|
| CPU | 11th Gen Intel(R) Core(TM) i5-11320H @ 3.20GHz (8 logical CPUs) |
| RAM | 15.8 GB |
| Drive | W: HDD / RAID / TOSHIBA MQ04ABF100 |
| OS | Windows 11 (build 10.0.26200) |
| Python | CPython 3.13.7 |
| Windows Defender real-time scanning | on |
| Page size | 4096 bytes unless stated |
| Buffer pool | 16,384 frames (64 MB) unless stated, so data fits in memory |
| Row | `(id INTEGER PRIMARY KEY, grp INTEGER, payload TEXT)`, 60-byte payload |

## 1. Insert throughput

500 rows per `INSERT` statement, each statement its own transaction, `synchronous=False` (no fsync at commit).

| Rows | Seconds | Rows/second | File size | Pages |
|---|---|---|---|---|
| 1,000 | 0.19 | 5,360 | 0.11 MB | 29 |
| 10,000 | 1.87 | 5,360 | 1.00 MB | 255 |
| 100,000 | 28.40 | 3,521 | 9.81 MB | 2,511 |

Reproduce: `python benchmarks/run_benchmarks.py --only load`

## 2. Point lookup: index versus full scan

`SELECT * FROM items WHERE id = k` uses the primary-key B+ tree. `WHERE grp = k` returns the same row but `grp` has no index, so the whole table is read. 2,000 random keys for the index, 15 for the scan. Microseconds.

| Rows | Index median | p95 | p99 | After reopen median | Full scan median | Speedup |
|---|---|---|---|---|---|---|
| 1,000 | 85.7 | 90.3 | 124.2 | 85.5 | 1,941 | 23x |
| 10,000 | 113.3 | 124.1 | 172.2 | 114.5 | 19,076 | 168x |
| 100,000 | 196.6 | 219.3 | 350.9 | 198.4 | 195,816 | 996x |

Parsing the lookup statement alone takes a median of 15.5 microseconds, so roughly that much of each index lookup is the SQL front end, not storage. Index latency grows slowly with table size (one more tree level); scan latency grows in proportion to it.

![Point lookup latency](lookup.png)

Reproduce: `python benchmarks/run_benchmarks.py --only lookup`

## 3. Range scans and ORDER BY

100,000 rows. Full table scan: **409,936 rows/second**.

| Rows in range | Plan chosen | Milliseconds | Rows/second |
|---|---|---|---|
| 10 | IndexScan | 0.93 | 10,745 |
| 100 | IndexScan | 1.24 | 80,561 |
| 1,000 | IndexScan | 5.44 | 183,949 |
| 10,000 | IndexScan | 45.27 | 220,874 |

| Query | Milliseconds |
|---|---|
| `ORDER BY id LIMIT 10` (walks the index, stops after 10) | 0.55 |
| `ORDER BY grp LIMIT 10` (scans everything, keeps the best 10) | 239.53 |
| `ORDER BY grp` (scans and sorts everything) | 308.39 |

Reproduce: `python benchmarks/run_benchmarks.py --only range`

## 4. Commit throughput: fsync on versus off

Each transaction is one `INSERT` statement. With fsync on, commit returns only after the log is on stable storage, so the drive's sync latency sets the ceiling.

| Configuration | Transactions/s | Rows/s | Median ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| fsync on, 1 row per transaction | 35 | 35 | 22.99 | 34.07 | 54.96 |
| fsync off, 1 row per transaction | 3,321 | 3,321 | 0.28 | 0.50 | 0.54 |
| fsync on, 100 rows per transaction | 16 | 1,629 | 56.15 | 78.34 | 155.08 |
| fsync off, 100 rows per transaction | 55 | 5,464 | 18.66 | 24.21 | 26.48 |

Batching rows into one transaction is the cheap way to get throughput with durability: the fsync cost is paid once per transaction, not once per row.

![Commit throughput](commit.png)

Reproduce: `python benchmarks/run_benchmarks.py --only commit`

## 5. Buffer pool size

3,000 random primary-key lookups against a 2,511-page database, reopened with each pool size. A lookup touches the index root, one or two more index pages, and one heap page.

| Frames | Pool memory | Hit ratio | Median us | p95 us | p99 us |
|---|---|---|---|---|---|
| 16 | 0.06 MB | 79.3% | 223.2 | 271.8 | 391.4 |
| 64 | 0.25 MB | 80.8% | 222.9 | 274.7 | 389.9 |
| 256 | 1.00 MB | 83.2% | 222.7 | 245.5 | 372.9 |
| 1,024 | 4.00 MB | 93.0% | 211.5 | 236.4 | 364.2 |
| 4,096 | 16.00 MB | 99.6% | 197.8 | 229.0 | 340.3 |

Misses here are served from the operating system's file cache, not the disk, so the penalty shown is the cost of a system call, a checksum and an eviction. On a cold disk the gap would be far larger.

![Buffer pool size](buffer_pool.png)

Reproduce: `python benchmarks/run_benchmarks.py --only buffer_pool`

## 6. Page size

30,000 rows loaded at each page size.

| Page size | Load rows/s | File size | Pages | Index height | Lookup median us | Full scan ms |
|---|---|---|---|---|---|---|
| 4,096 | 4,871 | 2.95 MB | 755 | 2 | 176.1 | 71.9 |
| 8,192 | 3,076 | 2.94 MB | 376 | 2 | 129.6 | 71.1 |
| 16,384 | 1,730 | 2.97 MB | 190 | 2 | 107.4 | 70.4 |
| 32,768 | 919 | 3.03 MB | 97 | 2 | 96.3 | 70.8 |

Load throughput falls as the page grows. The log is not the reason: with delta records the bytes logged per commit barely change with page size (section 12). Profiling shows the cost is CPU per commit that grows with the page: a changed page is compared byte-range by byte-range against its earlier copy, and a B+ tree node is decoded and re-encoded whole, so a larger node means more entries to process for each inserted row. At this table size the index has the same height at every page size, so the lookup difference is not from a shallower tree.

Reproduce: `python benchmarks/run_benchmarks.py --only page_size`

## 7. Cost of maintaining a second index

| Indexes | Rows | Insert rows/s |
|---|---|---|
| primary key only | 30,000 | 4,885 |
| primary key + unique text | 30,000 | 2,757 |

There is no separate `CREATE INDEX`, so index build time is part of loading. Each index adds a uniqueness check and an insert per row.

Reproduce: `python benchmarks/run_benchmarks.py --only index_maintenance`

## 8. Recovery time

Small transactions (10 rows each) with checkpoints disabled, so every change is only in the log. The files are copied while the database is open, which is exactly the state a crash leaves, and the copy is opened.

| Commits in log | Log size | Records | Pages restored | Recovery ms | Clean open+close ms |
|---|---|---|---|---|---|
| 100 | 0.22 MB | 360 | 28 | 100.5 | 233.6 |
| 1,000 | 2.03 MB | 3,545 | 254 | 166.7 | 111.1 |
| 5,000 | 10.05 MB | 17,680 | 1,024 | 386.8 | 67.6 |

Recovery reads the whole log but writes each page once, however many records (images and deltas) the log holds for it. Recovery time therefore follows log size, and the default 8 MB checkpoint threshold bounds it.

Reproduce: `python benchmarks/run_benchmarks.py --only recovery`

## 9. Memory

Peak Python heap measured with `tracemalloc` (which itself slows the run and does not count memory held by the interpreter outside Python objects).

| Pool frames | Pool capacity | Peak while loading | Peak during `SELECT *` |
|---|---|---|---|
| 64 | 0.25 MB | 1.07 MB | 7.35 MB |
| 1,024 | 4.00 MB | 3.85 MB | 10.19 MB |
| 8,192 | 32.00 MB | 3.85 MB | 10.20 MB |

30,000 rows. A result set is materialised in full before `execute()` returns, so `SELECT *` on a large table costs memory in proportion to the result, independent of the buffer pool.

Reproduce: `python benchmarks/run_benchmarks.py --only memory`

## 10. Does the optimiser choose correctly?

`SELECT * FROM items WHERE id < N` on 50,000 rows, for N covering 0.1% to 100% of the table. Both plans were built by hand and timed; the last columns show what the planner picked and what a wrong pick costs.

| Matching | Index scan ms | Seq scan ms | Faster | Planner chose | Correct? |
|---|---|---|---|---|---|
| 0.1% | 0.37 | 96.32 | index | index | yes |
| 1% | 2.00 | 94.52 | index | index | yes |
| 5% | 9.16 | 97.42 | index | index | yes |
| 10% | 18.19 | 95.56 | index | index | yes |
| 20% | 36.67 | 97.05 | index | index | yes |
| 30% | 55.04 | 96.14 | index | index | yes |
| 40% | 74.43 | 96.66 | index | index | yes |
| 50% | 92.63 | 97.40 | index | seq | no (1.05x slower) |
| 60% | 111.32 | 97.39 | seq | seq | yes |
| 80% | 148.29 | 98.40 | seq | seq | yes |
| 100% | 185.12 | 99.48 | seq | seq | yes |

The planner was wrong at 1 of 11 points, all near the crossover, where the two plans cost almost the same; the worst case was 1.05x slower than the better plan.

The crossover is far to the right of what a disk-bound engine would show, because with a warm cache a heap fetch through the index is a dictionary lookup, not a seek. The constants were fitted with `benchmarks/calibrate_optimizer.py`.

![Scan versus index](crossover.png)

Reproduce: `python benchmarks/run_benchmarks.py --only crossover`

## 11. SQLite, for scale only

> **SQLite 3.50.4 is written in C. MVRS_DB is written in Python.** These numbers show the size of that gap. They are not a contest, and nothing here suggests MVRS_DB could replace SQLite.

Identical SQL statements, 100,000 rows, one transaction per 500-row `INSERT`, no fsync on either side (MVRS_DB `synchronous=False`; SQLite `PRAGMA synchronous=OFF` with its default rollback journal), then 2,000 primary-key lookups and a full scan.

| Operation | MVRS_DB | SQLite | SQLite is faster by |
|---|---|---|---|
| Insert, rows/second | 4,105 | 343,444 | 84x |
| Point lookup, median microseconds | 194.9 | 26.9 | 7x |
| Full scan, milliseconds | 239.4 | 68.9 | 3x |

Reproduce: `python benchmarks/run_benchmarks.py --only sqlite`

## 12. Delta log records

Each workload runs on a fresh database with `log_deltas=False` (a whole page logged for every changed page) and with `log_deltas=True` (only the bytes that changed, except for new pages, the first change to a page after a checkpoint, and changes of more than half a page). `synchronous=False`, so the timings show the cost of building and writing log records and not the drive's fsync. Checkpoints are off, so the recovery column replays every commit.

| Workload | Log size, images | Log size, deltas | Smaller by | Bytes per commit (deltas) | Commits/s, images | Commits/s, deltas | Recovery ms, images | Recovery ms, deltas |
|---|---|---|---|---|---|---|---|---|
| 3,000 single-row inserts | 24.04 MB | 1.00 MB | 24.0x | 350 | 3,125 | 2,679 | 244 | 134 |
| 60 inserts of 500 rows | 3.68 MB | 3.27 MB | 1.1x | 57,213 | 9 | 9 | 198 | 211 |
| 1,500 single-row updates | 6.17 MB | 0.44 MB | 14.0x | 315 | 1,548 | 1,603 | 278 | 165 |
| 1,500 single-row deletes | 12.22 MB | 3.83 MB | 3.2x | 2,676 | 1,160 | 1,225 | 277 | 165 |

Page size, 3,000 single-row inserts:

| Page size | Bytes per commit, images | Bytes per commit, deltas | Commits/s, deltas |
|---|---|---|---|
| 4,096 | 8,401 | 350 | 2,679 |
| 16,384 | 32,988 | 361 | 1,167 |
| 32,768 | 65,761 | 376 | 673 |

What the numbers show:

- **The log shrinks a lot for small changes**: a single-row insert or update logs a few hundred bytes instead of two or three pages.
- **Throughput with fsync off did not change in a way these runs can tell apart from noise.** The ratio (deltas over whole pages) ran from 0.86 to 1.06 across workloads in this run. The same workload has varied by about 15% between runs on this machine, so differences of that size mean nothing. Building the log record was not where the time went; parsing, executing and rewriting B+ tree nodes were.
- **Throughput with fsync on did not change either.** 150 durable single-row inserts took a median of 23.0 ms per commit with whole pages (8.4 KB logged per commit) and 22.8 ms with deltas (0.3 KB), because the drive's sync latency dominates. A drive with fast syncs, such as an SSD, might show a difference; this one is a hard disk.
- **Bulk inserts barely benefit** (about 1.1 times smaller): most of what they write is new pages, which are always logged whole.
- **Deletes benefit least.** A B+ tree node is decoded and re-encoded whole, so removing one key shifts many cells and the change often exceeds half a page, which is then logged as a full page. Rewriting nodes in place would fix this.
- **The log no longer grows with the page size**, but time per commit still does: comparing a changed page against its earlier copy and rewriting a B+ tree node both cost more on bigger pages.
- **Recovery was faster where the log shrank most**: for single-row inserts and updates it took 1.7 to 2.1 times less time in each of two runs, since there is less to read. For deletes and bulk inserts it varied between no change and a modest gain, within the noise.

Reproduce: `python benchmarks/log_volume.py`

## Known bottlenecks

- **Whole-page log images remain for new pages, for the first change to a page after a checkpoint, and for changes of more than half a page.** Deletes from an index hit the last case often (section 12).
- **One fsync per commit.** There is no group commit, so durable throughput is bounded by the drive's sync latency. Smaller log records did not change it.
- **B+ tree nodes are decoded and re-encoded whole** on every insert and delete. That costs CPU, grows with the page size, and makes index changes larger in the log than they need to be.
- **Row-at-a-time execution.** Each row passes through a Python generator per operator and is decoded into a tuple even if most columns are unused.
- **Results are materialised** before being returned.
- **The SQL text is parsed on every call.** There are no prepared statements.
- **Single writer.** Write throughput does not scale with threads, and the GIL prevents read scaling as well.

## What was not measured

- Cold-disk performance, and behaviour once the database is larger than RAM.
- Power-loss durability. `fsync` is called; whether the drive honours it was not tested.
- Concurrent throughput. The concurrency tests check correctness, not speed.
- Skewed data. The optimiser assumes evenly spread values, and every dataset here is evenly spread.
