# Decision log

Each entry: what was decided, what else was considered, and why. Newest at the bottom. "Proposed" means it awaits your approval at the Phase 0 review.

## D-001 — Python 3.13, standard library only in the engine core
* **Alternatives:** C or Rust; third-party storage libraries.
* **Reason:** The goal is understanding the design. The core imports nothing outside the standard library, so it also runs on PyPy.
* **Date:** 2026-10-10

## D-002 — Add `storage/record.py` and `storage/heap_file.py` (proposed)
* **Alternatives:** Put the record codec in `page.py` and table storage in `catalog/` or `execution/`.
* **Reason:** The brief's tree had no home for either. Record encoding is independent of where bytes live, and a table-as-chain-of-pages is a storage concern that the executor should not implement.
* **Date:** 2026-10-10

## D-003 — Heap files with a separate B+ tree index
* **Alternatives:** Clustered storage, where rows live in the B+ tree leaves (SQLite, InnoDB).
* **Reason:** Slotted pages and the tree can be built and tested separately, and record IDs make the index contract simple. Cost: a primary-key lookup reads one extra page.
* **Date:** 2026-10-10

## D-004 — Page size fixed at creation, default 4096
* **Alternatives:** Hard-coded 4096.
* **Reason:** Phase 7 compares page sizes. 4096 matches the common OS page and disk block size.
* **Date:** 2026-10-10

## D-005 — File header has its own 64-byte layout and CRC
* **Alternatives:** Make page 0 an ordinary page.
* **Reason:** The page size must be read before any page can be interpreted.
* **Date:** 2026-10-10

## D-006 — CRC32 page checksum over everything after the checksum field
* **Alternatives:** No checksum; a stronger hash (xxHash, SHA).
* **Reason:** `zlib.crc32` is in the standard library, runs in C, and is the conventional choice for detecting torn and damaged pages. It is not a defence against deliberate tampering.
* **Date:** 2026-10-10

## D-007 — Stable slot numbers; record ID = (page, slot)
* **Alternatives:** Byte offsets as record IDs; forwarding pointers when a record moves.
* **Reason:** Compaction can move bytes without invalidating index entries. A record that outgrows its page gets a new ID and its index entries are updated, which is simpler than forwarding pointers.
* **Date:** 2026-10-10

## D-008 — Free pages in a linked list; free-space map in memory only
* **Alternatives:** Allocation bitmap pages; a persistent free-space map (PostgreSQL).
* **Reason:** A linked list needs no extra page type. The in-memory map cannot be wrong after a crash because it is rebuilt from the pages. Cost: the first write to a table after open walks its chain.
* **Date:** 2026-10-10

## D-009 — Index keys are order-preserving bytes; root pages never move
* **Alternatives:** Typed comparison in the tree; updating the catalog when a root splits.
* **Reason:** Byte comparison runs in C and keeps the tree ignorant of column types. A fixed root page means a split never touches the catalog.
* **Date:** 2026-10-10

## D-010 — Log whole-page after-images at commit (proposed; deviates from the brief)
* **Alternatives:** Per-operation records (insert/update/delete/structural) as listed in the brief and used by ARIES and PostgreSQL; full image only on first change after a checkpoint with deltas afterwards.
* **Reason:** With one writer and no-steal, nothing needs logging before commit. Page images make redo trivially repeatable, cover allocation, free-list, header and catalog changes with no special cases, and repair torn pages for free. This is how SQLite's WAL mode works. Cost: a large log, measured in Phase 7; delta records are an optional later milestone (4.6).
* **Date:** 2026-10-10

## D-011 — Rollback from in-memory before-images
* **Alternatives:** Re-read the page from disk; undo logging.
* **Reason:** Under no-force the in-memory page may hold committed changes that are not on disk, so re-reading would lose them. Undo logging is the ARIES path and out of scope.
* **Date:** 2026-10-10

## D-012 — One writer at a time through Phase 5
* **Alternatives:** Row-level locking with concurrent writers.
* **Reason:** Page images and page-level before-images are only correct if a page never holds changes from two uncommitted transactions.
* **Date:** 2026-10-10

## D-013 — LRU eviction
* **Alternatives:** Clock; LRU-K.
* **Reason:** Exact and easy to test deterministically. Clock's benefit is lower contention under many threads, which this engine does not have. Scan flooding is measured in Phase 7, not solved up front.
* **Date:** 2026-10-10

## D-014 — Three column types; NaN rejected
* **Alternatives:** Adding `BOOLEAN`, `BLOB`, dates; storing NaN.
* **Reason:** The smallest set that exercises fixed-width, variable-width and NULL handling. NaN breaks ordering, which the B+ tree depends on.
* **Date:** 2026-10-10

## D-015 — Every table has exactly one single-column primary key
* **Alternatives:** A hidden row ID for tables without a key (SQLite); composite keys.
* **Reason:** Every table then has an index path, and the key encoding stays simple.
* **Date:** 2026-10-10

## D-016 — No arithmetic expressions in SQL version 1
* **Alternatives:** Support `+ - * /`.
* **Reason:** The brief asks for comparisons and boolean predicates. Arithmetic adds type-promotion and overflow rules without teaching more about storage or recovery. It is an easy later addition.
* **Date:** 2026-10-10

## D-017 — A failed `fsync` is fatal for writes until reopen
* **Alternatives:** Retry the `fsync`.
* **Reason:** After a failed `fsync` the OS may discard the dirty data and report success on a retry, silently losing a commit.
* **Date:** 2026-10-10

## D-018 — A damaged record in the middle of the log ends the log
* **Alternatives:** Skip the bad record and continue.
* **Reason:** A torn tail and mid-log damage look the same. Continuing past a bad record could apply later changes on top of a missing one. Recorded as a limitation.
* **Date:** 2026-10-10

## D-020 — D-002 and D-010 approved; phases built in one pass
* **Decision:** I approved the extra storage modules and page-image logging with "go ahead", then asked for all phases to be built without stopping and with Claude writing every component, including the four I had reserved for myself.
* **Alternatives:** The original gated, write-it-yourself plan.
* **Reason:** I chose to get a complete, tested engine first and study it afterwards. `STUDY-GUIDE.md` is the plan for that. The risk is that I end up with code I cannot explain; the guide's questions are how I check.
* **Date:** 2026-10-11

## D-021 — Page allocation is logical until the page is written
* **Decision:** Allocating a page only increases the in-memory page count. The file grows when the page is first flushed, and the header on disk is updated at checkpoints.
* **Alternatives:** Extend the file immediately (the first disk manager did this).
* **Reason:** Extending the file inside a transaction would put uncommitted state on disk and would have to be undone on rollback. Cost: the header can lag the file after a crash, so the length check runs after recovery.
* **Date:** 2026-10-11

## D-022 — The file header is logged as its own record type
* **Decision:** `HEADER` records carry the 64-byte header; pages use `PAGE_IMAGE`.
* **Alternatives:** Keep page 0 in the buffer pool and log it as a page.
* **Reason:** The header is tiny and has its own layout and checksum. Snapshotting it at `BEGIN` and restoring it on rollback is one assignment.
* **Date:** 2026-10-11

## D-023 — No BEGIN, ABORT or CHECKPOINT log records
* **Alternatives:** The classic set of record types.
* **Reason:** A transaction's records are written together at commit, so nothing marks its start and an aborted one writes nothing. A checkpoint empties the log, so there is nothing for a checkpoint record to point at.
* **Date:** 2026-10-11

## D-024 — The WAL rule is enforced through a callback into the buffer pool
* **Alternatives:** Let `storage/` import `recovery/`; or rely on commits always being synced.
* **Reason:** With `synchronous=False` a committed page could otherwise reach the data file before its log record was durable. The callback keeps the layering rule and makes the pool enforce the ordering in every mode.
* **Date:** 2026-10-11

## D-025 — Dirtiness is declared at fetch, not at unpin
* **Alternatives:** `unpin(page_id, dirty=True)`, as originally sketched.
* **Reason:** The before-image has to be taken before the caller changes the page, which is at fetch. One flag at one moment cannot be forgotten.
* **Date:** 2026-10-11

## D-026 — B+ tree: no rebalancing on delete; uneven split when appending
* **Alternatives:** Merge and redistribute on underflow; always split in the middle.
* **Reason:** Rebalancing is the most error-prone part of a B+ tree and PostgreSQL also does without it. Splitting unevenly at the right edge keeps leaves full for keys that arrive in order, which roughly halves index size for auto-numbered keys.
* **Date:** 2026-10-11

## D-027 — B+ tree nodes are decoded and rewritten whole on change
* **Alternatives:** Edit cells in place inside the page.
* **Reason:** Split logic over Python lists is short and easy to check. Searches do not pay for it: they binary-search the page directly. Listed as a bottleneck in the benchmark report.
* **Date:** 2026-10-11

## D-028 — A statement that fails after changing pages rolls back its whole transaction
* **Alternatives:** Statement-level savepoints.
* **Reason:** Before-images are kept per transaction. A statement that fails before changing anything leaves the transaction open; the engine tells the cases apart by counting page changes.
* **Date:** 2026-10-11

## D-029 — Strict two-phase locking with table locks and a single writer slot
* **Alternatives:** Row-level locks; MVCC.
* **Reason:** Required by page-image logging (D-012), and table locks also prevent phantoms without extra machinery. New readers queue behind a waiting writer so writers are not starved, a problem the concurrency tests exposed.
* **Date:** 2026-10-11

## D-030 — Deadlock victim is the transaction that would close the cycle
* **Alternatives:** Abort the youngest, or the one that has done the least work.
* **Reason:** Simplest policy that is always correct. The application retries.
* **Date:** 2026-10-11

## D-031 — Optimiser constants come from measurement on a warm cache
* **Decision:** `ROW_CPU = 0.28`, `INDEX_ROW = 0.63`, `SORT_ROW = 0.005`, with one sequential page as the unit.
* **Alternatives:** Textbook constants that assume random disk I/O.
* **Reason:** Measured with `benchmarks/calibrate_optimizer.py`. With pages in memory an index fetch costs only about twice a scanned row, so the index stays worthwhile until a third to a half of the table matches. The constants would be wrong on a cold spinning disk.
* **Date:** 2026-10-11

## D-032 — The full WHERE clause is re-applied above every index scan
* **Alternatives:** Remove the conditions the index already guarantees.
* **Reason:** A small cost for the guarantee that range extraction can never change a query's result. A parametrised test compares every plan against a full scan.
* **Date:** 2026-10-11

## D-033 — One lock file per database, held with an operating-system lock
* **Alternatives:** A lock file whose mere existence means "locked".
* **Reason:** The OS releases the lock when the process dies, so a crash never leaves the database unopenable.
* **Date:** 2026-10-11

## D-034 — Project renamed from KestrelDB to MVRS_DB
* **Decision:** Package `mvrs_db`, command `mvrs_db`, base error `MvrsDbError`, files `.mvrs`, magic bytes `MVRS_DB\0` and `MVRSWAL\0`.
* **Reason:** To match the GitHub repository name. Earlier entries in this log keep the old name where they describe what was decided at the time.
* **Date:** 2026-10-11

## D-035 — Vectors are stored as 32-bit floats, self-describing in length
* **Decision:** A `VECTOR(n)` value is a `u16` element count followed by 4-byte floats. The declared dimension lives in the catalog and is enforced when a value is written.
* **Alternatives:** 64-bit floats; omitting the count and trusting the catalog.
* **Reason:** 32 bits is the norm for embeddings and halves the space, so a 768-dimension vector fits a 4 KiB page. Keeping the count in the record lets the record decoder validate a vector without knowing the table's definition. Cost: values come back rounded to 32-bit precision.
* **Date:** 2026-10-11

## D-036 — Vectors cannot be compared, ordered or indexed by the B+ tree
* **Alternatives:** Allow `=` on vectors; allow lexicographic ordering.
* **Reason:** Equality on floating-point vectors is almost never what a query means, and vectors have no useful total order. Rejecting these at planning time is clearer than returning surprising results. Similarity search is the intended way to query them.
* **Date:** 2026-10-11

## D-037 — The catalog stores a vector's dimension only for vector columns
* **Alternatives:** Bump the catalog record format and add the field to every column.
* **Reason:** Catalog records written before vectors existed decode unchanged, so no format version change was needed.
* **Date:** 2026-10-11

## D-038 — Nearest-neighbour search is written as ORDER BY with pgvector's operators
* **Decision:** `ORDER BY column <-> [vector] LIMIT k` (Euclidean) and `<=>` (cosine). The expression is also allowed in the `SELECT` list.
* **Alternatives:** A function such as `NEAREST(column, vector, k)`; a separate `SEARCH` statement.
* **Reason:** It reuses `ORDER BY ... LIMIT`, which already means "the best k", and anyone who has used pgvector can read it. Cost: the grammar gains its only expression form outside `WHERE`.
* **Date:** 2026-10-11

## D-039 — Rows with no distance sort last in both directions
* **Alternatives:** Follow the engine's general rule (NULL first ascending, last descending).
* **Reason:** A nearest-neighbour result that starts with the rows that could not be measured is useless. This is a stated exception, tested in both directions.
* **Date:** 2026-10-11

## D-040 — Exact search reads undecoded vectors when it can
* **Decision:** With NumPy installed and no `WHERE` clause, `VectorTopK` reads records from the heap, passes the stored float bytes to NumPy, and decodes only the winning rows.
* **Alternatives:** Always pull decoded rows from the child operator, as every other operator does.
* **Reason:** Measured. With decoded rows NumPy was only 1.3 to 1.5 times faster than pure Python, because building tuples of Python floats dominated. The raw path is 2.8 to 8.4 times faster than pure Python and beats a plain table scan. Cost: one operator bypasses the row abstraction, and queries with a `WHERE` clause do not benefit.
* **Date:** 2026-10-11

## D-041 — NumPy stays optional; pure Python is the fallback
* **Alternatives:** Require NumPy for vector queries.
* **Reason:** The engine still installs with no third-party packages, and the pure-Python `distance()` doubles as the readable reference the fast paths are tested against. Both run in every search test.
* **Date:** 2026-10-11

## D-019 — Optional dependencies only
* **Alternatives:** Make `typer` a hard dependency.
* **Reason:** The engine installs with no third-party packages. `typer`, test tools, `matplotlib` and `numpy` are extras.
* **Date:** 2026-10-10
