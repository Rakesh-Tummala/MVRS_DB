# 05 — Write-ahead log, torn writes and recovery

This is the policy for Phase 4, written now so the storage format (page LSNs, `db_id`) supports it from the start. You write the record format and the redo pass; the framing below is a proposal for you to confirm or change in milestone 4.1.

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

During a transaction nothing is logged. At commit, for each page the transaction dirtied, the engine appends the page's **after-image** to the log, then a commit record, then flushes and `fsync`s once. This is how SQLite's WAL mode works.

* Structural changes need no special treatment. Allocating a page, changing the free list, updating the file header and writing a catalog record are all just modified pages.
* Redo is "put this page back", which is naturally repeatable.
* An uncommitted transaction leaves at most a partial batch with no commit record, which recovery ignores.

The cost is log volume: a 4 KiB image for a one-byte change. A later, optional milestone logs a byte-range *delta* for a page that already has an image since the last checkpoint. Phase 7 measures the difference.

Alternative considered: one logical record per operation (insert row, split node), as in ARIES and PostgreSQL. It gives a much smaller log and is required for concurrent writers, but every operation needs its own redo code and structural changes need careful handling. See D-010. This is a deviation from the brief's list of record types (insert/update/delete/structural), which collapse into page images under this design.

### Rollback

When a transaction first modifies a page, the engine keeps a copy of the page as it was (a **before-image**) in memory. Rollback copies the before-images back. They are never written to disk. Re-reading the page from disk would be wrong: under no-force, the in-memory page can hold earlier *committed* changes that have not reached the data file.

### Transaction size

No-steal means a transaction's dirty pages stay in the buffer pool until commit. A transaction that needs more dirty pages than the pool has frames fails with `TransactionTooLargeError` and is rolled back.

## Log file

`name.kdb.wal` has a small header (magic, version, page size, `db_id`, `base_lsn`, CRC) followed by records.

**LSN** (log sequence number): a 64-bit number that only increases. It is `base_lsn` plus the record's byte offset after the header. When the log is emptied at a checkpoint, `base_lsn` moves forward so LSNs never repeat.

Proposed record framing:

| Field | Size | Notes |
|---|---|---|
| `length` | 4 | Payload length |
| `crc` | 4 | CRC32 of everything else in the record |
| `lsn` | 8 | Must equal the record's computed position |
| `txn_id` | 8 | |
| `type` | 1 | `BEGIN`, `PAGE_IMAGE`, `PAGE_DELTA`, `COMMIT`, `ABORT`, `CHECKPOINT` |
| payload | `length` | For `PAGE_IMAGE`: page ID + page bytes |

**Finding the end of the log.** Reading stops at the first record that is incomplete, has an impossible length, fails its CRC, or carries the wrong LSN. Everything from there on is discarded. The stored LSN catches leftover bytes from before a checkpoint that happen to have a valid CRC.

**User-space buffer.** The log writer accumulates records in memory. `flush()` hands them to the OS and `fsync` makes them durable. A process killed before `flush()` loses them. Without this buffer, bytes passed to the OS would survive a process kill and the "written but not flushed" crash test would prove nothing.

## Write ordering (the WAL rule)

A page may be written to the data file only when the log is durable up to that page's LSN. Under commit-time logging this means: only pages of committed transactions, and only after their commit `fsync`.

Each logged page has its header `lsn` set to the LSN of its image record before the image is taken.

## What COMMIT guarantees

When `commit()` returns successfully:

* Every page image of the transaction and its commit record are in the log file, and `os.fsync` on the log returned without error.
* After a process crash, recovery restores all of the transaction's effects.
* After an OS crash or power loss, the same holds **if** the OS and the drive honour `fsync`.

It does **not** guarantee:

* Survival of a failed or lying disk.
* Anything about transactions after a damaged record in the middle of the log. A damaged record is indistinguishable from the end of the log, so later commits are lost. This is a stated limitation.
* That the data file is up to date. It may be arbitrarily behind the log.

If `fsync` fails, the commit fails and the engine refuses further writes until reopened. It does not retry: after a failed `fsync` the OS may have dropped the data while reporting success on the second attempt.

## Torn pages

A torn page is a page write interrupted halfway: part new, part old.

* **Detection:** the page checksum fails on read.
* **Repair:** every page written to the data file was dirtied after the last checkpoint (a checkpoint leaves all pages clean), so its full image is in the log. Recovery overwrites the page with that image.
* **No image available:** if a page fails its checksum and the log has no image for it, the damage did not come from a torn write we can repair. The engine raises `UnrecoverableCorruptionError` and does not guess.
* **Phases 1–3:** torn pages are detected and reported but not repaired, because there is no log yet.

Assumption: a write that fits in one 512-byte disk sector is atomic. The log header and the data file header rely on this.

## Checkpoint

A checkpoint bounds the log's size and recovery time. It runs between transactions:

1. Write every dirty page to the data file. All are committed, because no transaction is active.
2. `fsync` the data file.
3. Append a `CHECKPOINT` record and `fsync` the log.
4. Advance `base_lsn` in the log header, `fsync`, then truncate the log to the header.

A crash at any step is safe: until step 4 completes, the old log is intact and redo simply repeats work. Truncation is done in place, because Windows cannot rename or delete an open file.

Checkpoints run when the log passes a size threshold, on clean close, and at the end of recovery.

## Recovery

On open, if the log has records:

1. **Scan** the log to its end and collect the transaction IDs that have a `COMMIT` record.
2. **Redo** in LSN order, for records of committed transactions only. For each page record: read the page; if its checksum is valid and its `lsn` is at least the record's LSN, skip it; otherwise apply the record and set the page's `lsn`.
3. **Checkpoint**, which empties the log.

**Idempotent:** recovery changes only pages whose LSN is behind the log, and leaves the log untouched until its final checkpoint. Crashing during recovery and running it again reaches the same state.

## Upgrade path (future work, not built)

To support concurrent writers and transactions larger than memory, move to ARIES-style logging: per-operation log records, **steal** (uncommitted pages may be written), **undo** records and an undo pass, compensation log records so undo is itself recoverable, and a dirty-page table for fuzzy checkpoints.

## What is and is not tested

Tested by killing a real child process at injected points: the crash matrix in `docs/ROADMAP.md`. Not tested: real power loss, drives that ignore `fsync`, damage in the middle of the log, and media failure.
