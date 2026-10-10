"""Page header and checksum."""

from __future__ import annotations

import struct

import pytest

from kestreldb import config
from kestreldb.errors import (
    ChecksumMismatchError,
    CorruptPageError,
    PageIdMismatchError,
    UnsupportedVersionError,
)
from kestreldb.storage.page import (
    PAGE_HEADER,
    PAGE_HEADER_SIZE,
    PageType,
    check_header,
    compute_checksum,
    new_page,
    read_header,
    stamp_checksum,
    verify_page,
)

PAGE_SIZE = config.DEFAULT_PAGE_SIZE


def stamped_page(page_id: int = 7, page_type: PageType = PageType.HEAP) -> bytearray:
    buf = new_page(PAGE_SIZE, page_id, page_type)
    buf[PAGE_HEADER_SIZE:] = bytes(i % 251 for i in range(PAGE_SIZE - PAGE_HEADER_SIZE))
    stamp_checksum(buf)
    return buf


def test_header_is_24_bytes() -> None:
    assert PAGE_HEADER_SIZE == 24
    assert PAGE_HEADER.format == "<IIQBBHI"


def test_new_page_has_expected_header_and_zero_body() -> None:
    buf = new_page(PAGE_SIZE, 42, PageType.BTREE_LEAF, lsn=99)

    assert len(buf) == PAGE_SIZE
    header = read_header(buf)
    assert header.page_id == 42
    assert header.lsn == 99
    assert header.page_type == PageType.BTREE_LEAF
    assert header.format_version == config.PAGE_FORMAT_VERSION
    assert header.flags == 0
    assert header.reserved == 0
    assert bytes(buf[PAGE_HEADER_SIZE:]) == bytes(PAGE_SIZE - PAGE_HEADER_SIZE)


def test_header_fields_are_little_endian_at_documented_offsets() -> None:
    buf = new_page(PAGE_SIZE, 0x01020304, PageType.HEAP, lsn=0x1122334455667788)

    assert bytes(buf[4:8]) == bytes([0x04, 0x03, 0x02, 0x01])
    assert bytes(buf[8:16]) == bytes([0x88, 0x77, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11])
    assert buf[16] == PageType.HEAP
    assert buf[17] == config.PAGE_FORMAT_VERSION


def test_stamped_page_verifies() -> None:
    buf = stamped_page(page_id=7)

    header = verify_page(buf, 7)

    assert header.page_id == 7
    assert header.checksum == compute_checksum(buf)


def test_checksum_covers_everything_after_the_checksum_field() -> None:
    buf = stamped_page()
    before = compute_checksum(buf)

    buf[0:4] = b"\xff\xff\xff\xff"  # the checksum field itself is not covered
    assert compute_checksum(buf) == before

    buf[4] ^= 0x01  # the very next byte is
    assert compute_checksum(buf) != before


def test_unstamped_page_fails_verification() -> None:
    buf = new_page(PAGE_SIZE, 1, PageType.HEAP)

    with pytest.raises(ChecksumMismatchError):
        verify_page(buf, 1)


def test_all_zero_page_is_never_valid() -> None:
    with pytest.raises(ChecksumMismatchError):
        verify_page(bytearray(PAGE_SIZE), 0)


def test_flipping_any_single_byte_is_detected() -> None:
    original = stamped_page()

    for offset in range(PAGE_SIZE):
        damaged = bytearray(original)
        damaged[offset] ^= 0x01
        with pytest.raises(ChecksumMismatchError):
            verify_page(damaged, 7)


def test_torn_page_is_detected() -> None:
    old = stamped_page()
    new = bytearray(old)
    new[PAGE_HEADER_SIZE:] = bytes(PAGE_SIZE - PAGE_HEADER_SIZE)
    stamp_checksum(new)

    torn = new[: PAGE_SIZE // 2] + old[PAGE_SIZE // 2 :]

    with pytest.raises(ChecksumMismatchError):
        verify_page(torn, 7)


def test_valid_page_at_wrong_position_is_detected() -> None:
    buf = stamped_page(page_id=7)

    with pytest.raises(PageIdMismatchError):
        verify_page(buf, 8)


def test_unknown_page_format_version_is_refused() -> None:
    buf = stamped_page()
    buf[17] = config.PAGE_FORMAT_VERSION + 1
    stamp_checksum(buf)

    with pytest.raises(UnsupportedVersionError):
        verify_page(buf, 7)


@pytest.mark.parametrize("bad_type", [0, 5, 255])
def test_invalid_page_type_is_refused(bad_type: int) -> None:
    buf = stamped_page()
    buf[16] = bad_type
    stamp_checksum(buf)

    with pytest.raises(CorruptPageError):
        verify_page(buf, 7)


@pytest.mark.parametrize("offset", [18, 19, 20, 23])
def test_nonzero_reserved_bytes_are_refused(offset: int) -> None:
    buf = stamped_page()
    buf[offset] = 1
    stamp_checksum(buf)

    with pytest.raises(CorruptPageError):
        verify_page(buf, 7)


def test_buffer_shorter_than_a_header_is_refused() -> None:
    with pytest.raises(CorruptPageError):
        verify_page(bytes(10), 1)
    with pytest.raises(CorruptPageError):
        check_header(bytes(10), 1)


def test_check_header_ignores_the_checksum() -> None:
    buf = new_page(PAGE_SIZE, 3, PageType.FREE)
    struct.pack_into("<I", buf, 0, 0xDEADBEEF)

    assert check_header(buf, 3).page_id == 3
