# 09 — B+ tree

Code: `mvrs_db/index/keys.py`, `btree_node.py`, `btree.py`. Tests: `tests/unit/test_keys.py`, `tests/unit/test_btree.py`, `tests/property/test_btree_model.py`.

## What it is for

An index answers "where is the row whose key is K?" without reading the whole table. A B+ tree keeps keys sorted in pages arranged as a shallow, wide tree, so a lookup reads one page per level. With 4 KiB pages and 8-byte keys a node holds about 230 entries, so three levels cover roughly 12 million rows.

Two properties make it the standard choice for databases:

* **It stays balanced.** Every leaf is at the same depth, so every lookup costs the same.
* **Leaves are linked.** A range query finds its first leaf with one descent and then walks sideways.

Alternatives: a hash index is faster for exact matches but cannot do ranges or ordering. An LSM tree is faster for writes but slower for reads and needs background compaction.

## Keys are bytes

The tree compares keys as raw bytes and never learns the column's type. `keys.py` encodes each type so that byte order equals value order:

| Type | Encoding |
|---|---|
| `INTEGER` | 8 bytes big-endian with the sign bit flipped |
| `REAL` | IEEE-754 bits, big-endian; sign bit set for positives, all bits inverted for negatives; `-0.0` stored as `0.0` |
| `TEXT` | UTF-8 (byte order equals code point order), at most 255 bytes |

Comparing two keys is then one `bytes` comparison, which runs in C.

## Node layout

Both node types use the common 24-byte page header, then:

| Offset | Size | Field |
|---|---|---|
| 24 | 2 | `key_count` |
| 26 | 2 | `free_end`: offset of the lowest cell byte |
| 28 | 4 | `link`: right sibling for a leaf (0 = none); leftmost child for an internal node |
| 32 | 4 | `frag_bytes`: bytes of the cell area that no live cell uses (holes left by deletes) |
| 36 | 2 × n | cell pointers, in key order |

Cells are written from the end of the page downward. They need not be contiguous: a delete leaves a hole where its cell was.

* Leaf cell: `[key_len u8][key][record page u32][record slot u16]`
* Internal cell: `[key_len u8][key][child u32]`

`frag_bytes` was a reserved field that was always zero, so pages written before in-place edits existed read as having no holes.

An internal node with keys k₁..kₙ has n+1 children. The leftmost child holds keys below k₁. The child stored with kᵢ holds keys from kᵢ up to, but not including, the next key.

## Operations

**Search** walks from the root, binary-searching the cell pointers of each page directly, without decoding the node.

**Insert** descends recursively to the leaf, choosing the child at each level by binary search (nothing is decoded on the way down). A duplicate key is reported here, before any page is touched. If the leaf has room, the entry is written **in place**: the pointer array is shifted by one entry from the insertion point, the cell is written just below the lowest existing cell, and `key_count` and `free_end` are updated. Only those bytes change. If there is room in total but not in one piece (the free gap is smaller than the cell, but holes make up the difference), the cells are first repacked, which clears `frag_bytes`. Otherwise the node **splits**:

* *Leaf split.* The entries are divided by bytes, not by count, since keys vary in length. The right half goes to a new page, the leaves are re-linked, and a **copy** of the right half's first key goes up as the separator. It must stay in the leaf too, because leaves hold all the data.
* *Internal split.* The middle key **moves** up and appears in neither half.
* *Root split.* The root's contents are copied to a new page and the root page is rewritten in place as the parent of the two halves. The root page ID therefore never changes (invariant T8), and the catalog never has to be updated because of a split.

Appending to the rightmost leaf splits unevenly, leaving the old leaf full. Keys that arrive in ascending order (the common case for an auto-numbered primary key) then fill leaves completely instead of leaving each one half empty.

When a child splits, the separator is added to its parent the same way: in place if it fits, by splitting the parent if not. Only a **split** decodes a node into Python lists and writes two nodes from scratch; that is rare, and short enough to check by eye.

**Delete** removes the key from its leaf and stops. The pointer array is closed up by one entry and the cell stays where it is as a hole, counted in `frag_bytes` (if it was the lowest cell, the free area simply grows over it; if it was the last cell in the node, the node resets). Nodes are not merged or rebalanced. A leaf may become underfull or empty; scans skip empty leaves, and later inserts in the same key range reuse the space. PostgreSQL makes the same choice. The cost is that a tree which shrinks a lot keeps its height and pages until the table is dropped.

### Why in place

Until this change every insert and delete decoded the whole node into lists and rewrote the whole page. That cost CPU in proportion to the number of keys on the page, and it also made the change look huge to the log: removing one key moved most cells, so the log record covered most of the page. Measured on 30,000 random keys, before and after:

| | Before | After |
|---|---|---|
| Insert, 4 KiB pages | 235 µs | 17 µs |
| Insert, 32 KiB pages | 1,536 µs | 17 µs |
| Delete, 4 KiB pages | 161 µs | 15 µs |
| Delete, 32 KiB pages | 1,454 µs | 16 µs |
| Log bytes for a random insert, 4 KiB (median) | 735 | 137 |
| Log bytes for a delete, 4 KiB (median) | 705 | 121 |
| Log bytes for a random insert, 32 KiB (median) | 7,162 | 993 |

The cost of an operation no longer depends on the page size. What remains in the log record is the pointer array shifted by one entry, which does grow with the page (about 2 bytes per key after the insertion point), plus the new cell and the header. Appending in key order moves no pointers and logs about 45 bytes. Reproduce: `python benchmarks/btree_ops.py`. The "before" figures are kept in `benchmarks/results/btree_ops_before.json`.

Through SQL this took deletes from a log 3.2 times smaller than whole-page logging to 13.9 times smaller, and made every single-row workload faster (inserts about 3,300 to 5,900 commits/s, updates 1,700 to 3,900, deletes 1,200 to 3,500, fsync off). See `benchmarks/log_volume.py`.

**Range scan** descends to the leaf for the lower bound and follows the sibling links, stopping at the upper bound.

## Duplicate keys

The tree stores each key once and rejects a second insert of the same key with `DuplicateKeyError`. That is all a primary-key or unique index needs. NULLs are never inserted, which is why a `UNIQUE` column can hold many of them.

## Invariants

`BTree.check_invariants()` walks the whole tree and verifies T1–T6 from `07-invariants.md`: keys ordered within a node, every key inside its subtree's bounds, all leaves at one depth, the leaf chain visiting every leaf once in order, valid child pointers with no page reachable twice, and no key appearing twice. It also checks the layout of every node (N1–N3 below). The model-based test calls it after every single operation of every generated sequence.

Node layout invariants, checked by `btree_node.validate()`:

| ID | Invariant |
|---|---|
| N1 | The pointer array fits below the cell area (`36 + 2 × key_count ≤ free_end ≤ page size`) |
| N2 | Every cell lies inside `[free_end, page size)` and no two cells overlap |
| N3 | `frag_bytes` equals the bytes of the cell area that no live cell uses |

N3 is what makes holes safe: it is how an insert knows whether repacking would free enough room, and how a corrupt count is noticed.

## What to be ready to explain

* Why the separator is copied on a leaf split but moved on an internal split.
* Why splitting by bytes matters when keys have different lengths.
* What would go wrong if the root page moved.
* What not rebalancing on delete costs, and when it would matter.
* Why the tree needs `node_capacity` only for tests (it forces deep trees from few keys).
* How an in-place insert works byte by byte, and why it leaves most of the page unchanged.
* What a hole is, how `frag_bytes` tracks it, and when a repack is needed.
* Why the cost of an insert used to grow with the page size and no longer does, and what still does grow.
* Why a node is still decoded for a split, and why that is acceptable.
