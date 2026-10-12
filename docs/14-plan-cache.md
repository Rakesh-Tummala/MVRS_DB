# 14 — The plan cache

Code: `mvrs_db/execution/plan_cache.py` (the cache), `mvrs_db/execution/params.py` (per-execution values), `Executor._reusable` and `_targets` in `mvrs_db/execution/executor.py`, the placeholder handling in `extract_ranges` and `plan_access` in `mvrs_db/planner/planner.py`, and `Filter`, `IndexScan`, `TopN` and `Limit` in `mvrs_db/execution/operators.py`. Tests: `tests/integration/test_plan_cache.py`.

## What it is for

After parsing was cached (see `06-sql-grammar.md`, Parameters), planning was the largest cost of a point query. Measured with `cProfile` on a primary-key lookup: about 35 µs of the 81 µs a call took was planning, and about 18 µs was executing. A plan cache keeps the operator tree built for a statement and runs it again.

It is automatic: any `execute(sql, params)` or `prepare()` statement of a supported kind uses it. There is nothing to switch on.

## The design problem: values

A plan built for `WHERE id = 5` cannot simply be reused for `WHERE id = 6`: the 5 is inside the plan, in the index lookup and in the compiled filter. And the best plan can depend on the value: `id > 10` and `id > 999990` may want different access paths. So a cache has to answer two questions.

**1. How do the values get into a shared plan?** The plan is built from the *template*, the parsed statement with its `?` still in place. Operators that need a value read it when they start running:

* `IndexScan` with a point range holding a placeholder looks the value up and converts it to the column's key type.
* `Filter` binds the condition to the values, runs the same type checks the planner makes for a literal, and compiles it. A statement with no placeholders compiles once, at plan time.
* `Limit` and `TopN` read their row count.

The values themselves live in a context variable (`params.bound_values`) set around the execution, not in the plan, so one plan can be running in several threads with different values.

**2. When does the value not matter?** The planner decides, while planning. It refuses with `PlanNotReusable` whenever the shape would depend on a value:

| Statement | Reusable? | Why |
|---|---|---|
| `WHERE id = ?`, `id` indexed | yes | the indexes are unique, so a point lookup matches at most one row for any value |
| `WHERE g = ?` or any condition on an unindexed column | yes | it is a filter; its estimated selectivity does not depend on the value |
| `LIMIT ?` without `ORDER BY` | yes | the limit only stops reading early |
| `WHERE id > ?` and other ranges on an indexed column | no | how much of the table the value selects decides between index and scan |
| `ORDER BY id LIMIT ?` | no | the limit decides between sorting and walking the index in order |
| Joins, `GROUP BY`/aggregates, vector search | not attempted | planned each time, as before |

A refusal is remembered, so the statement is not tried again; it is planned from scratch on each execution, exactly as without a cache. The effect of a mistake in this table would be a *slower* plan, not a wrong answer, with one exception: the code that runs a placeholder lookup must reproduce what a literal would do. It does:

* NULL: `id = NULL` is never true, so the index scan returns nothing.
* A value that cannot be an index key of that column (a float such as `3.0` for an INTEGER column): the planner would not use the index for a literal, so the scan falls back to walking the whole index and lets the filter decide. `id = 3.0` still finds row 3. The test `test_values_that_are_not_index_keys_still_match_like_literals` is there because the obvious shortcut ("not a key, so no match") is wrong here.
* A value of the wrong type (`id = '3'`): the filter's type check runs on every execution, not only on the one that built the plan.

## When a cached plan is discarded

A plan holds `Table` objects (their heap and index handles). It is valid only while those are the live ones.

* **`Catalog.epoch` changes.** It increases whenever tables are created or dropped, and whenever the in-memory catalog is reloaded, which happens after every rollback (the reload rebuilds the Table objects from the restored pages). So a rollback discards all plans, which is blunt but cheap and obviously safe.
* **Another catalog is in use.** In snapshot isolation each commit has its own catalog view, so a plan built for one view is not used for the next. Plans are reused between commits, not across them.
* **The table's size moved a lot**: more than doubled or less than halved since the plan was built (with a margin of 16 rows). The planner's choices rest on row counts, and a plan made for 10 rows should not live on after the table reaches a million.

An entry is found by the identity of the template object that the parse cache hands out, not by hashing the statement tree, and the entry keeps the template and the catalog alive so that identity cannot be reused. The cache holds 256 entries, least recently used out.

## Why this is safe to share

* Plan operators keep no state between runs: each `rows()` call starts from nothing (hash tables, sorts and counters are local to the call).
* Per-execution values are in a context variable, not on the plan.
* The cache's own dictionary is guarded by a lock; the plan itself is built outside it.

## Measured cost and benefit

One run on the development machine, best of five, 20,000 rows, per statement, including parsing (cached) and execution:

| | without the plan cache | with it |
|---|---|---|
| `SELECT g FROM t WHERE id = ?` | 110.7 µs | 55.2 µs |
| `UPDATE t SET g = 5 WHERE id = ?` | 87.9 µs | 68.4 µs |
| `SELECT id FROM t WHERE g = ? LIMIT ?` (scans) | 726 µs | 577 µs |
| `SELECT id FROM t WHERE id > ? LIMIT 1` (not reusable) | 6,444 µs | 6,503 µs |

The gain is the planning time, so it is large for point queries and small where execution dominates. The last row shows a refused statement costing the same as before (the difference is noise). These are single runs, so differences under about 10% mean nothing. The last query's 6 ms is an existing cost of range scans, not of this feature.

## How it is tested

* **Equal to planning afresh.** For twelve statement shapes, 60 random parameter sets each (NULL, integers, floats equal to integers, text, huge and negative numbers, empty strings), the answers with the cache equal the answers with the cache disabled, including which ones raise which error. A second test does the same for 80 random `UPDATE`, `DELETE` and `INSERT` statements and compares the resulting table.
* **Reuse really happens, and only where claimed.** The planner is counted: a point lookup is planned once for many executions; ranges and `ORDER BY ... LIMIT ?` are planned every time; joins and aggregates never enter the cache.
* **Staleness.** Drop and recreate a table with a different shape; roll back an insert; roll back a `DROP`; grow a table from 2 to 72 rows and shrink it again.
* **Errors every time.** A type error from a parameter is raised on the 5th execution as on the 1st; a failed plan is never cached.
* **Threads and snapshots.** Six threads share cached plans with their own values; a snapshot reader and a writer get different, correct answers from the same statement.
* **Seven deliberate bugs** were each caught: allowing a placeholder in a range, not bumping the epoch on `DROP`, not bumping it on reload, never discarding for growth, making unkeyable values find nothing, skipping the runtime type check, and ignoring which catalog a plan was built for.

## Not built

* Plans for joins, aggregates and vector queries. Join planning is the most expensive kind, so this is where a cache would gain most per query; it needs the same value analysis for each join method and for join ordering.
* Reusable plans for ranges. PostgreSQL plans a few times with the real values and switches to a generic plan only if it is not much worse; that is the standard answer and would let `id > ?` be cached.
* Invalidating one table's plans instead of all of them.
* Counting hits per statement for tuning (`db._executor.plans` has totals only).

## What to be ready to explain

* Why the values cannot be baked into a cached plan, and how they reach it instead.
* How the planner decides that a statement's plan does not depend on its values, and what is cached when it cannot.
* Why `id = 3.0` on an INTEGER key must not be answered "no rows" by the cached index lookup.
* Why every rollback discards all plans, and why that is acceptable.
* Why a plan must not outlive a large change in table size.
* Why one plan can be shared by threads running at the same time.
* Why a cached plan is looked up by identity, and what keeps that safe.
