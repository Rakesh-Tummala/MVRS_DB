# KestrelDB benchmark report

Generated 2026-10-11T00:57:38 by `python benchmarks/run_benchmarks.py` (KestrelDB 0.1.0). Every number below was measured in that run and is stored in `results.json` next to this file.

## Read this first

- **KestrelDB is written in Python.** It is one to three orders of magnitude slower than engines written in C. The comparisons that mean something here are *internal*: the same engine with and without an index, with and without fsync, with different buffer pool and page sizes.
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
| 1,000 | 0.19 | 5,256 | 0.11 MB | 29 |
| 10,000 | 1.91 | 5,245 | 1.00 MB | 255 |
| 100,000 | 28.47 | 3,512 | 9.81 MB | 2,511 |

Reproduce: `python benchmarks/run_benchmarks.py --only load`

## 2. Point lookup: index versus full scan

`SELECT * FROM items WHERE id = k` uses the primary-key B+ tree. `WHERE grp = k` returns the same row but `grp` has no index, so the whole table is read. 2,000 random keys for the index, 15 for the scan. Microseconds.

| Rows | Index median | p95 | p99 | After reopen median | Full scan median | Speedup |
|---|---|---|---|---|---|---|
| 1,000 | 75.9 | 123.1 | 173.8 | 72.2 | 1,985 | 26x |
| 10,000 | 100.2 | 132.3 | 174.2 | 101.0 | 18,790 | 188x |
| 100,000 | 181.5 | 192.4 | 295.5 | 183.6 | 192,880 | 1,063x |

Parsing the lookup statement alone takes a median of 14.2 microseconds, so roughly that much of each index lookup is the SQL front end, not storage. Index latency grows slowly with table size (one more tree level); scan latency grows in proportion to it.

![Point lookup latency](lookup.png)

Reproduce: `python benchmarks/run_benchmarks.py --only lookup`

## 3. Range scans and ORDER BY

100,000 rows. Full table scan: **412,432 rows/second**.

| Rows in range | Plan chosen | Milliseconds | Rows/second |
|---|---|---|---|
| 10 | IndexScan | 0.94 | 10,591 |
| 100 | IndexScan | 1.38 | 72,322 |
| 1,000 | IndexScan | 5.46 | 183,201 |
| 10,000 | IndexScan | 45.94 | 217,688 |

| Query | Milliseconds |
|---|---|
| `ORDER BY id LIMIT 10` (walks the index, stops after 10) | 0.40 |
| `ORDER BY grp LIMIT 10` (scans everything, keeps the best 10) | 234.37 |
| `ORDER BY grp` (scans and sorts everything) | 303.45 |

Reproduce: `python benchmarks/run_benchmarks.py --only range`

## 4. Commit throughput: fsync on versus off

Each transaction is one `INSERT` statement. With fsync on, commit returns only after the log is on stable storage, so the drive's sync latency sets the ceiling.

| Configuration | Transactions/s | Rows/s | Median ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| fsync on, 1 row per transaction | 36 | 36 | 22.93 | 34.13 | 45.04 |
| fsync off, 1 row per transaction | 4,365 | 4,365 | 0.20 | 0.42 | 0.51 |
| fsync on, 100 rows per transaction | 19 | 1,943 | 45.00 | 67.26 | 155.10 |
| fsync off, 100 rows per transaction | 55 | 5,484 | 18.11 | 24.98 | 24.99 |

Batching rows into one transaction is the cheap way to get throughput with durability: the fsync cost is paid once per transaction, not once per row.

![Commit throughput](commit.png)

Reproduce: `python benchmarks/run_benchmarks.py --only commit`

## 5. Buffer pool size

3,000 random primary-key lookups against a 2,511-page database, reopened with each pool size. A lookup touches the index root, one or two more index pages, and one heap page.

| Frames | Pool memory | Hit ratio | Median us | p95 us | p99 us |
|---|---|---|---|---|---|
| 16 | 0.06 MB | 79.3% | 209.4 | 265.7 | 381.7 |
| 64 | 0.25 MB | 80.8% | 208.2 | 237.6 | 340.3 |
| 256 | 1.00 MB | 83.2% | 208.0 | 231.7 | 348.7 |
| 1,024 | 4.00 MB | 93.0% | 197.2 | 258.1 | 373.2 |
| 4,096 | 16.00 MB | 99.6% | 182.9 | 213.7 | 301.3 |

Misses here are served from the operating system's file cache, not the disk, so the penalty shown is the cost of a system call, a checksum and an eviction. On a cold disk the gap would be far larger.

![Buffer pool size](buffer_pool.png)

Reproduce: `python benchmarks/run_benchmarks.py --only buffer_pool`

## 6. Page size

30,000 rows loaded at each page size.

| Page size | Load rows/s | File size | Pages | Index height | Lookup median us | Full scan ms |
|---|---|---|---|---|---|---|
| 4,096 | 4,731 | 2.95 MB | 755 | 2 | 162.7 | 72.8 |
| 8,192 | 3,027 | 2.94 MB | 376 | 2 | 116.3 | 69.2 |
| 16,384 | 1,679 | 2.97 MB | 190 | 2 | 94.5 | 70.2 |
| 32,768 | 909 | 3.03 MB | 97 | 2 | 84.9 | 70.3 |

Larger pages mean fewer pages to manage and fewer splits, but every commit logs more bytes, because the log stores whole page images. That is why load throughput falls as the page grows. At this table size the index has the same height at every page size, so the lookup difference is not from a shallower tree.

Reproduce: `python benchmarks/run_benchmarks.py --only page_size`

## 7. Cost of maintaining a second index

| Indexes | Rows | Insert rows/s |
|---|---|---|
| primary key only | 30,000 | 4,857 |
| primary key + unique text | 30,000 | 2,641 |

There is no separate `CREATE INDEX`, so index build time is part of loading. Each index adds a uniqueness check and an insert per row.

Reproduce: `python benchmarks/run_benchmarks.py --only index_maintenance`

## 8. Recovery time

Small transactions (10 rows each) with checkpoints disabled, so every change is only in the log. The files are copied while the database is open, which is exactly the state a crash leaves, and the copy is opened.

| Commits in log | Log size | Records | Pages restored | Recovery ms | Clean open+close ms |
|---|---|---|---|---|---|
| 100 | 0.92 MB | 360 | 28 | 111.1 | 99.3 |
| 1,000 | 9.08 MB | 3,545 | 254 | 188.2 | 89.2 |
| 5,000 | 45.32 MB | 17,680 | 1,024 | 822.1 | 114.9 |

Recovery reads the whole log but writes each page once, however many images of it the log holds. Recovery time therefore follows log size, and the default 8 MB checkpoint threshold bounds it.

Reproduce: `python benchmarks/run_benchmarks.py --only recovery`

## 9. Memory

Peak Python heap measured with `tracemalloc` (which itself slows the run and does not count memory held by the interpreter outside Python objects).

| Pool frames | Pool capacity | Peak while loading | Peak during `SELECT *` |
|---|---|---|---|
| 64 | 0.25 MB | 1.04 MB | 7.32 MB |
| 1,024 | 4.00 MB | 3.82 MB | 10.16 MB |
| 8,192 | 32.00 MB | 3.82 MB | 10.16 MB |

30,000 rows. A result set is materialised in full before `execute()` returns, so `SELECT *` on a large table costs memory in proportion to the result, independent of the buffer pool.

Reproduce: `python benchmarks/run_benchmarks.py --only memory`

## 10. Does the optimiser choose correctly?

`SELECT * FROM items WHERE id < N` on 50,000 rows, for N covering 0.1% to 100% of the table. Both plans were built by hand and timed; the last columns show what the planner picked and what a wrong pick costs.

| Matching | Index scan ms | Seq scan ms | Faster | Planner chose | Correct? |
|---|---|---|---|---|---|
| 0.1% | 0.38 | 100.55 | index | index | yes |
| 1% | 2.14 | 97.84 | index | index | yes |
| 5% | 9.09 | 97.34 | index | index | yes |
| 10% | 19.19 | 98.19 | index | index | yes |
| 20% | 37.81 | 99.89 | index | index | yes |
| 30% | 57.89 | 97.72 | index | index | yes |
| 40% | 77.03 | 101.01 | index | index | yes |
| 50% | 95.94 | 101.12 | index | seq | no (1.05x slower) |
| 60% | 115.97 | 111.55 | seq | seq | yes |
| 80% | 172.70 | 113.86 | seq | seq | yes |
| 100% | 203.01 | 104.23 | seq | seq | yes |

The planner was wrong at 1 of 11 points, all near the crossover, where the two plans cost almost the same; the worst case was 1.05x slower than the better plan.

The crossover is far to the right of what a disk-bound engine would show, because with a warm cache a heap fetch through the index is a dictionary lookup, not a seek. The constants were fitted with `benchmarks/calibrate_optimizer.py`.

![Scan versus index](crossover.png)

Reproduce: `python benchmarks/run_benchmarks.py --only crossover`

## 11. SQLite, for scale only

> **SQLite 3.50.4 is written in C. KestrelDB is written in Python.** These numbers show the size of that gap. They are not a contest, and nothing here suggests KestrelDB could replace SQLite.

Identical SQL statements, 100,000 rows, one transaction per 500-row `INSERT`, no fsync on either side (KestrelDB `synchronous=False`; SQLite `PRAGMA synchronous=OFF` with its default rollback journal), then 2,000 primary-key lookups and a full scan.

| Operation | KestrelDB | SQLite | SQLite is faster by |
|---|---|---|---|
| Insert, rows/second | 4,025 | 257,737 | 64x |
| Point lookup, median microseconds | 185.2 | 26.5 | 7x |
| Full scan, milliseconds | 242.1 | 71.3 | 3x |

Reproduce: `python benchmarks/run_benchmarks.py --only sqlite`

## Known bottlenecks

- **Whole-page log images.** A one-row change logs at least two 4 KB pages (heap and index leaf). Delta records would shrink the log several-fold.
- **One fsync per commit.** There is no group commit, so durable throughput is bounded by the drive's sync latency.
- **B+ tree nodes are decoded and re-encoded whole** on every insert and delete.
- **Row-at-a-time execution.** Each row passes through a Python generator per operator and is decoded into a tuple even if most columns are unused.
- **Results are materialised** before being returned.
- **The SQL text is parsed on every call.** There are no prepared statements.
- **Single writer.** Write throughput does not scale with threads, and the GIL prevents read scaling as well.

## What was not measured

- Cold-disk performance, and behaviour once the database is larger than RAM.
- Power-loss durability. `fsync` is called; whether the drive honours it was not tested.
- Concurrent throughput. The concurrency tests check correctness, not speed.
- Skewed data. The optimiser assumes evenly spread values, and every dataset here is evenly spread.
