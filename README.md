# KestrelDB

A small relational database engine written from scratch in Python, built to learn how databases work inside.

It stores tables in its own checksummed page file, indexes them with a B+ tree, runs a subset of SQL through a hand-written parser and a cost-based planner, and survives being killed mid-transaction by replaying a write-ahead log. It does not use SQLite, PostgreSQL or any other database engine underneath.

> **This is a learning project, not a database to trust with real data.** It is slow (it is Python), it supports a small part of SQL, and its crash safety is tested against process death, not power loss. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## What it does

| Layer | Implementation |
|---|---|
| Storage | Fixed-size pages (4 KiB by default) with a CRC32 checksum, page ID and version in every header; slotted pages with stable record IDs; an explicit binary row format |
| Buffer pool | LRU eviction, pin counts, dirty tracking, and transaction before-images (no-steal) |
| Index | A persistent B+ tree with order-preserving byte keys, splits at every level, and linked leaves for range scans |
| SQL | `CREATE TABLE`, `DROP TABLE`, `INSERT`, `SELECT`, `UPDATE`, `DELETE`, `WHERE` with `AND`/`OR`/`NOT` and three-valued NULL logic, `ORDER BY`, `LIMIT`, `PRIMARY KEY`, `UNIQUE`, `NOT NULL`, `EXPLAIN` |
| Planner | Cost-based choice between a full scan and an index scan, sort elimination, top-N; constants fitted from measurements |
| Durability | Write-ahead log of page images, redo-only recovery, checkpoints, torn-page repair |
| Transactions | `BEGIN` / `COMMIT` / `ROLLBACK`, strict two-phase locking per table, one writer and many readers, deadlock detection |

## Install

Requires Python 3.12 or newer. The engine itself has no dependencies.

```bash
py -3.13 -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -e ".[dev,cli]"
```

On Linux or macOS use `python3 -m venv .venv` and `.venv/bin/python`.

## Use it from Python

```python
import kestreldb

with kestreldb.connect("shop.kdb") as db:
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL)")
    db.execute("INSERT INTO items VALUES (1, 'pen', 1.5), (2, 'book', 12.0), (3, 'lamp', 30.0)")

    result = db.execute("SELECT name, price FROM items WHERE price >= 10 ORDER BY price DESC")
    print(result.columns)  # ('name', 'price')
    print(result.rows)  # [('lamp', 30.0), ('book', 12.0)]

    db.execute("BEGIN")
    db.execute("UPDATE items SET price = 0 WHERE id = 1")
    db.execute("ROLLBACK")  # the update never happened

    for (line,) in db.execute("EXPLAIN SELECT * FROM items WHERE id = 2").rows:
        print(line)
```

Give each thread its own connection with `db.connect()`.

## Use it from the command line

```bash
.venv\Scripts\kestrel.exe shell shop.kdb
```

```
kestrel> SELECT * FROM items WHERE price < 20;
id | name | price
---+------+------
1  | pen  | 1.5
2  | book | 12.0
(2 rows)
kestrel> .tables
kestrel> .check
kestrel> .quit
```

Other commands: `kestrel exec <file> "<sql>"`, `kestrel info <file>`, `kestrel check <file>`.

## See it recover from a crash

```bash
.venv\Scripts\python.exe examples\demo.py
```

The demo creates a database, commits one transfer between two accounts, then kills a child process halfway through a second transfer. On reopening, the committed transfer is there, the half-finished one has left no trace, and the integrity check passes.

## Tests

```bash
.venv\Scripts\python.exe -m pytest -q
```

About 700 tests, taking roughly five minutes. Add `-m "not crash"` to skip the slow crash tests.

| Kind | What it checks |
|---|---|
| Unit | Every module alone, including every way a file, page, record or log can be corrupt |
| Property (Hypothesis) | The B+ tree and the whole engine against a Python dictionary, over random operation sequences |
| Integration | SQL semantics, constraints, transactions, the planner's choices, the CLI |
| Crash recovery | A child process is killed with `os._exit` at 18 injected points in two workloads, and during recovery itself; then durability, atomicity and every structural invariant are checked |
| Concurrency | Dirty reads, lost updates, non-repeatable reads, phantoms and deadlocks, with real threads |

`ruff` and `mypy --strict` pass on the engine.

## Benchmarks

```bash
.venv\Scripts\python.exe benchmarks\run_benchmarks.py
```

Results and the exact conditions are in [benchmarks/results/REPORT.md](benchmarks/results/REPORT.md). The report compares the engine with itself (index against scan, fsync on against off, buffer pool sizes, page sizes) and, for scale only, with SQLite.

## How it works

Start with [docs/STUDY-GUIDE.md](docs/STUDY-GUIDE.md), which gives a reading order and the questions each part should let you answer.

| Document | Contents |
|---|---|
| [01-architecture.md](docs/01-architecture.md) | The layers, and a query traced from text to bytes on disk |
| [03-storage-format.md](docs/03-storage-format.md) | Every byte of the file format |
| [04-buffer-pool.md](docs/04-buffer-pool.md) | Pinning, eviction, before-images |
| [09-btree.md](docs/09-btree.md) | Node layout, splits, range scans |
| [05-wal-and-recovery.md](docs/05-wal-and-recovery.md) | The log, the commit protocol, recovery, what COMMIT guarantees |
| [10-transactions.md](docs/10-transactions.md) | Locking, isolation, deadlock |
| [06-sql-grammar.md](docs/06-sql-grammar.md) | The supported SQL, as a grammar, with its type and NULL rules |
| [07-invariants.md](docs/07-invariants.md) | Every invariant, and what happens in each failure case |
| [DECISIONS.md](docs/DECISIONS.md) | Each design decision, its alternatives and the reason |
| [LIMITATIONS.md](docs/LIMITATIONS.md) | What is missing, and what to build next |

## Layout

```
kestreldb/
  storage/      pages, records, slotted pages, buffer pool, table storage
  index/        key encoding, B+ tree
  recovery/     write-ahead log, recovery
  catalog/      table definitions
  sql/          lexer, parser, syntax tree
  planner/      plan choice, cost model
  execution/    expressions, operators, statement execution
  transactions/ lock manager
  database.py   the public API; commit, checkpoint, locking
  cli.py        command-line interface
tests/          unit, property, integration, crash_recovery, concurrency
benchmarks/     the benchmark suite and its report
docs/           design documents
examples/       the crash-recovery demo
```

## How it was built

The design documents and the code were written by Claude (Anthropic's AI model) working from a project brief, in a single pass through the phases. The decisions made along the way are recorded in `docs/DECISIONS.md`.
