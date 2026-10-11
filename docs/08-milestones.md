# 08 — Milestone plan and acceptance criteria

## Status (2026-10-11)

| Phase | State |
|---|---|
| 0 Design | Done |
| 1 Storage | Done |
| 2 B+ tree | Done. Rebalancing on delete deferred, as the plan allowed. |
| 3 SQL and execution | Done. Inner joins (stretch) not attempted. |
| 4 Log and recovery | Done. Optional delta records (4.6) not built. |
| 5 Transactions | Done with table locks and one writer. MVCC (stretch) not attempted. |
| 6 Optimiser | Done: cost-based, constants fitted from measurements. |
| 7 Benchmarks | Done: `benchmarks/results/REPORT.md`. |
| 8 Vector search | Done: the `VECTOR(n)` column type, exact top-k search, and a simplified in-memory HNSW index evaluated for recall against the exact search. The index is not persisted. |

The phases were built in one pass rather than one gated milestone at a time, and all code, including the parts marked "you" below, was written by Claude at my request. `STUDY-GUIDE.md` is the plan for learning it afterwards. The table below is the original plan, kept for the acceptance criteria.

One milestone at a time. Each ends with `pytest`, `ruff` and `mypy` clean, `docs/DECISIONS.md` and `CLAUDE.md` updated, a commit, and a stop for approval.

"You" marks a hard core that you write against a stub and failing tests. Milestones are not sized in weeks, because the weekly time budget in `CLAUDE.md` is not filled in yet.

## Phase 1 — Persistent storage

| # | Milestone | Acceptance criteria |
|---|---|---|
| 1.1 | Environment, errors, page header, disk manager | `.venv` on W: with dev tools installed. Create/open/close a file with a valid header. `read_page`/`write_page` round-trip. Every open-time failure in `07-invariants.md` (F1, F2) raises its specific error and leaves the file byte-for-byte unchanged. Flipping any byte of a page raises `ChecksumMismatchError`. Out-of-range page IDs raise. Import-layering test passes. |
| 1.2 | Record codec and slotted heap page | Round-trip of every type, NULLs and boundary values (Hypothesis). Malformed bytes raise `CorruptRecordError`. Page insert/read/update/delete/compact with H1–H5 validated after every operation in a property test. Oversized record rejected. |
| 1.3 | Page allocation and free list | Allocate extends the file; free then allocate reuses the page. A1 and A2 hold after random allocate/free sequences and after reopen. |
| 1.4 | Buffer pool (**you:** eviction, pin/unpin) | B1–B5 tested. Pinned pages survive pressure; dirty pages are written before reuse; all-pinned raises; a failed write leaves the page dirty. |
| 1.5 | Heap file, catalog bootstrap, database open/close | A table's records survive close and reopen. Catalog page 1 exists in a new file. A second open of the same file is refused. Integration test: create, insert 10,000 records, reopen, read all back. |

## Phase 2 — B+ tree

| # | Milestone | Acceptance criteria |
|---|---|---|
| 2.1 | Key encoding, node layout, search | Byte order of encoded keys equals value order for all three types (Hypothesis). Search on hand-built trees. `check_invariants()` exists and rejects deliberately broken trees. |
| 2.2 | Insert with leaf split (**you**) | Inserts in ascending, descending and random order; invariants after every insert; duplicate key rejected. |
| 2.3 | Internal and root split (**you**) | Tree grows to at least 3 levels with a small page size; root page ID unchanged (T8). |
| 2.4 | Range scans | Forward scans with inclusive/exclusive bounds match a sorted reference list. |
| 2.5 | Delete without rebalancing | Deleted keys are gone; scans skip empty leaves; relaxed occupancy rule documented in `check_invariants()`. |
| 2.6 | Model-based property test and persistence | Random insert/delete/search sequences agree with a Python `dict` at every step; tree survives reopen. Merge/redistribute either done or recorded as deferred. |

## Phase 3 — SQL and execution

| # | Milestone | Acceptance criteria |
|---|---|---|
| 3.1 | Lexer | Every token kind; error positions; size limit. |
| 3.2 | Parser and AST | Every grammar rule; precedence tests; every unsupported construct gives a clear error. |
| 3.3 | Catalog, `CREATE TABLE`, `DROP TABLE` | Definitions survive reopen; dropped tables' pages return to the free list (A2). |
| 3.4 | `INSERT`, table scan, `WHERE`, projection | Type and NULL rules of `06-sql-grammar.md` each have a test. |
| 3.5 | Index scan, constraints, `UPDATE`, `DELETE` | Primary-key and unique violations rejected with no effect; C2–C4 hold after random statement sequences. |
| 3.6 | `ORDER BY`, `LIMIT`, `EXPLAIN`, CLI | `EXPLAIN` shows index vs table scan; CLI runs the examples end to end. |

## Phase 4 — WAL and recovery

| # | Milestone | Acceptance criteria |
|---|---|---|
| 4.1 | Log file and record format (**you:** format) | Append/read round-trip; truncated and corrupted tails detected at every byte position; user-space buffer loses records on kill before `flush()`. |
| 4.2 | Transactions around statements; before-images; commit path | Failed statement leaves no trace; B6 and B7 enforced; `TransactionTooLargeError` raised and rolled back. |
| 4.3 | Recovery redo pass (**you**) | W2–W4 on hand-built logs; recovery twice equals recovery once. |
| 4.4 | Checkpoint and torn-page repair | Log is emptied; torn page restored from its image; corrupt page with no image raises. |
| 4.5 | Crash-test matrix | Every crash point in `docs/ROADMAP.md`, using real process death; after each: committed present, uncommitted absent, all invariants including A2. |
| 4.6 (optional) | Delta records | Same crash matrix passes; log volume compared with image-only. **Done:** the matrix passes with deltas on; the log is 24 times smaller for single-row inserts and 14 times for updates, but only 1.1 to 3.2 times for bulk inserts and deletes, and commit speed shows no change beyond run-to-run noise. See `benchmarks/log_volume.py`. |

## Phases 5–8 (outline; detailed when reached)

| Phase | Milestones |
|---|---|
| 5 | `BEGIN`/`COMMIT`/`ROLLBACK`; table-level shared/exclusive locks with one writer; deadlock handling; concurrency tests for the anomalies claimed |
| 6 | Table statistics; selectivity and cost estimates; scan-vs-index choice in `EXPLAIN`; measurements that confirm or refute each rule |
| 7 | Benchmark harness; internal comparisons; optional SQLite baseline; report with reproduction commands |
| 8 | Optional vector column and exact top-k; optional HNSW with recall evaluation |

## Definition of done for any milestone

1. Acceptance criteria above are covered by tests that were run, with output shown.
2. No test weakened or skipped to pass.
3. New limitations written down.
4. A regression test for every bug found on the way.
