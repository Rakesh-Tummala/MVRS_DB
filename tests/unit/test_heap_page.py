"""Slotted heap page."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kestreldb.errors import CorruptPageError, InvalidRecordIdError, InvalidValueError
from kestreldb.storage import heap_page
from kestreldb.storage.page import PageType, new_page

PAGE_SIZE = 4096


def empty_page(page_size: int = PAGE_SIZE) -> bytearray:
    buf = new_page(page_size, 1, PageType.HEAP)
    heap_page.init_heap_page(buf)
    return buf


def test_empty_page() -> None:
    buf = empty_page()

    heap_page.validate(buf)
    assert heap_page.slot_count(buf) == 0
    assert heap_page.free_space(buf) == PAGE_SIZE - 36
    assert heap_page.next_page(buf) == 0
    assert list(heap_page.live_records(buf)) == []


def test_insert_and_read() -> None:
    buf = empty_page()

    assert heap_page.insert(buf, b"first") == 0
    assert heap_page.insert(buf, b"second!") == 1

    assert heap_page.read(buf, 0) == b"first"
    assert heap_page.read(buf, 1) == b"second!"
    assert buf[-5:] == b"first"  # records grow down from the end of the page
    heap_page.validate(buf)


def test_a_record_of_the_maximum_size_fills_the_page() -> None:
    buf = empty_page()
    record = b"x" * heap_page.max_record_size(PAGE_SIZE)

    assert heap_page.insert(buf, record) == 0
    assert heap_page.free_space(buf) == 0
    assert heap_page.insert(buf, b"y") is None
    assert heap_page.read(buf, 0) == record
    heap_page.validate(buf)


def test_a_record_one_byte_too_large_does_not_fit() -> None:
    buf = empty_page()
    before = bytes(buf)

    assert heap_page.insert(buf, b"x" * (heap_page.max_record_size(PAGE_SIZE) + 1)) is None
    assert bytes(buf) == before


def test_delete_frees_the_slot_for_reuse() -> None:
    buf = empty_page()
    for record in (b"a", b"bb", b"ccc"):
        heap_page.insert(buf, record)

    heap_page.delete(buf, 1)

    with pytest.raises(InvalidRecordIdError):
        heap_page.read(buf, 1)
    assert heap_page.insert(buf, b"dddd") == 1
    assert [r for _, r in heap_page.live_records(buf)] == [b"a", b"dddd", b"ccc"]
    heap_page.validate(buf)


def test_deleting_everything_resets_the_page() -> None:
    buf = empty_page()
    for i in range(10):
        heap_page.insert(buf, bytes([i]) * 50)

    for slot in range(10):
        heap_page.delete(buf, slot)

    assert heap_page.slot_count(buf) == 0
    assert heap_page.free_space(buf) == PAGE_SIZE - 36
    heap_page.validate(buf)


def test_slot_numbers_survive_compaction() -> None:
    buf = empty_page()
    slots = [heap_page.insert(buf, bytes([i]) * 300) for i in range(13)]
    assert None not in slots
    for slot in (1, 3, 5, 7, 9, 11):
        heap_page.delete(buf, slot)

    # 1800 bytes are free only as scattered holes, so this forces a compaction.
    assert heap_page.insert(buf, b"z" * 1500) == 1

    for slot in (0, 2, 4, 6, 8, 10, 12):
        assert heap_page.read(buf, slot) == bytes([slot]) * 300
    heap_page.validate(buf)


def test_update_in_place_when_not_larger() -> None:
    buf = empty_page()
    heap_page.insert(buf, b"hello world")

    assert heap_page.update(buf, 0, b"bye") is True

    assert heap_page.read(buf, 0) == b"bye"
    heap_page.validate(buf)


def test_update_that_grows_keeps_the_slot() -> None:
    buf = empty_page()
    heap_page.insert(buf, b"a" * 10)
    heap_page.insert(buf, b"b" * 10)

    assert heap_page.update(buf, 0, b"A" * 2000) is True

    assert heap_page.read(buf, 0) == b"A" * 2000
    assert heap_page.read(buf, 1) == b"b" * 10
    heap_page.validate(buf)


def test_update_that_cannot_fit_leaves_the_page_unchanged() -> None:
    buf = empty_page()
    heap_page.insert(buf, b"a" * 2000)
    heap_page.insert(buf, b"b" * 2000)
    before = bytes(buf)

    assert heap_page.update(buf, 0, b"c" * 2100) is False

    assert bytes(buf) == before


def test_update_can_use_its_own_old_space() -> None:
    buf = empty_page()
    heap_page.insert(buf, b"a" * 2000)
    heap_page.insert(buf, b"b" * 2000)

    assert heap_page.update(buf, 0, b"c" * 2040) is True

    assert heap_page.read(buf, 0) == b"c" * 2040
    assert heap_page.read(buf, 1) == b"b" * 2000
    heap_page.validate(buf)


@pytest.mark.parametrize("slot", [-1, 3, 100])
def test_operations_on_missing_slots_raise(slot: int) -> None:
    buf = empty_page()
    for record in (b"a", b"b", b"c"):
        heap_page.insert(buf, record)

    for operation in (
        lambda: heap_page.read(buf, slot),
        lambda: heap_page.delete(buf, slot),
        lambda: heap_page.update(buf, slot, b"x"),
    ):
        with pytest.raises(InvalidRecordIdError):
            operation()


def test_empty_records_are_refused() -> None:
    buf = empty_page()
    heap_page.insert(buf, b"a")

    with pytest.raises(InvalidValueError):
        heap_page.insert(buf, b"")
    with pytest.raises(InvalidValueError):
        heap_page.update(buf, 0, b"")


def test_next_page_pointer() -> None:
    buf = empty_page()
    heap_page.insert(buf, b"record")

    heap_page.set_next_page(buf, 0xDEADBEEF)

    assert heap_page.next_page(buf) == 0xDEADBEEF
    assert heap_page.read(buf, 0) == b"record"
    heap_page.validate(buf)


@pytest.mark.parametrize(
    ("offset", "value", "message"),
    [
        (26, b"\x00\x00", "H1"),  # free_end below the slot directory
        (36, b"\x10\x00\x05\x00", "H2"),  # slot 0 points into the header
        (28, b"\x99\x00", "H4"),  # frag_bytes wrong
    ],
)
def test_validate_catches_broken_pages(offset: int, value: bytes, message: str) -> None:
    buf = empty_page()
    heap_page.insert(buf, b"a" * 20)
    heap_page.insert(buf, b"b" * 20)
    buf[offset : offset + len(value)] = value

    with pytest.raises(CorruptPageError, match=message):
        heap_page.validate(buf)


def test_validate_catches_overlapping_records() -> None:
    buf = empty_page()
    heap_page.insert(buf, b"a" * 20)
    heap_page.insert(buf, b"b" * 20)
    buf[40:44] = buf[36:40]  # slot 1 now points at slot 0's bytes

    with pytest.raises(CorruptPageError, match="H3"):
        heap_page.validate(buf)


OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("insert"), st.binary(min_size=1, max_size=900)),
        st.tuples(st.just("delete"), st.integers(0, 40)),
        st.tuples(st.just("update"), st.integers(0, 40), st.binary(min_size=1, max_size=900)),
    ),
    max_size=80,
)


@settings(max_examples=300, deadline=None)
@given(OPERATIONS)
def test_random_operations_match_a_dictionary_model(operations: list[tuple]) -> None:  # type: ignore[type-arg]
    buf = empty_page()
    model: dict[int, bytes] = {}

    for operation in operations:
        if operation[0] == "insert":
            slot = heap_page.insert(buf, operation[1])
            if slot is None:
                # A refusal must be genuine: the record really does not fit.
                dead_slot_free = heap_page.slot_count(buf) > len(model)
                needed = len(operation[1]) + (0 if dead_slot_free else heap_page.SLOT_SIZE)
                assert needed > heap_page.free_space(buf)
            else:
                assert slot not in model
                model[slot] = operation[1]
        elif operation[0] == "delete":
            slot = operation[1]
            if slot in model:
                heap_page.delete(buf, slot)
                del model[slot]
            else:
                with pytest.raises(InvalidRecordIdError):
                    heap_page.delete(buf, slot)
        else:
            slot, record = operation[1], operation[2]
            if slot in model:
                if heap_page.update(buf, slot, record):
                    model[slot] = record
            else:
                with pytest.raises(InvalidRecordIdError):
                    heap_page.update(buf, slot, record)

        heap_page.validate(buf)
        assert dict(heap_page.live_records(buf)) == model  # H5: slots are stable
        assert heap_page.live_count(buf) == len(model)
