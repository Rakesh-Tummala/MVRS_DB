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

## D-019 — Optional dependencies only
* **Alternatives:** Make `typer` a hard dependency.
* **Reason:** The engine installs with no third-party packages. `typer`, test tools, `matplotlib` and `numpy` are extras.
* **Date:** 2026-10-10
