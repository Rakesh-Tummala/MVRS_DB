# Known limitations and future work

What MVRS_DB does not do, stated plainly. Each item is a real difference from a production engine.

## Not built

| Area | Missing | Notes |
|---|---|---|
| SQL | Outer joins, joins of three or more tables, `HAVING`, subqueries, `DISTINCT`, `OFFSET`, `LIKE`, `IN`, `BETWEEN`, arithmetic, column aliases, views, triggers, foreign keys, `ALTER TABLE`, `CREATE INDEX` | Each is rejected with an error that names it. Inner joins of two tables on one equality are supported. Aggregates and `GROUP BY` are supported on plain columns only: no `SUM(price * qty)`. |
| Types | Booleans, dates, blobs, decimals | Three types only: `INTEGER`, `REAL`, `TEXT`. |
| Indexes | Composite keys, non-unique secondary indexes | Only the primary key and `UNIQUE` columns are indexed. |
| Concurrency | Concurrent writers, row-level locks, MVCC, savepoints | One writer at a time; locks are per table. |
| Logging | Delta log records, group commit, undo logging | Every commit logs whole pages and does its own `fsync`. |
| B+ tree | Merging or rebalancing after deletes, reverse scans | Emptied leaves are reused by later inserts but never freed. `ORDER BY ... DESC` always sorts. |
| Storage | Records larger than a page, shrinking the file, compression, encryption | |
| Interface | Network server, prepared statements, parameter binding, streaming results | Values are written into the SQL text; results are returned as a complete list. |
| Vector search | A persistent vector index; filtered approximate search | Exact search and an in-memory HNSW index both work. The index is rebuilt from the table on first use after every open (about 2 to 3 ms per row), is approximate (87% to 97% recall at the default setting on the test datasets), and is bypassed by any `WHERE` clause. See `11-vector-search.md`. |

## Limits that will surprise you

* **A transaction cannot change more pages than the buffer pool holds** (1,024 pages by default, about 4 MB of changed pages). It fails with `TransactionTooLargeError` and is rolled back. Split the work into several transactions or open the database with a larger `buffer_pages`.
* **A failed statement can end your transaction.** If it had already changed something, the whole transaction is rolled back.
* **Every table needs exactly one single-column primary key.**
* **Text ordering is by Unicode code point.** `'Z' < 'a'`, and accented letters sort after all unaccented ones.
* **An index key is at most 255 bytes**, so a `TEXT` primary key or unique value longer than that is rejected.
* **`SELECT` without `ORDER BY` returns rows in storage order**, which changes as rows are updated and deleted.
* **Keywords are reserved.** A column cannot be called `index`, `count`, `sum`, `min`, `max`, `group`, `vector`, `date`, `key`, and so on.
* **A grouped query keeps every group in memory, and a hash join keeps its smaller input in memory.** There is no spilling to disk.
* **A join's row estimate is a guess when neither join column is unique**, since there are no column statistics.
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
4. **Outer joins and joins of more than two tables**, which bring join-order optimisation with them.
5. **Non-unique secondary indexes**, by appending the record ID to the key.
6. **ARIES-style logging** (steal, undo, compensation records), which is what unlocks concurrent writers and transactions larger than memory.
7. **MVCC** on top of that, so readers never block.
