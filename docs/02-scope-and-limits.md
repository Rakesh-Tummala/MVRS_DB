# 02 — Scope, non-goals and limits

## In scope

* One database = one data file (`name.mvrs`) plus one log file (`name.mvrs.wal`, from Phase 4).
* Embedded use: the engine runs inside the calling Python process. One process opens a database at a time.
* Tables stored as heap files of slotted pages, with a B+ tree index on the primary key and on each `UNIQUE` column.
* Column types `INTEGER`, `REAL`, `TEXT`, each nullable unless declared otherwise.
* The SQL subset in `06-sql-grammar.md`.
* Crash recovery after process death (Phase 4), transactions with one writer and many readers (Phase 5).

## Non-goals

These are deliberate. Each one is a place where a real engine does more.

| Non-goal | What we do instead |
|---|---|
| Records larger than one page (overflow pages) | Reject with `RecordTooLargeError` |
| Concurrent writers | One writing transaction at a time (see `05-wal-and-recovery.md`) |
| Multi-process access to one database | Refuse to open a file that another process holds open |
| Composite (multi-column) keys | Single-column primary key and unique indexes only |
| Non-unique secondary indexes | Deferred; the documented route is to append the record ID to the key |
| Arithmetic in SQL (`a + 1`) | Operands are column names and literals only |
| Aggregates, `GROUP BY`, subqueries, views, triggers, foreign keys, `ALTER TABLE` | Clear "unsupported" parse error |
| Collations and locale-aware text ordering | Text compares by Unicode code point |
| Shrinking the file | Freed pages go on a free list and are reused; the file never gets smaller |
| Undo logging, stealing dirty pages | Redo-only, no-steal; ARIES is the documented upgrade path |
| Encryption, users, permissions, network server | Out of scope |
| Protection against a disk that lies about `fsync`, or media failure | Stated as a limitation, not tested |

## Limits

All limits are constants in `config.py` and are enforced with specific errors.

| Limit | Value | Reason |
|---|---|---|
| Page size | 4096 (default), 8192, 16384 or 32768 bytes, fixed at creation | In-page offsets are 16-bit |
| Page ID | 32-bit unsigned; page 0 is the file header | 4 bytes per reference |
| Record size | page size − 40 bytes (4056 at 4096) | One record must fit in one page with its slot |
| Index key size | 255 bytes encoded (confirmed in Phase 2) | Guarantees a useful fanout |
| `TEXT` value | Bounded by the record size; valid UTF-8 | No overflow pages |
| `INTEGER` | 64-bit signed | Fixed 8-byte encoding |
| `REAL` | 64-bit IEEE-754; NaN is rejected | NaN has no place in a total order |
| `VECTOR(n)` | n from 1 to 4096; finite 32-bit floats; must also fit in one record (about 1,000 dimensions at 4 KiB pages) | 4 bytes per element, no overflow pages |
| Columns per table | 64 | A table definition must fit in one catalog record |
| Identifier length | 63 bytes | Fits a 1-byte length |
| SQL statement length | 1 MiB | Bounded parser input |
| Buffer pool | 1024 frames by default, minimum 8 | Configurable |
| Transaction size (Phase 4+) | Fewer dirty pages than buffer pool frames | No-steal: uncommitted pages cannot be evicted |

## Durability statement by phase

* **Phases 1–3:** data survives a normal `close()`. After a crash the file may be missing recent changes or contain a page that fails its checksum. This is detected and reported, not repaired.
* **Phase 4 onward:** a successful commit survives process death. The precise guarantee is in `05-wal-and-recovery.md`.
