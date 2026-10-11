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
| 32 | 4 | reserved |
| 36 | 2 × n | cell pointers, in key order |

Cells are packed from the end of the page downward.

* Leaf cell: `[key_len u8][key][record page u32][record slot u16]`
* Internal cell: `[key_len u8][key][child u32]`

An internal node with keys k₁..kₙ has n+1 children. The leftmost child holds keys below k₁. The child stored with kᵢ holds keys from kᵢ up to, but not including, the next key.

## Operations

**Search** walks from the root, binary-searching the cell pointers of each page directly, without decoding the node.

**Insert** descends recursively to the leaf. If the leaf has room, it is rewritten with the new entry. Otherwise it **splits**:

* *Leaf split.* The entries are divided by bytes, not by count, since keys vary in length. The right half goes to a new page, the leaves are re-linked, and a **copy** of the right half's first key goes up as the separator. It must stay in the leaf too, because leaves hold all the data.
* *Internal split.* The middle key **moves** up and appears in neither half.
* *Root split.* The root's contents are copied to a new page and the root page is rewritten in place as the parent of the two halves. The root page ID therefore never changes (invariant T8), and the catalog never has to be updated because of a split.

Appending to the rightmost leaf splits unevenly, leaving the old leaf full. Keys that arrive in ascending order (the common case for an auto-numbered primary key) then fill leaves completely instead of leaving each one half empty.

**Delete** removes the key from its leaf and stops. Nodes are not merged or rebalanced. A leaf may become underfull or empty; scans skip empty leaves, and later inserts in the same key range reuse the space. PostgreSQL makes the same choice. The cost is that a tree which shrinks a lot keeps its height and pages until the table is dropped.

**Range scan** descends to the leaf for the lower bound and follows the sibling links, stopping at the upper bound.

## Duplicate keys

The tree stores each key once and rejects a second insert of the same key with `DuplicateKeyError`. That is all a primary-key or unique index needs. NULLs are never inserted, which is why a `UNIQUE` column can hold many of them.

## Invariants

`BTree.check_invariants()` walks the whole tree and verifies T1–T6 from `07-invariants.md`: keys ordered within a node, every key inside its subtree's bounds, all leaves at one depth, the leaf chain visiting every leaf once in order, valid child pointers with no page reachable twice, and no key appearing twice. The model-based test calls it after every single operation of every generated sequence.

## What to be ready to explain

* Why the separator is copied on a leaf split but moved on an internal split.
* Why splitting by bytes matters when keys have different lengths.
* What would go wrong if the root page moved.
* What not rebalancing on delete costs, and when it would matter.
* Why the tree needs `node_capacity` only for tests (it forces deep trees from few keys).
