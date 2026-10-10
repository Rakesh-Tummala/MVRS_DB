# 05 — Write-ahead log, torn writes and recovery

Code: `kestreldb/recovery/wal.py`, `kestreldb/recovery/recovery_manager.py`, and the commit and checkpoint code in `kestreldb/database.py`. Tests: `tests/unit/test_wal.py`, `tests/unit/test_recovery.py`, `tests/crash_recovery/`.

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

### Log page after-images at commit

During a transaction nothing is logged. At commit, for each page the transaction dirtied, the engine appends the page's **after-image** to the log, then a commit record, then flushes and `fsync`s once. SQLite's WAL mode works the same way.

* Structural changes need no special treatment. Allocating a page, changing the free list and writing a catalog record are all just modified pages. The file header is not a page in the buffer pool, so it gets its own record type.
* Redo is "put this page back", which is naturally repeatable.
* An uncommitted transaction leaves at most a partial batch with no commit record, which recovery ignores.

The cost is log volume: a 4 KiB image for a one-byte change. Delta records (logging only the changed bytes) would fix that and are not implemented; see `LIMITATIONS.md`.

Alternative considered: one logical record per operation (insert row, split node), as in ARIES and PostgreSQL. It gives a much smaller log and is required for concurrent writers, but every operation needs its own redo code and structural changes need careful handling. See D-010.

### Rollback

When a transaction first modifies a page, the buffer pool keeps a copy of the page as it was (a **before-image**) in memory. Rollback copies the before-images back. They are never written to disk. Re-reading the page from disk would be wrong: under no-force, the in-memory page can hold earlier *committed* changes that have not reached the data file.

### Transaction size

No-steal means a transaction's dirty pages stay in the buffer pool until commit. A transaction that needs more dirty pages than the pool has frames fails with `TransactionTooLargeError` and is rolled back.

## Log file

`name.kdb.wal`: a 40-byte header, then records back to back.

Header (`<8sHHIQQ4sI`): magic `KESTWAL\0`, version, reserved, page size, `db_id`, `base_lsn`, reserved, CRC32 of the 36 bytes before it. `db_id` must match the data file, so a log can never be replayed into the wrong database.

Record (`<IIQQB` + payload):

| Field | Size | Notes |
|---|---|---|
| `length` | 4 | Payload length |
| `crc` | 4 | CRC32 of everything else in the record |
| `lsn` | 8 | Must equal the record's computed position |
| `txn_id` | 8 | |
| `type` | 1 | 1 `PAGE_IMAGE`, 2 `HEADER`, 3 `COMMIT` |
| payload | `length` | `PAGE_IMAGE`: page ID (4 bytes) + the page. `HEADER`: the 64-byte file header. `COMMIT`: empty. |

There is no BEGIN or ABORT record. A transaction's records are written together at commit, so the first record with a new `txn_id` is its beginning, and a transaction that aborts never writes anything.

**LSN** (log sequence number): a 64-bit number that only increases. It is `base_lsn` plus the record's byte offset after the header. When a checkpoint empties the log, `base_lsn` moves forward so LSNs never repeat. The data file header stores the LSN of the last checkpoint, so LSNs keep growing even if the log file is lost.

**Finding the end of the log.** Reading stops at the first record that is incomplete, has an impossible length, fails its CRC, or carries the wrong LSN. Everything from there on is treated as never written. The stored LSN catches leftover records from before a checkpoint that have valid CRCs.

**User-space buffer.** `append()` only adds to a buffer in the process. `flush()` hands the bytes to the OS and `fsync` makes them durable. A process killed before `flush()` loses them. Without this buffer, bytes passed to the OS would survive a process kill and a "written but not flushed" crash test would prove nothing.

## Commit, step by step

1. If the header changed, append a `HEADER` record.
2. For each changed page: set the page's `lsn` field to the LSN its record is about to get, then append a `PAGE_IMAGE` record.
3. Append a `COMMIT` record.
4. `flush()` and `fsync` the log.
5. Tell the buffer pool the transaction is over. Its pages are now ordinary dirty pages.

The transaction is committed when step 4 returns. If the process dies before then, the transaction never happened.

## Write ordering (the WAL rule)

A page may be written to the data file only when the log is durable up to that page's LSN. Two mechanisms enforce it:

* A page of an uncommitted transaction is never written at all (no-steal).
* Before writing any page, the buffer pool calls the log's `sync()`. With synchronous commits there is never anything left to sync. With `synchronous=False`, commits skip the `fsync`, and this call is what keeps a page from reaching disk ahead of its log record.

## What COMMIT guarantees

When `commit` returns successfully with the default `synchronous=True`:

* Every page image of the transaction and its commit record are in the log file, and `os.fsync` on the log returned without error.
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
* **Repair:** recovery treats a page with a bad checksum as older than any image and overwrites it whole. Every page written since the last checkpoint has an image in the log, because a checkpoint leaves all pages clean and every later change is logged at commit.
* **No image available:** a page that fails its checksum and has no image in the log was not damaged by a torn write we can repair. Nothing touches it during recovery; reading it later raises `ChecksumMismatchError`, and `Database.check()` reports it.

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
2. **Redo.** Apply the newest committed `HEADER`, if any. For each page, take the newest image belonging to a committed transaction; write it unless the page on disk has a valid checksum and an `lsn` at least as new.
3. **Finish.** Write the header, `fsync` the data file, check that the file length matches the header, then empty the log.

**Idempotent:** recovery only moves pages forward to a state the log fully describes, and leaves the log untouched until the data file is synced. Crashing during recovery and running it again reaches the same state.

## What is tested

58 tests in `tests/crash_recovery/` start a child process, kill it with `os._exit` at an injected point (`kestreldb/faults.py`), reopen, and check durability, atomicity and `Database.check()`. Two workloads are used: row changes across two indexes, and creating and dropping tables.

| Crash point | Outcome required for the transaction in flight |
|---|---|
| Before any log record is written | absent |
| Records in the user-space buffer, not written | absent |
| Half the commit's records written (truncated tail) | absent |
| Commit record written, not yet fsynced | present (a killed process cannot lose bytes the OS has) |
| Commit record fsynced, commit not yet returned | present |
| Start of, middle of, and end of a checkpoint | present |
| Between rewriting the log header and truncating it | present |
| Halfway through a page write, at four different moments | either, never half |
| During database creation | a usable empty database |
| During recovery (five points), twice in a row | same result as uninterrupted recovery |
| Log truncated at 25 offsets; a byte flipped in each of the last 12 records | a whole number of transactions survives |

Not tested: real power loss, drives that ignore `fsync`, damage in the middle of the log followed by valid records, and media failure. A killed process leaves the operating system running, so these tests cannot show that `fsync` itself works.

## Upgrade path (not built)

To support concurrent writers and transactions larger than memory, move to ARIES-style logging: per-operation log records, **steal** (uncommitted pages may be written), **undo** records and an undo pass, compensation log records so undo is itself recoverable, and a dirty-page table for fuzzy checkpoints.
