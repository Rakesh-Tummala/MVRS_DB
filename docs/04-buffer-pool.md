# 04 — Buffer pool

Code: `kestreldb/storage/buffer_pool.py`. Tests: `tests/unit/test_buffer_pool.py`.

## Why it exists

Reading a page from disk is thousands of times slower than reading memory, and a B+ tree lookup touches the same root page on every query. The buffer pool keeps a fixed number of pages in memory, so hot pages are read once. It is also the single place that decides *when* a modified page may be written back, which is what crash recovery depends on.

## Structure

* A fixed number of **frames**. Each frame holds one page (a `bytearray`) plus `page_id`, `pin_count` and `dirty`.
* A **page table**: an ordered dictionary `page_id → frame`. Its order is the eviction order: the first entry is the least recently used page.
* The **active transaction's write set**: `page_id → (before-image, was_dirty)`.

## Interface

| Call | Behaviour |
|---|---|
| `fetch(page_id, write=False)` | Return the page's buffer, pinned. Loads it from disk if absent, evicting a victim first if the pool is full. `write=True` marks it dirty and, inside a transaction, saves its before-image first. |
| `unpin(page_id)` | Release one pin. |
| `page(page_id, write=False)` | Context manager: fetch on entry, unpin on exit, even if an exception is raised. Almost all code uses this. |
| `allocate(page_type)` | Return the ID of a zeroed page: from the free list if there is one, otherwise by growing the file's logical page count. |
| `free(page_id)` | Put a page on the free list. Freeing a page twice raises. |
| `flush_page(page_id)`, `flush_all()` | Write dirty pages (and, for `flush_all`, the file header). Neither calls `fsync`. |
| `begin()`, `commit()`, `rollback()` | Start a transaction; end it keeping the changes; end it restoring every page and the header. |
| `transaction_pages()` | The pages the active transaction changed, for the commit code to log. |

Dirtiness is declared when the page is fetched, not when it is unpinned. That is the same moment the before-image must be taken, so one flag does both jobs and a caller cannot modify a page and forget to say so.

## Rules

| ID | Rule | Why |
|---|---|---|
| B1 | A page is in at most one frame. | Two copies could diverge. |
| B2 | A pinned page is never evicted. | The caller holds a reference to the frame's memory. |
| B3 | A pin count never goes below zero. | Unpinning without pinning is a bug and raises at once. |
| B4 | A dirty page is written before its frame is reused. | Otherwise the change is lost. |
| B5 | A page enters the pool only after its checksum and page ID are verified. | Code above the pool never sees an unverified page. A failed read changes nothing in the pool. |
| B6 | Before any page is written, the log is made durable. | The WAL rule. The pool calls a `before_page_write` function it was given; the database passes the log's `sync`. The pool itself does not know a log exists. |
| B7 | A page changed by the active transaction is never written or evicted. | No-steal: nothing uncommitted reaches the data file. |

If no frame can be evicted, `fetch` raises instead of waiting:

* every frame pinned → `BufferPoolFullError`
* the only unpinned frames belong to the active transaction → `TransactionTooLargeError`

## Eviction policy: LRU, with clock as the alternative

| | LRU | Clock |
|---|---|---|
| Idea | Evict the page unused for the longest time | Sweep a circular hand; a page gets a second chance if used since the last sweep |
| Cost per access | Move an entry in an ordered structure | Set one bit |
| Accuracy | Exact recency | Approximates LRU |
| In real engines | Rare in pure form | Common, because the per-access cost matters under many threads |

**Choice: LRU.** In Python an ordered dictionary gives exact LRU in a few lines, and an exact policy makes tests deterministic: a test can say precisely which page must be evicted next. Clock's advantage is avoiding lock contention on every access, which a single-writer Python engine does not have.

**Known weakness:** one large table scan pushes every hot page out of the pool. Real engines counter this with variants such as LRU-K or a midpoint insertion list. This engine does not.

## Transactions

`begin()` remembers the file header. From then on, the first `fetch(write=True)` of each page copies the page into the write set before the caller can change it. A page allocated by growing the file has no before-image; the write set records that instead.

`rollback()` copies every before-image back, drops pages that did not exist before, restores each page's previous dirty flag, and restores the header (which undoes allocations and frees). Re-reading the pages from disk would be wrong: a page in the pool may hold earlier *committed* changes that have not been written yet.

`commit()` only forgets the write set. Making the changes durable is the database layer's job (see `05-wal-and-recovery.md`); it reads `transaction_pages()` first.

## Thread safety

One re-entrant latch guards the page table, pin counts and the write set. Reading or changing a page's bytes happens outside the latch; the table-level locks above (see `10-transactions.md`) guarantee that nobody writes a page another transaction is reading.
