"""Model-based test: the B+ tree must behave exactly like a sorted Python dict."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mvrs_db.errors import DuplicateKeyError
from mvrs_db.index.btree import BTree
from mvrs_db.storage.buffer_pool import BufferPool
from mvrs_db.storage.disk_manager import DiskManager
from mvrs_db.storage.heap_file import RecordId

# Short keys from a small alphabet collide often, which exercises duplicate
# handling, delete-then-reinsert, and keys that are prefixes of each other.
KEYS = st.binary(max_size=4).map(lambda b: bytes(x % 6 for x in b))
RIDS = st.builds(RecordId, st.integers(1, 1000), st.integers(0, 100))
OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("insert"), KEYS, RIDS),
        st.tuples(st.just("delete"), KEYS),
        st.tuples(st.just("get"), KEYS),
        st.tuples(st.just("scan"), KEYS, KEYS, st.booleans(), st.booleans()),
    ),
    max_size=250,
)


@settings(max_examples=150, deadline=None)
@given(operations=OPERATIONS, capacity=st.sampled_from([60, 90, 150, 4096]))
def test_btree_matches_a_dictionary(
    tmp_path_factory: pytest.TempPathFactory,
    operations: list[tuple],
    capacity: int,  # type: ignore[type-arg]
) -> None:
    path = tmp_path_factory.mktemp("btree") / "db.mvrs"
    disk = DiskManager.create(path)
    pool = BufferPool(disk, 16)
    root = BTree.create(pool)
    tree = BTree(pool, root, node_capacity=capacity)
    model: dict[bytes, RecordId] = {}

    for operation in operations:
        name = operation[0]
        if name == "insert":
            _, key, rid = operation
            if key in model:
                with pytest.raises(DuplicateKeyError):
                    tree.insert(key, rid)
            else:
                tree.insert(key, rid)
                model[key] = rid
        elif name == "delete":
            assert tree.delete(operation[1]) == (operation[1] in model)
            model.pop(operation[1], None)
        elif name == "get":
            assert tree.get(operation[1]) == model.get(operation[1])
        else:
            _, low, high, low_inclusive, high_inclusive = operation
            expected = [
                (key, model[key])
                for key in sorted(model)
                if (key > low or (low_inclusive and key == low))
                and (key < high or (high_inclusive and key == high))
            ]
            found = tree.scan(low, high, low_inclusive=low_inclusive, high_inclusive=high_inclusive)
            assert list(found) == expected
        assert tree.check_invariants() == len(model)

    assert list(tree.scan()) == sorted(model.items())
    assert tree.root_page == root
    pool.flush_all()
    disk.close()

    disk = DiskManager.open(path)
    reopened = BTree(BufferPool(disk, 16), root, node_capacity=capacity)
    assert list(reopened.scan()) == sorted(model.items())
    assert reopened.check_invariants() == len(model)
    disk.close()
