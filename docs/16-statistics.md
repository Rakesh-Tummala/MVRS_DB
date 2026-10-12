# 16 — Column statistics and `ANALYZE`

Code: `mvrs_db/catalog/stats.py` (collecting, estimating, storing), `Catalog.analyze` and the loading of statistics in `mvrs_db/catalog/catalog.py`, the estimate functions in `mvrs_db/planner/optimizer.py`, and their uses in `mvrs_db/planner/planner.py` (`extract_ranges`, `joined_estimate`, `_group_estimate`). Tests: `tests/unit/test_column_stats.py`, `tests/integration/test_statistics.py`, the `indexed` crash workload.

```sql
ANALYZE;            -- every table
ANALYZE orders;     -- one table
```

## What it is for

The planner chooses between plans by estimating how many rows each step produces. Before this, apart from a unique key it had only fixed guesses: an equality matches a tenth of the table, a range a third, `IS NULL` a tenth. Real data is not like that. In an orders table where 93% of rows are `status = 'done'`:

| Query | Guess without statistics | With statistics | Truth |
|---|---|---|---|
| `status = 'done'` | 500 of 5,000 | 4,644 | 4,644 |
| `status = 'refunded'` | 500 | 76 | 76 |
| `note IS NULL` | 500 | 3,750 | 3,750 |
| `amount > 130` | 1,667 | 752 | 744 |

(The planner's row estimates in `EXPLAIN` against the real counts, on the table `test_statistics.py` builds: 5,000 rows, one run.) A plan built on a wrong estimate is not wrong, only slow: it may use an index to fetch most of the table, or hash the bigger side of a join.

## What is collected

For each column (not vectors), by one full scan:

| | |
|---|---|
| `rows`, `nulls` | live rows and how many are NULL |
| `distinct` | different non-NULL values |
| `common` | up to ten **most common values with their exact counts** |
| `bounds` | an **equi-depth histogram** of all the other values: 21 ascending values cutting them into 20 buckets that each hold the same number of rows |

### Why two structures

A histogram is bad at spikes. If one value fills 40% of the table, every bucket boundary lands on it, and the histogram cannot say how many rows it has. Listing the few values that stand out, exactly, and spreading the remainder over equal-depth buckets, handles skew with a few hundred bytes. A value counts as common if it occurs at least twice and more than 1.25 times as often as an average value. A column with ten or fewer distinct values lists all of them and has no histogram, since a histogram of a handful of values is all duplicate bounds (the first version had this flaw; see below).

### Estimates from them

* **Equality** with a known value: exact if it is in the common list, otherwise the rows not covered by the list, divided evenly among the distinct values not in it.
* **Equality with a value not known yet (`?`)**: the average, `(rows − nulls) / distinct`, the same for every value. That sameness is what lets a plan built for a placeholder be reused (below).
* **A range**: common values inside it, counted exactly, plus the share of the histogram inside it, interpolating linearly inside a bucket for numbers and taking half a bucket for text.
* **`!=`, `IS NULL`, `IS NOT NULL`**: from the NULL fraction and the equality estimate.
* **A join on non-unique columns**: `|A| × |B| / max(distinct_A, distinct_B)`, each key value appearing about `rows / distinct` times on each side. Without statistics the planner still guesses the smaller input.
* **`GROUP BY`**: the product of the distinct counts (plus one per column that has NULLs), never more than the input rows.

What these assume: values outside the common list are evenly spread; columns are independent (conditions on two columns multiply); and the numbers are a **snapshot**. Inserts, updates and deletes do not change them. Run `ANALYZE` again after the data has changed a lot, or leave it to the engine (next section).

## Automatic `ANALYZE`

By default (`mvrs_db.connect(path, auto_analyze=True)`) the engine refreshes a table's statistics itself once enough of its rows have changed. This is the rule PostgreSQL's autovacuum uses: **50 changed rows plus 10% of the rows the table had when it was last analysed** (zero, for a table never analysed). Both numbers are in `config.py`.

* **What counts as a change.** Each row inserted, deleted, or actually modified by an `UPDATE` counts one (an update that leaves the row as it was, or matches nothing, counts nothing). A statement that fails counts nothing. Changes are tallied in the catalog and added to a table's count only when the transaction **commits**; a rolled-back transaction contributes nothing and, because the catalog is reloaded after a rollback, the tally lives in the catalog object itself and survives that reload.
* **When it runs.** Right after the commit that tipped the table over, in a transaction of its own, after the committing transaction has released its locks (and waited for its commit to be durable). That one statement pays for the analysis; readers and other connections do not. Plain reads never trigger it, since a read must not write.
* **If it cannot run.** It needs the writer slot and an exclusive lock on the table. If that times out, deadlocks, or the transaction would be too large, the attempt is dropped without an error: the user's statement has already committed, the table stays stale, and the next commit that touches it tries again.
* **What it costs.** An analysis reads the whole table, so one insert in a large table can take much longer than the others. The 10% rule keeps the total bounded: each analysis happens after the table has grown by a tenth, so the work over a bulk load is a small multiple of one scan. Measured once, loading 20,000 rows in 200 inserts of 100 rows (32 automatic analyses): 1.4 and 1.8 s without it, 2.2 and 2.1 s with it; the slowest single insert went from 11 and 14 ms to 76 and 61 ms. Single runs on a noisy machine.
* **After a restart** the tally starts from zero: nothing records how many rows changed before the database was last closed, so a table that was never analysed is analysed after its first 50 changes, and one that was is analysed when it has changed by 10% from then on.
* **Switching it off** (`auto_analyze=False`) restores the old behaviour: statistics change only when `ANALYZE` is run. The existing tests that count commits, or that need a table without statistics, turn it off for that reason; so do the crash workloads whose crash points are counted on a fixed number of commits, except one workload (`autoanalyze`) that runs with it on and is killed at every point.

## Where they are stored

Each column's statistics are one record in the **catalog heap**, next to the table definitions, with a different record format (2; tables are 1). `ANALYZE` is an ordinary statement: it inserts or replaces those records inside its transaction, so it is logged, undone by `ROLLBACK`, and cannot be half done after a crash. `DROP TABLE` deletes the table's records. On open the catalog is read once, and a record that names a missing table or column, has the wrong type, or appears twice is reported as corruption (invariant C5). A record too big for a small page is thinned (fewer buckets, fewer common values), which makes estimates coarser, never wrong.

Alternatives: a separate statistics file (like the vector index's cache, but that is valid only until the data changes, and statistics are meant to outlive small changes); keeping them in memory only (lost on every rollback reload and every open, so each would need a scan). The catalog heap gives durability and rollback for free.

A database that has statistics records cannot be opened by a version of the engine from before this change (it would not recognise record format 2). Databases without them are unchanged.

## How the planner uses them

* **Index or scan.** `range_selectivity` and `default_selectivity` ask the statistics first. The example in the measurement below is the main effect: a common value is scanned, a rare one uses the index.
* **A repeated-value index with a `?`.** Before, a plan for `WHERE indexed_col = ?` could not be cached, because how many rows a value has changes the best plan. With statistics every value is given the average share, so the plan no longer depends on the value and is cached (`extract_ranges` refuses only when a repeated-value column has no statistics). The price is the usual one for generic plans: for a very common value the cached plan may be the index scan an exact estimate would have rejected. Ranges with a `?` are still refused.
* **Join size, join order, and which side builds the hash table** all start from the estimates above.
* **Plans are rebuilt after `ANALYZE`**: it increases the catalog's epoch, which discards every cached plan (`14-plan-cache.md`).

## Measured

One run, 20,000 rows, 93% of them `status = 'done'`, an index on `status`, `WHERE status = ? AND region = 7`:

| | before `ANALYZE` | after |
|---|---|---|
| common value (`'done'`) | index scan, 85.1 ms | sequential scan, 42.4 ms |
| rare value (`'refunded'`) | index scan, 1.5 ms | index scan, 1.4 ms |
| `ANALYZE` itself, three columns | | 66 ms |

The common-value query was twice as slow because it fetched nine tenths of the table row by row through the index. Single runs; differences under about 10% are noise.

## How it is tested

* **Collecting** (`test_column_stats.py`): exact counts; empty and all-NULL columns; one distinct value; skew puts the hot values in the common list with exact counts; at most ten, the most common ones; histograms are ascending, with 21 bounds for 20 buckets, and equi-depth; long text is clipped.
* **Estimating**: for uniform, heavy-tailed, bell-shaped and four-valued data with NULLs, 300 random ranges all land within 0.09 of the true fraction, a common value's equality is exact and any other is within 0.05; a wider range never selects less than a narrower one (a property test).
* **Storing**: random statistics round-trip through the record format; every truncation of a record is refused; flipped bits are refused or decode to something consistent; trailing bytes are refused; `fit` thins a record to any limit and the result still decodes with the same totals.
* **Persistence**: survive reopening, `ANALYZE` again replaces rather than duplicates, rollback undoes it, `DROP TABLE` removes the records and leaves no leaked page, `CREATE`/`DROP INDEX` keep them, a snapshot reader plans with the statistics of its snapshot.
* **Planner effects**: estimates for eight conditions end within a factor of two of the truth (from clearly worse); a common value scans and a rare one uses the index; `GROUP BY` estimates equal the distinct counts (including the NULL group); a join estimate on repeated keys is within 5% of the true 20,000 (and ten times closer than before); a `?` on a repeated-value index is refused before `ANALYZE` and cached after, with correct answers for every value including NULL.
* **Answers never change**: 120 random queries (every comparison, grouping) return the same rows before and after `ANALYZE`; a cached plan is replaced after it.
* **Crash recovery**: the `indexed` crash workload runs `ANALYZE` inside some of its transactions and is killed at every crash point; the database must reopen (which checks C5) and agree with the model.
* **Automatic analysis** (`test_auto_analyze.py`): the 50-change threshold for a new table and 50 plus 10% for an analysed one, to the exact change; every kind of change counts once per row; updates that change nothing, failed statements and rolled-back transactions count nothing, while earlier commits are not forgotten across a rollback reload; a transaction counts when it commits, not before; it can be switched off; a refused analysis leaves the statement committed and is retried; no transaction is left open; the planner gets statistics without being asked (a common value stops using the index); statistics survive reopening; cached plans are replaced when statistics arrive; it works under snapshot isolation; a 20,000-row load is analysed a few dozen times, not on every commit; six threads inserting into two tables do not deadlock or corrupt anything. Six deliberate bugs were each caught (not counting updates, counting rolled-back changes, not restarting the count after an analysis, ignoring the 10%, not accumulating across commits, letting a refused analysis fail the statement). One more, keeping a dropped table's count, cannot be detected: table ids are not reused within a run, so it only leaks a dictionary entry. The `autoanalyze` crash workload is killed at every crash point and must recover to the model, with a check that the engine really analysed during a clean run.
* **Six deliberate bugs** were each caught: ignoring statistics in estimates, ignoring distinct counts for joins, keeping records after `DROP TABLE`, caching a repeated-index plan without statistics, not loading statistics on open, and ignoring distinct counts for grouping.

### Two flaws the tests found in the first version

* A column with one distinct value estimated **zero** rows for a range containing it, because the value was in neither the common list nor a histogram (a histogram needs two values).
* A column with a few distinct values had a histogram of repeated bounds, and range estimates were off by 0.24.

Both are fixed by listing every value when there are ten or fewer, and by never making more buckets than there are gaps between distinct values.

## Not built

* **Statistics that stay fresh between analyses.** Automatic `ANALYZE` refreshes them when 10% of the rows have changed, so for up to that much change the planner works from slightly old numbers, and an analysis rereads the whole table. Maintaining them incrementally, or analysing a sample, are the ways to avoid that.
* **Analysis outside the writing statement.** It runs in the committing thread; a background worker would move the cost away from the statement that triggers it, at the price of a thread and a way to test it deterministically.
* **Sampling.** `ANALYZE` reads every row and holds each column's distinct values in memory, so it is exact but costs memory proportional to the number of distinct values.
* **Correlation between columns** (multi-column statistics), so `city = 'Pune' AND country = 'India'` is estimated as if independent.
* **A histogram for joins**: the join estimate uses only distinct counts, not whether the two columns' value ranges overlap.
* **Reusing a plan for a range with a `?`.** Statistics give the means to estimate it, but a generic plan for ranges can be far worse for the extremes, so the decision is left for later.

## What to be ready to explain

* What a histogram is bad at, and why a list of common values fixes it.
* Why a `?` gets the average share, and why that makes a plan cacheable and sometimes worse.
* How a range estimate is made from the common list and the histogram.
* How the join estimate `|A||B| / max(distinct)` comes from the assumption of uniformity, and when it is wrong.
* Why statistics are stored in the catalog heap, and what that gives for free.
* Why the estimates can be wrong without making any query's answer wrong.
* Which assumptions fail on real data (independence, staleness) and what would reduce the damage.
