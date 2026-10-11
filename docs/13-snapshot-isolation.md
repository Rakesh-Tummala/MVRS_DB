# 13 — Snapshot isolation (multi-version reads)

Code: `commit_seq`, `snapshot_page`, `begin_snapshot`/`end_snapshot` and `SnapshotPool` in `mvrs_db/storage/buffer_pool.py`; `Catalog.view` in `mvrs_db/catalog/catalog.py`; `_snapshot_catalog`, `_run`, `_finish` in `mvrs_db/database.py`. Tests: `tests/unit/test_snapshots.py`, `tests/concurrency/test_snapshot_isolation.py`, the snapshot cases in `tests/crash_recovery/test_crash_concurrent.py`.

Turn it on with `mvrs_db.connect(path, isolation="snapshot")`. The default, `isolation="locking"`, is the strict two-phase locking described in `10-transactions.md` and is unchanged.

## The problem it solves

With locking, a reader takes a shared lock on each table it reads, and a writer needs an exclusive lock. So one open write transaction stops every reader of the tables it changed, and one long read stops every writer. The test `test_the_same_scenario_blocks_in_locking_mode` shows it: a reader times out behind an open writer.

Snapshot isolation removes both waits. A reader sees the database exactly as it was at one commit, takes no lock, and is never seen by the writer.

## What a transaction sees

| | `isolation="locking"` | `isolation="snapshot"` |
|---|---|---|
| A `SELECT` | shared table locks; waits for writers | no locks; reads the last commit when it started |
| Autocommit `SELECT` | the current state | the state at the commit it started on |
| `BEGIN; SELECT; ...; SELECT` | repeatable because the locks are held | repeatable because the snapshot is fixed |
| Dirty read | impossible (locks) | impossible (a snapshot only contains commits) |
| Writers | one at a time (writer slot) | one at a time (writer slot), unchanged |
| A reader then writes | waits for the lock, may deadlock | works if nothing committed since its first read; otherwise `SerializationError` |
| Deadlock | possible | possible only among writers (they still take table locks) |

The snapshot is taken by the transaction's **first read**, not by `BEGIN`.

### The write rule, and why it is so coarse

There is only ever one writer. So the lost-update problem has a very simple form: transaction A reads a row, B commits a change to it, then A writes a value computed from the old row. A's write would silently undo B's.

The rule that prevents it: **a transaction that has read from a snapshot may start writing only if no transaction has committed anything since that snapshot.** The check happens when it takes the writer slot, and while it holds the slot nobody else can commit, so the answer cannot change under it. If the database has moved on, the transaction gets `SerializationError` ("roll back and retry"), and its transaction ends.

This is coarser than PostgreSQL's rule, which only objects when the *same row* was changed. Here any commit since the snapshot conflicts, including one on an unrelated table. The coarse rule is easy to state and to prove, and it has a nice consequence: read-write transactions are effectively serial (each one writes only from an up-to-date view), while read-only transactions run fully in parallel. A transaction that writes without reading first has no snapshot and is never refused.

## How it works

### Commit numbers

Every transaction that changes at least one page gets the next number in `BufferPool.commit_seq` when it commits. A snapshot is just the number that was current when the reader began. `begin_snapshot()` registers it; `end_snapshot()` releases it.

### Where a page's content at a given snapshot lives

Reading page `P` as of snapshot `s`:

1. **A later commit changed `P`.** The version store holds `P`'s before-image for each such commit, oldest first; the first one with commit number above `s` is the content at `s` (the page does not change between commits).
2. **The writer is changing `P` right now.** The writer's own before-image of `P` (the one it already keeps to roll back) is the last committed content.
3. **Neither.** `P` has not changed since `s`, so the cached frame, or the file, is current.

The reader copies the bytes while holding the pool's latch and works on the private copy. There is nothing to pin and nothing to hold while parsing.

### Why a reader can never see a half-edited page

This is the part to be able to defend. The writer edits a page in place, in several steps. A reader must not copy the page in the middle. It cannot, because:

* the writer saves a page's before-image *before* it first modifies the page (inside `fetch(write=True)`, under the latch);
* a reader holds the same latch while it chooses what to copy;
* so a reader either runs before the writer touched the page (and copies an unmodified frame) or after (and finds the before-image registered, which it uses instead of the frame). It never reads a frame that has a modification in flight, because for any such page it takes the immutable before-image.

### Keeping the versions, and dropping them

At commit number `n`, the writer's before-images become versions, **but only if some snapshot is registered**. If none is, nothing is kept: a reader that starts later gets a number `≥ n` and never needs them. When a snapshot ends, versions with commit number at or below the oldest remaining snapshot are dropped, or all of them if none remain.

Cost: memory of one page copy per page changed per commit, for as long as an older snapshot lives. A reader that never ends (an abandoned `BEGIN; SELECT`) pins every page changed since. `db.pool.version_count` shows the number held. `Database.close()` releases open snapshots.

### Reading through structures: `SnapshotPool` and catalog views

A table scan, a B+ tree lookup and a catalog read are all written against "a pool of pages". `SnapshotPool` is a read-only pool whose `fetch` returns `snapshot_page(page, s)`. A `HeapFile` or `BTree` built on it reads the database as of `s` with no other change. Its write methods raise.

The catalog is stored in pages too, so `Catalog.view(SnapshotPool(...))` loads the table definitions as of `s`. This is what makes DDL safe: a table dropped after the snapshot is still readable from it, and a table created after it does not exist for it. Views are cached per commit number and shared by every reader at that number; views no snapshot can use are dropped. Row counts and page counts for the planner are copied from the live catalog (they only steer estimates), which avoids counting every page of a table for every new commit number.

### Reading your own writes

Once a transaction writes it stops using snapshots and reads the live state, which contains its own uncommitted changes. This is safe because it holds the writer slot, so no other transaction can commit in the meantime.

### Durability still holds

A commit becomes visible slightly before its log record is on disk (early lock release, see `05-wal-and-recovery.md`). A snapshot read can therefore see a commit that a crash could still take back, so after a snapshot read returns its rows the database waits for the log to catch up, exactly as locking-mode reads do. The crash tests run with a reader thread active throughout.

## What does not work the same

* **The approximate vector index is not used for snapshot reads.** The HNSW index describes the newest rows, including the writer's uncommitted ones, and it is not safe to search while the writer updates it. A snapshot reader searches exactly (`VectorTopK`). The writer's own reads in the same database still use the index.
* **Write conflicts are detected per database, not per row** (see above).
* **A long reader costs memory** (see above).
* **`EXPLAIN` of `UPDATE`/`DELETE` still takes a shared table lock.**

## Measured cost

One run on the development machine, 20,000 rows, best of five, synchronous off:

| | locking | snapshot |
|---|---|---|
| `COUNT(*), SUM(g)` with a filter, full scan | 47.3 ms | 50.0 ms |
| `SELECT id WHERE g = 7` | 37.6 ms | 38.0 ms |
| `GROUP BY g` | 50.6 ms | 50.6 ms |
| 1,000 point reads by primary key | 88.3 ms | 95.0 ms |

So the single-thread price is a few percent, from copying each page that is read (the point reads, which touch the most pages per row, cost the most). These are single runs and differences under about 10% are within noise. The gain is concurrency, not speed: nothing here measures it, because Python threads share one interpreter lock and cannot read faster in parallel. What changes is that a reader no longer *waits*.

## How it is tested

* `test_snapshots.py` tests the pool alone: each rule above, the version count after each step, freed and reused pages, copies not aliasing pages.
* `test_snapshot_isolation.py` tests behaviour: repeatable reads, no dirty reads, DDL, the write rule (including the lost-update scenario), non-blocking in both directions, releasing versions, three threads auditing a total while four move money, and readers scanning while a writer inserts with a 32-page pool that forces evictions.
* A randomized test runs a writer and three readers through hundreds of operations (inserts, updates, deletes, multi-statement commits and rollbacks, `DROP`/`CREATE TABLE`) and compares every read, through scans, primary-key lookups, ranges, aggregates and a join, with a Python dictionary snapshot taken when that reader's snapshot began. It also predicts exactly which reader writes must be refused.
* Six deliberate bugs (ignoring the writer's before-image, never keeping versions, no staleness check, pruning everything, reading the live catalog, ignoring committed versions) were each caught.
* Crash tests kill the process, at five injected points, with snapshot isolation on and a reader thread running; recovery is identical because the log and recovery do not know snapshots exist.

## Not built

* Row-level versions (xmin/xmax) and row-level conflict detection. Versions here are whole pages.
* Making the approximate vector index snapshot-consistent.
* Read committed or serializable-with-predicate-locks as separate levels. There are two modes: locking, and snapshot.
* Bounding the memory a long reader can pin (for example by aborting it).

## What to be ready to explain

* What snapshot isolation guarantees in general, and why write skew (the anomaly it allows elsewhere) cannot happen here: transactions that write are serialised by the single writer slot plus the staleness check, and a read-only transaction sees the database between two commits, so every history is equivalent to some serial order. (The tests check specific scenarios, not serializability in general.)
* Where a page's old content is found, in the three cases, and why the order of the checks matters.
* Why the latch plus "before-image before modification" is enough for a reader never to see a torn page.
* Why versions need only be kept when a snapshot is registered, and when they are dropped.
* Why the first read, not `BEGIN`, takes the snapshot, and why a transaction with no reads is never refused.
* Why the staleness check is safe once the writer slot is held.
* Why a snapshot reader of a dropped table still works, and why the catalog is read through the snapshot.
* Why this needs no change to the log or recovery.
* Page-level versions against row-level versions: what each costs and allows.
