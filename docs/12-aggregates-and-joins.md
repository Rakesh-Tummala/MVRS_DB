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

Code: `mvrs_db/planner/scope.py` (name resolution), `_plan_join` in `mvrs_db/planner/planner.py`, `HashJoin` and `IndexJoin` in `mvrs_db/execution/operators.py`. Tests: `tests/integration/test_joins.py`. Measurement: `benchmarks/join_methods.py`.

### What they are for

Data is split across tables so that each fact is stored once: a customer's name lives in `customers`, not on every order. A join puts the pieces back together for a query, pairing each row of one table with the rows of another that share a value.

```sql
SELECT o.id, c.name, o.total
FROM orders o JOIN customers c ON o.customer_id = c.id
WHERE c.city = 'pune' AND o.total > 10
ORDER BY o.total DESC
LIMIT 5;
```

This is an **inner** join: a row appears only if it has a partner. An order whose `customer_id` is NULL, or points at no customer, is left out; so is a customer with no orders.

### What is supported

* Exactly two tables, joined with `JOIN` or `INNER JOIN`.
* An `ON` condition that is one equality between a column of each table.
* Table aliases (`orders o`) and qualified names (`o.total`, or `orders.total` when there is no alias).
* A bare column name, when only one of the tables has a column of that name. A name both tables have (`id`) must be qualified; the error says how.
* `WHERE`, aggregates, `GROUP BY`, `ORDER BY` and `LIMIT`, all applied to the joined rows.
* A table joined to itself, with two aliases.

`SELECT *` returns every column of the first table, then every column of the second, named `alias.column`. Otherwise result columns are named as the query wrote them.

Qualified names and aliases also work in a query on one table.

### NULL never matches

`NULL = NULL` is not true in SQL, so a NULL join value pairs with nothing, including another NULL. Both join operators drop such rows before comparing.

### Three steps of planning

**1. Resolve names.** `scope.py` works out which table each column name refers to and rewrites every name into one form: `alias.column` for a join, the bare column name for a single table. Errors that depend on the whole query are caught here: an unknown qualifier, a column neither table has, an ambiguous bare name. After this step the rest of the planner never looks at a qualifier.

The joined row is the first table's columns followed by the second's. The planner describes it with a combined schema, and from then on the existing filter, sort, aggregate and projection code treats a join exactly like a table.

**2. Push filters down.** `WHERE` is split at its top-level `AND`s. A condition that mentions only one table is applied while that table is read, *below* the join. A condition that needs both (`o.city = c.city`) is applied to the joined rows, above it.

```
Project(name)
  HashJoin(orders.customer_id = customers.id; hash table on customers)
    Filter(id = 12)
      IndexScan(orders.id: id = 12)        <- orders' own index is used
    Filter(city = 'delhi')
      SeqScan(customers)
```

This matters twice over: fewer rows reach the join, and each table's own index can be used for its condition. Filtering after the join would give the same answer and do far more work.

Pushing down is only safe because this is an inner join. For an outer join, moving a condition below the join can change the result.

**3. Choose the join method.**

| | Hash join | Index join |
|---|---|---|
| How | Read one input fully into a dictionary keyed by the join value; read the other and look each row up | For each row of one input, look the join value up in the other table's B+ tree and fetch the row |
| Cost | About rows(A) + rows(B) | About rows(outer) × (a tree descent + a row fetch) |
| Memory | The smaller input, held in a dictionary | None |
| Needs | Nothing | The inner join column is a primary key or `UNIQUE` column of the same type |
| Wins when | Both inputs are large | The outer input is small |

Both are far better than the naive method, comparing every pair, which costs rows(A) × rows(B).

The planner estimates both and takes the cheaper. Measured on the development machine (`emp` restricted to its first N rows, joined to a 20,000-row `dept` on its primary key):

| Outer rows | Hash join | Index join | Faster | Planner chose |
|---|---|---|---|---|
| 10 | 41.0 ms | 0.46 ms | index | index |
| 100 | 41.3 ms | 2.29 ms | index | index |
| 1,000 | 43.2 ms | 23.3 ms | index | index |
| 3,000 | 57.0 ms | 69.1 ms | hash | hash |
| 10,000 | 92.0 ms | 235 ms | hash | hash |
| 25,000 | 190 ms | 750 ms | hash | hash |
| 50,000 | 213 ms | 1,042 ms | hash | hash |

The hash join's time barely moves for small N because it always reads all of `dept`. The index join grows in step with N at about 20 microseconds per outer row. They cross between 1,000 and 3,000 rows here.

The cost constants (`HASH_ROW`, `INDEX_LOOKUP`, `INDEX_LEVEL` in `optimizer.py`) were set from this measurement. Before they were, the planner picked the index join at 3,000 rows, where it is the slower method.

On very small tables the hash join is chosen even when an index exists, and that is correct: scanning four rows costs less than one tree descent.

### How many rows a join produces

The planner needs this estimate for the operators above the join. If the join column is unique on one side, each row of the other side matches at most one row, so the estimate is rows(A) × rows(B) ÷ rows in the unique table. With no unique side there are no statistics to go on and the estimate is the smaller input's row count, which can be badly wrong.

### Locking

A join takes a shared lock on both tables, in name order. A fixed order means two joins over the same pair of tables can never each hold one lock while waiting for the other.

### Not built

* Outer joins (`LEFT`, `RIGHT`, `FULL`), `CROSS JOIN`, `NATURAL`, `USING`
* More than two tables
* Join conditions other than one equality (`ON a.x = b.x AND a.y = b.y`, `ON a.x < b.y`). Extra conditions go in `WHERE`, where they give the same result for an inner join.
* Merge join, and any join that spills to disk when the hash table does not fit in memory
* Join-order optimisation, which only matters with three or more tables
* Vector distance in a join query
* Column aliases (`AS`)

### What to be ready to explain

* What an inner join leaves out, and how a NULL key behaves.
* Why comparing every pair is quadratic, and how hashing and an index each avoid it.
* When the index join beats the hash join, with the measured crossover, and why.
* What predicate pushdown is, why it helps, and why it is unsafe for outer joins.
* Why the hash table is built on the smaller input.
* How name resolution finds an ambiguous column, and why that cannot be done while parsing.
* What a merge join is and when it would be the best choice (both inputs already sorted on the key).
* Why join order matters with three tables.
