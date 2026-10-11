"""Disk manager: create/open validation, page I/O, corruption detection."""

from __future__ import annotations

import struct
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mvrs_db import config
from mvrs_db.errors import (
    ChecksumMismatchError,
    CorruptFileError,
    CorruptHeaderError,
    CorruptPageError,
    DatabaseClosedError,
    DatabaseExistsError,
    DatabaseFullError,
    DatabaseNotFoundError,
    InvalidPageBufferError,
    InvalidPageIdError,
    InvalidPageSizeError,
    MvrsDbError,
    NotADatabaseError,
    PageIdMismatchError,
    StorageIOError,
    UnsupportedVersionError,
)
from mvrs_db.storage.disk_manager import DiskManager, FileHeader
from mvrs_db.storage.page import PAGE_HEADER_SIZE, PageType, new_page, read_header

PAGE_SIZE = config.DEFAULT_PAGE_SIZE


# ---------------------------------------------------------------------- helpers


def make_db(path: Path, pages: int = 0, page_size: int = PAGE_SIZE) -> Path:
    """Create a database with `pages` data pages (all FREE) and close it."""
    with DiskManager.create(path, page_size) as dm:
        for _ in range(pages):
            dm.extend()
    return path


def raw_header(
    *,
    magic: bytes = config.MAGIC,
    version: int = config.FILE_FORMAT_VERSION,
    reserved_a: int = 0,
    page_size: int = PAGE_SIZE,
    page_count: int = 1,
    free_list_head: int = 0,
    catalog_root: int = 0,
    reserved_b: int = 0,
    reserved_c: bytes = bytes(12),
) -> bytes:
    """Build a 64-byte header with a *valid* CRC, so only the named field is wrong."""
    body = struct.pack(
        "<8sHHIIIIIQQ12s",
        magic,
        version,
        reserved_a,
        page_size,
        page_count,
        free_list_head,
        catalog_root,
        reserved_b,
        0,
        1234,
        reserved_c,
    )
    return body + struct.pack("<I", zlib.crc32(body))


def write_file(path: Path, header: bytes, total_size: int = PAGE_SIZE) -> Path:
    path.write_bytes(header + bytes(total_size - len(header)))
    return path


def assert_open_fails_without_modifying(path: Path, error: type[MvrsDbError]) -> None:
    before = path.read_bytes()
    with pytest.raises(error):
        DiskManager.open(path)
    assert path.read_bytes() == before


def filled_page(dm: DiskManager, page_id: int, fill: int) -> bytearray:
    buf = new_page(dm.page_size, page_id, PageType.HEAP)
    buf[PAGE_HEADER_SIZE:] = bytes([fill]) * (dm.page_size - PAGE_HEADER_SIZE)
    return buf


# ---------------------------------------------------------------------- create


def test_create_writes_one_page_with_a_valid_header(tmp_path: Path) -> None:
    path = tmp_path / "new.mvrs"

    with DiskManager.create(path) as dm:
        assert dm.page_size == PAGE_SIZE
        assert dm.page_count == 1
        assert dm.header.free_list_head == 0
        assert dm.header.catalog_root == 0

    raw = path.read_bytes()
    assert len(raw) == PAGE_SIZE
    assert raw[:8] == b"MVRS_DB\x00"
    assert raw[config.FILE_HEADER_SIZE :] == bytes(PAGE_SIZE - config.FILE_HEADER_SIZE)
    assert FileHeader.unpack(raw[: config.FILE_HEADER_SIZE]).page_count == 1


@pytest.mark.parametrize("page_size", config.ALLOWED_PAGE_SIZES)
def test_every_allowed_page_size_round_trips(tmp_path: Path, page_size: int) -> None:
    path = tmp_path / "sized.mvrs"

    with DiskManager.create(path, page_size) as dm:
        page_id = dm.extend()
        dm.write_page(page_id, filled_page(dm, page_id, 0xAB))

    assert path.stat().st_size == 2 * page_size
    with DiskManager.open(path) as dm:
        assert dm.page_size == page_size
        assert dm.read_page(page_id)[PAGE_HEADER_SIZE:] == bytes([0xAB]) * (
            page_size - PAGE_HEADER_SIZE
        )


@pytest.mark.parametrize("page_size", [0, 1, 512, 4095, 4097, 5000, 65536, -4096])
def test_create_rejects_invalid_page_size(tmp_path: Path, page_size: int) -> None:
    path = tmp_path / "bad.mvrs"

    with pytest.raises(InvalidPageSizeError):
        DiskManager.create(path, page_size)

    assert not path.exists()


def test_create_refuses_to_overwrite_an_existing_database(tmp_path: Path) -> None:
    path = make_db(tmp_path / "existing.mvrs", pages=2)
    before = path.read_bytes()

    with pytest.raises(DatabaseExistsError):
        DiskManager.create(path)

    assert path.read_bytes() == before


def test_create_refuses_to_overwrite_an_unrelated_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("important notes")

    with pytest.raises(DatabaseExistsError):
        DiskManager.create(path)

    assert path.read_text() == "important notes"


def test_create_in_a_missing_directory_is_a_storage_error(tmp_path: Path) -> None:
    with pytest.raises(StorageIOError):
        DiskManager.create(tmp_path / "no_such_dir" / "db.mvrs")


def test_each_database_gets_its_own_id(tmp_path: Path) -> None:
    with DiskManager.create(tmp_path / "a.mvrs") as a, DiskManager.create(tmp_path / "b.mvrs") as b:
        assert a.header.db_id != b.header.db_id


# ---------------------------------------------------------------------- open


def test_reopen_sees_the_same_header(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm.extend()
        dm.extend()
        created = dm.header

    with DiskManager.open(path) as dm:
        assert dm.header == created
        assert dm.page_count == 3


def test_open_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.mvrs"

    with pytest.raises(DatabaseNotFoundError):
        DiskManager.open(path)

    assert not path.exists()


def test_open_a_directory_is_a_storage_error(tmp_path: Path) -> None:
    with pytest.raises(StorageIOError):
        DiskManager.open(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"K",
        b"MVRS_DB",  # one byte short of the magic
        b"SQLite format 3\x00" + bytes(4080),
        b"just a text file\r\nwith two lines\r\n",
        bytes(PAGE_SIZE),
        b"\xff" * PAGE_SIZE,
    ],
    ids=["empty", "one-byte", "partial-magic", "sqlite", "text", "zeros", "ones"],
)
def test_open_refuses_files_that_are_not_databases(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "other.bin"
    path.write_bytes(content)

    assert_open_fails_without_modifying(path, NotADatabaseError)


def test_open_refuses_a_header_cut_short_after_the_magic(tmp_path: Path) -> None:
    path = tmp_path / "short.mvrs"
    path.write_bytes(raw_header()[:40])

    assert_open_fails_without_modifying(path, CorruptHeaderError)


@pytest.mark.parametrize("offset", range(config.FILE_HEADER_SIZE))
def test_flipping_any_header_byte_is_detected(tmp_path: Path, offset: int) -> None:
    path = make_db(tmp_path / "db.mvrs", pages=1)
    raw = bytearray(path.read_bytes())
    raw[offset] ^= 0x01
    path.write_bytes(raw)

    expected = NotADatabaseError if offset < len(config.MAGIC) else CorruptHeaderError
    assert_open_fails_without_modifying(path, expected)


@pytest.mark.parametrize("version", [0, 2, 65535])
def test_open_refuses_unknown_format_versions(tmp_path: Path, version: int) -> None:
    path = write_file(tmp_path / "future.mvrs", raw_header(version=version))

    assert_open_fails_without_modifying(path, UnsupportedVersionError)


@pytest.mark.parametrize("page_size", [0, 512, 4095, 65536])
def test_open_refuses_invalid_page_size_even_with_a_valid_crc(
    tmp_path: Path, page_size: int
) -> None:
    path = write_file(tmp_path / "bad.mvrs", raw_header(page_size=page_size))

    assert_open_fails_without_modifying(path, CorruptHeaderError)


@pytest.mark.parametrize(
    "fields",
    [
        {"page_count": 0},
        {"free_list_head": 1},  # only page 0 exists
        {"catalog_root": 5},
        {"reserved_a": 1},
        {"reserved_b": 1},
        {"reserved_c": b"\x00" * 11 + b"\x01"},
    ],
    ids=["no-pages", "free-list-oob", "catalog-oob", "reserved-a", "reserved-b", "reserved-c"],
)
def test_open_refuses_inconsistent_headers_with_a_valid_crc(
    tmp_path: Path, fields: dict[str, int]
) -> None:
    path = write_file(tmp_path / "bad.mvrs", raw_header(**fields))  # type: ignore[arg-type]

    assert_open_fails_without_modifying(path, CorruptHeaderError)


def test_open_refuses_a_truncated_file(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.mvrs", pages=3)
    raw = path.read_bytes()

    path.write_bytes(raw[: len(raw) - PAGE_SIZE // 2])  # half a page missing
    assert_open_fails_without_modifying(path, CorruptFileError)

    path.write_bytes(raw[: len(raw) - PAGE_SIZE])  # a whole page missing
    assert_open_fails_without_modifying(path, CorruptFileError)


def test_open_refuses_a_file_with_extra_bytes(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.mvrs", pages=1)
    path.write_bytes(path.read_bytes() + b"x")

    assert_open_fails_without_modifying(path, CorruptFileError)


def test_opening_a_valid_database_does_not_modify_it(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.mvrs", pages=2)
    before = path.read_bytes()

    DiskManager.open(path).close()

    assert path.read_bytes() == before


# ---------------------------------------------------------------------- page I/O


def test_extend_appends_valid_free_pages(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"

    with DiskManager.create(path) as dm:
        assert [dm.extend(), dm.extend(), dm.extend()] == [1, 2, 3]
        assert dm.page_count == 4
        for page_id in (1, 2, 3):
            header = read_header(dm.read_page(page_id))
            assert header.page_id == page_id
            assert header.page_type == PageType.FREE

    assert path.stat().st_size == 4 * PAGE_SIZE


def test_written_pages_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        for fill in (0x11, 0x22, 0x33):
            page_id = dm.extend()
            dm.write_page(page_id, filled_page(dm, page_id, fill))

    with DiskManager.open(path) as dm:
        for page_id, fill in ((1, 0x11), (2, 0x22), (3, 0x33)):
            page = dm.read_page(page_id)
            assert page[PAGE_HEADER_SIZE:] == bytes([fill]) * (PAGE_SIZE - PAGE_HEADER_SIZE)


def test_pages_are_written_at_page_id_times_page_size(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm.extend()
        dm.extend()
        dm.write_page(2, filled_page(dm, 2, 0x5A))

    raw = path.read_bytes()
    assert raw[2 * PAGE_SIZE + PAGE_HEADER_SIZE : 3 * PAGE_SIZE] == b"\x5a" * (
        PAGE_SIZE - PAGE_HEADER_SIZE
    )


def test_newline_bytes_are_stored_unchanged(tmp_path: Path) -> None:
    """Guards against Windows text-mode translation of \\n to \\r\\n."""
    path = tmp_path / "db.mvrs"
    body = (b"\n\r\n\x1a" * PAGE_SIZE)[: PAGE_SIZE - PAGE_HEADER_SIZE]

    with DiskManager.create(path) as dm:
        page_id = dm.extend()
        buf = new_page(PAGE_SIZE, page_id, PageType.HEAP)
        buf[PAGE_HEADER_SIZE:] = body
        dm.write_page(page_id, buf)

    assert path.stat().st_size == 2 * PAGE_SIZE
    with DiskManager.open(path) as dm:
        assert dm.read_page(page_id)[PAGE_HEADER_SIZE:] == body


def test_write_page_does_not_modify_the_callers_buffer(tmp_path: Path) -> None:
    with DiskManager.create(tmp_path / "db.mvrs") as dm:
        page_id = dm.extend()
        buf = filled_page(dm, page_id, 0x77)
        before = bytes(buf)

        dm.write_page(page_id, buf)

        assert bytes(buf) == before


def test_read_page_returns_an_independent_copy(tmp_path: Path) -> None:
    with DiskManager.create(tmp_path / "db.mvrs") as dm:
        page_id = dm.extend()
        dm.write_page(page_id, filled_page(dm, page_id, 0x01))

        first = dm.read_page(page_id)
        first[100] = 0xFF

        assert dm.read_page(page_id)[100] == 0x01


@pytest.mark.parametrize("page_id", [0, -1, 3, 4, 2**32, 2**64])
def test_out_of_range_page_ids_are_rejected(tmp_path: Path, page_id: int) -> None:
    with DiskManager.create(tmp_path / "db.mvrs") as dm:
        dm.extend()
        dm.extend()  # valid IDs are 1 and 2

        with pytest.raises(InvalidPageIdError):
            dm.read_page(page_id)
        with pytest.raises(InvalidPageIdError):
            dm.write_page(page_id, new_page(PAGE_SIZE, 1, PageType.HEAP))


@pytest.mark.parametrize("page_id", [1.0, "1", None, True])
def test_non_integer_page_ids_are_rejected(tmp_path: Path, page_id: object) -> None:
    with DiskManager.create(tmp_path / "db.mvrs") as dm:
        dm.extend()

        with pytest.raises(InvalidPageIdError):
            dm.read_page(page_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [0, 1, PAGE_SIZE - 1, PAGE_SIZE + 1, 2 * PAGE_SIZE])
def test_write_rejects_buffers_that_are_not_one_page(tmp_path: Path, length: int) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm.extend()
        with pytest.raises(InvalidPageBufferError):
            dm.write_page(1, bytes(length))

    assert path.stat().st_size == 2 * PAGE_SIZE


def test_write_refuses_a_page_whose_header_names_another_page(tmp_path: Path) -> None:
    with DiskManager.create(tmp_path / "db.mvrs") as dm:
        dm.extend()
        dm.extend()
        dm.write_page(2, filled_page(dm, 2, 0x22))

        with pytest.raises(PageIdMismatchError):
            dm.write_page(2, filled_page(dm, 1, 0x99))

        assert dm.read_page(2)[PAGE_HEADER_SIZE] == 0x22


def test_write_refuses_a_page_with_an_invalid_type(tmp_path: Path) -> None:
    with DiskManager.create(tmp_path / "db.mvrs") as dm:
        dm.extend()
        buf = new_page(PAGE_SIZE, 1, PageType.HEAP)
        buf[16] = 0

        with pytest.raises(CorruptPageError):
            dm.write_page(1, buf)


# ---------------------------------------------------------------------- corruption on disk


def test_damaged_page_on_disk_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm.extend()
        dm.extend()
        dm.write_page(1, filled_page(dm, 1, 0x11))
        dm.write_page(2, filled_page(dm, 2, 0x22))

    raw = bytearray(path.read_bytes())
    raw[PAGE_SIZE + 1000] ^= 0x40
    path.write_bytes(raw)

    with DiskManager.open(path) as dm:
        with pytest.raises(ChecksumMismatchError):
            dm.read_page(1)
        # The damage is confined to page 1.
        assert dm.read_page(2)[PAGE_HEADER_SIZE] == 0x22


def test_zeroed_page_on_disk_is_detected(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.mvrs", pages=2)
    raw = bytearray(path.read_bytes())
    raw[PAGE_SIZE : 2 * PAGE_SIZE] = bytes(PAGE_SIZE)
    path.write_bytes(raw)

    with DiskManager.open(path) as dm, pytest.raises(ChecksumMismatchError):
        dm.read_page(1)


def test_torn_page_on_disk_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm.extend()
        dm.write_page(1, filled_page(dm, 1, 0xAA))
    old = path.read_bytes()
    with DiskManager.open(path) as dm:
        dm.write_page(1, filled_page(dm, 1, 0xBB))
    new = path.read_bytes()

    half = PAGE_SIZE + PAGE_SIZE // 2
    path.write_bytes(new[:half] + old[half:])  # first half new, second half old

    with DiskManager.open(path) as dm, pytest.raises(ChecksumMismatchError):
        dm.read_page(1)


def test_page_stored_at_the_wrong_offset_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm.extend()
        dm.extend()
        dm.write_page(1, filled_page(dm, 1, 0x11))
    raw = bytearray(path.read_bytes())
    raw[2 * PAGE_SIZE : 3 * PAGE_SIZE] = raw[PAGE_SIZE : 2 * PAGE_SIZE]  # page 1 copied over page 2
    path.write_bytes(raw)

    with DiskManager.open(path) as dm, pytest.raises(PageIdMismatchError):
        dm.read_page(2)


@settings(max_examples=60, deadline=None)
@given(offset=st.integers(0, PAGE_SIZE - 1), mask=st.integers(1, 255))
def test_any_single_byte_damage_on_disk_is_detected(
    tmp_path_factory: pytest.TempPathFactory, offset: int, mask: int
) -> None:
    # The database is created inside the test body: Hypothesis runs this many
    # times, and a function-scoped fixture would be shared between runs.
    path = tmp_path_factory.mktemp("damage") / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm.extend()
        dm.write_page(1, filled_page(dm, 1, 0x3C))
    raw = bytearray(path.read_bytes())
    raw[PAGE_SIZE + offset] ^= mask
    path.write_bytes(raw)

    with DiskManager.open(path) as dm, pytest.raises(ChecksumMismatchError):
        dm.read_page(1)


@settings(max_examples=40, deadline=None)
@given(body=st.binary(min_size=PAGE_SIZE - PAGE_HEADER_SIZE, max_size=PAGE_SIZE - PAGE_HEADER_SIZE))
def test_any_page_body_round_trips_through_reopen(
    tmp_path_factory: pytest.TempPathFactory, body: bytes
) -> None:
    path = tmp_path_factory.mktemp("roundtrip") / "db.mvrs"
    with DiskManager.create(path) as dm:
        page_id = dm.extend()
        buf = new_page(PAGE_SIZE, page_id, PageType.HEAP)
        buf[PAGE_HEADER_SIZE:] = body
        dm.write_page(page_id, buf)

    with DiskManager.open(path) as dm:
        assert bytes(dm.read_page(page_id)[PAGE_HEADER_SIZE:]) == body


# ---------------------------------------------------------------------- lifecycle and I/O errors


def test_use_after_close_raises(tmp_path: Path) -> None:
    dm = DiskManager.create(tmp_path / "db.mvrs")
    dm.extend()
    dm.close()

    assert dm.is_closed
    with pytest.raises(DatabaseClosedError):
        dm.read_page(1)
    with pytest.raises(DatabaseClosedError):
        dm.write_page(1, new_page(PAGE_SIZE, 1, PageType.HEAP))
    with pytest.raises(DatabaseClosedError):
        dm.extend()
    with pytest.raises(DatabaseClosedError):
        dm.sync()


def test_close_twice_is_harmless(tmp_path: Path) -> None:
    dm = DiskManager.create(tmp_path / "db.mvrs")
    dm.close()
    dm.close()


def test_file_can_be_deleted_after_close(tmp_path: Path) -> None:
    """On Windows an open handle blocks deletion, so this proves close() released it."""
    path = tmp_path / "db.mvrs"
    DiskManager.create(path).close()

    path.unlink()

    assert not path.exists()


def test_failed_open_releases_the_file_handle(tmp_path: Path) -> None:
    path = tmp_path / "other.bin"
    path.write_bytes(b"not a database")
    with pytest.raises(NotADatabaseError):
        DiskManager.open(path)

    path.unlink()

    assert not path.exists()


def test_sync_succeeds_on_an_open_database(tmp_path: Path) -> None:
    with DiskManager.create(tmp_path / "db.mvrs") as dm:
        dm.extend()
        dm.sync()


def test_extend_refuses_to_grow_past_the_page_id_limit(tmp_path: Path) -> None:
    path = tmp_path / "db.mvrs"
    with DiskManager.create(path) as dm:
        dm._header = replace(dm.header, page_count=config.MAX_PAGE_ID + 1)  # pretend it is full

        with pytest.raises(DatabaseFullError):
            dm.extend()

    assert path.stat().st_size == PAGE_SIZE


def test_failed_header_update_during_extend_leaves_a_consistent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "db.mvrs"
    dm = DiskManager.create(path)
    dm.extend()

    def failing_header_write(header: FileHeader) -> None:
        raise StorageIOError("simulated disk failure")

    monkeypatch.setattr(dm, "_write_header", failing_header_write)
    with pytest.raises(StorageIOError):
        dm.extend()
    assert dm.page_count == 2
    dm.close()

    with DiskManager.open(path) as reopened:
        assert reopened.page_count == 2
        assert read_header(reopened.read_page(1)).page_type == PageType.FREE
