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

Code: `mvrs_db/planner/scope.py` (name resolution), `mvrs_db/planner/joins.py` (outer-join rules and join order), `_plan_join` in `mvrs_db/planner/planner.py`, `HashJoin` and `IndexJoin` in `mvrs_db/execution/operators.py`. Tests: `tests/integration/test_joins.py`, `test_outer_joins.py`. Measurement: `benchmarks/join_methods.py`.

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

An **outer** join keeps the unmatched rows of one or both sides and fills the other side's columns with NULL:

| Join | Keeps unmatched rows of |
|---|---|
| `a LEFT JOIN b` | `a` |
| `a RIGHT JOIN b` | `b` |
| `a FULL JOIN b` | both |

`SELECT c.name, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.name` lists customers with no orders, with a count of 0. The inner version would drop them.

### What is supported

* Any number of tables, joined left to right: `a JOIN b ON .. JOIN c ON ..` means `((a JOIN b) JOIN c)`. Kinds are `[INNER] JOIN`, `LEFT [OUTER] JOIN`, `RIGHT [OUTER] JOIN` and `FULL [OUTER] JOIN`, and they may be mixed.
* Each `ON` condition is one equality between a column of the new table and a column of any earlier table (not necessarily the one just before).
* Table aliases (`orders o`) and qualified names (`o.total`, or `orders.total` when there is no alias).
* A bare column name, when only one of the tables has a column of that name. A name both tables have (`id`) must be qualified; the error says how.
* `WHERE`, aggregates, `GROUP BY`, `ORDER BY` and `LIMIT`, all applied to the joined rows.
* A table joined to itself, with two aliases.

`SELECT *` returns every column of each table in the order the tables are written, named `alias.column`, however the planner ordered the joins. Otherwise result columns are named as the query wrote them.

Qualified names and aliases also work in a query on one table.

### NULL never matches

`NULL = NULL` is not true in SQL, so a NULL join value pairs with nothing, including another NULL. Both join operators drop such rows before comparing. An outer join still keeps such a row if its side is preserved, padded with NULLs.

### Steps of planning

**1. Resolve names.** `scope.py` works out which table each column name refers to and rewrites every name into one form: `alias.column` for a join, the bare column name for a single table. Errors that depend on the whole query are caught here: an unknown qualifier, a column neither table has, an ambiguous bare name. After this step the rest of the planner never looks at a qualifier.

The joined row is each table's columns in the order the tables were joined. The planner describes it with a combined schema, and from then on the existing filter, sort, aggregate and projection code treats a join exactly like a table.

A chain is planned **left-deep**: each step joins "everything so far" (the left input) with one new table (the right input). The tree of operators has that shape, and a join key is always (a column somewhere in the accumulated row) = (a column of the new table).

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

**Outer joins make this unsafe, and that is where the care goes.** In `a LEFT JOIN b ... WHERE b.x IS NULL`, the rows with `b.x` NULL are the ones the join added. A filter pushed below the join would run on `b` before those rows exist, and the query would find nothing. So a condition is pushed to a table's scan only if no join can NULL-extend that table: not its own join (`LEFT`, `FULL`), and not a later `RIGHT` or `FULL`, which NULL-extends everything joined so far. Otherwise the condition waits and is applied to the joined rows. A multi-table condition is applied as soon as all its tables are present, again unless a later `RIGHT` or `FULL` would still NULL-extend them.

**Weakening an outer join.** If `WHERE` can never be true for the rows an outer join added, those rows would be removed anyway, so the join may become a cheaper kind. `LEFT JOIN b ... WHERE b.x = 1` is an inner join in disguise: every row with `b` NULL fails the comparison. The planner rewrites `LEFT` to `INNER`, `RIGHT` to `INNER`, and `FULL` to `LEFT`, `RIGHT` or `INNER` depending on which side is filtered. Then the condition can be pushed down and the planner may reorder. The test for "never true when these columns are NULL" (`rejects_null` in `joins.py`) is deliberately conservative: it says yes for comparisons and `IS NOT NULL`, handles `AND`/`OR`, and says no for `IS NULL` and `NOT`, because a wrong yes silently drops rows.

**Join order.** Changing the order of an inner chain never changes its rows, so the planner picks one: the smallest (after its own filter) table first, then repeatedly the connected table that gives the smallest estimated result. This is greedy and can miss the best order; trying every order (dynamic programming over subsets) is more machinery than these row estimates can justify. A chain containing an outer join keeps the order written, because moving an outer join changes the result. In both cases `SELECT *` still lists the tables in the order written.

**3. Choose the join method.**

| | Hash join | Index join |
|---|---|---|
| How | Read one input fully into a dictionary keyed by the join value; read the other and look each row up | For each row of one input, look the join value up in the other table's B+ tree and fetch the row |
| Cost | About rows(A) + rows(B) | About rows(outer) × (a tree descent + a row fetch) |
| Memory | The smaller input, held in a dictionary | None |
| Needs | Nothing | The looked-up column is a primary key or `UNIQUE` column of the same type |
| Outer joins | All three kinds | `LEFT` when the new table is looked up (an outer row with no partner is still emitted); inner joins either way |
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

For an outer hash join, each build row carries a "matched" flag. If the preserved side is the probe side, its unmatched rows are emitted as they stream past; if it is the build side, they can only be known at the end and are emitted after the probe finishes. Rows with a NULL key are kept aside because they never match but must still be emitted.

### How it is tested

`test_outer_joins.py` compares the engine with a plain nested-loop evaluation written separately in the test (random tables of 0 to 12 rows with NULLs and duplicates, random kinds, random `ON` columns including indexed ones, and random `WHERE` trees with `AND`/`OR`/`NOT`/`IS NULL`), for two, three and four tables, with and without outer joins. Deliberately breaking four things (treating `IS NULL` as null-rejecting, dropping NULL-keyed build rows, ignoring that `RIGHT` NULL-extends earlier tables, and not holding conditions back) each made it fail. A separate test checks that every outer operator is chosen by the planner for some query of the kind the comparison runs.

### Locking

A join takes a shared lock on every table it names, in name order. A fixed order means two joins over the same pair of tables can never each hold one lock while waiting for the other.

### Not built

* `CROSS JOIN`, `NATURAL`, `USING`
* Reordering a chain that contains an outer join, and an exhaustive search for the best order
* Join conditions other than one equality (`ON a.x = b.x AND a.y = b.y`, `ON a.x < b.y`). Extra conditions go in `WHERE`, where they give the same result for an inner join.
* Merge join, and any join that spills to disk when the hash table does not fit in memory
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
* Why join order matters with three tables, and why an outer join pins it.
* Why a filter on the NULL-supplied side of a LEFT JOIN cannot be pushed below it, and when it can be turned into an inner join.
* How a RIGHT or FULL hash join finds the preserved rows it never matched.
