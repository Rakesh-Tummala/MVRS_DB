# 11 — Vector search

Code: `mvrs_db/vector/distance.py`, `VectorTopK` in `mvrs_db/execution/operators.py`, `_plan_nearest` in `mvrs_db/planner/planner.py`, `raw_vector` in `mvrs_db/storage/record.py`. Tests: `tests/integration/test_vectors.py`, `tests/integration/test_vector_search.py`.

## What it is for

A model can turn a piece of text, an image or a product into an *embedding*: a list of a few hundred numbers, arranged so that similar things get nearby lists. "Find the documents most like this one" then becomes "find the stored vectors closest to this vector". That is nearest-neighbour search, and it is what vector databases do.

MVRS_DB supports it in two steps so far:

1. **A column type**, `VECTOR(n)`, that stores `n` 32-bit floats per row.
2. **Exact search**: `ORDER BY column <-> [query] LIMIT k` returns the `k` closest rows by measuring every row.

An approximate index, which trades a little accuracy for a lot of speed, is not built.

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

## Cost and limits

* **Exact means linear.** Every candidate row is measured, so time grows with rows × dimensions. That is fine for tens of thousands of rows and too slow for millions. An approximate index (HNSW, IVF) is the standard answer and is not built.
* **A vector must fit in one record.** 768 dimensions fits the default 4 KiB page; 1,536 needs `page_size=8192`.
* **Each vector is 4 bytes per dimension**, uncompressed.
* **No index.** There is nothing to maintain on insert or update, and nothing to make a search sub-linear.

## What to be ready to explain

* What an embedding is, and why "similar" becomes "close".
* The difference between Euclidean and cosine distance, with an example where they disagree.
* Why cosine distance to a zero vector is undefined.
* Why exact search is the baseline an approximate index is judged against (recall = how many of the true top `k` the index returns).
* Why NumPy alone barely helped here, and what the raw path does differently.
* Why the search can beat a plain table scan.
