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

## D-027 — B+ tree nodes are decoded and rewritten whole on change (superseded by D-064)
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

## D-042 — The HNSW index lives in memory and is rebuilt from the table
* **Decision:** Only the declaration (`HNSW` or `HNSW COSINE`) is stored, in the catalog. The graph is built on first use after each open, kept up to date by inserts, updates and deletes, and discarded by a rollback that changed it.
* **Alternatives:** Store the graph in pages and log it like the B+ tree.
* **Reason:** A persistent graph needs its own page layout, logging and recovery, which is a project of its own. An in-memory index cannot be corrupted by a crash. Cost: the first approximate query after each open rebuilds it at about 2 to 3 ms per row.
* **Date:** 2026-10-11

## D-043 — The planner uses the vector index by rule, and falls back to exact search
* **Decision:** Index only when the column is declared for the query's distance, there is a `LIMIT`, nearest-first order, no `WHERE`, at least 1,000 rows, and NumPy. Otherwise exact.
* **Alternatives:** Use the index with a `WHERE` clause and filter afterwards; choose by estimated cost.
* **Reason:** Filtering after an approximate search can return fewer than k rows or miss matches entirely, and doing it properly is its own design problem. Rules are easy to state and to check in `EXPLAIN`. Adding an index can make a query approximate but never makes one fail.
* **Date:** 2026-10-11

## D-044 — Neighbours are chosen by the diversity heuristic, without filling spare slots
* **Alternatives:** Link to the closest candidates only; apply the heuristic and then fill unused slots with the closest rejected candidates.
* **Reason:** Measured on 5,000 vectors of 64 dimensions. Closest-only gave recall@10 of 0.64 at `ef_search` 100 on clustered data. The heuristic gave 0.99. Filling spare slots raised that to 1.00 but took 25 s to build against 11 s. With the index rebuilt on every open, build time decided it.
* **Date:** 2026-10-11

## D-045 — Default `ef_search` is 100
* **Alternatives:** A lower default for speed.
* **Reason:** Measured recall@10 at 100 was 0.87 to 0.97 across the three evaluation datasets, at 10 to 16 times the speed of exact search. At 50 recall fell to 0.75 to 0.85. The setting can be changed per database at any time.
* **Date:** 2026-10-11

## D-046 — Aggregation by hashing, in memory
* **Decision:** `HashAggregate` keeps one small running state per group in a dictionary keyed by the `GROUP BY` values.
* **Alternatives:** Sort the input by the group columns and aggregate runs of equal keys.
* **Reason:** One pass, no sort, and memory proportional to the number of groups. Sort-based aggregation can spill to disk when groups outgrow memory, which this engine does not attempt.
* **Date:** 2026-10-11

## D-047 — A selected column must be in GROUP BY or inside an aggregate
* **Alternatives:** Return an arbitrary row's value for such a column, as MySQL historically did.
* **Reason:** There is no correct single value, and silently picking one hides bugs. The error names the column.
* **Date:** 2026-10-11

## D-048 — COUNT(*), MIN and MAX are answered from metadata when possible
* **Decision:** With no `WHERE` and no `GROUP BY`, `COUNT(*)` reads slot counts from heap page headers and `MIN`/`MAX` of an indexed column read the first or last B+ tree key.
* **Alternatives:** Always scan; or answer `COUNT(*)` from the planner's cached row count.
* **Reason:** These are common queries and the stored structures already hold the answer. The cached row count is not used because it exists for cost estimates and a query's answer should come from stored data.
* **Date:** 2026-10-11

## D-049 — No HAVING, DISTINCT or expressions over aggregates
* **Reason:** Each needs general expressions in the select list, which the grammar does not have. Aggregates take a plain column, and results are named after the expression (`sum(qty)`), since there are no aliases.
* **Date:** 2026-10-11

## D-050 — Column names are resolved once, before planning
* **Decision:** `scope.py` rewrites every column name to `alias.column` for a join and to the bare name for one table. The joined row is described by a combined schema, so filters, sorts, aggregates and projection treat a join like a table.
* **Alternatives:** Carry (table, column) pairs through the whole planner and executor.
* **Reason:** Joins were added without changing the expression compiler, the aggregate planner or any existing operator. Ambiguous and unknown names are reported in one place.
* **Date:** 2026-10-11

## D-051 — Two join methods, chosen by cost
* **Decision:** Hash join always; index join as an alternative when the inner join column is a primary key or `UNIQUE` column of the same type as the outer one. The cheaper estimate wins.
* **Alternatives:** Nested loops over every pair; merge join; always hash.
* **Reason:** Comparing every pair is quadratic. Measured on a 20,000-row inner table, the index join is 90 times faster than the hash join for 10 outer rows and 5 times slower for 50,000, so neither can be the only method. Merge join needs sorted inputs and is left out.
* **Date:** 2026-10-11

## D-052 — Join cost constants come from measurement
* **Decision:** `HASH_ROW = 0.085`, `INDEX_LOOKUP = 2.6`, `INDEX_LEVEL = 0.55`.
* **Reason:** With first-guess constants the planner chose the index join at 3,000 outer rows, where it measured slower. After fitting to `benchmarks/join_methods.py` it chose the faster method at all seven tested sizes. Only one index height was measured, so the split between the two index constants is an assumption.
* **Date:** 2026-10-11

## D-053 — Single-table WHERE conditions are pushed below the join
* **Alternatives:** Apply the whole `WHERE` clause to the joined rows.
* **Reason:** Fewer rows are joined, and each table's own index can serve its condition. It is safe because only inner joins exist; an outer join would need care here.
* **Date:** 2026-10-11

## D-054 — Joins are limited to two tables and one equality
* **Alternatives:** Arbitrary join trees and conditions.
* **Reason:** A third table brings join-order optimisation, a subject of its own. For an inner join, extra conditions can go in `WHERE` with the same result, so a single equality in `ON` loses no expressive power.
* **Date:** 2026-10-11

## D-055 — A join locks both tables in name order
* **Reason:** A fixed order rules out two joins deadlocking against each other over the same pair of tables. The deadlock detector would catch it anyway; this avoids the rollback.
* **Date:** 2026-10-11

## D-056 — Log the bytes that changed, not the whole page (refines D-010)
* **Decision:** A changed page is logged as a `PAGE_DELTA`: the byte ranges that differ from the page's state when the transaction first touched it, plus the LSN of that state. A full `PAGE_IMAGE` is still used for a new page, for the first change to a page after a checkpoint, and for a change of more than half a page.
* **Alternatives:** Keep whole-page images; per-operation (logical) log records as in ARIES.
* **Reason:** Measured on 3,000 single-row inserts, the log went from 25.2 MB to 1.05 MB (8,401 to 350 bytes per commit), and recovery of single-row inserts and updates took 1.7 to 2.1 times less time in each of two runs. Logical records would shrink it further but need redo code for every operation; deltas reuse the existing page-level redo and before-images.
* **What it did not do:** Speed up commits. With fsync off, the throughput ratio (deltas over images) ran from 0.86 to 1.06 across workloads and varied by about 15% for the same workload between two runs, so there is no measurable change. With fsync on a commit took 23.0 ms with images and 22.8 ms with deltas, because a hard disk's sync latency dominates. Bulk inserts shrank only 1.1 times, since they write new pages, and deletes 3.2 times (D-058).
* **Date:** 2026-10-11

## D-057 — The first change to a page after a checkpoint is always a full image
* **Alternatives:** Log deltas from the start and rely on the page on disk as the base.
* **Reason:** If the disk tears a page while writing it, a delta has nothing intact to apply to. A full image earlier in the log is what lets recovery rebuild such a page, so it cannot be skipped. The engine records which pages have an image since the last checkpoint, and adds a page to that set only after its commit is durable.
* **Date:** 2026-10-11

## D-058 — Index deletes are poorly served by deltas, and that is accepted for now
* **Decision:** None needed in code; recorded because it was found by measurement. Removing one key rewrites a whole B+ tree node (D-027), which shifts many cells, so 52 of 200 single-row deletes produced a change over half a page and were logged whole.
* **Alternatives:** Edit B+ tree cells in place so that a delete moves few bytes.
* **Reason:** Editing nodes in place is a larger change to the most delicate code in the engine and would also cut insert CPU. It is first on the future-work list in `LIMITATIONS.md`.
* **Date:** 2026-10-11

## D-059 — A delta names the LSN it follows, and recovery refuses a chain that does not fit
* **Alternatives:** Apply deltas in log order without checking.
* **Reason:** A delta is only meaningful against the exact page state it was computed from. A mismatch means the log and the page have come apart; stopping with an error is safer than building a page that never existed.
* **Date:** 2026-10-11

## D-060 — Group commit by letting waiting threads take turns as the writer of the log
* **Decision:** Commits append to a shared in-memory buffer. The first thread that needs the log durable becomes the leader: it takes the whole buffer, writes it and calls `fsync` without holding the log's lock, and meanwhile others append to a fresh buffer and wait. The next waiter then leads, covering everything that piled up.
* **Alternatives:** A dedicated flusher thread; deliberately delaying a commit for a few milliseconds to collect more (PostgreSQL's `commit_delay`).
* **Reason:** No extra thread, no timer, and a lone commit pays no added delay. Batching happens only when commits genuinely overlap. Measured on a hard disk: 36 to 40 commits/s with one thread, 268 to 320 with 16 (two runs), 8.5 commits per `fsync`. A delay would help low concurrency at the price of latency for everyone; not built.
* **Date:** 2026-10-12

## D-061 — Early lock release, with a durability wait for readers
* **Decision:** A transaction releases its locks as soon as its commit record is appended, then waits for durability. A `SELECT` waits for everything appended so far before returning.
* **Alternatives:** Hold locks until the commit is durable (then no two commits to the same table can ever overlap, and group commit gains nothing for the common case).
* **Reason:** Overlap needs the locks free. Recovery stays consistent because the log is only lost from its end: a transaction that read an unlogged commit has its own commit record behind it. The wait for readers stops a caller from acting on data a crash could take back. Cost: a read that follows a commit by a few milliseconds may wait for its `fsync`.
* **Date:** 2026-10-12

## D-062 — A page waits for its transaction's commit record, not for its own log record
* **Decision:** On commit the pool records, for each page the transaction changed, the log position its `COMMIT` record ends at. Before writing the page it waits for the log to be durable to that position.
* **Alternatives:** Wait for everything appended (simple, correct, but any eviction waits for unrelated commits); wait for the page's own record (wrong).
* **Reason:** Found while testing. A page's own record being durable is not enough: the page could reach the disk, the process could die before the `COMMIT` record did, and recovery would discard the transaction while its page stayed. A mutation test (disabling this wait) is caught by a deterministic test; the first version of that test was a randomised one that did not catch it.
* **Date:** 2026-10-12

## D-063 — After a log write failure the engine stops, reads included
* **Alternatives:** Roll the failed commit back in memory and carry on (the earlier behaviour).
* **Reason:** After early lock release other transactions may already have seen and built on the failed commit, so rolling back only that transaction is no longer possible. Stopping everything until the database is reopened and recovery decides is the only consistent answer.
* **Date:** 2026-10-12

## D-064 — B+ tree nodes are edited in place (replaces D-027)
* **Decision:** An insert or delete that does not split changes the node page in place: shift the pointer array by one entry, write or forget one cell, update the header. A delete leaves a hole counted in `frag_bytes`; an insert that has room in total but not in one piece repacks the cells first. Only a split decodes the node into lists.
* **Alternatives:** Keep decoding and rewriting whole nodes (D-027); a different node layout with fixed-size slots.
* **Reason:** Measured, not assumed (D-027 had been recorded as a known bottleneck). Insert went from 235 µs to 17 µs on 4 KiB pages and from 1,536 µs to 17 µs on 32 KiB pages, and the cost no longer depends on the page size. The log record for an index change shrank 5 to 7 times, which took deletes from 3.2 times smaller than whole-page logging to 13.9 times. Cost: the node layout has holes, which needs a validator (N1–N3) and a repack path, both tested.
* **Date:** 2026-10-12

## D-065 — `frag_bytes` reuses the node header's reserved field
* **Alternatives:** Bump the file format version and add a field.
* **Reason:** The field was always written as zero, and zero means "no holes", which is exactly what every page written before this change has. Nothing needs migrating, and a page written by the new code is still valid to the old reading code as long as it has no holes.
* **Date:** 2026-10-12

## D-066 — A leaf with fewer than two keys, or an internal node with fewer than three, accepts an entry whatever the size cap
* **Alternatives:** Reject, and let the split code cope.
* **Reason:** A split needs at least two keys in a leaf (three in an internal node) to leave two non-empty halves, and the size cap (`node_capacity`) is a test knob for building deep trees from few keys. This was already the rule before in-place edits; it is recorded here because the in-place path had to reproduce it exactly, and the model-based tests with tiny caps are what check that it does.
* **Date:** 2026-10-12

## D-067 — Changed byte ranges are found in two levels: 1 KiB chunks, then 32-byte blocks
* **Alternatives:** Compare every 32-byte block (the first version); recursive bisection; a lower-level comparison written in C.
* **Reason:** Found by profiling after in-place B+ tree edits (D-064) removed the larger cost. `changed_ranges` was 52% of an insert on 32 KiB pages and 16% on 4 KiB pages, because it looped over every block of every changed page. Skipping unchanged chunks with one comparison each made it 4.9 times cheaper on 32 KiB pages (0.451 s to 0.093 s for 1,200 inserts) with identical output, checked against the original at all four page sizes with edits placed on the chunk edges. It is not free of page-size dependence: it still makes one comparison per chunk.
* **Date:** 2026-10-12

## D-019 — Optional dependencies only
* **Alternatives:** Make `typer` a hard dependency.
* **Reason:** The engine installs with no third-party packages. `typer`, test tools, `matplotlib` and `numpy` are extras.
* **Date:** 2026-10-10
