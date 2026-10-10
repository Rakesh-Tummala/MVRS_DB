# 04 — Buffer pool

## Why it exists

Reading a page from disk is thousands of times slower than reading memory, and a B+ tree lookup touches the same root page on every query. The buffer pool keeps a fixed number of pages in memory, so hot pages are read once. It is also the single place that decides *when* a modified page may be written back, which is what crash recovery depends on.

## Structure

* A fixed array of **frames**. Each frame holds one page (`bytearray` of page size) plus `page_id`, `pin_count` and `dirty`.
* A **page table**: `page_id → frame`.
* A **replacement policy** that orders unpinned frames for eviction.

## Interface

| Call | Behaviour |
|---|---|
| `fetch_page(page_id)` | Return the page pinned. Loads it from disk if absent, evicting a victim first if the pool is full. |
| `new_page()` | Allocate a page ID, return a formatted page pinned and dirty. |
| `unpin_page(page_id, dirty)` | Decrease the pin count. `dirty=True` marks the page modified; it can never clear the flag. |
| `flush_page(page_id)` | Write the page if dirty and clear the flag. |
| `flush_all()` | Flush every dirty page, then `fsync`. |

A context manager wraps fetch/unpin so callers cannot forget to unpin.

## Rules

1. **A pinned page is never evicted.** A caller holding a page has a reference into the frame's memory; evicting it would hand that memory to a different page.
2. **A dirty page is written before its frame is reused.** Otherwise the change is lost.
3. **Every fetch is matched by exactly one unpin.** A pin count below zero is a bug and raises immediately.
4. **Checksums at the boundary.** The checksum is computed just before a page is written and verified just after it is read, together with the page ID. Code above the pool never sees an unverified page.
5. **No evictable frame is an error, not a hang.** If every frame is pinned, `fetch_page` raises `BufferPoolFullError`.

From Phase 4, two more rules apply to writing a dirty page:

6. **WAL rule.** The log must be durable up to the page's LSN before the page is written.
7. **No-steal.** A page modified by the active transaction is not evictable. If the pool fills with such pages, the transaction fails with `TransactionTooLargeError` and is rolled back.

## Eviction policy: LRU, with clock as the alternative

| | LRU | Clock |
|---|---|---|
| Idea | Evict the page unused for the longest time | Sweep a circular hand; a page gets a second chance if used since the last sweep |
| Cost per access | Move an entry in an ordered structure | Set one bit |
| Accuracy | Exact recency | Approximates LRU |
| In real engines | Rare in pure form | Common, because the per-access cost matters under many threads |

**Choice: LRU.** In Python an ordered dictionary gives exact LRU in a few lines, and an exact policy makes tests deterministic: a test can say precisely which page must be evicted next. Clock's advantage is avoiding lock contention on every access, which a single-writer Python engine does not have.

**Known weakness:** one large table scan pushes every hot page out of the pool. Real engines counter this with variants such as LRU-K or a midpoint insertion list. We measure the effect in Phase 7 instead of solving it up front.

You write the eviction and pin/unpin logic in milestone 1.4, against tests and a stub written first.

## Failure cases the tests cover

* Fetching a page while every frame is pinned raises and leaves the pool unchanged.
* A dirty page evicted and fetched again has its modifications.
* Unpinning a page that is not pinned raises.
* A page whose bytes on disk are damaged raises `ChecksumMismatchError` on fetch and does not enter the pool.
* A failed disk write during eviction leaves the page in the pool, still dirty.
