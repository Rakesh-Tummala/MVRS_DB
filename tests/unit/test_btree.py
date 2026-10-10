"""B+ tree: search, insert with splits, range scans, delete, invariants."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kestreldb.errors import CorruptIndexError, DuplicateKeyError, KeyTooLargeError
from kestreldb.index import btree_node as node
from kestreldb.index.btree import BTree
from kestreldb.index.keys import encode_key
from kestreldb.storage.buffer_pool import BufferPool
from kestreldb.storage.disk_manager import DiskManager
from kestreldb.storage.heap_file import RecordId
from kestreldb.storage.page import PageType, page_type_of
from kestreldb.storage.record import ColumnType

SMALL = 150  # node capacity in bytes: about 6 integer keys per node, so trees get deep fast


def key(value: int) -> bytes:
    return encode_key(ColumnType.INTEGER, value)


def rid(value: int) -> RecordId:
    return RecordId(value + 1, value % 7)


def new_tree(
    path: Path, capacity: int | None = SMALL, frames: int = 64
) -> tuple[DiskManager, BufferPool, BTree]:
    disk = DiskManager.create(path)
    pool = BufferPool(disk, frames)
    return disk, pool, BTree(pool, BTree.create(pool), node_capacity=capacity)


def test_empty_tree(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")

    assert tree.get(key(1)) is None
    assert list(tree.scan()) == []
    assert tree.first_key() is None
    assert tree.last_key() is None
    assert tree.height() == 1
    assert tree.check_invariants() == 0
    assert tree.delete(key(1)) is False
    disk.close()


def test_insert_and_get_without_splitting(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")

    for value in (3, 1, 2):
        tree.insert(key(value), rid(value))

    assert tree.height() == 1
    assert [tree.get(key(v)) for v in (1, 2, 3, 4)] == [rid(1), rid(2), rid(3), None]
    assert [k for k, _ in tree.scan()] == [key(1), key(2), key(3)]
    disk.close()


def test_duplicate_key_is_rejected_and_changes_nothing(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(50):
        tree.insert(key(value), rid(value))

    with pytest.raises(DuplicateKeyError):
        tree.insert(key(25), RecordId(999, 0))

    assert tree.get(key(25)) == rid(25)
    assert tree.check_invariants() == 50
    disk.close()


def test_oversized_key_is_rejected(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb", capacity=None)

    tree.insert(b"k" * 255, RecordId(1, 0))
    with pytest.raises(KeyTooLargeError):
        tree.insert(b"k" * 256, RecordId(1, 1))
    disk.close()


@pytest.mark.parametrize("order", ["ascending", "descending", "random"])
def test_splits_keep_every_invariant_after_every_insert(tmp_path: Path, order: str) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    values = list(range(400))
    if order == "descending":
        values.reverse()
    elif order == "random":
        random.Random(1).shuffle(values)

    for count, value in enumerate(values, start=1):
        tree.insert(key(value), rid(value))
        assert tree.check_invariants() == count

    assert tree.height() >= 4
    assert tree.root_page == 1  # T8: the root never moved
    assert [k for k, _ in tree.scan()] == [key(v) for v in range(400)]
    assert all(tree.get(key(v)) == rid(v) for v in range(400))
    assert tree.first_key() == key(0)
    assert tree.last_key() == key(399)
    disk.close()


def test_leaf_split_copies_the_separator_up(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in (10, 20, 30, 40, 50, 60, 25):  # the 7th key overflows a 150-byte leaf
        tree.insert(key(value), rid(value))

    with pool.page(tree.root_page) as buf:
        assert page_type_of(buf) == PageType.BTREE_INTERNAL
        separators, children = node.decode_internal(buf)
    with pool.page(children[1]) as buf:
        right_keys, _rids, _link = node.decode_leaf(buf)

    assert len(separators) == 1
    assert separators[0] == right_keys[0]  # copied, so it is still in a leaf
    assert tree.get(separators[0]) is not None
    disk.close()


def test_ascending_inserts_leave_leaves_full(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb", capacity=None, frames=256)

    for value in range(20_000):
        tree.insert(key(value), rid(value))

    per_leaf = (4096 - node.CELLS_START) // (8 + node.LEAF_CELL_OVERHEAD)
    leaves = sum(1 for p in tree.pages() if _is_leaf(pool, p))
    assert leaves <= 20_000 // per_leaf + 2  # a 50/50 split would need about twice as many
    assert tree.check_invariants() == 20_000
    disk.close()


def _is_leaf(pool: BufferPool, page_id: int) -> bool:
    with pool.page(page_id) as buf:
        return page_type_of(buf) == PageType.BTREE_LEAF


def test_variable_length_keys_including_the_largest(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb", capacity=None)
    rng = random.Random(7)
    keys = {
        bytes(rng.randrange(256) for _ in range(rng.choice([0, 1, 8, 100, 255])))
        for _ in range(600)
    }

    for index, k in enumerate(sorted(keys, key=lambda _: rng.random())):
        tree.insert(k, RecordId(index + 1, 0))

    assert tree.check_invariants() == len(keys)
    assert [k for k, _ in tree.scan()] == sorted(keys)
    assert tree.height() >= 2
    disk.close()


@pytest.mark.parametrize(
    ("low", "high", "low_inclusive", "high_inclusive", "expected"),
    [
        (None, None, True, True, list(range(0, 200, 2))),
        (50, 60, True, True, [50, 52, 54, 56, 58, 60]),
        (50, 60, False, True, [52, 54, 56, 58, 60]),
        (50, 60, True, False, [50, 52, 54, 56, 58]),
        (50, 60, False, False, [52, 54, 56, 58]),
        (51, 59, True, True, [52, 54, 56, 58]),  # bounds that are not keys
        (None, 5, True, True, [0, 2, 4]),
        (195, None, True, True, [196, 198]),
        (60, 50, True, True, []),
        (300, None, True, True, []),
        (None, -1, True, True, []),
        (58, 58, True, True, [58]),
        (58, 58, False, True, []),
    ],
)
def test_range_scans(
    tmp_path: Path,
    low: int | None,
    high: int | None,
    low_inclusive: bool,
    high_inclusive: bool,
    expected: list[int],
) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(0, 200, 2):
        tree.insert(key(value), rid(value))

    found = tree.scan(
        None if low is None else key(low),
        None if high is None else key(high),
        low_inclusive=low_inclusive,
        high_inclusive=high_inclusive,
    )

    assert list(found) == [(key(v), rid(v)) for v in expected]
    disk.close()


def test_delete(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(300):
        tree.insert(key(value), rid(value))

    for value in range(0, 300, 3):
        assert tree.delete(key(value)) is True
    assert tree.delete(key(0)) is False

    remaining = [v for v in range(300) if v % 3]
    assert tree.check_invariants() == len(remaining)
    assert [k for k, _ in tree.scan()] == [key(v) for v in remaining]
    assert tree.get(key(3)) is None
    assert tree.get(key(4)) == rid(4)
    disk.close()


def test_scans_skip_leaves_emptied_by_deletes(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(300):
        tree.insert(key(value), rid(value))

    for value in range(20, 280):
        tree.delete(key(value))

    expected = [*range(20), *range(280, 300)]
    assert [k for k, _ in tree.scan()] == [key(v) for v in expected]
    assert [k for k, _ in tree.scan(key(100), key(290))] == [key(v) for v in range(280, 291)]
    assert tree.check_invariants() == 40
    disk.close()


def test_emptied_tree_can_be_refilled(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(200):
        tree.insert(key(value), rid(value))
    pages_before = len(tree.pages())
    for value in range(200):
        tree.delete(key(value))

    assert list(tree.scan()) == []
    assert tree.first_key() is None
    assert tree.last_key() is None

    for value in range(200):
        tree.insert(key(value), rid(value))
    assert tree.check_invariants() == 200
    assert len(tree.pages()) == pages_before  # the emptied leaves were reused
    disk.close()


def test_tree_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "db.kdb"
    disk, pool, tree = new_tree(path, frames=8)
    root = tree.root_page
    values = list(range(2000))
    random.Random(3).shuffle(values)
    for value in values:
        tree.insert(key(value), rid(value))
    pool.flush_all()
    disk.close()

    disk = DiskManager.open(path)
    tree = BTree(BufferPool(disk, 8), root, node_capacity=SMALL)
    assert tree.check_invariants() == 2000
    assert [k for k, _ in tree.scan()] == [key(v) for v in range(2000)]
    assert tree.get(key(1234)) == rid(1234)
    disk.close()


def test_drop_frees_every_page(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(300):
        tree.insert(key(value), rid(value))
    pages = tree.pages()

    tree.drop()

    assert sorted(pool.free_list()) == sorted(pages)
    assert len(pages) == disk.page_count - 1
    disk.close()


def test_no_pages_stay_pinned(tmp_path: Path) -> None:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(300):
        tree.insert(key(value), rid(value))
    tree.delete(key(5))
    list(tree.scan(key(10), key(50)))
    next(iter(tree.scan()))  # an abandoned scan must not hold a pin either
    tree.check_invariants()

    assert all(pool.pin_count(p) == 0 for p in pool.cached_page_ids())
    disk.close()


# ---------------------------------------------------------------------- broken trees


def build(tmp_path: Path, count: int = 60) -> tuple[DiskManager, BufferPool, BTree]:
    disk, pool, tree = new_tree(tmp_path / "db.kdb")
    for value in range(count):
        tree.insert(key(value), rid(value))
    return disk, pool, tree


def leaves_of(pool: BufferPool, tree: BTree) -> list[int]:
    return [p for p in tree.pages() if _is_leaf(pool, p)]


def test_check_catches_unordered_keys(tmp_path: Path) -> None:
    disk, pool, tree = build(tmp_path)
    leaf = leaves_of(pool, tree)[0]
    with pool.page(leaf, write=True) as buf:
        keys, rids, link = node.decode_leaf(buf)
        keys[0], keys[1] = keys[1], keys[0]
        node.encode_leaf(buf, keys, rids, link)

    with pytest.raises(CorruptIndexError, match="T1"):
        tree.check_invariants()
    disk.close()


def test_check_catches_a_key_outside_its_subtree_bounds(tmp_path: Path) -> None:
    disk, pool, tree = build(tmp_path)
    leaf = tree._leaf_for(key(0))
    with pool.page(leaf, write=True) as buf:
        keys, rids, link = node.decode_leaf(buf)
        keys[-1] = key(10_000)
        node.encode_leaf(buf, keys, rids, link)

    with pytest.raises(CorruptIndexError, match="T2"):
        tree.check_invariants()
    disk.close()


def test_check_catches_a_broken_leaf_chain(tmp_path: Path) -> None:
    disk, pool, tree = build(tmp_path)
    leaf = tree._leaf_for(key(0))
    with pool.page(leaf, write=True) as buf:
        keys, rids, _link = node.decode_leaf(buf)
        node.encode_leaf(buf, keys, rids, 0)  # cut the chain after the first leaf

    with pytest.raises(CorruptIndexError, match="T4"):
        tree.check_invariants()
    disk.close()


def test_check_catches_a_page_shared_by_two_parents(tmp_path: Path) -> None:
    disk, pool, tree = build(tmp_path, count=200)
    with pool.page(tree.root_page, write=True) as buf:
        keys, children = node.decode_internal(buf)
        children[1] = children[0]
        node.encode_internal(buf, keys, children)

    with pytest.raises(CorruptIndexError, match="T5"):
        tree.check_invariants()
    disk.close()


def test_check_catches_a_non_tree_page(tmp_path: Path) -> None:
    disk, pool, tree = build(tmp_path)
    stray = pool.allocate(PageType.HEAP)
    with pool.page(tree.root_page, write=True) as buf:
        keys, children = node.decode_internal(buf)
        children[0] = stray
        node.encode_internal(buf, keys, children)

    with pytest.raises(CorruptIndexError, match="T5"):
        tree.check_invariants()
    with pytest.raises(CorruptIndexError):
        tree.get(key(0))
    disk.close()


def test_check_catches_leaves_at_different_depths(tmp_path: Path) -> None:
    disk, pool, tree = build(tmp_path, count=200)
    assert tree.height() >= 3
    leaf = tree._leaf_for(key(0))
    with pool.page(tree.root_page, write=True) as buf:
        keys, children = node.decode_internal(buf)
        children[0] = leaf  # a leaf directly under the root, beside internal nodes
        node.encode_internal(buf, keys, children)

    with pytest.raises(CorruptIndexError):
        tree.check_invariants()
    disk.close()


def test_corrupt_cell_pointer_is_reported_not_crashed_on(tmp_path: Path) -> None:
    disk, pool, tree = build(tmp_path)
    leaf = tree._leaf_for(key(0))
    with pool.page(leaf, write=True) as buf:
        buf[node.CELLS_START : node.CELLS_START + 2] = b"\x05\x00"  # points into the page header

    with pytest.raises(CorruptIndexError):
        list(tree.scan())
    disk.close()
