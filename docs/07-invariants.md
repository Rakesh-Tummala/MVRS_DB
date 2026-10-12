# 07 — Invariants and failure scenarios

An invariant is a statement that must be true whenever no operation is in progress. Each has an ID so tests and code comments can refer to it. "Checked by" says where it is enforced.

## File and pages

| ID | Invariant | Why it holds | Checked by |
|---|---|---|---|
| F1 | File length = `page_count × page_size` | The file only grows by whole pages, and `page_count` changes with it | Open |
| F2 | The file header has the right magic, a valid CRC, a known version and an allowed page size | Written only by our code, as one unit | Open |
| P1 | Every page on disk has a checksum matching its bytes | Computed at every write | Every read |
| P2 | A page's `page_id` field equals its position in the file | Set when the page is formatted, never changed | Every read |
| P3 | A page's type is one of the defined types, never 0 | Pages are formatted before first use | Every read |
| P4 | Every page ID used anywhere is in `1 .. page_count − 1` | IDs come only from the allocator | Disk manager bounds check |

## Heap pages

| ID | Invariant | Checked by |
|---|---|---|
| H1 | `36 + 4 × slot_count ≤ free_end ≤ P` | Page validator |
| H2 | Every live slot's `[offset, offset + length)` lies inside `[free_end, P)` | Page validator |
| H3 | No two live records overlap | Page validator |
| H4 | `frag_bytes` equals the bytes in `[free_end, P)` not covered by a live record | Page validator |
| H5 | A slot number, once returned as part of a record ID, refers to the same record until that record is deleted | Property tests |
| H6 | Following `next_page` from a table's first page ends at 0 without repeating a page | Table validator |

## Allocation

| ID | Invariant | Checked by |
|---|---|---|
| A1 | The free list ends at 0 without repeating a page, and every page on it has type "free" | `check_allocation()` |
| A2 | Every page from 1 to `page_count − 1` is reachable from exactly one place: the free list, the catalog chain, one table's heap chain, or one index's tree | `check_allocation()` |

A2 is the "no leaks, no double allocation" check that runs after every crash test.

## Buffer pool

| ID | Invariant |
|---|---|
| B1 | A page ID appears in at most one frame |
| B2 | A frame with `pin_count > 0` is never chosen as a victim |
| B3 | `pin_count` is never negative |
| B4 | A dirty page is written before its frame is reused |
| B5 | A page enters the pool only after its checksum and page ID are verified |
| B6 (Phase 4) | A page is written only when the log is durable through the commit record of the transaction that last changed it |
| B7 (Phase 4) | A page modified by the active transaction is never written |

## B+ tree

| ID | Invariant |
|---|---|
| T1 | Keys within a node are strictly increasing |
| T2 | For an internal node, every key in the subtree left of separator `k` is `< k`, and every key in the subtree to its right is `≥ k` |
| T3 | All leaves are at the same depth |
| T4 | Following right-sibling links from the leftmost leaf visits every leaf once, in key order |
| T5 | An internal node with `n` keys has `n + 1` children, all valid tree pages |
| T6 | No key appears twice in the leaves |
| T7 | Every record ID in a leaf refers to a live record whose key column encodes to that key |
| T8 | The root page ID never changes |

Each node page must also satisfy N1–N3 (see `09-btree.md`): its pointers fit below its cells, its cells lie inside the cell area without overlapping, and its hole count matches the space actually unused. `check_invariants()` verifies T1–T6, T8 and N1–N3 on the whole tree. T7 needs the table and is checked by a separate table-level validator.

## Catalog and data

| ID | Invariant |
|---|---|
| C1 | Table names are unique |
| C2 | Every record in a table decodes against that table's schema |
| C3 | Every live record with a non-NULL indexed column has exactly one entry in that index, and every index entry points at such a record |
| C4 | No two live records share a primary-key or unique value (not required of a secondary index, which keys each entry by value plus record ID) |
| C5 | Every statistics record names a table and a column that exist, has that column's type, and is the only one for it (checked when the catalog is loaded) |

## Log and recovery (Phase 4)

| ID | Invariant |
|---|---|
| W1 | LSNs strictly increase through the log |
| W2 | A transaction is committed if and only if its `COMMIT` record lies before the end of the valid log |
| W3 | After recovery, the database contains the effects of exactly the committed transactions |
| W4 | Running recovery twice gives the same state as running it once |
| W5 | For every page, on-disk `lsn` ≤ the LSN up to which the log is durable |
| W6 | A `PAGE_DELTA` is applied only to a page whose LSN equals the delta's `prev_lsn` |
| W7 | Every page written to the data file since the last checkpoint has a full `PAGE_IMAGE` in the log, committed before the first delta for that page |
| W8 | `execute()` and `COMMIT` return only after the log is durable through the caller's own commit record |
| W9 | A `SELECT` returns only after the log is durable through every commit record appended before it finished reading |
| W10 | A transaction's records are appended to the log as one unit, so a batch written by another thread never contains half a transaction |

## Failure scenarios

| Scenario | Detected by | Outcome |
|---|---|---|
| File is not a MVRS_DB file | Magic | `NotADatabaseError`; file untouched |
| File header damaged | Header CRC | `CorruptHeaderError`; file untouched |
| Created by a newer version | `format_version` | `UnsupportedVersionError` |
| File truncated or has a partial page | F1 | `CorruptFileError` |
| Page ID out of range | P4 | `InvalidPageIdError` |
| Page bytes damaged or torn | P1 | `ChecksumMismatchError`; from Phase 4, repaired from the log during recovery |
| Page written to the wrong place | P2 | `PageIdMismatchError` |
| Record bytes malformed | Record decoder | `CorruptRecordError` |
| Record too large | Encoder | `RecordTooLargeError`; nothing written |
| Every frame pinned | Buffer pool | `BufferPoolFullError` |
| Disk full or I/O error on write | `OSError` from the OS | Wrapped in `StorageIOError`; the page stays dirty in memory; from Phase 4 the transaction fails |
| Duplicate primary key | Index lookup | `ConstraintViolationError`; statement has no effect |
| Database already open in another process | Lock file / exclusive open | `DatabaseLockedError` |
| Crash mid-transaction (Phase 4) | No `COMMIT` in the log | Transaction's effects absent after recovery |
| Crash after commit, before pages flushed (Phase 4) | Page LSN behind the log | Redo restores them |
| Torn or partial log tail (Phase 4) | Record CRC, length, LSN | Tail discarded |
| `fsync` fails (Phase 4) | `OSError` | Every commit waiting on that batch fails; the engine refuses all statements, reads included, until reopened |
| Page corrupt with no image in the log (Phase 4) | P1 when the page is next read, or by `Database.check()` | `ChecksumMismatchError`; recovery cannot repair it |
| Log belongs to another database | `db_id` in the log header | `WalMismatchError` |
| Log header damaged | Header CRC | `CorruptWalError` |
| Transaction changes more pages than the pool holds | Buffer pool | `TransactionTooLargeError`; rolled back |
| Two transactions wait for each other | Waits-for graph | `DeadlockError` for one of them; rolled back |

Every error class derives from `MvrsDbError` in `errors.py`.

## Checking everything at once

`Database.check()` (also `mvrs_db check <file>`) verifies, for the whole database: P1–P3 for every page, H1–H6 for every heap, T1–T6, T8 and N1–N3 for every index, C1–C5, A1 and A2. It raises the first violation it finds. Every crash test and both model-based tests end by calling it.
