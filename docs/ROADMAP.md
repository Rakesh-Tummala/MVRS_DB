# MVRS_DB roadmap

The working rules and environment rules are in `CLAUDE.md`. This file holds the project vision, the architecture, and the requirements for each phase. Read the relevant phase before starting a milestone.

**Status:** Phases 0 to 7 are built. See `08-milestones.md` for what was deferred and `LIMITATIONS.md` for what is missing. Where this roadmap says "I write", the code was in fact written by Claude at my request; `STUDY-GUIDE.md` is how I learn it.

## Vision

MVRS_DB is an educational but technically rigorous persistent relational database engine written in Python. It must implement, by ourselves:

* Custom on-disk storage with fixed-size, checksummed pages
* A buffer pool
* A B+ tree index
* A SQL lexer, parser, planner, and execution engine
* A write-ahead log (WAL) and crash recovery
* Transactions and concurrency control
* A small, explainable query optimiser
* A reproducible benchmark suite
* An optional vector-search extension (last, only if everything else is solid)

This is not a CRUD app on top of PostgreSQL or SQLite. The storage format, indexing, query execution, transactions and recovery must be ours.

Build in stages. Each stage ends with a working, tested, committed milestone.

## Architecture

Clear, independently testable modules:

```
mvrs_db/
  __init__.py
  config.py
  errors.py
  storage/
    disk_manager.py
    page.py
    buffer_pool.py
    free_space.py
  index/
    btree.py
    btree_node.py
  catalog/
    schema.py
    catalog.py
  sql/
    lexer.py
    parser.py
    ast.py
  execution/
    operators.py
    executor.py
    expressions.py
  transactions/
    transaction.py
    lock_manager.py
    mvcc.py            # stretch goal
  recovery/
    wal.py
    recovery_manager.py
  planner/
    planner.py
    optimizer.py
  cli.py

tests/
  unit/  integration/  property/  crash_recovery/  concurrency/
benchmarks/
docs/            # design docs + DECISIONS.md + this file
examples/
pyproject.toml
README.md
CLAUDE.md
```

Adjust when justified, but explain major structural changes before making them. The storage engine must stay independent of the CLI, any API, and any frontend.

## Phase 0: Architecture and design (docs only)

Before any code, run `git init` and produce concise documents in `docs/`:

1. Architecture overview and data flow (query text to bytes on disk and back).
2. Scope and explicit non-goals. Overflow pages for records larger than one page are a non-goal. Concurrent writers are a non-goal (see Phase 5).
3. On-disk format: file header (magic number, format version, page size), page header (page type, format version, page ID, LSN, checksum), page body layout, slotted-page record layout, record encoding.
4. Page IDs, record IDs (page ID + slot), how indexes reference records, how storage is allocated and freed.
5. Catalog storage: where table and index definitions live on disk, and how the engine finds them when it opens a file (a reserved bootstrap page).
6. Limits: maximum record size (a record must fit in one page), maximum query size, maximum transaction size (see the no-steal limit in Phase 4).
7. Buffer pool behaviour (pinning, dirty tracking, eviction policy, and what happens when no frame can be evicted).
8. Torn-write strategy, as fixed in Phase 4: full-page images in the WAL.
9. The initial SQL grammar (BNF-style) for the supported subset.
10. Invariants and failure scenarios.
11. The WAL policy (Phase 4) stated up front, including the rollback mechanism, the checkpoint definition, and the upgrade path.
12. A milestone plan with acceptance criteria for each milestone. Confirm or adjust the indicative Phase 1 milestones (1.1 to 1.5) before any code.

Also create `docs/DECISIONS.md` (a running log: decision, alternatives considered, reason, date) and update it after every milestone. Create `pyproject.toml` (including the project-local pytest temp folder) and `.gitignore`.

Do not generate the whole project in one response.

## Phase 1: Persistent storage

Implement:

* **Disk manager:** read/write pages by page ID at explicit offsets, with `fsync` where required. On Windows this is one binary-mode file handle using seek + read/write, guarded by a lock, hidden behind `read_page`/`write_page` so no other module does file I/O.
* **Pages:** fixed-size pages with a versioned binary header and a per-page CRC32 checksum. Page size is chosen when the database is created, stored in the file header, and defaults to 4096 bytes. No module may hard-code 4096.
* **Records:** encoding/decoding with an explicit, versioned binary format (ints, floats, text, NULL handling). A record larger than one page is rejected with a specific error.
* **Slotted pages**, page allocation, and free-space management.
* **Buffer pool** with pin/unpin, dirty tracking, and an eviction policy (LRU or clock; explain the trade-off). I write eviction and pin/unpin.
* **Create/open/close/reopen** a database. A file header with a magic number and format version, plus the reserved catalog bootstrap page.

Requirements:

* Data survives normal restarts.
* Invalid page IDs, bad magic numbers, unsupported versions, corrupt headers and checksum mismatches raise controlled, specific errors. They never crash or silently return garbage.
* Page boundaries are checked on every read/write.
* Opening a file that is not a MVRS_DB file is refused without modifying it.
* The buffer pool is tested independently: pinned pages are never evicted; dirty pages are written before eviction; eviction when everything is pinned raises a clear error.
* Every page modification, including allocation, free-list and file-header updates, goes through one code path, so Phase 4 can add logging in one place.

Indicative milestones (Phase 0 confirms them): 1.1 page format, checksums, disk manager, corruption tests; 1.2 record encoding and slotted pages; 1.3 allocation and free space; 1.4 buffer pool; 1.5 create/open/reopen with the catalog bootstrap page.

## Phase 2: B+ tree indexing

A persistent B+ tree stored through the storage layer. Support:

* Search, insertion, leaf split, internal split, root split. I write insert and the split logic.
* A documented duplicate-key policy.
* Ordered and range scans via leaf links.
* **Deletion of entries (mandatory).** Phase 3 `DELETE` and `UPDATE` depend on it. Simple removal that leaves nodes underfull is acceptable.
* **Rebalancing on delete (merge/redistribute) is deferrable.** If deferred, document it and make `check_invariants()` reflect the relaxed minimum-occupancy rule.

Requirements:

* Keys stay ordered; parent/child links are valid; leaf links give correct ordered traversal.
* A `check_invariants()` routine validates the whole tree and is called throughout tests.
* Property-based tests (Hypothesis) apply random insert/delete/search sequences and compare against a Python reference model (a dict or sorted list), checking invariants after each step.
* Index state survives close/reopen.
* No substituting a Python dict for the real persistent index.

Hypothesis notes:

* Create the database inside the test body, not through a function-scoped fixture such as `tmp_path`. Hypothesis runs the body many times per fixture and raises a health-check error otherwise.
* Set a modest `max_examples` for disk-backed tests, since each example does real file I/O.

## Phase 3: SQL and query execution

Supported subset: `CREATE TABLE`, `INSERT`, `SELECT`, `WHERE`, `UPDATE`, `DELETE`, `DROP TABLE`, comparisons and boolean predicates (`AND`, `OR`, `NOT`), `ORDER BY`, `LIMIT`, primary-key/unique constraints, and simple inner joins as a stretch goal.

Implement a hand-written lexer, a recursive-descent parser, an AST, schema validation, a catalog persisted through the storage layer, and a pull-based (iterator/Volcano) execution engine with operators: table scan, index scan, filter, projection, sort, limit. Provide `EXPLAIN` showing the chosen plan.

Requirements:

* A documented SQL subset. Unsupported SQL returns a clear error, never a silent misinterpretation.
* Size limits on queries and records.
* Single-process, single-writer execution.
* Type checking and NULL semantics are defined and tested.
* No crash safety yet: state this in the docs until Phase 4 lands.

## Phase 4: Write-ahead logging and crash recovery

**Chosen policy (do not over-scope):**

* **Redo-only logging with a no-steal buffer pool.** Dirty pages of uncommitted transactions are never written to disk.
* **Force-log-at-commit.** The commit record is `fsync`'d before commit returns. Dirty pages may be flushed lazily after commit ("no-force").
* **Single writer.** This policy assumes one writing transaction at a time. Page-level redo, full-page images and page-level before-images are all unsafe if two uncommitted writers can touch the same page.
* **Implicit transactions.** Until Phase 5 adds `BEGIN`/`COMMIT`, every statement is its own auto-commit transaction with a transaction ID and begin/commit log records.
* **All structural changes are logged.** Page allocation, free-list changes, file-header updates and catalog changes (`CREATE TABLE`, `DROP TABLE`) are logged and recovered like data changes. Otherwise recovery can leak pages, allocate one twice, or lose a table definition.
* **Rollback uses in-memory before-images.** Under no-force, a page in the pool can hold committed changes that are not on disk yet, so a failed statement cannot be undone by re-reading the page from disk. Keep before-images in memory for the running transaction and restore from them. They are never written to disk, so the log stays redo-only.
* **Transaction size limit.** No-steal means a transaction that dirties more pages than the buffer pool can hold cannot proceed. Raise a specific "transaction too large" error, roll back, and list this as a known limitation.
* **WAL buffer.** The WAL keeps a user-space buffer. Records in it are lost if the process dies before `flush()` + `fsync`. Without this buffer, bytes handed to the OS survive a process kill and the "written but not flushed" crash test proves nothing.
* **Page records, written at commit (D-010, D-056).** Every page a transaction changed is logged when it commits, as a full after-image or, where possible, as a delta of the bytes that changed. This replaces the per-operation records first planned. Torn-page repair works because the first change to a page after a checkpoint is always a full image: recovery rebuilds a page whose checksum fails from that image plus the deltas after it. A checksum failure with no image in the log is reported as corruption, not papered over.
* **Checkpoint.** Taken between transactions: flush all committed dirty pages, `fsync` the data file, then empty the log. A checkpoint never flushes pages of an active transaction (no-steal).
* **Recovery** = scan the log, redo committed transactions' changes that are not yet reflected on disk (using page LSNs), ignore uncommitted ones.

Document the upgrade path to undo/redo (ARIES-style: steal, undo, CLRs, concurrent writers) in `docs/` as future work.

Implement:

* Log sequence numbers, transaction IDs, log record types (page image, file header, commit), per-record checksums, and detection of a truncated or corrupt log tail.
* Defined write ordering: log records for a page change are durable before that page reaches persistent storage (the WAL rule).
* A precise, written definition of what a successful COMMIT guarantees, and what it does not.
* A recovery manager and the checkpoint above.

Crash tests must use real process death: a child process that calls `os._exit` at an injected crash point, or is killed by the parent with `Popen.kill()`. Then reopen and verify. Ordinary exceptions are not crashes. Cover at least:

* before a log record is written
* after a log record is in the WAL buffer but before it is flushed
* after the commit record is durable
* after commit but before dirty pages are flushed
* during a page write (torn page, simulated by writing part of a page and then exiting)
* during a structural change (page allocation, `CREATE TABLE`, `DROP TABLE`)
* during recovery itself (recovery must be idempotent: crash and recover again)
* a truncated or corrupted log tail

After every crash test, verify that committed data is present, uncommitted data is absent, tree/page invariants hold, and no page is leaked or allocated twice.

Do not claim production-grade crash safety. State precisely what is tested and what is not. Real power-loss behaviour depends on the OS and disk honouring `fsync`, which we cannot fully test; process death does not exercise it.

## Phase 5: Transactions and concurrency

Start conservative: `BEGIN`, `COMMIT`, `ROLLBACK`, and atomicity. Rollback reuses the in-memory before-images from Phase 4.

**One writer at a time, with concurrent readers.** The Phase 4 policy is only correct with a single writing transaction, so Phase 5 keeps it. Concurrent writers belong to the ARIES upgrade path and are listed as future work.

Locking design: strict two-phase locking with table-level shared/exclusive locks, plus a single writer slot. State the isolation guarantees explicitly. Lock upgrades (a reader that becomes the writer) can still deadlock, so include deadlock prevention or detection.

Write concurrent tests that mix readers with a writer and verify that the anomalies you claim to prevent (dirty reads, lost updates, and others) do not occur. Do not claim serializability unless it is genuinely implemented and validated.

Stretch goal: MVCC with transaction IDs, visibility rules, version chains, snapshot isolation, and garbage collection. Python's GIL limits parallel speed-up; the goal is demonstrating correct semantics, not throughput. Only attempt this after Phases 0–4 are solid.

## Phase 6: Query optimisation

A small, explainable optimiser: index selection, predicate pushdown, basic table statistics, selectivity estimates, and scan-vs-index cost comparison, all visible through `EXPLAIN`. Validate decisions with real measurements; do not claim an optimisation helps just because it looks good in theory. A rule-based planner is acceptable if cost-based proves too large.

## Phase 7: Benchmarking and reliability

A reproducible suite measuring: sequential insert throughput, point-lookup latency, range-scan throughput, query latency, index build time, transaction throughput, recovery time, and memory use. Use multiple dataset sizes and repeated runs.

Primary comparisons are internal (these are honest in Python): indexed lookup vs full scan, WAL with `fsync` vs without, different buffer-pool sizes, different page sizes (possible because page size is a creation-time setting). SQLite may be included only for clearly equivalent operations and documented configuration, with a prominent note that SQLite is C and MVRS_DB is Python, so absolute numbers are not comparable in spirit.

Every report states: dataset size, hardware, drive type of `W:`, OS, Python version, configuration, whether Windows Defender real-time scanning was active (it distorts file I/O timings), warm vs cold cache, median and tail latency (p95/p99) where useful, throughput, limitations, and known bottlenecks. Never fabricate numbers; include the exact command to reproduce each result.

## Phase 8: Optional vector search

Only after the relational engine and recovery are stable. Vector column type, exact top-k (cosine/Euclidean) with NumPy, then optionally a simplified HNSW with a recall-vs-latency evaluation against the exact baseline. Keep it modular so it cannot compromise relational correctness.

## Security and data integrity

Validate database paths and page IDs; reject malformed or corrupt files safely; no untrusted deserialisation; size limits on queries and records; protect files from accidental overwrite; define behaviour for disk-full and I/O errors; make transaction and recovery failures explicit; avoid SQL injection in any server interface (parameterised queries only).

## Deliverables

* Working source and automated tests (unit, integration, property-based, crash-recovery, concurrency where applicable).
* Installation instructions and a CLI with practical examples.
* Architecture diagram; storage-format and recovery-protocol documents.
* Supported-SQL list; known limitations; future work.
* Reproducible benchmark scripts with honest results.
* `docs/DECISIONS.md` kept up to date.
* A concise technical case study of the key design decisions (written with me at the end, in my own words where possible).
* A demo script: create a database, populate it, query, close, reopen, crash it (kill the process mid-transaction), and show recovery restoring a consistent state.

## First task

Start with Phase 0 only: design documents and no implementation code. Inspect the repository, run `git init` if it is not a repository yet, then produce the twelve documents listed under Phase 0, plus `docs/DECISIONS.md`, `pyproject.toml` and `.gitignore`.

Then stop. Summarise the design and wait while I review it. Do not write any storage, page, or disk-manager code until I explicitly approve Phase 1. When I approve, Phase 1 begins with milestone 1.1: the page format with checksums, the disk manager, and tests (including corruption-detection tests).

Build MVRS_DB as if another engineer will inspect every design decision, test every claim, and challenge every benchmark.
