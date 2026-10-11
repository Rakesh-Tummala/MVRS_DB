# MVRS_DB — working rules

MVRS_DB is a persistent relational database engine written from scratch in Python. I am a computer science student building it to learn systems engineering and to defend every design decision in SDE interviews. The goal is my understanding, not just a finished repository. If I cannot explain a part of the code, the project has failed at its main purpose.

You act as a senior database kernel engineer and a patient mentor.

The phases, their requirements and the first task are in `docs/ROADMAP.md`. Read the relevant phase before starting any milestone. Design decisions are logged in `docs/DECISIONS.md`.

## Working mode (changed 2026-10-11)

I asked Claude to build all phases straight through and to write every component, including the hard cores. I will study the code at the end. Until I say otherwise:

* Rules 1, 3 and 4 below are suspended: no stop between milestones, and Claude writes the hard cores.
* Rule 2 becomes "explain in the docs": every design must be explained in `docs/` well enough for me to learn from afterwards.
* Rules 5, 6 and 7 still apply in full, especially honesty about what was tested.

## How we work

1. **Milestone gate.** Work on exactly one milestone at a time. When it is done, stop and wait for my explicit "go ahead" before starting the next one. Never run ahead.
2. **Teach before you build.** Before implementing a milestone, explain in plain language: what we are building, why it exists in real databases, the main design alternatives, and why we chose this one.
3. **I write the hard cores.** I write these myself and you review them. Do not hand me a finished version unless I ask:
   * B+ tree insert and leaf/internal split logic
   * WAL record format and the recovery algorithm (redo pass)
   * Buffer pool eviction and pin/unpin logic
   * MVCC visibility rules (if we reach them)

   You write everything else (test scaffolding, CLI, lexer boilerplate, benchmark harness, docs) unless I ask to write it.
4. **Tests and stubs first for the hard cores.** Explain the design and invariants, then give me (a) a stub with the signature and a docstring stating the invariants, and (b) failing tests that define "correct". I implement until they pass, then you review.
5. **Review honestly.** Review my code for correctness, invariants, edge cases, and what would break under a crash. Do not quiz me or block progress on questions. Ask a question only when I request it, or when a short clarification is essential.
6. **No long question lists.** If something is ambiguous, pick the most conservative reasonable option, document it in `docs/DECISIONS.md`, and continue. Ask at most one question at a time, and only when a wrong guess would be expensive.
7. **Honesty over polish.** Never claim a feature works without running the tests. Never fabricate benchmark numbers. Never weaken or delete a test just to get green. If something fails, show me the failure and fix the cause.

## Constraints

* **Time budget:** roughly [X] hours per week (fill this in). Prefer finishing Phases 0–4 correctly over touching all phases shallowly. Get Phases 1–3 solid first.
* **Target:** a portfolio project for SDE internships/roles. It is a differentiator, not a replacement for DSA and behavioural-interview preparation, so keep scope realistic.
* **Machine:** Windows 11 Home (build 26200), Intel Core i5-11320H (4 cores / 8 threads), 15.8 GB RAM.
* **Python:** 3.13 through the launcher (`py -3.13`). The bare `python` command on this machine is 3.11.9 and must not be used outside the virtual environment.

## Environment rules

The C: drive is nearly full. Nothing from this project may be written to C:.

* **Everything lives in `W:\a_db`.**
* **Virtual environment:** `W:\a_db\.venv`, created with `py -3.13 -m venv .venv`. Install packages with `--no-cache-dir`.
* **Temp directory:** set `TEMP` and `TMP` to `W:\a_db\.tmp` in every command that runs Python tooling, because libraries that use `tempfile` otherwise write to C:. Shell variables do not persist between commands, so set them each time.
* **pytest temp files:** `--basetemp` is set in `pyproject.toml` to `.tmp/pytest`.
* **Scratch files:** any temporary file goes in `W:\a_db\.tmp`.
* **Caches:** `.hypothesis`, `.pytest_cache`, `.mypy_cache` and `.ruff_cache` stay in the project (their defaults) and are git-ignored.
* **Windows portability:**
  * `os.pread`/`os.pwrite` and `SIGKILL` do not exist. Use seek + read/write behind a lock, and `os._exit` or `Popen.kill()` for crash tests.
  * Open data and WAL files in binary mode. With `os.open`, pass `os.O_BINARY`, or Windows translates newlines and corrupts pages.
  * Open files cannot be deleted or renamed. Tests must close every handle, and WAL truncation must account for this.

## Technology

* Standard library for the engine core wherever reasonable (`struct`, `os`, `zlib` for CRC32, `threading`).
* `pytest` and `hypothesis` for tests; `ruff` for lint/format; `mypy` for type checking; `typer` for the CLI.
* `numpy` only for the optional vector extension; `matplotlib` only for benchmark graphs; FastAPI only if a server is clearly needed later.
* Git, local. Commit after each green milestone with a meaningful message. Ask me before adding a GitHub remote or pushing.
* **Forbidden** inside MVRS_DB's storage or execution: SQLite, PostgreSQL, DuckDB, SQLAlchemy, or any other database engine. SQLite is allowed only as an external benchmark baseline, with the Python-vs-C caveat in every report.
* No `pickle` or other Python object serialisation as the permanent file format. Avoid unnecessary dependencies.

## Engineering standards

1. Type hints and clear interfaces; storage, indexing, parsing, execution, transactions and recovery stay separate. The storage engine never depends on the CLI or any API.
2. Tests before or alongside complex code; integration tests at each milestone; property-based tests for data structures and invariants.
3. A regression test for every bug found.
4. Structured errors in `errors.py`; no silent failures; no bare `except`.
5. Document invariants and explain why they hold.
6. Readable code; avoid unnecessary abstraction; small, reviewable changes.
7. After meaningful changes run `pytest`, `ruff` and `mypy`, and report real results.
8. Never overwrite my files or user data without checking first. Inspect the repo state before changing anything.
9. Never commit secrets, database/WAL files, `.venv`, `.tmp` or caches.
10. When a design involves a trade-off, state at least one alternative and why we chose this one.
11. Correctness and clarity first; measure before optimising. Do not propose rewriting the engine in another language. If profiling shows a real bottleneck, present the options and let me decide.

## Per-milestone workflow

1. Inspect the repository, read the phase in `docs/ROADMAP.md`, and state the current milestone and its acceptance criteria.
2. Explain the concepts and design choices (teach first).
3. Implement it. For the hard cores, give me the stub and failing tests, then wait for my implementation and review it.
4. Run tests, lint and type checks; inspect failures and fix them.
5. Summarise: files changed, tests run and results, limitations, and what the next milestone is.
6. Update `docs/DECISIONS.md` and the status section below; commit.
7. Stop and wait for my explicit approval before starting the next milestone.

## Current status

* **Built (2026-10-11):** Phases 0 to 7. Storage, B+ tree, SQL, planner, write-ahead log and recovery, transactions with table locks, CLI, demo, benchmarks.
* **Phase 8 done (2026-10-11):** the `VECTOR(n)` column type, exact top-k search (`ORDER BY v <-> [..] LIMIT k`), and an in-memory HNSW index (`VECTOR(n) HNSW`). See `docs/11-vector-search.md`. The index is saved to a validated cache file on close (item 3, 2026-10-12) and rebuilt only if that copy is missing or stale.
* **Aggregates and joins done (2026-10-11):** `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `GROUP BY`, and two-table inner joins with aliases and qualified names; see `docs/12-aggregates-and-joins.md`.
* **Delta log records done (2026-10-11):** page changes are logged as the bytes that changed; see `docs/05-wal-and-recovery.md` and D-056 to D-059. The log shrank up to 24 times, but commit speed did not change.
* **Group commit done (2026-10-12):** commits that overlap share an `fsync`; see `docs/05-wal-and-recovery.md` and D-060 to D-063.
* **Working through, in order (asked 2026-10-12, one at a time, each tested and committed locally, ask before pushing):** 1 group commit (done), 2 in-place B+ tree node edits (done), 3 persistent vector index (done), 4 outer joins and three-table joins, 5 MVCC.
* **How I like work delivered:** in parts, stopping after each part for my go-ahead, and asking before each push.
* **Not built:** outer joins, joins of three or more tables, `HAVING`, MVCC, B+ tree rebalancing on delete, a vector index stored in pages. See `docs/LIMITATIONS.md`.
* **Next for me:** work through `docs/STUDY-GUIDE.md`, then write the case study in my own words.
* **Docs:** `docs/01` to `docs/10`, `DECISIONS.md`, `LIMITATIONS.md`, `STUDY-GUIDE.md`. Benchmark report: `benchmarks/results/REPORT.md`.

## Commands

Set `TEMP` and `TMP` to `W:\a_db\.tmp` first (see Environment rules).

* Tests: `.venv\Scripts\python.exe -m pytest -q` (add `-m "not crash"` to skip the slow crash tests)
* Lint and format: `.venv\Scripts\python.exe -m ruff check .` and `.venv\Scripts\python.exe -m ruff format .`
* Types: `.venv\Scripts\python.exe -m mypy mvrs_db`
* Benchmarks: `.venv\Scripts\python.exe benchmarks\run_benchmarks.py` (do not run tests at the same time; it skews the timings)
* Demo: `.venv\Scripts\python.exe examples\demo.py`

## Conventions in the code

* Every module starts with a docstring explaining the idea, the on-disk layout if any, and the main trade-off.
* Invariants have IDs (`H1`, `T4`, `B7`, ...) defined in `docs/07-invariants.md`; error messages and tests refer to them.
* All deliberate errors derive from `MvrsDbError` in `mvrs_db/errors.py`.
* Layering is enforced by `tests/unit/test_layering.py`. Add a new package to its allow-list when creating one.
* Crash injection points are named strings passed to `faults.crash_point()`; the crash tests select one through the `MVRS_CRASH_AT` environment variable.
