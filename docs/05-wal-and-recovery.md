# 05 — Write-ahead log, torn writes and recovery

Code: `mvrs_db/recovery/wal.py`, `mvrs_db/recovery/recovery_manager.py`, and the commit and checkpoint code in `mvrs_db/database.py`. Tests: `tests/unit/test_wal.py`, `tests/unit/test_recovery.py`, `tests/crash_recovery/`.

## The problem

Changing one row can modify several pages: a heap page, a B+ tree leaf, and more if a node splits. A crash between those page writes leaves the file inconsistent. A crash *during* one page write leaves a torn page. The log solves both: describe the change in an append-only file first, make that durable, and only then let the pages be written.

## Policy

| Choice | Meaning |
|---|---|
| **Redo-only** | The log can re-apply changes. It cannot undo them. |
| **No-steal** | A page changed by an uncommitted transaction is never written to the data file, so there is never anything on disk to undo. |
| **No-force** | Commit does not write the changed pages. They are written later. |
| **Force-log-at-commit** | Commit returns only after the commit record is `fsync`'d. |
| **Single writer** | One writing transaction at a time. |

### Log whole pages, or the bytes that changed, at commit

During a transaction nothing is logged. At commit, for each page the transaction dirtied, the engine appends one record describing the page's new state, then a commit record, then flushes and `fsync`s once. There are two kinds of page record:

| Record | Holds | Used for |
|---|---|---|
| `PAGE_IMAGE` | The whole page after the change | A new page; the **first** change to a page since the last checkpoint; any change of more than half a page |
| `PAGE_DELTA` | The byte ranges of the page that changed, and the LSN the page had before the change | Every other change |

* Structural changes need no special treatment. Allocating a page, changing the free list and writing a catalog record are all just modified pages. The file header is not a page in the buffer pool, so it gets its own record type.
* An uncommitted transaction leaves at most a partial batch with no commit record, which recovery ignores.

A delta is computed by comparing the page against the copy the buffer pool saved when the transaction first touched it. The comparison works in two levels: the page is compared in 1 KiB chunks (one slice comparison each, done in C), and only a chunk that differs is examined in 32-byte blocks. Unequal neighbouring blocks merge into one range, and each range is trimmed to the exact bytes. A page usually changes in a few places, so most chunks cost a single comparison. The first version compared every block, which cost time proportional to the page size and came to about half the cost of an insert on 32 KiB pages (D-067). The first 16 bytes of a page (checksum, page ID, LSN) are never part of a delta: the checksum is recomputed whenever the page is written, the page ID never changes, and the LSN comes from the record itself.

**Why the first change after a checkpoint is a full image.** A delta can only be applied to the exact page it was computed from. If the disk tore that page while writing it, there would be nothing intact to apply deltas to. A full image of the page in the log, from before it could have been written, is what makes recovery possible. A checkpoint empties the log, so the next change to each page logs an image again.

**Why a delta names the previous LSN.** It says which page state it follows. Recovery applies a delta only to a page at exactly that LSN, and stops with an error if the chain does not fit, instead of producing a page that never existed.

**Why a change of more than half a page is logged whole.** Past that point the ranges and their headers cost more than they save.

The first version of this engine logged only whole pages (as SQLite's WAL mode does), at 4 KiB or more per changed page. See D-010 for why that came first and D-056 for what deltas changed. `Database.open(log_deltas=False)` still selects it, for comparison.

Alternative considered: one logical record per operation (insert row, split node), as in ARIES and PostgreSQL. It gives the smallest log and is required for concurrent writers, but every operation needs its own redo code and structural changes need careful handling. Deltas capture most of the saving without that.

### Rollback

When a transaction first modifies a page, the buffer pool keeps a copy of the page as it was (a **before-image**) in memory. Rollback copies the before-images back. They are never written to disk. Re-reading the page from disk would be wrong: under no-force, the in-memory page can hold earlier *committed* changes that have not reached the data file.

### Transaction size

No-steal means a transaction's dirty pages stay in the buffer pool until commit. A transaction that needs more dirty pages than the pool has frames fails with `TransactionTooLargeError` and is rolled back.

## Log file

`name.mvrs.wal`: a 40-byte header, then records back to back.

Header (`<8sHHIQQ4sI`): magic `MVRSWAL\0`, version, reserved, page size, `db_id`, `base_lsn`, reserved, CRC32 of the 36 bytes before it. `db_id` must match the data file, so a log can never be replayed into the wrong database.

Record (`<IIQQB` + payload):

| Field | Size | Notes |
|---|---|---|
| `length` | 4 | Payload length |
| `crc` | 4 | CRC32 of everything else in the record |
| `lsn` | 8 | Must equal the record's computed position |
| `txn_id` | 8 | |
| `type` | 1 | 1 `PAGE_IMAGE`, 2 `HEADER`, 3 `COMMIT`, 4 `PAGE_DELTA` |
| payload | `length` | `PAGE_IMAGE`: page ID (4 bytes) + the page. `HEADER`: the 64-byte file header. `COMMIT`: empty. `PAGE_DELTA`: see below. |

A `PAGE_DELTA` payload is `page_id` (u32), `prev_lsn` (u64, the LSN the page had before this change), `count` (u16), then `count` ranges, each `offset` (u16), `length` (u16) and `length` bytes. `delta.py` checks every range against the page and the payload before applying it.

There is no BEGIN or ABORT record. A transaction's records are written together at commit, so the first record with a new `txn_id` is its beginning, and a transaction that aborts never writes anything.

**LSN** (log sequence number): a 64-bit number that only increases. It is `base_lsn` plus the record's byte offset after the header. When a checkpoint empties the log, `base_lsn` moves forward so LSNs never repeat. The data file header stores the LSN of the last checkpoint, so LSNs keep growing even if the log file is lost.

**Finding the end of the log.** Reading stops at the first record that is incomplete, has an impossible length, fails its CRC, or carries the wrong LSN. Everything from there on is treated as never written. The stored LSN catches leftover records from before a checkpoint that have valid CRCs.

**User-space buffer.** `append()` only adds to a buffer in the process. Writing the bytes to the file and `fsync` make them durable. A process killed before then loses them. Without this buffer, bytes passed to the OS would survive a process kill and a "written but not flushed" crash test would prove nothing.

## Commit, step by step

A commit has two moments: when it is **fixed in the order of commits**, and when it is **durable**. They used to be one moment. Separating them is what allows group commit.

1. If the header changed, append a `HEADER` record.
2. For each changed page, decide image or delta as described above. Set the page's `lsn` field to the LSN its record is about to get, then append the `PAGE_IMAGE` or `PAGE_DELTA` record. A page fetched for writing but left unchanged is skipped.
3. Append a `COMMIT` record. Steps 1 to 3 happen inside one `atomic_append()` block, so a batch written by another thread's flush never contains half a transaction.
4. Mark the transaction committed in memory and tell the buffer pool the log position its pages must wait for (the end of the `COMMIT` record). **Release all locks.** The changes are now visible to other transactions.
5. Wait until the log is durable through the `COMMIT` record. Only then does `execute()` or `COMMIT` return.

Nothing in steps 1 to 4 touches the disk.

If the process dies before step 5 finishes, the transaction may or may not survive, and either is correct: nobody was told it committed. A checkpoint forgets which pages have an image, since it empties the log.

### Group commit

Step 5 is where the time goes: an `fsync` takes milliseconds. The thread that needs the log made durable (the *leader*) writes everything in the buffer and calls `fsync` **without holding the log's lock**. Meanwhile other threads execute statements, append their own records to a fresh buffer, and wait. When the leader finishes, one waiter becomes the next leader and writes everything that piled up, with one `fsync`. Commits that arrive while an `fsync` is running cost one more `fsync` between them, not one each.

This only works because step 4 releases the locks early. With a single writer, the old design held the writer slot until its own `fsync` returned, so no second commit could ever overlap it.

Measured on the development machine (hard disk, `fsync` about 23 ms), durable single-row inserts into one table:

| Threads | Commits per second | Commits per `fsync` | Median commit | 99th percentile |
|---|---|---|---|---|
| 1 | 40 | 1.0 | 22.6 ms | 34 ms |
| 2 | 58 | 1.5 | 23.1 ms | 111 ms |
| 4 | 98 | 2.5 | 44.1 ms | 89 ms |
| 8 | 169 | 4.5 | 44.9 ms | 148 ms |
| 16 | 320 | 8.5 | 45.9 ms | 166 ms |

Throughput grows with the number of threads, up to 8.0 times at 16. **Latency grows too**: each commit now waits for a shared batch, not just its own `fsync`; the median doubles from 23 ms to 46 ms and the 99th percentile is several times higher. This is the usual trade. A single thread is unaffected. An earlier run of the same benchmark, before other speed-ups, gave 36 commits/s for one thread and 268 for 16 (7.4 times), so figures like these move by 10 to 20% between runs. Reproduce: `python benchmarks/group_commit.py`.

### Early lock release

Releasing locks at step 4, before the commit is durable, means other transactions can read and change data whose commit is not yet on disk. Three rules keep that safe:

1. **Order.** A later transaction's records come after this one's in the log, and the log is only ever lost from its end. If this commit is lost, so is everything that read it. No transaction is acknowledged before every commit it could have seen is durable, because its own commit record lies behind them.
2. **Readers wait.** A reader has no commit record of its own, so a `SELECT` waits for everything appended so far to become durable before it returns its rows. With nothing pending this is a comparison and costs nothing. (Otherwise a caller could act on data a crash then takes back.)
3. **Pages wait.** See the next section.

## Write ordering (the WAL rule)

A page may be written to the data file only when the log is durable through the **commit record** of the transaction that last changed it, not merely through the page's own record. Otherwise a crash could leave a transaction half on disk with no commit record in the log, and recovery would discard it, leaving its pages behind.

Two mechanisms enforce it:

* A page of a transaction still running is never written at all (no-steal).
* A page of a committed transaction remembers the log position its transaction's `COMMIT` record ends at. Before the buffer pool writes the page, it waits until the log is durable to that position. The file header, which can describe any commit, waits for everything appended so far. Under group commit a page can be committed in memory and evictable while its commit record is still queued behind an `fsync`; this is what makes the pool wait for the log in that case.

## What COMMIT guarantees

When `commit` returns successfully with the default `synchronous=True`:

* Every page record of the transaction and its commit record are in the log file, and `os.fsync` on the log returned without error.
* After a process crash, recovery restores all of the transaction's effects.
* After an OS crash or power loss, the same holds **if** the OS and the drive honour `fsync`.

It does **not** guarantee:

* Survival of a failed or lying disk.
* Anything about transactions after a damaged record in the middle of the log. A damaged record is indistinguishable from the end of the log, so later commits are lost.
* That the data file is up to date. It may be arbitrarily behind the log.

What other transactions may see is weaker than what the committer is told: they can see a commit slightly before it is durable (early lock release), but cannot be answered with it before it is.

With `synchronous=False`, a commit survives a crash of the process but can be lost if the operating system crashes or the power fails. The database stays consistent either way. Group commit still batches the writes.

### If the log cannot be written

If writing or syncing the log fails, every commit waiting on that batch fails with `StorageIOError`. The engine then **stops completely**: every later statement, reads included, raises `EngineFailedError` until the database is reopened.

Reads stop as well as writes because the in-memory state may now contain commits that never reached the disk, and other transactions have already seen them; there is nothing left to roll back to. Reopening runs recovery, which settles which commits survived. The engine does not retry: after a failed `fsync` the OS may have dropped the data while reporting success on the second attempt. (PostgreSQL, for the same reason, crashes and recovers on an `fsync` failure.)

## Torn pages

A torn page is a page write interrupted halfway: part new, part old.

* **Detection:** the page checksum fails on read.
* **Repair:** recovery cannot use a page with a bad checksum as a starting point, so it starts from the newest full image of the page in the log and applies every delta logged after it. A full image always exists: the first change to a page after a checkpoint is logged whole, and a page only reaches the disk after it has changed.
* **No image available:** a page that fails its checksum and has no image in the log cannot be rebuilt, and recovery stops with an error naming the page. This cannot arise from a torn write; it means damage after the page was last checkpointed, or a log that was lost.

Assumption: a write that fits in one 512-byte disk sector is atomic. The log header and the data file header rely on this.

## Checkpoint

A checkpoint bounds the log's size and recovery time. It runs between transactions, holding the writer slot:

1. Record the last LSN in the file header.
2. Write every dirty page and the header to the data file. All are committed, because no transaction is active.
3. `fsync` the data file.
4. Rewrite the log header with a new `base_lsn`, `fsync`, then truncate the log to the header and `fsync` again.

A crash at any step is safe. Until step 4, the old log is intact and recovery repeats work. Between rewriting the header and truncating, the leftover records have LSNs below the new base and are ignored. Truncation is done in place, because Windows cannot rename or delete an open file.

Checkpoints run when the log passes 8 MiB (configurable), on clean close, and at the end of recovery.

## Recovery

On open, before anything else reads the data file:

1. **Scan** the log to its valid end and collect the transaction IDs that have a `COMMIT` record.
2. **Redo.** Apply the newest committed `HEADER`, if any. Then, for each page that has committed records:
   * If the page on disk has a valid checksum and an `lsn` at least as new as the page's last record, leave it.
   * Otherwise pick a starting point: the newest full image newer than the page on disk, or, if there is none, the page on disk. Apply the deltas after it in order, checking that each one's `prev_lsn` equals the page's LSN at that moment. Write the result once.
3. **Finish.** Write the header, `fsync` the data file, check that the file length matches the header, then empty the log.

**Idempotent:** each page is written once, in its final state, and the log is untouched until the data file is synced. Crashing during recovery and running it again finds the pages that were written already current and skips them.

## What is tested

60 tests in `tests/crash_recovery/test_crash.py` start a child process, kill it with `os._exit` at an injected point (`mvrs_db/faults.py`), reopen, and check durability, atomicity and `Database.check()`. Two workloads are used: row changes across two indexes, and creating and dropping tables. Delta logging is on in all of them, and one test asserts that recovery after a kill really did rebuild pages by replaying deltas.

`tests/crash_recovery/test_crash_concurrent.py` does the same with six threads committing at once, each running two-statement transactions (insert a row, bump a counter). Fourteen kill points cover buffered-but-unwritten commits, a leader killed mid-write and mid-`fsync`, and kills during page eviction and checkpoints while commits are pending. After recovery every acknowledged commit must be present, each thread's counter must equal its row count (no half-applied transaction), its rows must form an unbroken run, and at most one extra transaction per thread may have survived. Making commits return before they are durable makes these tests fail.

`tests/concurrency/test_group_commit.py` checks the mechanism with `fsync` slowed or held shut, so the interleavings are certain: commits that overlap share `fsync`s; a commit is durable when its call returns; a reader sees an unacknowledged commit but is not answered with it; a second writer proceeds while the first waits for the disk; an `fsync` failure fails every commit waiting on it; and a page whose commit is not yet durable cannot be evicted until it is. Disabling that last rule makes that test fail.

`tests/unit/test_delta.py` covers the delta machinery on its own: finding changed ranges (including a property test over random edits), rejecting malformed records, resuming a page that reached disk partway through its history, rebuilding a torn page from its image plus later deltas, a later image superseding earlier records, refusing a delta that does not follow the page, and idempotence.

| Crash point | Outcome required for the transaction in flight |
|---|---|
| Before any log record is written | absent |
| Records in the user-space buffer, not written | absent |
| Half the commit's records written (truncated tail) | absent |
| Commit record written, not yet fsynced | present (a killed process cannot lose bytes the OS has) |
| Commit record fsynced, commit not yet returned | present |
| Start of, middle of, and end of a checkpoint | present |
| Between rewriting the log header and truncating it | present |
| Halfway through a page write, at five different moments (the 1st, 9th, 40th, 50th and 120th page write) | either, never half |
| Six threads committing: buffered, mid-write, mid-`fsync`, after `fsync`, during eviction, during checkpoints | every acknowledged commit present; no half-applied transaction |
| During database creation | a usable empty database |
| During recovery (five points), twice in a row | same result as uninterrupted recovery |
| Log truncated at 25 offsets; a byte flipped in each of the last 12 records | a whole number of transactions survives |

Not tested: real power loss, drives that ignore `fsync`, damage in the middle of the log followed by valid records, and media failure. A killed process leaves the operating system running, so these tests cannot show that `fsync` itself works.

## Upgrade path (not built)

To support concurrent writers and transactions larger than memory, move to ARIES-style logging: per-operation log records, **steal** (uncommitted pages may be written), **undo** records and an undo pass, compensation log records so undo is itself recoverable, and a dirty-page table for fuzzy checkpoints.
