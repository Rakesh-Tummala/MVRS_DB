# 10 — Transactions and concurrency

Code: `mvrs_db/database.py` (`Connection`, `Database._run`, `_finish`), `mvrs_db/transactions/lock_manager.py`. Tests: `tests/integration/test_transactions.py`, `tests/concurrency/`.

## Model

* A `Database` is shared by threads. Each thread uses its own `Connection`.
* A connection has at most one open transaction.
* Without `BEGIN`, each statement is its own transaction and commits when it finishes.
* `BEGIN` ... `COMMIT` groups statements. `ROLLBACK` undoes all of them.

## Atomicity

All changes of a transaction become visible and durable together, or none do.

* **Rollback** restores the before-images the buffer pool saved, then reloads the in-memory catalog and discards cached free-space maps and row counts.
* **A statement that fails halfway** cannot be undone on its own, because before-images belong to the transaction, not to the statement. So the whole transaction is rolled back and the connection leaves it. A statement that fails *before changing anything* (a syntax error, an unknown table, a constraint caught on the first row) leaves the transaction open. The engine tells the two cases apart by counting page changes.
* **Deadlock, lock timeout and "transaction too large"** always roll the transaction back.

There are no savepoints.

## Locking

Strict two-phase locking with two kinds of resource:

| Resource | Shared (S) | Exclusive (X) |
|---|---|---|
| A table | `SELECT`, `EXPLAIN` | `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `DROP TABLE` |
| The writer slot | — | any statement that writes |

A transaction takes locks as it goes and releases all of them only when it ends. A join takes a shared lock on both of its tables, in name order.

"Ends" means the moment its commit record is appended to the log, **not** the moment it is durable. The locks are released at the first and the committer then waits for the second. This is early lock release, and it is what lets group commit batch several commits into one `fsync`; see `05-wal-and-recovery.md`. A later transaction can therefore read or change data whose commit is still on its way to the disk. It cannot be answered with that data until it is durable: a writer's own commit record lies behind the one it read, and a `SELECT` waits for everything appended so far before it returns.

**One writer at a time.** The first writing statement of a transaction takes the writer slot and keeps it until commit or rollback. This is not an optimisation waiting to be removed: page-image logging and page-level rollback are only correct if a page never holds uncommitted changes from two transactions.

**Readers run concurrently** with each other and with a writer that is working on a different table.

**Fairness.** A new reader queues behind a writer that is already waiting for the same table. Otherwise a steady stream of readers would keep the writer waiting forever. A reader that already holds the lock is not affected.

## Isolation

Because locks are held to the end of the transaction and cover whole tables:

| Anomaly | Prevented? | How |
|---|---|---|
| Dirty read (seeing uncommitted data) | Yes | A writer holds X on the table until it ends |
| Lost update | Yes | Two read-then-write transactions deadlock; one is rolled back and retries |
| Non-repeatable read | Yes | A reader holds S on the table until it ends |
| Phantom rows | Yes | The lock is on the table, not on rows, so nothing can be inserted under a reader |

Strict two-phase locking with locks that cover everything a transaction reads and writes yields serializable schedules, and table-level locks cover predicates as well as rows. The tests exercise each anomaly above with real threads, plus a bank-transfer workload whose total must stay constant. That is evidence, not proof: the tests show these anomalies do not occur in the schedules they run.

The price is concurrency. One long transaction that reads a table blocks every writer of that table.

## Deadlock

Two transactions can each hold a lock the other needs. The classic case here: both read a table (S), then both try to update it (each needs the other to release S).

Before a transaction waits, the lock manager follows the waits-for graph from the owners it would wait for. If the path leads back to the requester, waiting would never end, so the requester gets `DeadlockError` immediately and its transaction is rolled back. The application retries. A lock timeout (10 seconds by default) backs this up.

Choosing the requester as the victim is the simplest policy. Real systems often pick the transaction that has done the least work.

## Threads and the GIL

CPython runs one thread at a time, so threads do not make queries faster here. The concurrency machinery exists to make interleaved transactions *correct*, which is what the tests check.

Internal structures are protected by: the buffer pool latch, the disk manager's file lock, the lock manager's condition variable, and the catalog's lock.

## Not built: MVCC

Multi-version concurrency control would let readers see a snapshot without blocking writers. It needs row versions with transaction IDs, visibility rules, and garbage collection of old versions, and it does not fit page-level before-images. It is future work.
