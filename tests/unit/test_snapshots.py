"""Multi-version reads in the buffer pool: what a snapshot sees, and what is kept for it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mvrs_db.errors import CorruptPageError, InternalError
from mvrs_db.storage.buffer_pool import BufferPool, SnapshotPool
from mvrs_db.storage.disk_manager import DiskManager
from mvrs_db.storage.page import PAGE_HEADER_SIZE, PageType

PAGE_SIZE = 4096


@pytest.fixture
def disk(tmp_path: Path) -> Iterator[DiskManager]:
    manager = DiskManager.create(tmp_path / "db.mvrs")
    yield manager
    manager.close()


@pytest.fixture
def pool(disk: DiskManager) -> BufferPool:
    return BufferPool(disk, 16)


def put(pool: BufferPool, page_id: int, value: int) -> None:
    with pool.page(page_id, write=True) as buf:
        buf[PAGE_HEADER_SIZE] = value


def get(pool: BufferPool, page_id: int, seq: int) -> int:
    return SnapshotPool(pool, seq).fetch(page_id)[PAGE_HEADER_SIZE]


def committed_page(pool: BufferPool, value: int) -> int:
    pool.begin()
    page_id = pool.allocate(PageType.HEAP)
    put(pool, page_id, value)
    pool.commit()
    return page_id


def write(pool: BufferPool, page_id: int, value: int) -> None:
    pool.begin()
    put(pool, page_id, value)
    pool.commit()


def test_each_change_that_commits_advances_the_sequence(pool: BufferPool) -> None:
    assert pool.commit_seq == 0
    page = committed_page(pool, 1)
    assert pool.commit_seq == 1
    write(pool, page, 2)
    assert pool.commit_seq == 2
    pool.begin()
    pool.commit()  # changed nothing
    assert pool.commit_seq == 2
    pool.begin()
    put(pool, page, 9)
    pool.rollback()
    assert pool.commit_seq == 2


def test_a_snapshot_keeps_seeing_the_page_as_it_was(pool: BufferPool) -> None:
    page = committed_page(pool, 1)
    first = pool.begin_snapshot()
    write(pool, page, 2)
    second = pool.begin_snapshot()
    write(pool, page, 3)
    third = pool.begin_snapshot()

    assert [get(pool, page, s) for s in (first, second, third)] == [1, 2, 3]
    assert pool.version_count == 2
    # ... and the live page is the newest.
    with pool.page(page) as buf:
        assert buf[PAGE_HEADER_SIZE] == 3


def test_a_snapshot_does_not_see_an_uncommitted_change(pool: BufferPool) -> None:
    page = committed_page(pool, 1)
    seq = pool.begin_snapshot()
    pool.begin()
    put(pool, page, 2)
    assert (
        get(pool, page, seq) == 1
    )  # the writer has changed the frame; the reader gets the before-image
    pool.rollback()
    assert get(pool, page, seq) == 1
    assert pool.version_count == 0


def test_the_change_becomes_a_version_when_it_commits(pool: BufferPool) -> None:
    page = committed_page(pool, 1)
    seq = pool.begin_snapshot()
    pool.begin()
    put(pool, page, 2)
    assert pool.version_count == 0
    pool.commit()
    assert pool.version_count == 1
    assert get(pool, page, seq) == 1
    pool.end_snapshot(seq)
    assert pool.version_count == 0  # nobody can need it now


def test_nothing_is_kept_when_no_snapshot_is_registered(pool: BufferPool) -> None:
    page = committed_page(pool, 1)
    for value in range(2, 10):
        write(pool, page, value)
    assert pool.version_count == 0


def test_versions_are_dropped_as_the_oldest_snapshot_ends(pool: BufferPool) -> None:
    page = committed_page(pool, 1)
    old = pool.begin_snapshot()
    write(pool, page, 2)
    newer = pool.begin_snapshot()
    write(pool, page, 3)
    assert pool.version_count == 2
    pool.end_snapshot(newer)
    assert pool.version_count == 2  # the old snapshot still needs both
    assert get(pool, page, old) == 1
    pool.end_snapshot(old)
    assert pool.version_count == 0


def test_two_readers_on_the_same_commit_share_it(pool: BufferPool) -> None:
    page = committed_page(pool, 1)
    a = pool.begin_snapshot()
    b = pool.begin_snapshot()
    assert a == b
    write(pool, page, 2)
    pool.end_snapshot(a)
    assert get(pool, page, b) == 1  # one reader leaving does not release the other's versions
    pool.end_snapshot(b)
    assert pool.version_count == 0


def test_unrelated_pages_are_not_versioned(pool: BufferPool) -> None:
    a = committed_page(pool, 1)
    b = committed_page(pool, 5)
    seq = pool.begin_snapshot()
    write(pool, a, 2)
    assert pool.version_count == 1
    assert get(pool, b, seq) == 5


def test_a_page_allocated_after_the_snapshot_did_not_exist_for_it(pool: BufferPool) -> None:
    seq = pool.begin_snapshot()
    pool.begin()
    page = pool.allocate(PageType.HEAP)
    with pytest.raises(CorruptPageError, match="did not exist"):
        get(pool, page, seq)
    pool.commit()
    pool.end_snapshot(seq)


def test_a_freed_and_reused_page_keeps_its_old_content_for_old_snapshots(pool: BufferPool) -> None:
    page = committed_page(pool, 7)
    seq = pool.begin_snapshot()
    pool.begin()
    pool.free(page)
    pool.commit()
    pool.begin()
    reused = pool.allocate(PageType.HEAP)
    assert reused == page
    put(pool, reused, 99)
    pool.commit()
    assert get(pool, page, seq) == 7
    with pool.page(page) as buf:
        assert buf[PAGE_HEADER_SIZE] == 99


def test_a_page_not_in_the_pool_is_read_from_the_file(pool: BufferPool, disk: DiskManager) -> None:
    page = committed_page(pool, 4)
    pool.flush_all()
    small = BufferPool(disk, 16)
    seq = small.begin_snapshot()
    assert not small.is_cached(page)
    assert get(small, page, seq) == 4


def test_a_snapshot_reads_a_copy(pool: BufferPool) -> None:
    page = committed_page(pool, 1)
    seq = pool.begin_snapshot()
    copy = SnapshotPool(pool, seq).fetch(page)
    copy[PAGE_HEADER_SIZE] = 200  # scribbling on the copy must not touch the page
    assert get(pool, page, seq) == 1
    with pool.page(page) as buf:
        assert buf[PAGE_HEADER_SIZE] == 1


def test_a_snapshot_pool_cannot_change_anything(pool: BufferPool) -> None:
    view = SnapshotPool(pool, 0)
    with pytest.raises(InternalError):
        view.fetch(1, write=True)
    with pytest.raises(InternalError):
        view.allocate(PageType.HEAP)
    with pytest.raises(InternalError):
        view.free(1)
    with pytest.raises(InternalError):
        view.update_header(catalog_root=1)
    assert not view.in_transaction


def test_ending_an_unregistered_snapshot_is_an_error(pool: BufferPool) -> None:
    with pytest.raises(InternalError):
        pool.end_snapshot(0)
    seq = pool.begin_snapshot()
    pool.end_snapshot(seq)
    with pytest.raises(InternalError):
        pool.end_snapshot(seq)


def test_header_only_changes_advance_the_sequence(pool: BufferPool) -> None:
    pool.begin()
    pool.update_header(catalog_root=5)
    pool.commit()
    assert pool.commit_seq == 1
