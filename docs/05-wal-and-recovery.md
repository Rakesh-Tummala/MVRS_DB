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

A delta is computed by comparing the page against the copy the buffer pool saved when the transaction first touched it. The comparison runs in 32-byte blocks (a slice comparison, done in C), merges unequal neighbours into ranges, and trims each range to the exact bytes. The first 16 bytes of a page (checksum, page ID, LSN) are never part of a delta: the checksum is recomputed whenever the page is written, the page ID never changes, and the LSN comes from the record itself.

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

**User-space buffer.** `append()` only adds to a buffer in the process. `flush()` hands the bytes to the OS and `fsync` makes them durable. A process killed before `flush()` loses them. Without this buffer, bytes passed to the OS would survive a process kill and a "written but not flushed" crash test would prove nothing.

## Commit, step by step

1. If the header changed, append a `HEADER` record.
2. For each changed page, decide image or delta as described above. Set the page's `lsn` field to the LSN its record is about to get, then append the `PAGE_IMAGE` or `PAGE_DELTA` record. A page fetched for writing but left unchanged is skipped.
3. Append a `COMMIT` record.
4. `flush()` and `fsync` the log.
5. Note which pages now have a full image in the log (only now, with the records durable), then tell the buffer pool the transaction is over. Its pages are now ordinary dirty pages.

The transaction is committed when step 4 returns. If the process dies before then, the transaction never happened.

A checkpoint forgets which pages have an image, since it empties the log.

## Write ordering (the WAL rule)

A page may be written to the data file only when the log is durable up to that page's LSN. Two mechanisms enforce it:

* A page of an uncommitted transaction is never written at all (no-steal).
* Before writing any page, the buffer pool calls the log's `sync()`. With synchronous commits there is never anything left to sync. With `synchronous=False`, commits skip the `fsync`, and this call is what keeps a page from reaching disk ahead of its log record.

## What COMMIT guarantees

When `commit` returns successfully with the default `synchronous=True`:

* Every page record of the transaction and its commit record are in the log file, and `os.fsync` on the log returned without error.
* After a process crash, recovery restores all of the transaction's effects.
* After an OS crash or power loss, the same holds **if** the OS and the drive honour `fsync`.

It does **not** guarantee:

* Survival of a failed or lying disk.
* Anything about transactions after a damaged record in the middle of the log. A damaged record is indistinguishable from the end of the log, so later commits are lost.
* That the data file is up to date. It may be arbitrarily behind the log.

With `synchronous=False`, a commit survives a crash of the process but can be lost if the operating system crashes or the power fails. The database stays consistent either way.

If writing or syncing the log fails, the commit fails, the in-memory changes are rolled back, and the engine refuses further writes with `EngineFailedError` until it is reopened. It does not retry: after a failed `fsync` the OS may have dropped the data while reporting success on the second attempt. Reopening runs recovery, which settles whether the commit made it.

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

58 tests in `tests/crash_recovery/` start a child process, kill it with `os._exit` at an injected point (`mvrs_db/faults.py`), reopen, and check durability, atomicity and `Database.check()`. Two workloads are used: row changes across two indexes, and creating and dropping tables. Delta logging is on in all of them, and one test asserts that recovery after a kill really did rebuild pages by replaying deltas.

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
| Halfway through a page write, at four different moments (the 1st, 9th, 40th and 50th page write) | either, never half |
| During database creation | a usable empty database |
| During recovery (five points), twice in a row | same result as uninterrupted recovery |
| Log truncated at 25 offsets; a byte flipped in each of the last 12 records | a whole number of transactions survives |

Not tested: real power loss, drives that ignore `fsync`, damage in the middle of the log followed by valid records, and media failure. A killed process leaves the operating system running, so these tests cannot show that `fsync` itself works.

## Upgrade path (not built)

To support concurrent writers and transactions larger than memory, move to ARIES-style logging: per-operation log records, **steal** (uncommitted pages may be written), **undo** records and an undo pass, compensation log records so undo is itself recoverable, and a dirty-page table for fuzzy checkpoints.
