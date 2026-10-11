# Known limitations and future work

What MVRS_DB does not do, stated plainly. Each item is a real difference from a production engine.

## Not built

| Area | Missing | Notes |
|---|---|---|
| SQL | Joins, aggregates (`COUNT`, `SUM`, ...), `GROUP BY`, subqueries, `DISTINCT`, `OFFSET`, `LIKE`, `IN`, `BETWEEN`, arithmetic, aliases, views, triggers, foreign keys, `ALTER TABLE`, `CREATE INDEX` | Each is rejected with an error that names it. Inner joins were a stretch goal and were not attempted. |
| Types | Booleans, dates, blobs, decimals | Three types only: `INTEGER`, `REAL`, `TEXT`. |
| Indexes | Composite keys, non-unique secondary indexes | Only the primary key and `UNIQUE` columns are indexed. |
| Concurrency | Concurrent writers, row-level locks, MVCC, savepoints | One writer at a time; locks are per table. |
| Logging | Delta log records, group commit, undo logging | Every commit logs whole pages and does its own `fsync`. |
| B+ tree | Merging or rebalancing after deletes, reverse scans | Emptied leaves are reused by later inserts but never freed. `ORDER BY ... DESC` always sorts. |
| Storage | Records larger than a page, shrinking the file, compression, encryption | |
| Interface | Network server, prepared statements, parameter binding, streaming results | Values are written into the SQL text; results are returned as a complete list. |
| Vector search | An approximate index (HNSW or similar) | Exact search works: `ORDER BY v <-> [..] LIMIT k`. It measures every row, so it is linear in table size. A query with a `WHERE` clause takes a slower path. See `11-vector-search.md`. |

## Limits that will surprise you

* **A transaction cannot change more pages than the buffer pool holds** (1,024 pages by default, about 4 MB of changed pages). It fails with `TransactionTooLargeError` and is rolled back. Split the work into several transactions or open the database with a larger `buffer_pages`.
* **A failed statement can end your transaction.** If it had already changed something, the whole transaction is rolled back.
* **Every table needs exactly one single-column primary key.**
* **Text ordering is by Unicode code point.** `'Z' < 'a'`, and accented letters sort after all unaccented ones.
* **An index key is at most 255 bytes**, so a `TEXT` primary key or unique value longer than that is rejected.
* **`SELECT` without `ORDER BY` returns rows in storage order**, which changes as rows are updated and deleted.
* **Keywords are reserved.** A column cannot be called `index`, `count`, `date`, `key`, and so on.
* **Only one process can open a database**, and only once.

## Durability caveats

* Crash safety is tested against **process death**, not power loss. `fsync` is called at every commit; whether the operating system and the drive honour it cannot be tested from here.
* Damage in the **middle** of the log ends the log there. Later committed transactions are lost, because a damaged record looks the same as a torn tail.
* A page that is damaged on disk **after** it was checkpointed cannot be repaired. It is detected by its checksum when read, and by `Database.check()`.
* The file header and the log header are assumed to be written atomically (each fits in one 512-byte sector).
* There is no backup tool. Copying the `.mvrs` and `.mvrs.wal` files together while the database is closed is a valid backup.

## Performance

It is slow. See `benchmarks/results/REPORT.md` for measurements and the list of known bottlenecks. The largest are whole-page logging, one `fsync` per commit, re-encoding B+ tree nodes on every change, row-at-a-time execution in Python, and parsing SQL text on every call.

The optimiser assumes values are spread evenly between the smallest and largest key. On skewed data its row estimates, and therefore its choices, can be wrong.

## Future work, in the order I would do it

1. **Delta log records.** The biggest single win for write throughput and log size.
2. **Group commit.** One `fsync` for several waiting transactions.
3. **Prepared statements with parameters.** Removes parsing from the hot path and removes the need to build SQL strings.
4. **Aggregates and inner joins.** The operator model already supports adding them.
5. **Non-unique secondary indexes**, by appending the record ID to the key.
6. **ARIES-style logging** (steal, undo, compensation records), which is what unlocks concurrent writers and transactions larger than memory.
7. **MVCC** on top of that, so readers never block.
