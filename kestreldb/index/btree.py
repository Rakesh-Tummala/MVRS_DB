"""A persistent B+ tree mapping key bytes to record IDs.

Shape
    Internal nodes hold separator keys and child pointers and only route
    searches. Leaves hold every key with its record ID and are linked left to
    right, so a range scan finds its starting leaf once and then walks sideways.

Unique keys
    A key appears at most once. Inserting an existing key raises
    DuplicateKeyError. That is exactly what primary-key and unique indexes need.

Splits
    A node that would overflow is split in two, and a separator key goes up to
    the parent. For a leaf the separator is a *copy* of the right half's first
    key (the key itself must stay in a leaf). For an internal node the middle
    key *moves* up. If the parent overflows in turn, the split continues upward.

The root never moves
    When the root splits, its contents are copied to a new page and the root
    page is rewritten in place as the new root. The catalog can therefore refer
    to an index by its root page forever (invariant T8).

Deletion
    Deleting removes the key from its leaf and stops. Nodes are not merged or
    rebalanced, so a leaf may become underfull or empty; scans skip empty
    leaves, and the space is reused by later inserts into the same key range.
    PostgreSQL makes the same choice. The cost is that a tree which shrinks a
    lot keeps its height and its pages until the index is rebuilt.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterator

from kestreldb import config
from kestreldb.errors import CorruptIndexError, DuplicateKeyError, KeyTooLargeError
from kestreldb.index import btree_node as node
from kestreldb.storage.buffer_pool import BufferPool
from kestreldb.storage.heap_file import RecordId
from kestreldb.storage.page import PAGE_HEADER_SIZE, PageType, new_page, page_type_of

_MAX_HEIGHT = 64  # far above anything reachable; a deeper descent means a cycle
_LEAF = int(PageType.BTREE_LEAF)
_INTERNAL = int(PageType.BTREE_INTERNAL)


class BTree:
    def __init__(self, pool: BufferPool, root_page: int, *, node_capacity: int | None = None):
        """`node_capacity` caps the bytes used per node. Tests use it to build deep
        trees from few keys; everything else leaves it at the page size."""
        self._pool = pool
        self._root = root_page
        self._capacity = node_capacity or pool.page_size
        if not node.CELLS_START < self._capacity <= pool.page_size:
            raise ValueError("node capacity must be above the node header and within a page")

    @staticmethod
    def create(pool: BufferPool) -> int:
        """Allocate an empty tree (a single empty leaf) and return its root page ID."""
        page_id = pool.allocate(PageType.BTREE_LEAF)
        with pool.page(page_id, write=True) as buf:
            node.init_leaf(buf)
        return page_id

    @property
    def root_page(self) -> int:
        return self._root

    # ------------------------------------------------------------------ search

    def _leaf_for(self, key: bytes | None) -> int:
        """Page ID of the leaf that would hold `key` (the leftmost leaf for None)."""
        page_id = self._root
        for _ in range(_MAX_HEIGHT):
            with self._pool.page(page_id) as buf:
                page_type = page_type_of(buf)
                if page_type == _LEAF:
                    return page_id
                if page_type != _INTERNAL:
                    raise CorruptIndexError(f"page {page_id} is not a B+ tree page")
                if key is None:
                    page_id = node.decode_internal(buf)[1][0]
                else:
                    page_id = node.internal_child(buf, key)
        raise CorruptIndexError("B+ tree is deeper than any valid tree; it contains a cycle")

    def get(self, key: bytes) -> RecordId | None:
        """Point lookup: one page per level, binary search within each page."""
        page_id = self._root
        for _ in range(_MAX_HEIGHT):
            with self._pool.page(page_id) as buf:
                page_type = page_type_of(buf)
                if page_type == _LEAF:
                    return node.leaf_find(buf, key)
                if page_type != _INTERNAL:
                    raise CorruptIndexError(f"page {page_id} is not a B+ tree page")
                page_id = node.internal_child(buf, key)
        raise CorruptIndexError("B+ tree is deeper than any valid tree; it contains a cycle")

    def scan(
        self,
        low: bytes | None = None,
        high: bytes | None = None,
        *,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
    ) -> Iterator[tuple[bytes, RecordId]]:
        """Yield (key, record ID) in key order for keys within the bounds."""
        page_id = self._leaf_for(low)
        first = True
        seen: set[int] = set()
        while page_id:
            if page_id in seen:
                raise CorruptIndexError(f"T4: leaf chain loops back to page {page_id}")
            seen.add(page_id)
            with self._pool.page(page_id) as buf:
                if page_type_of(buf) != _LEAF:
                    raise CorruptIndexError(f"T4: page {page_id} in the leaf chain is not a leaf")
                keys, rids, page_id = node.decode_leaf(buf)
            start = 0
            if first and low is not None:
                start = bisect_left(keys, low) if low_inclusive else bisect_right(keys, low)
            first = False
            for index in range(start, len(keys)):
                key = keys[index]
                if high is not None and (key > high or (key == high and not high_inclusive)):
                    return
                yield key, rids[index]

    # ------------------------------------------------------------------ insert

    def insert(self, key: bytes, rid: RecordId) -> None:
        if len(key) > config.MAX_KEY_SIZE:
            raise KeyTooLargeError(
                f"key of {len(key)} bytes exceeds the {config.MAX_KEY_SIZE}-byte limit"
            )
        split = self._insert(self._root, key, rid, 0)
        if split is not None:
            self._grow_root(*split)

    def _insert(
        self, page_id: int, key: bytes, rid: RecordId, depth: int
    ) -> tuple[bytes, int] | None:
        """Insert below `page_id`. Returns (separator, new right page) if it split."""
        if depth > _MAX_HEIGHT:
            raise CorruptIndexError("B+ tree is deeper than any valid tree; it contains a cycle")
        with self._pool.page(page_id) as buf:
            page_type = page_type_of(buf)
            if page_type == _LEAF:
                keys, rids, link = node.decode_leaf(buf)
            elif page_type == _INTERNAL:
                keys, children = node.decode_internal(buf)
            else:
                raise CorruptIndexError(f"page {page_id} is not a B+ tree page")

        if page_type == _LEAF:
            return self._insert_into_leaf(page_id, keys, rids, link, key, rid)

        index = bisect_right(keys, key)
        split = self._insert(children[index], key, rid, depth + 1)
        if split is None:
            return None
        separator, new_child = split
        keys.insert(index, separator)
        children.insert(index + 1, new_child)
        # An internal node needs three keys to split: one moves up, and each half
        # must keep at least one. (Only a tiny test capacity can overflow sooner.)
        if node.internal_size(keys) <= self._capacity or len(keys) < 3:
            with self._pool.page(page_id, write=True) as buf:
                node.encode_internal(buf, keys, children)
            return None

        # Internal split: the middle key moves up; it is not kept in either half.
        middle = _split_point(keys, node.INTERNAL_CELL_OVERHEAD)
        middle = max(1, min(middle, len(keys) - 2))
        right_id = self._pool.allocate(PageType.BTREE_INTERNAL)
        with self._pool.page(right_id, write=True) as buf:
            node.encode_internal(buf, keys[middle + 1 :], children[middle + 1 :])
        with self._pool.page(page_id, write=True) as buf:
            node.encode_internal(buf, keys[:middle], children[: middle + 1])
        return keys[middle], right_id

    def _insert_into_leaf(
        self,
        page_id: int,
        keys: list[bytes],
        rids: list[RecordId],
        link: int,
        key: bytes,
        rid: RecordId,
    ) -> tuple[bytes, int] | None:
        index = bisect_left(keys, key)
        if index < len(keys) and keys[index] == key:
            raise DuplicateKeyError("key already exists in the index")
        keys.insert(index, key)
        rids.insert(index, rid)
        if node.leaf_size(keys) <= self._capacity or len(keys) < 2:
            with self._pool.page(page_id, write=True) as buf:
                node.encode_leaf(buf, keys, rids, link)
            return None

        # Leaf split: the separator is a copy of the right half's first key.
        if link == 0 and index == len(keys) - 1:
            # Appending to the rightmost leaf (keys arriving in ascending order).
            # Keep the left leaf full instead of leaving it half empty forever.
            middle = len(keys) - 1
        else:
            middle = _split_point(keys, node.LEAF_CELL_OVERHEAD)
            middle = max(1, min(middle, len(keys) - 1))
        right_id = self._pool.allocate(PageType.BTREE_LEAF)
        with self._pool.page(right_id, write=True) as buf:
            node.encode_leaf(buf, keys[middle:], rids[middle:], link)
        with self._pool.page(page_id, write=True) as buf:
            node.encode_leaf(buf, keys[:middle], rids[:middle], right_id)
        return keys[middle], right_id

    def _grow_root(self, separator: bytes, right_id: int) -> None:
        """The root split: move its contents to a new page and make the root their parent."""
        with self._pool.page(self._root) as buf:
            old_type = PageType(page_type_of(buf))
            body = bytes(buf[PAGE_HEADER_SIZE:])
        left_id = self._pool.allocate(old_type)
        with self._pool.page(left_id, write=True) as buf:
            buf[PAGE_HEADER_SIZE:] = body
        with self._pool.page(self._root, write=True) as buf:
            buf[:] = new_page(self._pool.page_size, self._root, PageType.BTREE_INTERNAL)
            node.encode_internal(buf, [separator], [left_id, right_id])

    # ------------------------------------------------------------------ delete

    def delete(self, key: bytes) -> bool:
        """Remove a key. Returns False if it was not present."""
        page_id = self._leaf_for(key)
        with self._pool.page(page_id) as buf:
            keys, rids, link = node.decode_leaf(buf)
        index = bisect_left(keys, key)
        if index == len(keys) or keys[index] != key:
            return False
        del keys[index]
        del rids[index]
        with self._pool.page(page_id, write=True) as buf:
            node.encode_leaf(buf, keys, rids, link)
        return True

    # ------------------------------------------------------------------ inspection

    def first_key(self) -> bytes | None:
        for key, _rid in self.scan():
            return key
        return None

    def last_key(self) -> bytes | None:
        """The largest key. Usually one descent; a full scan if the last leaf is empty."""
        page_id = self._root
        for _ in range(_MAX_HEIGHT):
            with self._pool.page(page_id) as buf:
                if page_type_of(buf) == _LEAF:
                    keys = node.decode_leaf(buf)[0]
                    break
                page_id = node.decode_internal(buf)[1][-1]
        else:
            raise CorruptIndexError("B+ tree is deeper than any valid tree; it contains a cycle")
        if keys:
            return keys[-1]
        last = None
        for key, _rid in self.scan():
            last = key
        return last

    def height(self) -> int:
        """Number of levels; 1 for a tree that is a single leaf."""
        levels = 1
        page_id = self._root
        while levels <= _MAX_HEIGHT:
            with self._pool.page(page_id) as buf:
                if page_type_of(buf) == _LEAF:
                    return levels
                page_id = node.decode_internal(buf)[1][0]
            levels += 1
        raise CorruptIndexError("B+ tree is deeper than any valid tree; it contains a cycle")

    def pages(self) -> list[int]:
        """Every page of the tree, root first."""
        result: list[int] = []
        stack = [self._root]
        seen: set[int] = set()
        while stack:
            page_id = stack.pop()
            if page_id in seen:
                raise CorruptIndexError(f"page {page_id} is reachable twice")
            seen.add(page_id)
            result.append(page_id)
            with self._pool.page(page_id) as buf:
                page_type = page_type_of(buf)
                if page_type == _INTERNAL:
                    stack.extend(node.decode_internal(buf)[1])
                elif page_type != _LEAF:
                    raise CorruptIndexError(f"page {page_id} is not a B+ tree page")
        return result

    def drop(self) -> None:
        """Return every page of the tree to the free list."""
        for page_id in self.pages():
            self._pool.free(page_id)

    # ------------------------------------------------------------------ invariants

    def check_invariants(self) -> int:
        """Validate the whole tree (T1 to T6) and return the number of keys.

        Raises CorruptIndexError naming the first violated invariant.
        """
        leaves: list[int] = []
        seen: set[int] = set()
        leaf_depths: set[int] = set()
        all_keys: list[bytes] = []

        def visit(page_id: int, low: bytes | None, high: bytes | None, depth: int) -> None:
            if page_id in seen:
                raise CorruptIndexError(f"T5: page {page_id} is reachable twice")
            if depth > _MAX_HEIGHT:
                raise CorruptIndexError("T3: tree is deeper than any valid tree")
            seen.add(page_id)
            with self._pool.page(page_id) as buf:
                page_type = page_type_of(buf)
                if page_type == _LEAF:
                    keys, _rids, _link = node.decode_leaf(buf)
                    children: list[int] = []
                elif page_type == _INTERNAL:
                    keys, children = node.decode_internal(buf)
                else:
                    raise CorruptIndexError(f"T5: page {page_id} is not a B+ tree page")
            for previous, current in zip(keys, keys[1:], strict=False):
                if previous >= current:
                    raise CorruptIndexError(f"T1: keys in page {page_id} are not increasing")
            if keys and low is not None and keys[0] < low:
                raise CorruptIndexError(f"T2: page {page_id} has a key below its lower bound")
            if keys and high is not None and keys[-1] >= high:
                raise CorruptIndexError(f"T2: page {page_id} has a key at or above its upper bound")
            if page_type == _LEAF:
                leaves.append(page_id)
                leaf_depths.add(depth)
                all_keys.extend(keys)
                return
            if not keys:
                raise CorruptIndexError(f"T5: internal page {page_id} has no keys")
            bounds = [low, *keys, high]
            for index, child in enumerate(children):
                visit(child, bounds[index], bounds[index + 1], depth + 1)

        visit(self._root, None, None, 1)
        if len(leaf_depths) != 1:
            raise CorruptIndexError(f"T3: leaves are at different depths: {sorted(leaf_depths)}")

        chain: list[int] = []
        page_id = leaves[0]
        chain_seen: set[int] = set()
        while page_id:
            if page_id in chain_seen:
                raise CorruptIndexError(f"T4: leaf chain loops back to page {page_id}")
            chain_seen.add(page_id)
            chain.append(page_id)
            with self._pool.page(page_id) as buf:
                if page_type_of(buf) != _LEAF:
                    raise CorruptIndexError(f"T4: page {page_id} in the leaf chain is not a leaf")
                page_id = node.decode_leaf(buf)[2]
        if chain != leaves:
            raise CorruptIndexError("T4: the leaf chain does not visit the leaves in tree order")
        for previous, current in zip(all_keys, all_keys[1:], strict=False):
            if previous >= current:
                raise CorruptIndexError("T6: keys are not strictly increasing across leaves")
        return len(all_keys)


def _split_point(keys: list[bytes], overhead: int) -> int:
    """Index that divides the cells into two halves of roughly equal bytes."""
    total = sum(map(len, keys)) + overhead * len(keys)
    running = 0
    for index, key in enumerate(keys):
        running += len(key) + overhead
        if running * 2 >= total:
            return index + 1
    return len(keys) // 2
