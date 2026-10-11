"""The 24-byte header shared by every page except page 0, and its checksum.

Layout (little-endian), see docs/03-storage-format.md:

    offset  size  field
    0       4     checksum        CRC32 of bytes 4 .. end of page
    4       4     page_id         must equal the page's position in the file
    8       8     lsn             last log record applied (0 before Phase 4)
    16      1     page_type
    17      1     format_version
    18      2     flags           0
    20      4     reserved        0

The checksum comes first so that it covers one contiguous range.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

from mvrs_db import config
from mvrs_db.errors import (
    ChecksumMismatchError,
    CorruptPageError,
    PageIdMismatchError,
    UnsupportedVersionError,
)

PAGE_HEADER = struct.Struct("<IIQBBHI")
PAGE_HEADER_SIZE = PAGE_HEADER.size  # 24

_CHECKSUM = struct.Struct("<I")
_CHECKSUM_SIZE = _CHECKSUM.size

Buffer = bytes | bytearray | memoryview


class PageType(IntEnum):
    """Page types. 0 is deliberately not a member: an all-zero page is never valid."""

    FREE = 1
    HEAP = 2
    BTREE_LEAF = 3
    BTREE_INTERNAL = 4


_VALID_PAGE_TYPES = frozenset(int(t) for t in PageType)


@dataclass(frozen=True, slots=True)
class PageHeader:
    checksum: int
    page_id: int
    lsn: int
    page_type: int
    format_version: int
    flags: int
    reserved: int


def new_page(page_size: int, page_id: int, page_type: PageType, lsn: int = 0) -> bytearray:
    """Return a zero-filled page with a valid header. The checksum is not stamped yet."""
    buf = bytearray(page_size)
    PAGE_HEADER.pack_into(buf, 0, 0, page_id, lsn, int(page_type), config.PAGE_FORMAT_VERSION, 0, 0)
    return buf


def read_header(buf: Buffer) -> PageHeader:
    """Decode the header fields without validating them."""
    return PageHeader(*PAGE_HEADER.unpack_from(buf, 0))


_LSN = struct.Struct("<Q")
_LSN_OFFSET = 8
_TYPE_OFFSET = 16


def page_type_of(buf: Buffer) -> int:
    """The raw page type byte."""
    return buf[_TYPE_OFFSET]


def page_lsn(buf: Buffer) -> int:
    lsn: int = _LSN.unpack_from(buf, _LSN_OFFSET)[0]
    return lsn


def set_page_lsn(buf: bytearray, lsn: int) -> None:
    _LSN.pack_into(buf, _LSN_OFFSET, lsn)


def compute_checksum(buf: Buffer) -> int:
    """CRC32 of everything after the checksum field."""
    return zlib.crc32(memoryview(buf)[_CHECKSUM_SIZE:])


def stamp_checksum(buf: bytearray) -> None:
    """Write the checksum of the page's current contents into its header."""
    _CHECKSUM.pack_into(buf, 0, compute_checksum(buf))


def check_header(buf: Buffer, expected_page_id: int) -> PageHeader:
    """Validate every header field except the checksum.

    Raises PageIdMismatchError, UnsupportedVersionError or CorruptPageError.
    """
    if len(buf) < PAGE_HEADER_SIZE:
        raise CorruptPageError(
            f"page {expected_page_id}: buffer of {len(buf)} bytes is smaller than a page header"
        )
    header = read_header(buf)
    if header.page_id != expected_page_id:
        raise PageIdMismatchError(
            f"page {expected_page_id}: header says it is page {header.page_id}"
        )
    if header.format_version != config.PAGE_FORMAT_VERSION:
        raise UnsupportedVersionError(
            f"page {expected_page_id}: page format version {header.format_version} "
            f"is not supported (expected {config.PAGE_FORMAT_VERSION})"
        )
    if header.page_type not in _VALID_PAGE_TYPES:
        raise CorruptPageError(f"page {expected_page_id}: invalid page type {header.page_type}")
    if header.flags != 0 or header.reserved != 0:
        raise CorruptPageError(f"page {expected_page_id}: reserved header bytes are not zero")
    return header


def verify_page(buf: Buffer, expected_page_id: int) -> PageHeader:
    """Validate a page that was read from disk.

    The checksum is checked first: if it fails, no other field can be trusted.
    """
    if len(buf) < PAGE_HEADER_SIZE:
        raise CorruptPageError(
            f"page {expected_page_id}: buffer of {len(buf)} bytes is smaller than a page header"
        )
    stored = _CHECKSUM.unpack_from(buf, 0)[0]
    actual = compute_checksum(buf)
    if stored != actual:
        raise ChecksumMismatchError(
            f"page {expected_page_id}: stored checksum {stored:#010x} "
            f"does not match contents {actual:#010x}"
        )
    return check_header(buf, expected_page_id)
