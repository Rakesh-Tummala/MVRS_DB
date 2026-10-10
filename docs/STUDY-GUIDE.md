# Study guide

The code was written straight through, without the stop-and-learn steps the original plan had. This guide is the replacement: a reading order, what to understand in each file, and the questions an interviewer is likely to ask.

A component is "learned" when you can close the file and answer its questions out loud.

## How to study a component

1. Read the design doc.
2. Read the source file top to bottom. Every module starts with a docstring that explains the idea.
3. Read its tests. They show the edge cases better than prose does.
4. Break it. Change one line, predict which test fails, run the tests, and see if you were right.
5. For the four hard cores (marked ★), delete the function body and write it again from the docstring and the tests.

Run the tests with:

```bash
.venv\Scripts\python.exe -m pytest -q -m "not crash"
```

## Reading order

| # | Topic | Design doc | Code | Tests |
|---|---|---|---|---|
| 1 | Pages and checksums | `03-storage-format.md` | `storage/page.py`, `storage/disk_manager.py` | `test_page.py`, `test_disk_manager.py` |
| 2 | Records | `03-storage-format.md` | `storage/record.py` | `test_record.py` |
| 3 | Slotted pages | `03-storage-format.md` | `storage/heap_page.py` | `test_heap_page.py` |
| 4 | Buffer pool ★ | `04-buffer-pool.md` | `storage/buffer_pool.py` | `test_buffer_pool.py` |
| 5 | Table storage | `03-storage-format.md` | `storage/heap_file.py` | `test_heap_file.py` |
| 6 | B+ tree ★ | `09-btree.md` | `index/keys.py`, `index/btree_node.py`, `index/btree.py` | `test_btree.py`, `property/test_btree_model.py` |
| 7 | Log ★ | `05-wal-and-recovery.md` | `recovery/wal.py` | `test_wal.py` |
| 8 | Recovery ★ | `05-wal-and-recovery.md` | `recovery/recovery_manager.py`, `faults.py` | `test_recovery.py`, `crash_recovery/` |
| 9 | SQL front end | `06-sql-grammar.md` | `sql/lexer.py`, `sql/parser.py`, `sql/ast.py` | `test_sql_parser.py` |
| 10 | Catalog | `03-storage-format.md` | `catalog/schema.py`, `catalog/catalog.py` | `integration/test_sql.py` |
| 11 | Execution | `01-architecture.md` | `execution/expressions.py`, `operators.py`, `executor.py` | `integration/test_sql.py` |
| 12 | Planner and optimiser | `planner/optimizer.py` docstring | `planner/planner.py`, `planner/optimizer.py` | `integration/test_planner.py` |
| 13 | Transactions and locks | `10-transactions.md` | `database.py`, `transactions/lock_manager.py` | `test_transactions.py`, `concurrency/` |

## Questions to be able to answer

### Storage
* Why fixed-size pages? Why 4096 bytes?
* What three different failures do the checksum, the page ID in the header, and the format version each catch?
* What is a torn write? Can a checksum repair one?
* In a slotted page, why does the slot directory grow one way and the records the other?
* Why must a slot number never change? What would break if compaction renumbered slots?
* What happens when an updated record no longer fits in its page?
* Why is the free-space map kept only in memory?

### Buffer pool
* What does pinning protect against?
* Why must a dirty page be written before its frame is reused?
* LRU or clock: what is the trade-off, and why LRU here?
* What does "no-steal" mean, and what does it cost? (Hint: `TransactionTooLargeError`.)
* Why does rollback restore before-images instead of re-reading pages from disk?

### B+ tree
* Why a B+ tree and not a hash table or a binary search tree?
* Why is the separator copied up on a leaf split but moved up on an internal split?
* Why does the root page never change, and how is that achieved?
* How does a range scan work?
* What does not rebalancing on delete cost?
* How can the tree compare keys without knowing their types?

### Log and recovery
* State the write-ahead rule. What breaks without it?
* Why is redo enough here, with no undo? Which two policies make that true?
* What exactly has happened by the time `COMMIT` returns? What has not?
* How does recovery know where the log ends?
* Why is recovery safe to run twice?
* How is a torn page repaired? When can it not be?
* Why do the crash tests kill a real process instead of raising an exception?
* What can these tests not tell you? (Power loss.)
* Why whole-page images instead of per-operation records? What would ARIES add?

### SQL and execution
* What does a lexer do that a parser does not?
* How does recursive descent encode operator precedence?
* What is `NULL = NULL`? Why does `WHERE NOT (x = 1)` skip rows where `x` is NULL?
* What is the iterator (Volcano) model, and why does it make `LIMIT` cheap?
* What is the Halloween problem, and where does the executor avoid it?
* Why is the full `WHERE` clause re-checked above an index scan?

### Optimiser
* When is a full scan faster than an index? Where is the crossover on this engine, and why is it so far to the right?
* How were the cost constants obtained?
* What assumption does the selectivity estimate make, and when is it wrong?
* Why can `ORDER BY id LIMIT 10` skip the sort?
* Why is that shortcut not used for a nullable `UNIQUE` column?

### Transactions
* What does strict two-phase locking guarantee? Why "strict"?
* Show a deadlock with two transactions and one table. How is it detected?
* Why only one writer? What would have to change to allow two?
* What isolation level does this give, and what does it cost?
* Why does a failed statement sometimes end the whole transaction?

### The project as a whole
* Why Python? What did that cost, with numbers?
* How does this compare with SQLite? (Same shape: embedded, single file, single writer. A small fraction of the features and speed.)
* What is the weakest part of the design?
* If you had another month, what would you build first, and why?

## Good experiments

* Remove the `set_page_lsn` call in `Database._log_and_commit` and see which recovery tests fail, and why.
* Make `BufferPool._make_room` ignore the transaction's write set. Which test catches it?
* Change a leaf split to move the separator up instead of copying it. What does `check_invariants()` say, and what does a lookup return?
* Run `benchmarks/run_benchmarks.py --only commit` and explain the gap between fsync on and off.
* Kill `examples/demo.py`'s child at a different point and predict what recovery will show.
