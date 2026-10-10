"""Buffer pool: pinning, eviction, dirty tracking, allocation, transactions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kestreldb.errors import (
    BufferPoolFullError,
    ChecksumMismatchError,
    InternalError,
    InvalidPageIdError,
    PagePinError,
    StorageIOError,
    TransactionError,
    TransactionTooLargeError,
)
from kestreldb.storage.buffer_pool import BufferPool
from kestreldb.storage.disk_manager import DiskManager
from kestreldb.storage.page import PAGE_HEADER_SIZE, PageType, read_header

PAGE_SIZE = 4096
CAPACITY = 8


@pytest.fixture
def disk(tmp_path: Path) -> Iterator[DiskManager]:
    manager = DiskManager.create(tmp_path / "db.kdb")
    yield manager
    manager.close()


@pytest.fixture
def pool(disk: DiskManager) -> BufferPool:
    return BufferPool(disk, CAPACITY)


def make_pages(pool: BufferPool, count: int) -> list[int]:
    """Allocate `count` heap pages, each filled with its own page ID as a marker."""
    ids = []
    for _ in range(count):
        page_id = pool.allocate(PageType.HEAP)
        with pool.page(page_id, write=True) as buf:
            buf[PAGE_HEADER_SIZE:] = bytes([page_id % 256]) * (PAGE_SIZE - PAGE_HEADER_SIZE)
        ids.append(page_id)
    return ids


def marker(buf: bytearray) -> int:
    return buf[PAGE_HEADER_SIZE]


# ---------------------------------------------------------------------- pinning


def test_capacity_below_minimum_is_refused(disk: DiskManager) -> None:
    with pytest.raises(ValueError, match="at least"):
        BufferPool(disk, 2)


def test_fetch_pins_and_unpin_releases(pool: BufferPool) -> None:
    (page_id,) = make_pages(pool, 1)

    pool.fetch(page_id)
    pool.fetch(page_id)
    assert pool.pin_count(page_id) == 2

    pool.unpin(page_id)
    pool.unpin(page_id)
    assert pool.pin_count(page_id) == 0


def test_unpin_without_pin_raises(pool: BufferPool) -> None:
    (page_id,) = make_pages(pool, 1)

    with pytest.raises(PagePinError):
        pool.unpin(page_id)
    with pytest.raises(PagePinError):
        pool.unpin(999)


def test_context_manager_unpins_even_on_error(pool: BufferPool) -> None:
    (page_id,) = make_pages(pool, 1)

    with pytest.raises(RuntimeError), pool.page(page_id):
        raise RuntimeError("boom")

    assert pool.pin_count(page_id) == 0


def test_a_page_is_cached_in_exactly_one_frame(pool: BufferPool) -> None:
    (page_id,) = make_pages(pool, 1)

    first = pool.fetch(page_id)
    second = pool.fetch(page_id)

    assert first is second  # B1
    assert len(pool) == 1


def test_fetching_an_invalid_page_id_raises_and_changes_nothing(pool: BufferPool) -> None:
    make_pages(pool, 2)
    before = pool.cached_page_ids()

    with pytest.raises(InvalidPageIdError):
        pool.fetch(99)

    assert pool.cached_page_ids() == before


# ---------------------------------------------------------------------- eviction


def test_least_recently_used_page_is_evicted_first(pool: BufferPool) -> None:
    ids = make_pages(pool, CAPACITY)
    with pool.page(ids[0]):  # touch page 0, so page 1 is now the oldest
        pass

    make_pages(pool, 1)

    assert not pool.is_cached(ids[1])
    assert pool.is_cached(ids[0])
    assert pool.stats.evictions == 1


def test_pinned_pages_are_never_evicted(pool: BufferPool) -> None:
    ids = make_pages(pool, CAPACITY)
    pool.fetch(ids[0])
    pool.fetch(ids[1])

    more = make_pages(pool, CAPACITY - 2)  # enough pressure to cycle every unpinned frame

    assert pool.is_cached(ids[0])
    assert pool.is_cached(ids[1])
    assert all(pool.is_cached(page_id) for page_id in more)


def test_all_frames_pinned_raises_and_leaves_the_pool_unchanged(pool: BufferPool) -> None:
    ids = make_pages(pool, CAPACITY + 1)  # one page has been evicted to disk
    evicted = next(page_id for page_id in ids if not pool.is_cached(page_id))
    for page_id in pool.cached_page_ids():
        pool.fetch(page_id)
    before = pool.cached_page_ids()

    with pytest.raises(BufferPoolFullError):
        pool.fetch(evicted)
    with pytest.raises(BufferPoolFullError):
        pool.allocate(PageType.HEAP)

    assert pool.cached_page_ids() == before
    assert all(pool.pin_count(page_id) == 1 for page_id in before)


def test_dirty_page_is_written_before_its_frame_is_reused(pool: BufferPool) -> None:
    ids = make_pages(pool, CAPACITY)
    with pool.page(ids[0], write=True) as buf:
        buf[PAGE_HEADER_SIZE] = 0xEE

    make_pages(pool, CAPACITY)  # push every original page out
    assert not pool.is_cached(ids[0])

    with pool.page(ids[0]) as buf:
        assert marker(buf) == 0xEE


def test_clean_pages_are_not_rewritten_on_eviction(pool: BufferPool) -> None:
    ids = make_pages(pool, CAPACITY)
    pool.flush_all()
    writes = pool.stats.page_writes

    for page_id in ids:  # read-only access
        with pool.page(page_id):
            pass
    make_pages(pool, 1)

    assert pool.stats.page_writes == writes


def test_failed_write_during_eviction_keeps_the_page_dirty(
    pool: BufferPool, disk: DiskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = make_pages(pool, CAPACITY)

    def failing_write(page_id: int, data: bytearray) -> None:
        raise StorageIOError("simulated disk full")

    with monkeypatch.context() as patch:
        patch.setattr(disk, "write_page", failing_write)
        with pytest.raises(StorageIOError):
            pool.allocate(PageType.HEAP)

    assert pool.is_cached(ids[0])
    assert pool.is_dirty(ids[0])
    pool.flush_all()  # and the data is still there to be written later
    assert marker(disk.read_page(ids[0])) == ids[0]


def test_corrupt_page_does_not_enter_the_pool(tmp_path: Path) -> None:
    path = tmp_path / "db.kdb"
    with DiskManager.create(path) as disk:
        pool = BufferPool(disk, CAPACITY)
        (page_id,) = make_pages(pool, 1)
        pool.flush_all()
    raw = bytearray(path.read_bytes())
    raw[page_id * PAGE_SIZE + 500] ^= 0xFF
    path.write_bytes(raw)

    with DiskManager.open(path) as disk:
        pool = BufferPool(disk, CAPACITY)
        with pytest.raises(ChecksumMismatchError):
            pool.fetch(page_id)
        assert not pool.is_cached(page_id)  # B5


def test_flush_all_persists_pages_and_header(tmp_path: Path) -> None:
    path = tmp_path / "db.kdb"
    with DiskManager.create(path) as disk:
        pool = BufferPool(disk, CAPACITY)
        ids = make_pages(pool, 5)
        assert disk.physical_page_count() == 1  # nothing written yet
        pool.flush_all()
        assert pool.dirty_page_count() == 0

    with DiskManager.open(path) as disk:
        assert disk.page_count == 6
        for page_id in ids:
            assert marker(disk.read_page(page_id)) == page_id


# ---------------------------------------------------------------------- allocation


def test_freed_pages_are_reused_before_the_file_grows(pool: BufferPool, disk: DiskManager) -> None:
    ids = make_pages(pool, 4)

    pool.free(ids[1])
    pool.free(ids[2])
    assert pool.free_list() == [ids[2], ids[1]]

    assert pool.allocate(PageType.BTREE_LEAF) == ids[2]
    assert pool.allocate(PageType.HEAP) == ids[1]
    assert pool.free_list() == []
    assert disk.page_count == 5
    assert pool.allocate(PageType.HEAP) == 5


def test_reused_page_is_zeroed_and_retyped(pool: BufferPool) -> None:
    (page_id,) = make_pages(pool, 1)
    pool.free(page_id)

    assert pool.allocate(PageType.BTREE_LEAF) == page_id

    with pool.page(page_id) as buf:
        assert read_header(buf).page_type == PageType.BTREE_LEAF
        assert bytes(buf[PAGE_HEADER_SIZE:]) == bytes(PAGE_SIZE - PAGE_HEADER_SIZE)


def test_double_free_is_caught(pool: BufferPool) -> None:
    (page_id,) = make_pages(pool, 1)
    pool.free(page_id)

    with pytest.raises(InternalError):
        pool.free(page_id)


def test_free_list_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "db.kdb"
    with DiskManager.create(path) as disk:
        pool = BufferPool(disk, CAPACITY)
        ids = make_pages(pool, 6)
        for page_id in (ids[0], ids[3], ids[5]):
            pool.free(page_id)
        pool.flush_all()

    with DiskManager.open(path) as disk:
        pool = BufferPool(disk, CAPACITY)
        assert pool.free_list() == [ids[5], ids[3], ids[0]]
        assert pool.allocate(PageType.HEAP) == ids[5]


# ---------------------------------------------------------------------- transactions


def test_rollback_restores_page_contents(pool: BufferPool) -> None:
    ids = make_pages(pool, 3)

    pool.begin()
    for page_id in ids:
        with pool.page(page_id, write=True) as buf:
            buf[PAGE_HEADER_SIZE:] = b"\xff" * (PAGE_SIZE - PAGE_HEADER_SIZE)
    pool.rollback()

    for page_id in ids:
        with pool.page(page_id) as buf:
            assert marker(buf) == page_id
    assert not pool.in_transaction


def test_commit_keeps_changes(pool: BufferPool) -> None:
    (page_id,) = make_pages(pool, 1)

    pool.begin()
    with pool.page(page_id, write=True) as buf:
        buf[PAGE_HEADER_SIZE] = 0x42
    assert [p for p, _ in pool.transaction_pages()] == [page_id]
    pool.commit()

    with pool.page(page_id) as buf:
        assert marker(buf) == 0x42
    assert pool.is_dirty(page_id)


def test_rollback_restores_the_dirty_flag(pool: BufferPool) -> None:
    clean, dirty = make_pages(pool, 2)
    pool.flush_all()
    with pool.page(dirty, write=True):
        pass

    pool.begin()
    for page_id in (clean, dirty):
        with pool.page(page_id, write=True):
            pass
    pool.rollback()

    assert not pool.is_dirty(clean)
    assert pool.is_dirty(dirty)  # it still holds an earlier, unflushed change


def test_rollback_undoes_allocation_and_free(pool: BufferPool, disk: DiskManager) -> None:
    ids = make_pages(pool, 3)
    pool.free(ids[0])
    header_before = disk.header

    pool.begin()
    reused = pool.allocate(PageType.HEAP)  # takes ids[0] off the free list
    grown = pool.allocate(PageType.HEAP)  # extends the file
    pool.free(ids[1])
    assert pool.header_changed()
    pool.rollback()

    assert reused == ids[0]
    assert disk.header == header_before
    assert pool.free_list() == [ids[0]]
    assert not pool.is_cached(grown)
    with pool.page(ids[1]) as buf:
        assert read_header(buf).page_type == PageType.HEAP
        assert marker(buf) == ids[1]


def test_pages_of_the_active_transaction_are_never_written(
    pool: BufferPool, disk: DiskManager
) -> None:
    ids = make_pages(pool, CAPACITY - 1)
    pool.flush_all()

    pool.begin()
    with pool.page(ids[0], write=True) as buf:
        buf[PAGE_HEADER_SIZE] = 0x99
    for page_id in ids[1:]:  # make ids[0] the least recently used
        with pool.page(page_id):
            pass
    extra = pool.allocate(PageType.HEAP)
    with pytest.raises(TransactionError):
        pool.flush_page(ids[0])
    with pytest.raises(TransactionError):
        pool.flush_all()

    # The pool is full: the next load evicts a clean page, never ids[0] (B7).
    another = pool.allocate(PageType.HEAP)
    assert pool.is_cached(ids[0])
    assert marker(disk.read_page(ids[0])) == ids[0]
    assert disk.physical_page_count() == CAPACITY  # the new pages never reached the file
    pool.rollback()
    assert not pool.is_cached(extra)
    assert not pool.is_cached(another)


def test_transaction_larger_than_the_pool_fails_cleanly(pool: BufferPool) -> None:
    ids = make_pages(pool, CAPACITY)
    pool.flush_all()

    pool.begin()
    with pytest.raises(TransactionTooLargeError):
        for page_id in [*ids, *ids]:
            with pool.page(page_id, write=True) as buf:
                buf[PAGE_HEADER_SIZE] = 0xAA
            pool.allocate(PageType.HEAP)
    pool.rollback()

    for page_id in ids:
        with pool.page(page_id) as buf:
            assert marker(buf) == page_id


def test_transaction_misuse_raises(pool: BufferPool) -> None:
    with pytest.raises(TransactionError):
        pool.commit()
    with pytest.raises(TransactionError):
        pool.rollback()
    with pytest.raises(TransactionError):
        pool.transaction_pages()
    pool.begin()
    with pytest.raises(TransactionError):
        pool.begin()
    pool.rollback()
