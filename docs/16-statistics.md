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

What these assume: values outside the common list are evenly spread; columns are independent (conditions on two columns multiply); and the numbers are a **snapshot**. Inserts, updates and deletes do not change them. Run `ANALYZE` again after the data has changed a lot; nothing runs it automatically.

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
* **Six deliberate bugs** were each caught: ignoring statistics in estimates, ignoring distinct counts for joins, keeping records after `DROP TABLE`, caching a repeated-index plan without statistics, not loading statistics on open, and ignoring distinct counts for grouping.

### Two flaws the tests found in the first version

* A column with one distinct value estimated **zero** rows for a range containing it, because the value was in neither the common list nor a histogram (a histogram needs two values).
* A column with a few distinct values had a histogram of repeated bounds, and range estimates were off by 0.24.

Both are fixed by listing every value when there are ten or fewer, and by never making more buckets than there are gaps between distinct values.

## Not built

* **Automatic `ANALYZE`**, or noticing that statistics are stale. A plan made from statistics describing a table ten times smaller is still used.
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
