"""Page deltas: finding changed byte ranges, encoding them, and replaying them in recovery."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mvrs_db.errors import CorruptPageError, CorruptWalError
from mvrs_db.recovery.delta import (
    UNLOGGED_PREFIX,
    apply_delta,
    changed_ranges,
    delta_header,
    delta_size,
    encode_delta,
)
from mvrs_db.recovery.recovery_manager import recover
from mvrs_db.recovery.wal import RecordType, WalWriter
from mvrs_db.storage.disk_manager import DiskManager
from mvrs_db.storage.page import PAGE_HEADER_SIZE, PageType, new_page, page_lsn, set_page_lsn

PAGE_SIZE = 4096


def page_with(fill: int = 0, page_id: int = 1) -> bytearray:
    buf = new_page(PAGE_SIZE, page_id, PageType.HEAP)
    buf[PAGE_HEADER_SIZE:] = bytes([fill]) * (PAGE_SIZE - PAGE_HEADER_SIZE)
    return buf


# ---------------------------------------------------------------------- ranges


def test_identical_pages_have_no_changed_ranges() -> None:
    page = page_with(7)
    assert changed_ranges(bytes(page), page) == []


def test_the_first_sixteen_bytes_are_never_part_of_a_delta() -> None:
    before = page_with(7)
    after = bytearray(before)
    after[0:16] = b"\xff" * 16  # checksum, page ID, LSN

    assert changed_ranges(bytes(before), after) == []
    assert UNLOGGED_PREFIX == 16


def test_ranges_are_trimmed_to_the_exact_bytes() -> None:
    before = page_with(0)
    after = bytearray(before)
    after[100:105] = b"hello"
    after[2000] = 9
    after[4095] = 1  # the very last byte
    after[16] = 3  # the first loggable byte

    assert changed_ranges(bytes(before), after) == [(16, 1), (100, 5), (2000, 1), (4095, 1)]


def test_changes_in_neighbouring_blocks_merge_into_one_range() -> None:
    before = page_with(0)
    after = bytearray(before)
    after[60:70] = b"x" * 10  # spans the block boundary at 64
    after[90] = 1  # in the next block again

    assert changed_ranges(bytes(before), after) == [(60, 31)]


def test_encode_and_apply() -> None:
    before = page_with(0)
    after = bytearray(before)
    after[100:105] = b"hello"
    after[3000:3003] = b"abc"
    ranges = changed_ranges(bytes(before), after)

    payload = encode_delta(42, 777, after, ranges)
    rebuilt = bytearray(before)
    apply_delta(rebuilt, payload)

    assert delta_header(payload) == (42, 777)
    assert len(payload) == delta_size(ranges) == 14 + (4 + 5) + (4 + 3)
    assert rebuilt == after


EDITS = st.lists(
    st.tuples(st.integers(0, PAGE_SIZE - 1), st.binary(min_size=1, max_size=120)), max_size=12
)


@settings(max_examples=300)
@given(EDITS)
def test_any_set_of_edits_round_trips(edits: list[tuple[int, bytes]]) -> None:
    before = page_with(5)
    after = bytearray(before)
    for offset, data in edits:
        end = min(PAGE_SIZE, offset + len(data))
        after[offset:end] = data[: end - offset]

    ranges = changed_ranges(bytes(before), after)
    rebuilt = bytearray(before)
    apply_delta(rebuilt, encode_delta(1, 0, after, ranges))

    assert rebuilt[UNLOGGED_PREFIX:] == after[UNLOGGED_PREFIX:]
    assert rebuilt[:UNLOGGED_PREFIX] == before[:UNLOGGED_PREFIX]  # untouched by the delta
    # Ranges are in order, do not overlap, and never start or end on an unchanged byte.
    position = UNLOGGED_PREFIX
    for offset, length in ranges:
        assert offset >= position and length > 0
        assert before[offset] != after[offset]
        assert before[offset + length - 1] != after[offset + length - 1]
        position = offset + length


def reference_changed_ranges(before: bytes, after: bytes | bytearray) -> list[tuple[int, int]]:
    """The straightforward version: compare every 32-byte block, then trim each run."""
    size = len(after)
    runs: list[tuple[int, int]] = []
    start = -1
    for offset in range(UNLOGGED_PREFIX, size, 32):
        if before[offset : offset + 32] != after[offset : offset + 32]:
            if start < 0:
                start = offset
        elif start >= 0:
            runs.append((start, offset))
            start = -1
    if start >= 0:
        runs.append((start, size))
    out = []
    for low, high in runs:
        while before[low] == after[low]:
            low += 1
        while before[high - 1] == after[high - 1]:
            high -= 1
        out.append((low, high - low))
    return out


# Edits are placed on or around the edges of the 1 KiB chunk grid (which starts at
# byte 16), because that is where a two-level comparison could go wrong.
CHUNK_EDGES = [
    16 + 1024 * k + delta
    for k in range(32)
    for delta in (-33, -32, -1, 0, 1, 31, 32)
    if 16 + 1024 * k + delta >= 0
]
EDGE_EDITS = st.lists(
    st.tuples(
        st.one_of(st.sampled_from(CHUNK_EDGES), st.integers(16, 32767)),
        st.binary(min_size=1, max_size=70),
    ),
    max_size=10,
)


@settings(max_examples=400)
@given(EDGE_EDITS, st.sampled_from([4096, 8192, 16384, 32768]))
def test_two_level_comparison_gives_exactly_the_straightforward_answer(
    edits: list[tuple[int, bytes]], page_size: int
) -> None:
    before = bytes(page_with(3)) + bytes(page_size - PAGE_SIZE)
    before = before[:page_size]
    after = bytearray(before)
    for offset, data in edits:
        if offset >= page_size:
            continue
        end = min(page_size, offset + len(data))
        after[offset:end] = data[: end - offset]
    assert len(after) == page_size  # edits overwrite bytes; they never grow the page

    assert changed_ranges(before, after) == reference_changed_ranges(before, after)


def test_a_change_in_the_last_partial_chunk_is_found() -> None:
    before = bytes(4096)
    after = bytearray(before)
    after[4095] = 1  # the page ends part-way through a chunk

    assert changed_ranges(before, after) == [(4095, 1)]


GOOD = encode_delta(1, 0, page_with(9), [(100, 4), (200, 2)])


@pytest.mark.parametrize(
    "payload",
    [
        GOOD[:10],  # header cut short
        GOOD[:16],  # first range header cut short
        GOOD[:-1],  # last range's bytes cut short
        GOOD + b"\x00",  # trailing byte
        struct.pack("<IQH", 1, 0, 1) + struct.pack("<HH", 4, 4) + b"abcd",  # inside the header
        struct.pack("<IQH", 1, 0, 1) + struct.pack("<HH", 4094, 4) + b"abcd",  # past the end
        struct.pack("<IQH", 1, 0, 3) + struct.pack("<HH", 100, 1) + b"a",  # fewer ranges than said
    ],
    ids=["short-head", "short-range", "short-bytes", "trailing", "in-header", "overrun", "count"],
)
def test_malformed_deltas_are_rejected(payload: bytes) -> None:
    page = page_with(0)
    with pytest.raises(CorruptWalError):
        delta_header(payload)
        apply_delta(page, payload)


# ---------------------------------------------------------------------- recovery


class Log:
    """Builds a log of images and deltas the way the commit path does."""

    def __init__(self, tmp_path: Path, pages: int = 2) -> None:
        self.disk = DiskManager.create(tmp_path / "db.mvrs")
        for _ in range(pages):
            page_id = self.disk.extend()
            self.disk.write_page(page_id, new_page(PAGE_SIZE, page_id, PageType.HEAP))
        self.wal_path = tmp_path / "db.mvrs.wal"
        self.wal = WalWriter.create(self.wal_path, PAGE_SIZE, self.disk.header.db_id, 1)
        # What each page looks like after the records logged so far.
        self.state = {p: new_page(PAGE_SIZE, p, PageType.HEAP) for p in range(1, pages + 1)}

    def image(self, txn: int, page_id: int, fill: int) -> int:
        page = page_with(fill, page_id)
        lsn = self.wal.next_lsn
        set_page_lsn(page, lsn)
        self.wal.append(RecordType.PAGE_IMAGE, txn, struct.pack("<I", page_id) + bytes(page))
        self.state[page_id] = page
        return lsn

    def delta(self, txn: int, page_id: int, offset: int, data: bytes) -> int:
        before = self.state[page_id]
        after = bytearray(before)
        after[offset : offset + len(data)] = data
        lsn = self.wal.next_lsn
        payload = encode_delta(
            page_id, page_lsn(before), after, changed_ranges(bytes(before), after)
        )
        self.wal.append(RecordType.PAGE_DELTA, txn, payload)
        set_page_lsn(after, lsn)
        self.state[page_id] = after
        return lsn

    def commit(self, txn: int) -> None:
        self.wal.append(RecordType.COMMIT, txn)

    def recover(self) -> object:
        self.wal.flush()
        self.wal.close()
        return recover(self.disk, self.wal_path)


def test_image_then_deltas_are_replayed_in_order(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"first")
    log.commit(2)
    last = log.delta(3, 1, 102, b"XY")  # overlaps the previous delta
    log.commit(3)

    stats = log.recover()

    page = log.disk.read_page(1)
    assert bytes(page[100:105]) == b"fiXYt"
    assert page[99] == 0x11 and page[105] == 0x11
    assert page_lsn(page) == last
    assert (stats.pages_restored, stats.deltas_applied) == (1, 2)  # type: ignore[attr-defined]
    log.disk.close()


def test_uncommitted_deltas_are_ignored(tmp_path: Path) -> None:
    log = Log(tmp_path)
    committed = log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"never committed")

    log.recover()

    page = log.disk.read_page(1)
    assert bytes(page[100:105]) == b"\x11" * 5
    assert page_lsn(page) == committed
    log.disk.close()


def test_page_flushed_partway_resumes_from_its_lsn(tmp_path: Path) -> None:
    """The page reached disk after the first delta; only the later ones are replayed."""
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"aaaa")
    log.commit(2)
    log.disk.write_page(1, log.state[1])  # flushed here, before the crash
    log.delta(3, 1, 200, b"bbbb")
    log.commit(3)
    last = log.delta(4, 1, 300, b"cccc")
    log.commit(4)

    stats = log.recover()

    page = log.disk.read_page(1)
    assert (bytes(page[100:104]), bytes(page[200:204]), bytes(page[300:304])) == (
        b"aaaa",
        b"bbbb",
        b"cccc",
    )
    assert page_lsn(page) == last
    assert stats.deltas_applied == 2  # type: ignore[attr-defined]
    log.disk.close()


def test_page_already_current_is_left_alone(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"aaaa")
    log.commit(2)
    log.disk.write_page(1, log.state[1])

    stats = log.recover()

    assert (stats.pages_restored, stats.pages_already_current) == (0, 1)  # type: ignore[attr-defined]
    log.disk.close()


def test_torn_page_is_rebuilt_from_the_image_and_every_delta_after_it(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"aaaa")
    log.commit(2)
    log.delta(3, 1, 3000, b"bbbb")
    log.commit(3)
    # The newest version was being written when the power failed.
    log.disk.write_page(1, log.state[1])
    log.disk.close()
    path = tmp_path / "db.mvrs"
    raw = bytearray(path.read_bytes())
    raw[PAGE_SIZE + 2048 : 2 * PAGE_SIZE] = bytes(2048)
    path.write_bytes(raw)
    log.disk = DiskManager.open(path)

    stats = log.recover()

    page = log.disk.read_page(1)
    assert bytes(page[100:104]) == b"aaaa" and bytes(page[3000:3004]) == b"bbbb"
    assert page[4000] == 0x11
    assert stats.deltas_applied == 2  # type: ignore[attr-defined]
    log.disk.close()


def test_a_later_image_supersedes_earlier_records(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"aaaa")
    log.commit(2)
    log.image(3, 1, 0x33)  # for example, a change too large for a delta
    log.commit(3)
    last = log.delta(4, 1, 100, b"zz")
    log.commit(4)

    stats = log.recover()

    page = log.disk.read_page(1)
    assert bytes(page[100:104]) == b"zz\x33\x33"
    assert page_lsn(page) == last
    assert stats.deltas_applied == 1  # type: ignore[attr-defined]
    log.disk.close()


def test_deltas_to_different_pages_do_not_interfere(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.image(1, 2, 0x22)
    log.commit(1)
    log.delta(2, 2, 50, b"two")
    log.delta(2, 1, 50, b"one")
    log.commit(2)

    log.recover()

    assert bytes(log.disk.read_page(1)[50:53]) == b"one"
    assert bytes(log.disk.read_page(2)[50:53]) == b"two"
    log.disk.close()


def test_recovery_with_deltas_is_idempotent(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"aaaa")
    log.commit(2)

    log.recover()
    log.disk.sync()
    first = (tmp_path / "db.mvrs").read_bytes()
    again = recover(log.disk, log.wal_path)
    log.disk.sync()

    assert (tmp_path / "db.mvrs").read_bytes() == first
    assert (again.pages_restored, again.pages_already_current) == (0, 1)
    log.disk.close()


def test_a_delta_that_does_not_follow_the_page_is_refused(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.image(1, 1, 0x11)
    log.commit(1)
    log.delta(2, 1, 100, b"aaaa")
    log.commit(2)
    # A delta claiming to follow an LSN the page never had.
    bogus = encode_delta(1, 999_999, log.state[1], [(200, 4)])
    log.wal.append(RecordType.PAGE_DELTA, 3, bogus)
    log.commit(3)

    with pytest.raises(CorruptWalError, match="follows LSN 999999"):
        log.recover()
    log.disk.close()


def test_a_damaged_page_with_only_deltas_in_the_log_cannot_be_recovered(tmp_path: Path) -> None:
    """Cannot happen in normal operation: the first change after a checkpoint is an image."""
    log = Log(tmp_path)
    log.delta(1, 1, 100, b"aaaa")
    log.commit(1)
    log.disk.close()
    path = tmp_path / "db.mvrs"
    raw = bytearray(path.read_bytes())
    raw[PAGE_SIZE + 500] ^= 0xFF
    path.write_bytes(raw)
    log.disk = DiskManager.open(path)

    with pytest.raises(CorruptPageError, match="holds no full image"):
        log.recover()
    log.disk.close()


def test_delta_for_a_page_outside_the_file_is_refused(tmp_path: Path) -> None:
    log = Log(tmp_path)
    log.wal.append(RecordType.PAGE_DELTA, 1, encode_delta(77, 0, page_with(1, 77), [(100, 4)]))
    log.commit(1)

    with pytest.raises(CorruptWalError, match="outside the file"):
        log.recover()
    log.disk.close()
