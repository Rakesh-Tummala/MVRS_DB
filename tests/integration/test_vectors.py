"""Phase 8, part 1: the VECTOR(n) column type."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import mvrs_db
from mvrs_db.catalog.schema import Column, TableSchema
from mvrs_db.cli import format_result
from mvrs_db.errors import (
    CatalogError,
    ConstraintViolationError,
    CorruptCatalogError,
    CorruptRecordError,
    InvalidValueError,
    RecordTooLargeError,
    SqlSyntaxError,
    TypeMismatchError,
)
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse
from mvrs_db.storage.record import ColumnType, decode_record, encode_record

V = ColumnType.VECTOR


def f32(value: float) -> float:
    """The value a number has after being stored as a 32-bit float."""
    result: float = struct.unpack("<f", struct.pack("<f", value))[0]
    return result


def as_stored(*values: float) -> tuple[float, ...]:
    return tuple(f32(v) for v in values)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "vec.mvrs", synchronous=False)
    database.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT, embedding VECTOR(3))")
    yield database
    database.close()


# ---------------------------------------------------------------------- record format


def test_documented_layout() -> None:
    data = encode_record([V], [(1.0, -2.5, 0.0)])

    assert data[:2] == b"\x01\x00"  # format byte, empty null bitmap
    assert data[2:4] == struct.pack("<H", 3)  # element count
    assert data[4:] == struct.pack("<3f", 1.0, -2.5, 0.0)  # 4 bytes per element
    assert len(data) == 2 + 2 + 3 * 4


FLOATS32 = st.floats(width=32, allow_nan=False, allow_infinity=False)


@given(st.lists(FLOATS32, min_size=1, max_size=64))
def test_values_exact_in_32_bits_round_trip_exactly(values: list[float]) -> None:
    vector = tuple(values)
    (decoded,) = decode_record([V], encode_record([V], [vector]))
    assert struct.pack(f"<{len(vector)}f", *decoded) == struct.pack(f"<{len(vector)}f", *vector)


def test_values_are_rounded_to_32_bit_precision() -> None:
    (decoded,) = decode_record([V], encode_record([V], [(0.1, 1 / 3)]))

    assert decoded == as_stored(0.1, 1 / 3)
    assert decoded != (0.1, 1 / 3)  # the documented loss of precision
    assert decoded[0] == pytest.approx(0.1, rel=1e-7)


def test_null_vector_and_mixed_columns() -> None:
    types = [ColumnType.INTEGER, V, ColumnType.TEXT, V]
    row = (7, None, "x", (1.5, 2.5))

    assert decode_record(types, encode_record(types, row)) == row


@pytest.mark.parametrize(
    "value",
    [
        (),
        [1.0, 2.0],
        (1, 2),
        (1.0, "2"),
        (1.0, None),
        (math.nan,),
        (math.inf,),
        (-math.inf, 0.0),
        (1e39,),  # finite as a double, too large for 32 bits
        "text",
        5,
        1.5,
    ],
)
def test_invalid_vectors_are_rejected(value: object) -> None:
    with pytest.raises(InvalidValueError):
        encode_record([V], [value])  # type: ignore[list-item]


def test_vector_longer_than_the_maximum_dimension() -> None:
    with pytest.raises(RecordTooLargeError):
        encode_record([V], [(0.0,) * 4097])


GOOD = encode_record([V], [(1.0, 2.0)])


@pytest.mark.parametrize(
    "data",
    [
        GOOD[:3],  # element count cut short
        GOOD[:-1],  # last element cut short
        GOOD + b"\x00",  # trailing byte
        GOOD[:2] + struct.pack("<H", 0),  # zero elements
        GOOD[:2] + struct.pack("<H", 9) + GOOD[4:],  # count larger than the data
        GOOD[:4] + struct.pack("<2f", math.nan, 1.0),
        GOOD[:4] + struct.pack("<2f", 1.0, math.inf),
    ],
    ids=["short-count", "short-element", "trailing", "empty", "overrun", "nan", "inf"],
)
def test_malformed_vector_records_are_rejected(data: bytes) -> None:
    with pytest.raises(CorruptRecordError):
        decode_record([V], data)


# ---------------------------------------------------------------------- parsing


def test_vector_type_and_literal_parse() -> None:
    created = parse("CREATE TABLE t (id INT PRIMARY KEY, e VECTOR(4) NOT NULL)")
    assert isinstance(created, ast.CreateTable)
    assert created.columns[1] == ast.ColumnDef("e", "VECTOR", not_null=True, dimension=4)

    inserted = parse("INSERT INTO t VALUES (1, [1, -2.5, +3e2, .5]), (2, NULL)")
    assert isinstance(inserted, ast.Insert)
    assert inserted.rows == ((1, (1.0, -2.5, 300.0, 0.5)), (2, None))
    assert all(type(x) is float for x in inserted.rows[0][1])  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("CREATE TABLE t (e VECTOR)", "expected '\\('"),
        ("CREATE TABLE t (e VECTOR())", "number of dimensions"),
        ("CREATE TABLE t (e VECTOR(x))", "number of dimensions"),
        ("CREATE TABLE t (e VECTOR(3.5))", "number of dimensions"),
        ("CREATE TABLE t (e VECTOR(3)", "expected"),
        ("INSERT INTO t VALUES ([])", "expected a value"),
        ("INSERT INTO t VALUES ([1, 2)", "expected '\\]'"),
        ("INSERT INTO t VALUES ([1, 'a'])", "only numbers"),
        ("INSERT INTO t VALUES ([1, NULL])", "only numbers"),
        ("INSERT INTO t VALUES ([1, [2]])", "only numbers"),
        ("INSERT INTO t VALUES ([1,])", "expected a value"),
        ("CREATE TABLE vector (id INT PRIMARY KEY)", "reserved word"),
    ],
)
def test_vector_syntax_errors(statement: str, message: str) -> None:
    with pytest.raises(SqlSyntaxError, match=message):
        parse(statement)


# ---------------------------------------------------------------------- through SQL


def test_insert_and_select(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO docs VALUES (1, 'a', [0.1, 0.2, 0.3]), (2, 'b', [1, 0, -1])")
    db.execute("INSERT INTO docs (id, title) VALUES (3, 'no embedding')")

    result = db.execute("SELECT id, embedding FROM docs ORDER BY id")

    assert result.rows == [
        (1, as_stored(0.1, 0.2, 0.3)),
        (2, (1.0, 0.0, -1.0)),
        (3, None),
    ]
    assert db.execute("SELECT id FROM docs WHERE embedding IS NULL").rows == [(3,)]
    assert db.execute("SELECT id FROM docs WHERE embedding IS NOT NULL ORDER BY id").rows == [
        (1,),
        (2,),
    ]
    db.check()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ("[1, 2]", "VECTOR\\(3\\) and cannot hold a vector of 2 numbers"),
        ("[1, 2, 3, 4]", "a vector of 4 numbers"),
        ("'text'", "is VECTOR\\(3\\) and cannot hold 'text'"),
        ("5", "is VECTOR\\(3\\) and cannot hold 5"),
    ],
)
def test_wrong_dimension_or_type_is_rejected(
    db: mvrs_db.Database, values: str, message: str
) -> None:
    with pytest.raises(TypeMismatchError, match=message):
        db.execute(f"INSERT INTO docs VALUES (1, 'a', {values})")
    assert db.execute("SELECT * FROM docs").rows == []


def test_vector_cannot_go_into_another_column(db: mvrs_db.Database) -> None:
    with pytest.raises(TypeMismatchError, match="is TEXT and cannot hold \\[1.0, 2.0\\]"):
        db.execute("INSERT INTO docs VALUES (1, [1, 2], NULL)")
    with pytest.raises(TypeMismatchError, match="is INTEGER"):
        db.execute("INSERT INTO docs VALUES ([1], 'a', NULL)")


def test_element_too_large_for_32_bits_is_rejected(db: mvrs_db.Database) -> None:
    with pytest.raises(InvalidValueError, match="32-bit"):
        db.execute("INSERT INTO docs VALUES (1, 'a', [1e39, 0, 0])")
    assert db.execute("SELECT * FROM docs").rows == []


def test_update_and_delete(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO docs VALUES (1, 'a', [1, 2, 3]), (2, 'b', [4, 5, 6])")

    db.execute("UPDATE docs SET embedding = [9, 9, 9] WHERE id = 1")
    db.execute("UPDATE docs SET embedding = NULL WHERE id = 2")
    with pytest.raises(TypeMismatchError):
        db.execute("UPDATE docs SET embedding = [1, 2] WHERE id = 1")

    assert db.execute("SELECT embedding FROM docs ORDER BY id").rows == [
        ((9.0, 9.0, 9.0),),
        (None,),
    ]
    assert db.execute("DELETE FROM docs WHERE id = 1").rowcount == 1
    db.check()


def test_not_null_vector_column(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "v.mvrs") as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, e VECTOR(2) NOT NULL)")
        with pytest.raises(ConstraintViolationError, match="NOT NULL"):
            db.execute("INSERT INTO t VALUES (1, NULL)")
        db.execute("INSERT INTO t VALUES (1, [0, 0])")


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM docs WHERE embedding = [1, 2, 3]",
        "SELECT * FROM docs WHERE embedding < [1, 2, 3]",
        "SELECT * FROM docs WHERE embedding != embedding",
        "SELECT * FROM docs WHERE [1, 2] = [1, 2]",
        "SELECT * FROM docs WHERE id = [1]",
        "SELECT * FROM docs WHERE embedding = NULL",
        "DELETE FROM docs WHERE embedding > [0, 0, 0]",
        "SELECT * FROM docs ORDER BY embedding",
        "SELECT * FROM docs ORDER BY id, embedding DESC LIMIT 2",
    ],
)
def test_vectors_cannot_be_compared_or_ordered(db: mvrs_db.Database, statement: str) -> None:
    db.execute("INSERT INTO docs VALUES (1, 'a', [1, 2, 3])")

    with pytest.raises(TypeMismatchError):
        db.execute(statement)


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ("e VECTOR(3) PRIMARY KEY", "cannot be PRIMARY KEY or UNIQUE"),
        ("id INTEGER PRIMARY KEY, e VECTOR(3) UNIQUE", "cannot be PRIMARY KEY or UNIQUE"),
        ("id INTEGER PRIMARY KEY, e VECTOR(0)", "between 1 and 4096"),
        ("id INTEGER PRIMARY KEY, e VECTOR(4097)", "between 1 and 4096"),
    ],
)
def test_vector_column_rules(db: mvrs_db.Database, definition: str, message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        db.execute(f"CREATE TABLE bad ({definition})")
    assert db.table_names() == ["docs"]


def test_vector_must_fit_in_a_page(tmp_path: Path) -> None:
    big = "[" + ", ".join(["0.5"] * 1536) + "]"  # 6 KiB of floats

    with mvrs_db.connect(tmp_path / "small.mvrs") as db:  # 4 KiB pages
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, e VECTOR(1536))")
        with pytest.raises(RecordTooLargeError):
            db.execute(f"INSERT INTO t VALUES (1, {big})")

    with mvrs_db.connect(tmp_path / "large.mvrs", page_size=8192) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, e VECTOR(1536))")
        db.execute(f"INSERT INTO t VALUES (1, {big})")
        assert db.execute("SELECT e FROM t").rows == [((0.5,) * 1536,)]
        db.check()


def test_vectors_survive_reopen_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "v.mvrs"
    with mvrs_db.connect(path) as db:
        db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, e VECTOR(768))")
        db.execute("BEGIN")
        for i in range(40):  # each row fills most of a page, so this spans many pages
            vector = "[" + ", ".join(str((i + j) % 7 - 3) for j in range(768)) + "]"
            db.execute(f"INSERT INTO docs VALUES ({i}, {vector})")
        db.execute("COMMIT")
        db.execute("BEGIN")
        db.execute("DELETE FROM docs WHERE id < 20")
        db.execute("ROLLBACK")

    with mvrs_db.connect(path) as db:
        rows = db.execute("SELECT * FROM docs ORDER BY id").rows
        assert len(rows) == 40
        for i, (row_id, vector) in enumerate(rows):
            assert row_id == i
            assert vector == tuple(float((i + j) % 7 - 3) for j in range(768))
        assert db.check().tables == {"docs": 40}
        # The dimension came back from the catalog, not from the CREATE statement.
        with pytest.raises(TypeMismatchError, match="VECTOR\\(768\\)"):
            db.execute("INSERT INTO docs VALUES (99, [1, 2, 3])")


# ---------------------------------------------------------------------- catalog and display


def test_catalog_record_stores_the_dimension_only_for_vectors() -> None:
    plain = TableSchema(1, "t", 2, (Column("id", ColumnType.INTEGER, primary_key=True),))
    with_vector = TableSchema(
        1, "t", 2, (*plain.columns, Column("e", ColumnType.VECTOR, dimension=384))
    )

    assert TableSchema.decode(with_vector.encode()) == with_vector
    assert TableSchema.decode(plain.encode()) == plain
    # name length + name + type, flags, index root + the two-byte dimension
    assert len(with_vector.encode()) == len(plain.encode()) + 1 + 1 + 6 + 2


def test_catalog_rejects_an_impossible_dimension() -> None:
    schema = TableSchema(1, "t", 2, (Column("e", ColumnType.VECTOR, dimension=5),))
    data = bytearray(schema.encode())
    data[-2:] = struct.pack("<H", 0)

    with pytest.raises(CorruptCatalogError):
        TableSchema.decode(bytes(data))
    with pytest.raises(CorruptCatalogError):
        TableSchema.decode(schema.encode()[:-1])  # dimension cut short


def test_explain_and_cli_show_vectors_as_written(db: mvrs_db.Database) -> None:
    db.execute("INSERT INTO docs VALUES (1, 'a', [0.5, -2, 3])")

    plan = db.execute("EXPLAIN UPDATE docs SET embedding = [1, 2, 3] WHERE id = 1").rows
    table = format_result(db.execute("SELECT id, embedding FROM docs"))

    assert plan[0][0] == "Update(docs: embedding = [1.0, 2.0, 3.0])"
    assert "1  | [0.5, -2, 3]" in table
