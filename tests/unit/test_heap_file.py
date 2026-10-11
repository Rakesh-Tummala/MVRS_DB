"""Heap file: a table's records across a chain of pages."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mvrs_db.errors import InvalidRecordIdError, RecordTooLargeError
from mvrs_db.storage import heap_page
from mvrs_db.storage.buffer_pool import BufferPool
from mvrs_db.storage.disk_manager import DiskManager
from mvrs_db.storage.heap_file import HeapFile, RecordId
from mvrs_db.storage.page import PageType

PAGE_SIZE = 4096


def new_heap(path: Path, capacity: int = 16) -> tuple[DiskManager, BufferPool, HeapFile]:
    disk = DiskManager.create(path)
    pool = BufferPool(disk, capacity)
    return disk, pool, HeapFile(pool, HeapFile.create(pool))


def test_insert_read_across_many_pages(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    records = [f"record-{i:05d}".encode() * 10 for i in range(500)]

    rids = [heap.insert(record) for record in records]

    assert len(set(rids)) == 500
    assert len(heap.pages()) > 10
    for rid, record in zip(rids, records, strict=True):
        assert heap.read(rid) == record
    assert sorted(r for _, r in heap.scan()) == sorted(records)
    assert heap.count() == (500, len(heap.pages()))
    heap.validate()
    disk.close()


def test_records_survive_reopen_with_a_small_buffer_pool(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    disk, pool, heap = new_heap(path, capacity=8)
    first_page = heap.first_page
    rids = {heap.insert(f"row {i}".encode() * 20): i for i in range(10_000)}
    pool.flush_all()
    disk.close()

    disk = DiskManager.open(path)
    heap = HeapFile(BufferPool(disk, 8), first_page)
    assert heap.count()[0] == 10_000
    for rid, i in list(rids.items())[::97]:
        assert heap.read(rid) == f"row {i}".encode() * 20
    assert sum(1 for _ in heap.scan()) == 10_000
    heap.validate()
    disk.close()


def test_record_size_limit(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    limit = heap_page.max_record_size(PAGE_SIZE)

    rid = heap.insert(b"x" * limit)
    assert heap.read(rid) == b"x" * limit

    with pytest.raises(RecordTooLargeError):
        heap.insert(b"x" * (limit + 1))
    with pytest.raises(RecordTooLargeError):
        heap.update(rid, b"x" * (limit + 1))
    assert heap.read(rid) == b"x" * limit
    disk.close()


def test_deleted_space_is_reused(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    rids = [heap.insert(b"a" * 1000) for _ in range(40)]
    pages_before = len(heap.pages())

    for rid in rids:
        heap.delete(rid)
    for _ in range(40):
        heap.insert(b"b" * 1000)

    assert len(heap.pages()) == pages_before
    assert heap.count()[0] == 40
    disk.close()


def test_emptied_pages_can_hold_large_records(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    rids = [heap.insert(b"a" * 3000) for _ in range(6)]
    pages_before = len(heap.pages())

    for rid in rids:
        heap.delete(rid)
    for _ in range(6):
        heap.insert(b"b" * 3000)

    assert len(heap.pages()) == pages_before
    disk.close()


def test_update_that_outgrows_its_page_moves_the_record(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    rids = [heap.insert(b"a" * 1300) for _ in range(3)]  # fills the first page

    same = heap.update(rids[0], b"b" * 1000)
    moved = heap.update(rids[1], b"c" * 3000)

    assert same == rids[0]
    assert moved != rids[1]
    assert heap.read(moved) == b"c" * 3000
    with pytest.raises(InvalidRecordIdError):
        heap.read(rids[1])
    assert heap.count()[0] == 3
    heap.validate()
    disk.close()


@pytest.mark.parametrize("rid", [RecordId(0, 0), RecordId(999, 0), RecordId(1, 50), RecordId(1, 0)])
def test_bad_record_ids_raise(tmp_path: Path, rid: RecordId) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")

    with pytest.raises(InvalidRecordIdError):
        heap.read(rid)
    with pytest.raises(InvalidRecordIdError):
        heap.delete(rid)
    disk.close()


def test_record_id_on_a_non_heap_page_raises(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    other = pool.allocate(PageType.BTREE_LEAF)

    with pytest.raises(InvalidRecordIdError):
        heap.read(RecordId(other, 0))
    assert pool.pin_count(other) == 0
    disk.close()


def test_drop_returns_every_page_to_the_free_list(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    for _ in range(100):
        heap.insert(b"x" * 500)
    pages = heap.pages()

    heap.drop()

    assert sorted(pool.free_list()) == sorted(pages)
    disk.close()


def test_rollback_then_invalidate_keeps_the_free_space_map_honest(tmp_path: Path) -> None:
    disk, pool, heap = new_heap(tmp_path / "db.mvrs")
    keep = [heap.insert(b"k" * 700) for _ in range(20)]

    pool.begin()
    for _ in range(50):
        heap.insert(b"gone" * 200)
    pool.rollback()
    heap.invalidate()

    assert heap.count()[0] == 20
    for _ in range(50):
        heap.insert(b"new" * 200)
    assert heap.count()[0] == 70
    assert all(heap.read(rid) == b"k" * 700 for rid in keep)
    heap.validate()
    disk.close()


OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("insert"), st.binary(min_size=1, max_size=1500)),
        st.tuples(st.just("delete"), st.integers(0, 200)),
        st.tuples(st.just("update"), st.integers(0, 200), st.binary(min_size=1, max_size=3000)),
    ),
    max_size=120,
)


@settings(max_examples=60, deadline=None)
@given(OPERATIONS)
def test_random_operations_match_a_model_and_survive_reopen(
    tmp_path_factory: pytest.TempPathFactory,
    operations: list[tuple],  # type: ignore[type-arg]
) -> None:
    path = tmp_path_factory.mktemp("heap") / "db.mvrs"
    disk, pool, heap = new_heap(path, capacity=8)
    first_page = heap.first_page
    model: dict[RecordId, bytes] = {}

    for operation in operations:
        live = sorted(model)
        if operation[0] == "insert":
            rid = heap.insert(operation[1])
            assert rid not in model
            model[rid] = operation[1]
        elif live:
            rid = live[operation[1] % len(live)]
            if operation[0] == "delete":
                heap.delete(rid)
                del model[rid]
            else:
                new_rid = heap.update(rid, operation[2])
                del model[rid]
                assert new_rid not in model
                model[new_rid] = operation[2]

    assert dict(heap.scan()) == model
    heap.validate()
    pool.flush_all()
    disk.close()

    disk = DiskManager.open(path)
    reopened = HeapFile(BufferPool(disk, 8), first_page)
    assert dict(reopened.scan()) == model
    reopened.validate()
    disk.close()
