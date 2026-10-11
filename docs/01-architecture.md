# 01 — Architecture and data flow

## Layers

MVRS_DB is a stack of layers. Each layer talks only to the layer directly below it, so each can be tested alone.

```
        SQL text
           |
   sql/lexer, sql/parser        text  -> tokens -> AST
           |
   planner (+ catalog)          AST   -> validated plan (names, types, access path)
           |
   execution/                   plan  -> rows, pulled one at a time (iterators)
           |
   +-------+---------+
   |                 |
 storage/heap_file  index/btree  rows <-> records in pages; key -> record ID
   |                 |
   +-------+---------+
           |
   storage/buffer_pool          in-memory cache of pages: pin/unpin, dirty, evict
           |
   +-------+---------+
   |                 |
 storage/disk_manager  recovery/wal    the only two modules that touch files
   |                 |
 name.mvrs          name.mvrs.wal
```

| Module | Owns | Knows nothing about |
|---|---|---|
| `storage/disk_manager` | Reading and writing whole pages by page ID; the file header; `fsync` | What is inside a page body |
| `storage/page` | Page header, checksum, slotted-page layout | Tables, SQL |
| `storage/record` | Encoding a row to bytes and back | Where the bytes are stored |
| `storage/buffer_pool` | Which pages are in memory, pin counts, dirty flags, eviction | Page contents |
| `storage/free_space` | Free-page list and per-table free-space map | Records |
| `storage/heap_file` | A table as a chain of slotted pages; insert/read/update/delete by record ID | Indexes, SQL |
| `index/btree` | Ordered map from key bytes to record ID | Tables, column types |
| `catalog` | Table and index definitions, persisted in the catalog pages | Query execution |
| `sql` | Tokens and syntax trees | Storage |
| `planner` | Name/type checks, choosing scan vs index | Bytes on disk |
| `execution` | Operators that produce rows | File I/O |
| `recovery` | Log records, commit durability, redo after a crash | SQL |
| `transactions` | Transaction lifetime, locks, before-images | SQL syntax |

Modules that the original brief's tree did not list, and why they exist:

| Module | Why |
|---|---|
| `storage/record.py`, `storage/heap_page.py`, `storage/heap_file.py` | The record codec, the slotted-page layout and table storage are three separate jobs (D-002). |
| `index/keys.py` | Order-preserving key encoding, kept apart from the tree so the tree stays type-agnostic. |
| `database.py` | The public `Database` and `Connection` classes: opening, recovery, locking, commit, checkpoint. It is the only module that knows about every layer. |
| `faults.py` | Crash injection points used by the crash tests. A leaf module like `config.py` and `errors.py`. |

`transactions/mvcc.py` from the brief does not exist, because MVCC was not built.

## What happens on `SELECT name FROM users WHERE id = 7`

1. **Lexer** turns the text into tokens: `SELECT`, identifier `name`, `FROM`, ...
2. **Parser** builds an AST: a `Select` node with a column list, a table name and a `Compare(id = 7)` predicate.
3. **Planner** looks up `users` in the catalog, checks that `name` and `id` exist and that `id = 7` is type-correct, sees that `id` is the primary key, and picks an index scan. The plan is `Project(name) <- IndexScan(users.pk, key=7)`.
4. **Executor** asks the top operator for the next row. `Project` asks `IndexScan`.
5. **B+ tree** encodes `7` as key bytes, walks from the root page to a leaf, and finds the record ID `(page 12, slot 3)`. Every page it touches is fetched through the buffer pool and unpinned when done.
6. **Heap file** fetches page 12 from the buffer pool, reads slot 3, and returns the record bytes.
7. **Record codec** decodes the bytes into `(7, "asha", ...)`. `Project` keeps `name`.
8. **Buffer pool**: if page 12 was not in memory, it picked a victim frame, asked the disk manager for the page, and verified the checksum and page ID before handing it over.

## What happens on `INSERT INTO users VALUES (8, 'ravi')`

1. Lexer, parser and planner as above. The planner checks types and that `id` is not NULL.
2. The executor checks the primary-key index for `8`. If it exists, the statement fails before anything changes.
3. The heap file finds a page with enough free space, writes the record into a new slot, and gets back a record ID.
4. The B+ tree inserts `key(8) -> record ID`, splitting nodes if a leaf is full.
5. Every modified page is marked dirty in the buffer pool.
6. **Phases 1–3:** dirty pages reach disk on eviction or on close. A crash can lose or corrupt data.
7. **Phase 4 onward:** at commit, a record for each dirtied page (the changed bytes, or the whole page when it is new or was just checkpointed) plus a commit record are appended to the WAL and `fsync`'d. Only then does the statement report success. The pages themselves are written to the data file later.

## Dependency rule

A module may import from layers below it, never from layers above. `storage/` imports nothing from `index/`, `sql/`, `execution/` or `cli.py`. `tests/unit/test_layering.py` enforces this by parsing every source file's imports against an allow-list per layer.

Two places bend the simple stack, both deliberately:

* The planner builds trees of operators, so it sits above `execution/operators.py` and `execution/expressions.py`. Only `execution/executor.py`, which drives the planner, imports it back.
* The buffer pool must not write a page before the log is durable, but `storage/` may not import `recovery/`. The database passes the log's `sync` function into the pool as a plain callback.
