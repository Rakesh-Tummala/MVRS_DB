# 11 — Vector search

Code: `mvrs_db/vector/distance.py`, `VectorTopK` in `mvrs_db/execution/operators.py`, `_plan_nearest` in `mvrs_db/planner/planner.py`, `raw_vector` in `mvrs_db/storage/record.py`. Tests: `tests/integration/test_vectors.py`, `tests/integration/test_vector_search.py`.

## What it is for

A model can turn a piece of text, an image or a product into an *embedding*: a list of a few hundred numbers, arranged so that similar things get nearby lists. "Find the documents most like this one" then becomes "find the stored vectors closest to this vector". That is nearest-neighbour search, and it is what vector databases do.

MVRS_DB supports it in three layers:

1. **A column type**, `VECTOR(n)`, that stores `n` 32-bit floats per row.
2. **Exact search**: `ORDER BY column <-> [query] LIMIT k` returns the `k` closest rows by measuring every row.
3. **An approximate index** (HNSW), declared on the column, that answers the same query by looking at a small part of the table. It is held in memory only.

## Syntax

```sql
CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT, embedding VECTOR(3));
INSERT INTO docs VALUES (1, 'intro', [0.1, 0.2, 0.3]);

-- the 5 nearest rows, with their distances
SELECT id, title, embedding <-> [0.1, 0.2, 0.25]
FROM docs
WHERE title IS NOT NULL
ORDER BY embedding <-> [0.1, 0.2, 0.25]
LIMIT 5;
```

| Operator | Measure | Meaning |
|---|---|---|
| `<->` | Euclidean (L2) distance | Straight-line distance, `sqrt(sum((a - b)^2))`. 0 means identical. |
| `<=>` | Cosine distance | `1 - cos(angle)`. Ignores length, compares direction. 0 same direction, 1 perpendicular, 2 opposite. |

The operators are the ones pgvector uses, so the queries look familiar.

Rules:

* A distance may appear in the `SELECT` list (its column is named `distance`) and as the single `ORDER BY` item. It may not appear in `WHERE` or be combined with other sort keys.
* The query vector must have the column's dimension.
* `DESC` returns the farthest rows instead.
* `WHERE` is applied first; the search runs over the rows that pass it.
* Without `LIMIT`, every row is returned in distance order.

## Which measure to use

Euclidean distance cares about length: `[10, 0]` is far from `[1, 0]`. Cosine distance does not: they point the same way, so their distance is 0. Text embeddings are usually compared by cosine, because the direction carries the meaning and the length mostly reflects things like document size. If every vector is scaled to length 1, the two measures rank rows identically.

## Rows with no distance

A row has no distance when its vector is NULL, or, for cosine, when its vector is all zeros (a zero vector has no direction, so the angle is undefined). Such rows come **last** in both directions, and their `distance` column is NULL.

This is a deliberate exception to the engine's usual "NULLs first when ascending" rule: a nearest-neighbour query that began with all the rows it could not measure would be useless.

A cosine search *for* an all-zero query vector is rejected outright.

## Precision

Vectors are stored as 32-bit floats. The query vector is rounded to 32 bits too before anything is measured, so searching for a vector that is in the table finds it at distance exactly 0. The arithmetic itself is done in 64 bits.

## How the search runs

`VectorTopK` is the operator. It takes candidate rows in chunks of 1,024, computes all the distances for a chunk in one batch, and merges them with the best `k` seen so far. Memory stays at one chunk plus `k` rows however large the table is.

There are three ways the distances get computed, all giving the same answers:

| Path | When | How |
|---|---|---|
| Raw | NumPy installed and no `WHERE` | Records are read from the heap and the vector's stored bytes are handed straight to NumPy (`numpy.frombuffer`). No row is decoded until the `k` winners are known. |
| Decoded, NumPy | NumPy installed and there is a `WHERE` | Rows are decoded so the filter can run; the surviving vectors are converted to a matrix. |
| Pure Python | NumPy not installed | A loop over `distance()`. Slow, but the engine needs no third-party package. |

`EXPLAIN` says which one is in use:

```
VectorTopK(5 nearest by embedding <-> [0.1, 0.2, 0.25]; exact scan, NumPy on undecoded rows)
```

### Why the raw path exists

The first version had only the last two paths, and NumPy made it just 1.3 to 1.5 times faster than pure Python. Measuring showed why: almost all the time went into turning each stored vector into a Python tuple of float objects, and then into a NumPy array again. The distance arithmetic was a small fraction.

The raw path skips that round trip. Measured with `benchmarks/vector_search.py` on the development machine (20,000 rows, top 10 by Euclidean distance, median of 5 queries, warm cache):

| Dimensions | Plain scan of the table | Search, raw NumPy path | Search, pure Python | Raw path vs pure Python |
|---|---|---|---|---|
| 16 | 60 ms | 33 ms | 93 ms | 2.8× |
| 128 | 120 ms | 61 ms | 360 ms | 6.0× |
| 768 | 502 ms | 209 ms | 1,763 ms | 8.4× |

Searching is now *faster* than `SELECT id FROM e`, because an ordinary scan decodes every vector and the search does not.

A query with a `WHERE` clause uses the decoded NumPy path. Before the raw path existed, that path was measured on the same data at 68, 298 and 1,350 ms for 16, 128 and 768 dimensions: only 1.3 to 1.5 times faster than pure Python. Making the filter work on undecoded rows would close that gap and is not done.

## The approximate index (HNSW)

Code: `mvrs_db/vector/hnsw.py`, `VectorIndexScan` in `operators.py`, `Table.ann_index` in `catalog/catalog.py`. Tests: `tests/integration/test_vector_index.py`. Evaluation: `benchmarks/vector_recall.py`.

Exact search is linear in the table size. An approximate index gives up the guarantee of finding *all* of the true nearest rows in exchange for looking at far fewer of them.

### Declaring and using it

```sql
CREATE TABLE docs (
    id INTEGER PRIMARY KEY,
    embedding VECTOR(64) HNSW          -- index for <-> (Euclidean)
);
-- or:  embedding VECTOR(64) HNSW COSINE   -- index for <=> (cosine)

SELECT id FROM docs ORDER BY embedding <-> [ ... ] LIMIT 10;
```

There is nothing to add to the query. The planner uses the index when **all** of these hold, and otherwise runs the exact search:

| Condition | Why |
|---|---|
| The column is declared `HNSW` for the distance the query uses | A graph is built for one distance |
| The query has a `LIMIT` and asks for nearest first | The index finds the nearest k; it cannot find the farthest or order everything |
| There is no `WHERE` clause | The index knows nothing about other columns |
| The table has at least 1,000 rows (`db.vector.min_rows`) | Below that the exact scan is quick and always right |
| NumPy is installed and `db.vector.use_index` is true | |

So declaring an index can make a query approximate, but it never makes a query fail, and `EXPLAIN` always says which search will run:

```
VectorIndexScan(10 nearest by embedding <-> [0.1, 0.2, ... 64 numbers]; HNSW, approximate, ef=100)
```

Three settings on an open database, changeable at any time:

```python
db.vector.use_index = False  # force exact search (this is how ground truth is obtained)
db.vector.ef_search = 200  # more candidates: higher recall, slower
db.vector.min_rows = 0  # use the index even on small tables
```

The `distance` column, if selected, is always computed exactly for the rows returned.

### How HNSW works

Each vector is a node in a graph, linked to a handful of nearby vectors. Searching means starting at some node and repeatedly moving to whichever neighbour is closer to the query.

One flat graph would need many small steps, so the graph has layers, like a skip list. Every node is in layer 0. A node is also in layer 1 with a small probability, in layer 2 with a smaller one, and so on. The few nodes in the top layers have long links. A search starts at the top, covers a lot of ground in a few hops, drops a layer each time it can get no closer, and finishes with a careful search in the dense bottom layer.

| Parameter | Value | Effect |
|---|---|---|
| `M` | 16 | Links per node (32 in layer 0). More links: better recall, more memory, slower build. |
| `ef_construction` | 100 | Candidates examined when inserting a node. |
| `ef_search` | 100 by default | Candidates kept while searching layer 0. The recall/speed dial at query time. Never less than `k`. |

Which neighbours a node links to matters as much as how many. Linking to simply the closest ones fails on clustered data: every link stays inside the cluster. The rule used here keeps a candidate only if it is closer to the node than to every neighbour already chosen, which spreads links in different directions. The docstring of `HnswIndex._select` has the measurements behind that choice.

### Measured recall and speed

`recall@10` is the share of the true 10 nearest rows that the index returned, with the exact search as ground truth. 10,000 rows of 64 dimensions, 100 queries, median latency per query including SQL parsing, on the development machine:

| Dataset | Exact | `ef_search` 10 | 50 | 100 | 200 | 400 |
|---|---|---|---|---|---|---|
| Clustered, Euclidean: recall | 1.000 | 0.562 | 0.852 | 0.968 | 0.995 | 1.000 |
| latency | 23.7 ms | 0.61 ms | 1.06 ms | 1.52 ms | 2.55 ms | 4.22 ms |
| Clustered, cosine: recall | 1.000 | 0.492 | 0.748 | 0.873 | 0.986 | 0.999 |
| latency | 23.0 ms | 0.67 ms | 1.24 ms | 1.90 ms | 2.96 ms | 4.67 ms |
| Uniform random, Euclidean: recall | 1.000 | 0.363 | 0.789 | 0.917 | 0.983 | 0.997 |
| latency | 22.4 ms | 0.71 ms | 1.36 ms | 2.21 ms | 3.70 ms | 6.36 ms |

Building the index took 18, 23 and 32 seconds for the three datasets. Reproduce with `python benchmarks/vector_recall.py`; the raw numbers are in `benchmarks/results/vector_recall.json`.

Reading the table:

* At the default `ef_search` of 100 the index is 10 to 16 times faster than exact search and finds 87% to 97% of the true neighbours.
* Recall rises with `ef_search` and so does latency. Near-perfect recall costs about a quarter of the speed-up.
* Uniform random data is the hard case: with no structure, "nearby" means little, and every such index struggles.
* A low `ef_search` is fast and wrong. 10 misses about half the true neighbours.
* These recall figures are lower than a tuned library such as hnswlib would report. This implementation is simplified and has not been tuned.

### Where it misses

The search is greedy: it only explores outward from the best candidates it has found. If a query falls between two clusters, the search can settle in one of them, fill its candidate list there, and never visit the other, even though some true neighbours are in it. Raising `ef_search` widens the candidate list until the other cluster is reached. With `ef_search` equal to the number of rows the search is exhaustive and exact, which the tests use to confirm the graph is fully connected.

### In memory, with a saved copy

The index lives in memory and is not logged. It is saved to a separate file as a cache.

* **A saved copy is written on close** (and by `db.save_vector_indexes()`), to `<database>.ann.<table id>.<column>`. It is a flat dump of the graph (`vector/snapshot.py` has the byte layout), written to a temporary file and renamed into place, so a crash leaves the old copy or the new one.
* **It is used only if it is provably current.** It records which database file, table, column, dimension and metric it was built from, plus a *stamp* of the table's heap: the newest page LSN, the live row count and the page count (`HeapFile.stamp`). Every committed change gives a page a higher LSN, so any change since saving makes the stamp differ and the copy is ignored. A checksum catches damaged files, and `HnswIndex.from_arrays` checks the graph itself (links in range, layers consistent, entry point valid), so a corrupt or hostile file cannot make a search crash or loop.
* **Any doubt means rebuilding from the table**, which is always correct. A crash before close just loses the copy. Nothing about the copy is trusted for correctness, which is why it needs no logging or recovery.
* **Without a usable copy it is rebuilt on first use**: the first approximate query reads the whole table and inserts every vector, at about 2 to 3 ms per row. `EXPLAIN` shows `index not built yet`, or `index not in memory yet (saved copy on disk)` when a file exists (it is only checked for validity when the query runs).
* **Measured once** (10,000 rows, 64 dimensions, 3.5 MB snapshot, synchronous off, one run each): the first approximate query took 25.5 s when rebuilding and 0.12 s when loading the copy. Loading is dominated by turning the arrays back into Python lists.
* **A copy goes stale when the table changes while the index is not loaded.** For example, rows are deleted in a session that never ran an approximate query. That is detected by the stamp, not prevented. The next open rebuilds.
* **Inserts get slower once it is built**, because each new row is also inserted into the graph.
* **A rollback discards it** if the rolled-back transaction changed it, since it would describe rows that no longer exist. It is then rebuilt on next use. An index the transaction did not touch is kept.
* **Deletes mark graph nodes dead.** Dead nodes still route searches but are never returned. When more than 30% of an index is dead it is rebuilt.
* **It needs no logging or recovery**, which is the point: a crash cannot corrupt something that is rebuilt from the table.

An index stored in pages and logged like the B+ tree would never need rebuilding and could grow past memory. That is a much larger piece of work and is not done; the saved copy still has to fit in memory when loaded.

## Cost and limits

* **Exact means linear.** Every candidate row is measured, so time grows with rows × dimensions.
* **Approximate means approximate.** The index can miss true neighbours, and how many depends on the data and on `ef_search`. Use `db.vector.use_index = False` when the exact answer matters.
* **The index is rebuilt after an open if the saved copy is missing or out of date**, on the first query that uses it.
* **A filtered search is always exact.** Any `WHERE` clause bypasses the index and also the fast exact path.
* **A vector must fit in one record.** 768 dimensions fits the default 4 KiB page; 1,536 needs `page_size=8192`.
* **Each vector is 4 bytes per dimension**, uncompressed, plus the index's own copy in memory once built.
* **The index needs NumPy.** Without it every search is exact and in pure Python.

## What to be ready to explain

* What an embedding is, and why "similar" becomes "close".
* The difference between Euclidean and cosine distance, with an example where they disagree.
* Why cosine distance to a zero vector is undefined.
* Why exact search is the baseline an approximate index is judged against (recall = how many of the true top `k` the index returns).
* Why NumPy alone barely helped here, and what the raw path does differently.
* Why the search can beat a plain table scan.
* How HNSW's layers resemble a skip list, and what each of `M`, `ef_construction` and `ef_search` controls.
* What recall is, how it was measured here, and why it is below 1.
* A query for which a greedy graph search misses the true neighbours, and what fixes it.
* Why this index is not persisted, what that costs, and what persisting it would require.
* Why a `WHERE` clause disables the index, and what "pre-filtering" versus "post-filtering" would mean.
