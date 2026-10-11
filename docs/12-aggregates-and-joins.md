# 12 — Aggregates and joins

## Aggregates and GROUP BY

Code: `HashAggregate` and `MetadataAggregate` in `mvrs_db/execution/operators.py`, `_plan_aggregate` in `mvrs_db/planner/planner.py`. Tests: `tests/integration/test_aggregates.py`.

### What they are for

An ordinary query returns rows. An aggregate query returns facts *about* rows: how many there are, their total, their average, the smallest and largest. `GROUP BY` splits the rows into groups first and reports those facts once per group.

```sql
SELECT COUNT(*), SUM(qty), AVG(price) FROM sales WHERE product = 'pen';

SELECT region, COUNT(*), SUM(qty)
FROM sales
GROUP BY region
ORDER BY SUM(qty) DESC
LIMIT 3;
```

| Function | Result | Type |
|---|---|---|
| `COUNT(*)` | Number of rows | `INTEGER` |
| `COUNT(column)` | Number of rows where the column is not NULL | `INTEGER` |
| `SUM(column)` | Total | Same as the column |
| `AVG(column)` | Total divided by the number of non-NULL values | `REAL` |
| `MIN(column)`, `MAX(column)` | Smallest, largest | Same as the column |

`SUM` and `AVG` need a numeric column. `MIN` and `MAX` work on numbers and text. `COUNT` works on anything.

The result column is named after the expression in lower case: `count(*)`, `sum(qty)`.

### The rules that surprise people

**Every selected column must be in `GROUP BY` or inside an aggregate.** `SELECT region, COUNT(*) FROM sales` is an error. A group holds many rows; if `region` is not what defines the group, there is no single value of it to show. Some databases pick an arbitrary one. This one refuses.

**NULLs are ignored by every aggregate except `COUNT(*)`.** `AVG(price)` divides by the number of prices that exist, not by the number of rows. `COUNT(price)` can therefore be smaller than `COUNT(*)`.

**An aggregate over nothing still returns one row.** With no `GROUP BY`, a query whose `WHERE` matches no rows returns a single row: `COUNT` is 0 and every other function is NULL. With `GROUP BY` it returns no rows, because there are no groups.

**In `GROUP BY`, NULLs form one group.** This is the one place SQL treats two NULLs as equal.

**`WHERE` filters rows before grouping.** There is no `HAVING` to filter groups afterwards.

**`SUM` of integers is exact.** It does not wrap around at 64 bits; the result is simply a larger integer.

### How it runs

`HashAggregate` reads its input once. For each row it builds a key from the `GROUP BY` values, looks that key up in a dictionary, and updates the group's running state: a count, a total, a smallest and a largest value. When the input ends, each dictionary entry becomes one output row of the form `(group columns..., aggregate results...)`.

Memory grows with the number of groups, not the number of rows. `ORDER BY` and `LIMIT` are applied to the output rows by the same `Sort`, `TopN` and `Limit` operators every query uses.

```
Project(product, sum(qty))
  TopN(2; sum(qty) DESC)
    HashAggregate(group by product; sum(qty))
      Filter(qty > 1)
        SeqScan(sales)
```

The alternative is **sort-based aggregation**: sort by the group columns, then walk the sorted rows, finishing a group each time the key changes. It needs no dictionary and can spill to disk when there are more groups than memory, which is why large systems keep both. It pays for a sort. Hashing is the usual choice when the groups fit in memory, the only case this engine handles.

### Answering from metadata

Three questions can be answered without reading a single row:

| Query | Answered from | Cost |
|---|---|---|
| `SELECT COUNT(*) FROM t` | The count of live slots in each heap page's header | One look at each page, no record decoded |
| `SELECT MIN(col) FROM t` | The first key of the column's B+ tree | One walk down the left edge |
| `SELECT MAX(col) FROM t` | The last key of the column's B+ tree | One walk down the right edge |

The planner uses `MetadataAggregate` when there is no `WHERE`, no `GROUP BY`, and *every* aggregate in the query is one of these (`MIN`/`MAX` only for the primary key or a `UNIQUE` column, since only those are indexed). `EXPLAIN` shows it:

```
MetadataAggregate(sales: min(id) from index edge; count(*) from heap page slot counts)
```

`MIN` and `MAX` of a nullable `UNIQUE` column are still correct this way: NULLs are not in the index, and aggregates ignore NULLs.

`COUNT(*)` is not taken from the planner's cached row count. That number is maintained for cost estimates; a query's answer should come from the stored data.

### Not built

* `HAVING`
* `DISTINCT`, including `COUNT(DISTINCT x)`
* Expressions inside or around aggregates: `SUM(price * qty)`, `SUM(a) / COUNT(*)`
* Aliases (`AS total`), so result columns are always named `sum(qty)` and the like
* Grouping by an expression
* Aggregation that spills to disk when the groups do not fit in memory

### What to be ready to explain

* Why a selected column must be in `GROUP BY` or inside an aggregate.
* Why `COUNT(*)` and `COUNT(column)` can differ, and what `AVG` divides by.
* Why an aggregate with no `GROUP BY` returns one row even from an empty table.
* Hash aggregation versus sort aggregation: what each costs and when each wins.
* How `MIN` of an indexed column can be answered in a few page reads.
* Why `HAVING` cannot be replaced by `WHERE`.

## Joins

Not built yet.
