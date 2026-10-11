"""B+ tree node pages: in-place insert and delete, hole accounting, and validation."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mvrs_db.errors import CorruptIndexError
from mvrs_db.index import btree_node as node
from mvrs_db.recovery.delta import changed_ranges, delta_size
from mvrs_db.storage.heap_file import RecordId
from mvrs_db.storage.page import PageType, new_page

PAGE_SIZE = 4096


def key(n: int) -> bytes:
    return n.to_bytes(8, "big")


def rid(n: int) -> RecordId:
    return RecordId(1 + n % 500, n % 40)


def empty_leaf(page_size: int = PAGE_SIZE) -> bytearray:
    buf = new_page(page_size, 1, PageType.BTREE_LEAF)
    node.init_leaf(buf)
    return buf


def empty_internal(first_child: int = 7) -> bytearray:
    buf = new_page(PAGE_SIZE, 1, PageType.BTREE_INTERNAL)
    node.encode_internal(buf, [], [first_child])
    return buf


def leaf_entries(buf: bytearray) -> list[tuple[bytes, RecordId]]:
    keys, rids, _link = node.decode_leaf(buf)
    return list(zip(keys, rids, strict=True))


def insert_sorted(buf: bytearray, n: int, capacity: int = PAGE_SIZE) -> bool:
    found, index = node.locate_leaf(buf, key(n))
    assert not found
    return node.leaf_insert(buf, index, key(n), rid(n), capacity)


# ---------------------------------------------------------------------- leaf inserts


def test_inserting_in_any_order_keeps_the_leaf_sorted() -> None:
    buf = empty_leaf()
    values = [50, 10, 90, 30, 70, 20, 80, 40, 60, 0, 100]

    for n in values:
        assert insert_sorted(buf, n)
        node.validate(buf, leaf=True)

    assert leaf_entries(buf) == [(key(n), rid(n)) for n in sorted(values)]


def test_a_leaf_insert_matches_rebuilding_the_page() -> None:
    in_place = empty_leaf()
    for n in range(0, 200, 3):
        insert_sorted(in_place, n)
    insert_sorted(in_place, 100)
    rebuilt = empty_leaf()
    expected = sorted([*range(0, 200, 3), 100])
    node.encode_leaf(rebuilt, [key(n) for n in expected], [rid(n) for n in expected], 0)

    assert leaf_entries(in_place) == leaf_entries(rebuilt)
    assert node.locate_leaf(in_place, key(100)) == node.locate_leaf(rebuilt, key(100))


def test_the_sibling_link_survives_edits() -> None:
    buf = empty_leaf()
    node.encode_leaf(buf, [key(5)], [rid(5)], 4242)

    insert_sorted(buf, 1)
    insert_sorted(buf, 9)
    node.leaf_delete(buf, 1)

    assert node.decode_leaf(buf)[2] == 4242


def test_a_leaf_that_is_full_refuses_the_insert_and_is_left_unchanged() -> None:
    buf = empty_leaf()
    n = 0
    while insert_sorted(buf, n):
        n += 1
    before = bytes(buf)

    assert not insert_sorted(buf, n)

    assert bytes(buf) == before
    # Roughly (page - header) / (pointer + length byte + 8-byte key + 6-byte record id).
    assert 230 <= n <= 245


def test_a_node_capacity_below_the_page_size_is_respected() -> None:
    buf = empty_leaf()
    capacity = 150
    n = 0
    while insert_sorted(buf, n, capacity):
        n += 1

    assert n == (capacity - node.CELLS_START) // 17  # 17 bytes per entry, header excluded
    assert node.leaf_size([key(i) for i in range(n)]) <= capacity


def test_an_almost_empty_leaf_accepts_an_entry_whatever_the_cap() -> None:
    """A split needs two keys to make two non-empty halves, so the first one always fits."""
    buf = empty_leaf()

    assert node.leaf_insert(buf, 0, key(1), rid(1), capacity=node.CELLS_START + 1)
    assert not node.leaf_insert(buf, 1, key(2), rid(2), capacity=node.CELLS_START + 1)


# ---------------------------------------------------------------------- deletes and holes


def test_deleting_leaves_a_hole_that_is_counted() -> None:
    buf = empty_leaf()
    for n in range(10):
        insert_sorted(buf, n)
    free_end_before = node._NODE_HEADER.unpack_from(buf, node.NODE_HEADER_OFFSET)[1]

    node.leaf_delete(buf, 4)  # a cell in the middle of the cell area

    count, free_end, _link, frag = node._NODE_HEADER.unpack_from(buf, node.NODE_HEADER_OFFSET)
    assert (count, free_end, frag) == (9, free_end_before, 15)  # 1 + 8-byte key + 6
    node.validate(buf, leaf=True)
    assert [k for k, _ in leaf_entries(buf)] == [key(n) for n in (0, 1, 2, 3, 5, 6, 7, 8, 9)]


def test_deleting_the_lowest_cell_gives_its_space_back_at_once() -> None:
    buf = empty_leaf()
    for n in range(10):
        insert_sorted(buf, n)  # ascending, so the newest cell is the lowest in memory
    free_end_before = node._NODE_HEADER.unpack_from(buf, node.NODE_HEADER_OFFSET)[1]

    node.leaf_delete(buf, 9)

    _count, free_end, _link, frag = node._NODE_HEADER.unpack_from(buf, node.NODE_HEADER_OFFSET)
    assert free_end == free_end_before + 15
    assert frag == 0
    node.validate(buf, leaf=True)


def test_emptying_a_leaf_resets_it() -> None:
    buf = empty_leaf()
    for n in range(5):
        insert_sorted(buf, n)

    for _ in range(5):
        node.leaf_delete(buf, 0)

    count, free_end, _link, frag = node._NODE_HEADER.unpack_from(buf, node.NODE_HEADER_OFFSET)
    assert (count, free_end, frag) == (0, PAGE_SIZE, 0)
    node.validate(buf, leaf=True)
    assert insert_sorted(buf, 3)


def header(buf: bytearray) -> tuple[int, int, int, int]:
    return node._NODE_HEADER.unpack_from(buf, node.NODE_HEADER_OFFSET)  # type: ignore[no-any-return]


def test_a_hole_is_reused_by_repacking_when_the_free_gap_is_too_small() -> None:
    buf = empty_leaf()
    n = 0
    while insert_sorted(buf, n * 2):
        n += 1
    assert not insert_sorted(buf, 1)  # full
    node.leaf_delete(buf, 5)  # a cell that is not the lowest, so it leaves a hole
    count, free_end, _link, frag = header(buf)
    gap = free_end - (node.CELLS_START + 2 * count)
    need = 1 + 8 + 6 + 2
    assert frag == 15 and gap < need <= gap + frag  # room in total, but not in one piece

    assert insert_sorted(buf, 1)

    node.validate(buf, leaf=True)
    assert header(buf)[3] == 0  # the cells were packed, so no holes remain
    expected = sorted([*(2 * i for i in range(n) if i != 5), 1])
    assert [k for k, _ in leaf_entries(buf)] == [key(v) for v in expected]


def test_a_hole_is_not_repacked_when_the_gap_is_big_enough() -> None:
    buf = empty_leaf()
    for i in range(10):
        insert_sorted(buf, i)
    node.leaf_delete(buf, 4)
    assert header(buf)[3] == 15

    insert_sorted(buf, 100)

    assert header(buf)[3] == 15  # no needless repack: the hole is still there
    node.validate(buf, leaf=True)


def test_delete_rejects_a_cell_that_does_not_exist() -> None:
    buf = empty_leaf()
    insert_sorted(buf, 1)

    with pytest.raises(CorruptIndexError):
        node.leaf_delete(buf, 1)
    with pytest.raises(CorruptIndexError):
        node.leaf_delete(buf, -1)


OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("insert"), st.integers(0, 400)),
        st.tuples(st.just("delete"), st.integers(0, 400)),
    ),
    max_size=300,
)


@settings(max_examples=300, deadline=None)
@given(OPERATIONS, st.sampled_from([4096, 1024, 300, 90]))
def test_random_edits_match_a_sorted_list(operations: list[tuple[str, int]], capacity: int) -> None:
    buf = empty_leaf()
    model: dict[int, RecordId] = {}

    for kind, n in operations:
        found, index = node.locate_leaf(buf, key(n))
        assert found == (n in model)
        if kind == "insert" and not found:
            fits = node.leaf_insert(buf, index, key(n), rid(n), capacity)
            if fits:
                model[n] = rid(n)
            else:
                # A refusal must be genuine: the entry really would exceed the cap.
                count = len(model)
                logical = node.leaf_size([key(k) for k in model]) + 17
                assert logical > capacity and count >= 1
        elif kind == "delete" and found:
            node.leaf_delete(buf, index)
            del model[n]

        node.validate(buf, leaf=True)
        assert leaf_entries(buf) == [(key(k), model[k]) for k in sorted(model)]


# ---------------------------------------------------------------------- how little changes


def test_appending_changes_only_a_few_bytes() -> None:
    buf = empty_leaf()
    for n in range(100):
        insert_sorted(buf, n)
    before = bytes(buf)

    insert_sorted(buf, 100)

    # The new cell, the new pointer, and the header: no other pointer moves.
    assert delta_size(changed_ranges(before, buf)) < 80


def test_inserting_at_the_front_shifts_only_the_pointer_array() -> None:
    buf = empty_leaf()
    for n in range(100, 200):
        insert_sorted(buf, n)
    before = bytes(buf)

    insert_sorted(buf, 0)

    # 100 pointers move (200 bytes), plus the new cell and header. Rewriting the
    # node would have changed the whole cell area, about 1,500 bytes.
    assert delta_size(changed_ranges(before, buf)) < 320


def test_deleting_changes_only_the_pointer_array_and_header() -> None:
    buf = empty_leaf()
    for n in range(200):
        insert_sorted(buf, n)
    before = bytes(buf)

    node.leaf_delete(buf, 50)

    assert delta_size(changed_ranges(before, buf)) < 420  # 150 pointers shift
    assert bytes(buf[PAGE_SIZE - 600 :]) == before[PAGE_SIZE - 600 :]  # the cells did not move


# ---------------------------------------------------------------------- internal nodes


def internal_entries(buf: bytearray) -> tuple[list[bytes], list[int]]:
    return node.decode_internal(buf)


def test_internal_inserts_match_the_list_model() -> None:
    buf = empty_internal(first_child=100)
    keys: list[bytes] = []
    children = [100]
    for n in (50, 10, 90, 30, 70, 20):
        index, _child = node.locate_internal(buf, key(n))
        assert node.internal_insert(buf, index, key(n), 1000 + n, PAGE_SIZE)
        keys.insert(index, key(n))
        children.insert(index + 1, 1000 + n)
        node.validate(buf, leaf=False)

    assert internal_entries(buf) == (keys, children)
    assert keys == sorted(keys)


def test_locate_internal_routes_and_reports_the_position() -> None:
    buf = empty_internal(first_child=100)
    for n in (10, 20, 30):
        index, _ = node.locate_internal(buf, key(n))
        node.internal_insert(buf, index, key(n), 1000 + n, PAGE_SIZE)

    assert node.locate_internal(buf, key(5)) == (0, 100)
    assert node.locate_internal(buf, key(10)) == (
        1,
        1010,
    )  # a separator equal to the key goes right
    assert node.locate_internal(buf, key(15)) == (1, 1010)
    assert node.locate_internal(buf, key(30)) == (3, 1030)
    assert node.locate_internal(buf, key(99)) == (3, 1030)
    assert node.internal_child(buf, key(25)) == 1020


def test_first_and_last_child() -> None:
    buf = empty_internal(first_child=100)
    assert node.leftmost_child(buf) == node.rightmost_child(buf) == 100

    for n in (10, 20):
        index, _ = node.locate_internal(buf, key(n))
        node.internal_insert(buf, index, key(n), 1000 + n, PAGE_SIZE)

    assert node.leftmost_child(buf) == 100
    assert node.rightmost_child(buf) == 1020


def test_a_full_internal_node_refuses_and_a_tiny_one_always_accepts_two() -> None:
    buf = empty_internal()
    n = 0
    while True:
        index, _ = node.locate_internal(buf, key(n))
        if not node.internal_insert(buf, index, key(n), 1000 + n, PAGE_SIZE):
            break
        n += 1
    assert 250 <= n <= 300  # about (4096 - 36) / (2 + 1 + 8 + 4)

    small = empty_internal()
    for i in range(2):
        assert node.internal_insert(small, i, key(i), i, node.CELLS_START + 1)
    assert not node.internal_insert(small, 2, key(2), 2, node.CELLS_START + 1)


# ---------------------------------------------------------------------- validation


def corrupted(change: str) -> bytearray:
    buf = empty_leaf()
    for n in range(10):
        insert_sorted(buf, n)
    node.leaf_delete(buf, 3)  # leaves a hole, so frag_bytes is not zero
    count, free_end, link, frag = node._NODE_HEADER.unpack_from(buf, node.NODE_HEADER_OFFSET)
    if change == "frag":
        frag += 1
    elif change == "count":
        count = 2000
    elif change == "free_end":
        free_end = node.CELLS_START
    elif change == "pointer-low":
        node._POINTER.pack_into(buf, node.CELLS_START, free_end - 100)
    elif change == "pointer-high":
        node._POINTER.pack_into(buf, node.CELLS_START, PAGE_SIZE)
    elif change == "overlap":
        first = node._POINTER.unpack_from(buf, node.CELLS_START)[0]
        node._POINTER.pack_into(buf, node.CELLS_START + 2, first + 2)  # cell 1 now inside cell 0
    node._NODE_HEADER.pack_into(buf, node.NODE_HEADER_OFFSET, count, free_end, link, frag)
    return buf


def test_a_valid_node_with_holes_validates() -> None:
    node.validate(corrupted("none"), leaf=True)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("frag", "N3"),
        ("count", "N1"),
        ("free_end", "N1"),
        ("pointer-low", "N2"),
        ("pointer-high", "N2"),
        ("overlap", "N2"),
    ],
)
def test_validate_names_each_kind_of_damage(change: str, message: str) -> None:
    with pytest.raises(CorruptIndexError, match=message):
        node.validate(corrupted(change), leaf=True)
