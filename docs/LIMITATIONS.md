# Known limitations and future work

What MVRS_DB does not do, stated plainly. Each item is a real difference from a production engine.

## Not built

| Area | Missing | Notes |
|---|---|---|
| SQL | `CROSS`/`NATURAL`/`USING` joins, `HAVING`, subqueries, `DISTINCT`, `OFFSET`, `LIKE`, `IN`, `BETWEEN`, arithmetic, column aliases, views, triggers, foreign keys, `ALTER TABLE`, `CREATE INDEX` | Each is rejected with an error that names it. Inner, `LEFT`, `RIGHT` and `FULL` joins of any number of tables, each on one equality, are supported; a chain with an outer join is not reordered. Aggregates and `GROUP BY` are supported on plain columns only: no `SUM(price * qty)`. |
| Types | Booleans, dates, blobs, decimals | Three types only: `INTEGER`, `REAL`, `TEXT`. |
| Indexes | Composite keys, non-unique secondary indexes | Only the primary key and `UNIQUE` columns are indexed. |
| Concurrency | Concurrent writers, row-level locks, MVCC, savepoints | One writer at a time; locks are per table. |
| Logging | Undo logging, logical (per-operation) log records, a commit delay or timer for batching | Commits that overlap share an `fsync`, but a lone commit still pays for its own and the engine never waits to collect more. Page changes are logged as deltas where possible, but new pages, the first change to a page after a checkpoint, and changes over half a page are still logged whole. |
| B+ tree | Merging or rebalancing after deletes, reverse scans | Emptied leaves are reused by later inserts but never freed. `ORDER BY ... DESC` always sorts. |
| Storage | Records larger than a page, shrinking the file, compression, encryption | |
| Interface | Network server, prepared statements, parameter binding, streaming results | Values are written into the SQL text; results are returned as a complete list. |
| Vector search | A vector index stored in pages; filtered approximate search | Exact search and an HNSW index both work. The index is saved to a cache file on close and loaded on the next open if the table has not changed; otherwise it is rebuilt from the table on first use (about 2 to 3 ms per row). It must fit in memory, is lost to a crash (rebuilt), is approximate (87% to 97% recall at the default setting on the test datasets), and is bypassed by any `WHERE` clause. See `11-vector-search.md`. |

## Limits that will surprise you

* **A transaction cannot change more pages than the buffer pool holds** (1,024 pages by default, about 4 MB of changed pages). It fails with `TransactionTooLargeError` and is rolled back. Split the work into several transactions or open the database with a larger `buffer_pages`.
* **A failed statement can end your transaction.** If it had already changed something, the whole transaction is rolled back.
* **A commit is visible to others slightly before it is durable**, and group commit makes a commit's latency depend on the batch it joins: with 16 busy threads the median durable commit measured 46 ms against 23 ms for one thread, in exchange for 8 times the throughput.
* **After a log write failure the whole engine stops, reads included,** until the database is reopened.
* **Every table needs exactly one single-column primary key.**
* **Text ordering is by Unicode code point.** `'Z' < 'a'`, and accented letters sort after all unaccented ones.
* **An index key is at most 255 bytes**, so a `TEXT` primary key or unique value longer than that is rejected.
* **`SELECT` without `ORDER BY` returns rows in storage order**, which changes as rows are updated and deleted.
* **Keywords are reserved.** A column cannot be called `index`, `count`, `sum`, `min`, `max`, `group`, `vector`, `date`, `key`, and so on.
* **A grouped query keeps every group in memory, and a hash join keeps its smaller input in memory.** There is no spilling to disk.
* **A join's row estimate is a guess when neither join column is unique**, since there are no column statistics. Join order for an inner chain is chosen greedily from those estimates and can be wrong.
* **Only one process can open a database**, and only once.

## Durability caveats

* Crash safety is tested against **process death**, not power loss. `fsync` is called at every commit; whether the operating system and the drive honour it cannot be tested from here.
* Damage in the **middle** of the log ends the log there. Later committed transactions are lost, because a damaged record looks the same as a torn tail.
* A page that is damaged on disk **after** it was checkpointed cannot be repaired. It is detected by its checksum when read, and by `Database.check()`.
* The file header and the log header are assumed to be written atomically (each fits in one 512-byte sector).
* There is no backup tool. Copying the `.mvrs` and `.mvrs.wal` files together while the database is closed is a valid backup.

## Performance

It is slow. See `benchmarks/results/REPORT.md` for measurements and the list of known bottlenecks. The largest are the cost of an `fsync` per durable commit (shared between overlapping commits, not removed), row-at-a-time execution in Python, and parsing SQL text on every call.

The optimiser assumes values are spread evenly between the smallest and largest key. On skewed data its row estimates, and therefore its choices, can be wrong.

## Future work, in the order I would do it

1. **Prepared statements with parameters.** Removes parsing from the hot path and removes the need to build SQL strings.
3. **Non-unique secondary indexes**, by appending the record ID to the key.
4. **ARIES-style logging** (steal, undo, compensation records), which is what unlocks concurrent writers and transactions larger than memory.
5. **MVCC** on top of that, so readers never block.
