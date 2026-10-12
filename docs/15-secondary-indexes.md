# 15 — Secondary (non-unique) indexes

Code: `secondary_prefix`, `secondary_key`, `decode_secondary_value` in `mvrs_db/index/keys.py`; `Table.is_unique`, `index_key`, `index_value`, `count_equal` and `Catalog.create_index` / `drop_index` in `mvrs_db/catalog/catalog.py`; `IndexScan._secondary_rows` in `mvrs_db/execution/operators.py`; the maintenance in `_insert_row`, `update` and `delete` in `mvrs_db/execution/executor.py`. Tests: `tests/unit/test_secondary_keys.py`, `tests/integration/test_secondary_indexes.py`, the `indexed` workload in `tests/crash_recovery/`.

```sql
CREATE INDEX by_city ON customers (city);
SELECT name FROM customers WHERE city = 'pune';     -- uses the index
DROP INDEX by_city;
```

## What it is for

Until now only a primary key or a `UNIQUE` column could be indexed. Any other lookup (`WHERE city = 'pune'`, `WHERE price < 5`) read the whole table. A secondary index lets a column hold the same value in many rows and still be searched directly. On 20,000 rows (one run, 20 matching rows each): 47.3 ms per lookup by scanning, 0.51 ms through the index.

## The one idea: make the key longer

The B+ tree stores each key once and maps it to one record ID (`09-btree.md`). Rather than teach it to hold duplicates, a secondary index makes every key unique by appending the row's record ID:

```
key = (the value, encoded as usual) + (page number, slot number)
```

Two rows with the value `pune` get different keys, and all of them sit next to each other in the tree, ordered by record ID. "Every row with city `pune`" is a range scan from `prefix('pune')` to `prefix('pune') + FF FF FF FF FF FF`, where the six bytes are the largest possible record ID. `>`, `>=`, `<`, `<=` are the same trick with the bounds moved to the start or the end of that run: `> v` starts just past v's run, `<= v` ends at its end.

Alternatives: a tree whose leaves hold a list of record IDs per key (compact when values repeat a lot, but a hot value makes one huge cell and every insert rewrites it); or an overflow chain per key. The suffix approach reuses the tree, its in-place edits, its splits, and its recovery unchanged.

### One value's run must not touch another's

For this to work, the bytes of one value must never be the start of another value's bytes. INTEGER and REAL keys are always 8 bytes, so they are safe. TEXT is not: `ab` is the start of `abc`, and the record ID bytes after `ab` would be read as the next characters of a longer value. So a TEXT prefix is made self-delimiting:

* the end of the text is the two bytes `00 00`;
* a NUL character inside the text is written `00 FF`, so it cannot look like the end.

Order is preserved (after a shared start, `ab` continues with `00 00` and any longer text with something above that). The property test `test_a_value_owns_a_contiguous_run_of_keys` checks that for random text, every key of a value lies inside its run and no other value's key does.

### Limits this gives

A key length is one byte in the tree node, so a key is at most 255 bytes. A secondary key is the prefix plus 6, so TEXT in a secondary index can be at most 247 bytes (fewer if it contains NUL characters). A longer value fails the `INSERT`, `UPDATE` or `CREATE INDEX` with `KeyTooLargeError` before anything changes. Looking such a value up is fine: it matches nothing. (A primary-key or `UNIQUE` text key can still be up to 255 bytes.)

## What is stored where

* The index is a B+ tree like any other. The catalog record of the table (`03-storage-format.md`) marks the column with flag bit 5 and stores the index's **name** after the column. Older records have no such bit and read unchanged.
* `Column.index_root` is the tree's root; `Column.index_name` is non-empty only for secondary indexes. A column has at most one index: a `PRIMARY KEY` or `UNIQUE` column cannot also get a secondary one.
* NULLs are not indexed (as for `UNIQUE`), so `WHERE col IS NULL` still reads the table.
* Index names are unique across the whole database, not per table.

## Keeping it right

* **Insert**: the value is checked to fit a key first, then the row is stored, then `(value, rid)` is added.
* **Update**: if the value changed *or the row moved to another page* (its record ID changed), the old key is deleted and the new one inserted. The second case is easy to forget and has a test (`test_a_row_that_moves_keeps_its_index_entry`).
* **Delete**: the key is removed.
* **Rollback** and **recovery** need nothing new: the index is pages, logged like all pages.
* **`Database.check()`** now compares each index with the rows as a set of (key, record ID): same entries, none extra, none missing (invariant C3). A secondary index has no uniqueness rule, so C4 applies only to unique indexes.

## `CREATE INDEX` and `DROP INDEX`

* `CREATE INDEX` allocates a new tree, fills it by scanning the heap, rewrites the table's catalog record with the new column fields, and swaps in a new `Table` object, all inside the statement's transaction. Rolling back, or any failure (such as a too-long value in the table), removes it again. The statement takes an exclusive lock on the table.
* Because a transaction may change only as many pages as the buffer pool holds (see LIMITATIONS), the table an index can be built on in one statement is bounded by that. Measured with the default 1,024-frame pool and an INTEGER column: 100,000 rows built in 2.9 s, 160,000 failed with `TransactionTooLargeError` (a larger `buffer_pages` raises the limit; longer TEXT keys lower it).
* `DROP INDEX` frees the tree's pages and rewrites the record. `DROP TABLE` frees all of a table's indexes with it.

## How the planner uses it

* A secondary index is an ordinary candidate in `plan_access`: a range or equality on the column becomes an `IndexScan`, costed like the others, and the `Filter` above it still checks every row.
* **Estimating how many rows match.** A unique equality matches at most one row. A repeated-value equality has no statistics to go on, so the planner counts the entries in the index for that value, up to 64. Under 64 it knows the exact number; at 64 or more it assumes the value is common (a tenth of the table, at least 64). This costs at most 64 key reads at planning time, and it is what makes a rare value use the index and a common one scan.
* **Ranges** use the existing interpolation between the smallest and largest key, now decoding the value out of the longer key.
* **`MIN`/`MAX`** with no `WHERE` read the edge of the index, as for a unique one.
* **Plan cache.** A cached plan cannot depend on the value, but how many rows a value has does. So `col = ?` on a repeated-value index is refused by the planner (`PlanNotReusable`) and planned on each execution, like a range, until `ANALYZE` has given the column statistics: then every value is given the average share and the plan is reused. A primary-key lookup is always cached.
* **Joins.** A secondary index is not used for an index join: that operator looks up at most one match per outer row, which only unique indexes guarantee. Joining through a secondary index is a possible extension.

## How it is tested

* **Key order.** For random integers, reals and text with random record IDs, the order of the keys equals the order of (value, record ID); a value's run contains exactly its own keys; values round-trip through decode.
* **Same answers with and without an index.** Twenty-three query shapes (every comparison, `IS NULL`, `OR`, `ORDER BY`, `LIMIT`, `GROUP BY`, `MIN`/`MAX`, three column types) return the same rows before and after `CREATE INDEX`. A randomized test runs 220 random inserts, updates, deletes and queries against two identical tables, one indexed on all three columns, and requires equal results every time and a clean `check()` at the end.
* **The scan itself is exact.** The `Filter` above an index scan hides a range that is too wide (the rows are discarded and the answer is right). One test therefore counts the rows the `IndexScan` returns and requires exactly the matching ones, for every operator and at the edges of the data.
* **DDL and rollback.** Duplicate names, double indexing, vectors, unknown columns; `DROP INDEX` leaves no leaked page; `CREATE INDEX` rolled back or failed midway leaves nothing; the index survives reopening.
* **Crash recovery.** A third crash workload (`indexed`) moves rows between groups of a secondary index, grows rows so they move between pages, and deletes; it is killed at every crash point the other workloads use and, after recovery, `check()` and a query through the index must agree with the model. The concurrent crash test also keeps a secondary index.
* **Seven deliberate bugs** were each caught: the end of a `<=` range, the start of a `>` range (caught only by the exact-scan test), a stale entry left by `UPDATE`, one left by `DELETE`, TEXT keys without a terminator, no plan invalidation on `CREATE INDEX`, and caching a plan for a repeated-value parameter.

## Measured

One run, 20,000 rows with 1,000 distinct values of the indexed column, synchronous off:

| | without | with |
|---|---|---|
| `WHERE g = ?` (20 rows) | 47.3 ms | 0.51 ms |
| `WHERE g > 995` (80 rows) | 47.5 ms | 0.83 ms |
| `CREATE INDEX` | | 476 ms |
| insert 5,000 rows | 369 ms | 664 ms with two secondary indexes |

So an index costs roughly 30 microseconds per row per index on insert here, and pays for itself when reads are selective.

## Not built

* Composite (multi-column) indexes, unique indexes through `CREATE INDEX` (use `UNIQUE`), partial and expression indexes, `IF NOT EXISTS`.
* Index joins through a secondary index, and index-only scans.
* Reusing a plan across values for a repeated-value index *before* `ANALYZE`. After it, statistics give every value the average share, and such a plan is reused (`16-statistics.md`).
* Rebuilding an index after many deletes: deleted keys leave empty space, as for the other trees.
* A `CREATE INDEX` larger than one transaction can hold.

## What to be ready to explain

* Why appending the record ID makes a unique tree hold repeated values, and how a lookup of one value becomes a range scan.
* Why TEXT needed a terminator and an escape for NUL, and what goes wrong without them.
* Why an `UPDATE` must touch the index when only the row's location changed.
* How the planner estimates matches for a value it has no statistics on, and why a placeholder makes that impossible to cache.
* Why the `Filter` above an index scan can hide a bug in the scan, and how the test closes that gap.
* What `CREATE INDEX` does inside a transaction, and what bounds the size it can handle.
