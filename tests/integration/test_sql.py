"""SQL end to end through the public API: types, NULLs, constraints, persistence."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import (
    CatalogError,
    ColumnNotFoundError,
    ConstraintViolationError,
    DatabaseClosedError,
    DatabaseLockedError,
    DatabaseNotFoundError,
    KeyTooLargeError,
    NotADatabaseError,
    RecordTooLargeError,
    SqlError,
    TableExistsError,
    TableNotFoundError,
    TypeMismatchError,
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "test.mvrs", synchronous=False)
    yield database
    database.close()


@pytest.fixture
def people(db: mvrs_db.Database) -> mvrs_db.Database:
    db.execute(
        "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "age INTEGER, score REAL, email TEXT UNIQUE)"
    )
    db.execute(
        "INSERT INTO people VALUES "
        "(1, 'asha', 30, 9.5, 'asha@x.io'), "
        "(2, 'ravi', NULL, 7.0, NULL), "
        "(3, 'mei', 25, NULL, 'mei@x.io'), "
        "(4, 'omar', 41, 3.25, NULL), "
        "(5, 'zoe', 25, 8.0, 'zoe@x.io')"
    )
    return db


def ids(db: mvrs_db.Database, where: str = "", tail: str = "ORDER BY id") -> list[int]:
    clause = f"WHERE {where}" if where else ""
    return [row[0] for row in db.execute(f"SELECT id FROM people {clause} {tail}").rows]


# ---------------------------------------------------------------------- basics


def test_select_star_and_projection(people: mvrs_db.Database) -> None:
    result = people.execute("SELECT * FROM people WHERE id = 1")
    assert result.columns == ("id", "name", "age", "score", "email")
    assert result.rows == [(1, "asha", 30, 9.5, "asha@x.io")]
    assert result.rowcount == 1

    result = people.execute("SELECT email, id, id FROM people WHERE id = 3")
    assert result.columns == ("email", "id", "id")
    assert result.rows == [("mei@x.io", 3, 3)]


def test_insert_with_column_list_fills_the_rest_with_null(people: mvrs_db.Database) -> None:
    assert people.execute("INSERT INTO people (name, id) VALUES ('kim', 6)").rowcount == 1

    assert people.execute("SELECT * FROM people WHERE id = 6").rows == [
        (6, "kim", None, None, None)
    ]


def test_identifiers_and_keywords_are_case_insensitive(people: mvrs_db.Database) -> None:
    assert people.execute("select NAME from PEOPLE where ID = 1").rows == [("asha",)]


def test_text_values_are_stored_exactly(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    samples = ["", "it's", "line\nbreak\ttab", "emoji \U0001f600 ünïcode", "  spaces  ", "--x", ";"]
    for i, text in enumerate(samples):
        quoted = text.replace("'", "''")
        db.execute(f"INSERT INTO notes VALUES ({i}, '{quoted}')")

    assert [r[0] for r in db.execute("SELECT body FROM notes ORDER BY id").rows] == samples
    assert db.execute("SELECT id FROM notes WHERE body = 'it''s'").rows == [(1,)]


# ---------------------------------------------------------------------- WHERE


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ("age = 25", [3, 5]),
        ("age != 25", [1, 4]),  # the NULL age is neither equal nor unequal
        ("age <> 25", [1, 4]),
        ("age < 30", [3, 5]),
        ("age <= 30", [1, 3, 5]),
        ("age > 30", [4]),
        ("age >= 30", [1, 4]),
        ("30 < age", [4]),
        ("name = 'mei'", [3]),
        ("name > 'omar'", [2, 5]),
        ("score >= 7", [1, 2, 5]),  # integer literal against a REAL column
        ("age < 25.5", [3, 5]),  # real literal against an INTEGER column
        ("id = age", []),
        ("id < age", [1, 3, 4, 5]),
        ("age = 25 AND score > 5", [5]),
        ("age = 25 OR score > 9", [1, 3, 5]),
        ("NOT age = 25", [1, 4]),
        ("NOT (age = 25 OR age = 30)", [4]),
        ("age = 25 OR age = 30 AND score > 100", [3, 5]),
        ("(age = 25 OR age = 30) AND score > 8.5", [1]),
        ("1 = 1", [1, 2, 3, 4, 5]),
        ("1 = 2", []),
        ("'a' < 'b'", [1, 2, 3, 4, 5]),
    ],
)
def test_where(people: mvrs_db.Database, where: str, expected: list[int]) -> None:
    assert ids(people, where) == expected


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ("age IS NULL", [2]),
        ("age IS NOT NULL", [1, 3, 4, 5]),
        ("age = NULL", []),  # always UNKNOWN, never true
        ("age != NULL", []),
        ("NOT age = NULL", []),  # NOT UNKNOWN is still UNKNOWN
        ("NULL = NULL", []),
        ("NULL IS NULL", [1, 2, 3, 4, 5]),
        ("1 IS NULL", []),
        ("age > 20 OR id = 2", [1, 2, 3, 4, 5]),  # TRUE OR UNKNOWN is TRUE
        ("age > 20 AND id = 2", []),  # UNKNOWN AND TRUE is UNKNOWN
        ("age > 100 AND id = 2", []),  # UNKNOWN AND TRUE again; no row
        ("NOT (age > 100 AND id = 2)", [1, 3, 4, 5]),  # row 2 is NOT UNKNOWN = UNKNOWN
        ("NOT (age > 100 OR id = 2)", [1, 3, 4, 5]),
        ("age IS NULL OR age < 30", [2, 3, 5]),
        ("score IS NULL AND email IS NOT NULL", [3]),
    ],
)
def test_three_valued_logic(people: mvrs_db.Database, where: str, expected: list[int]) -> None:
    assert ids(people, where) == expected


# ---------------------------------------------------------------------- ORDER BY / LIMIT


def test_order_by(people: mvrs_db.Database) -> None:
    assert ids(people, tail="ORDER BY name") == [1, 3, 4, 2, 5]
    assert ids(people, tail="ORDER BY name DESC") == [5, 2, 4, 3, 1]
    assert ids(people, tail="ORDER BY id DESC") == [5, 4, 3, 2, 1]


def test_order_by_puts_nulls_first_ascending_and_last_descending(
    people: mvrs_db.Database,
) -> None:
    assert ids(people, tail="ORDER BY age, id") == [2, 3, 5, 1, 4]
    assert ids(people, tail="ORDER BY age DESC, id") == [4, 1, 3, 5, 2]
    assert ids(people, tail="ORDER BY score DESC") == [1, 5, 2, 4, 3]


def test_order_by_several_keys_with_mixed_directions(people: mvrs_db.Database) -> None:
    assert ids(people, tail="ORDER BY age ASC, name DESC") == [2, 5, 3, 1, 4]
    assert ids(people, tail="ORDER BY age DESC, name ASC") == [4, 1, 3, 5, 2]


def test_limit(people: mvrs_db.Database) -> None:
    assert ids(people, tail="ORDER BY id LIMIT 2") == [1, 2]
    assert ids(people, tail="ORDER BY id DESC LIMIT 2") == [5, 4]
    assert ids(people, tail="ORDER BY age DESC, name LIMIT 3") == [4, 1, 3]
    assert ids(people, tail="ORDER BY id LIMIT 0") == []
    assert ids(people, tail="ORDER BY id LIMIT 100") == [1, 2, 3, 4, 5]
    assert len(ids(people, tail="LIMIT 3")) == 3
    assert ids(people, "age = 25", "ORDER BY name DESC LIMIT 1") == [5]


# ---------------------------------------------------------------------- types


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO people VALUES (6, 'x', 'thirty', 1.0, NULL)",
        "INSERT INTO people VALUES (6, 'x', 3.5, 1.0, NULL)",
        "INSERT INTO people VALUES (6, 7, 1, 1.0, NULL)",
        "INSERT INTO people VALUES (6, 'x', 1, 'high', NULL)",
        "INSERT INTO people VALUES ('six', 'x', 1, 1.0, NULL)",
        "UPDATE people SET age = 'old'",
        "UPDATE people SET name = 5",
        "SELECT * FROM people WHERE name = 5",
        "SELECT * FROM people WHERE age = 'x'",
        "SELECT * FROM people WHERE name > age",
        "SELECT * FROM people WHERE 1 = 'one'",
        "DELETE FROM people WHERE score = 'x'",
        "EXPLAIN SELECT * FROM people WHERE name < 3",
    ],
)
def test_type_mismatches_are_rejected(people: mvrs_db.Database, statement: str) -> None:
    with pytest.raises(TypeMismatchError):
        people.execute(statement)
    assert ids(people) == [1, 2, 3, 4, 5]


def test_type_error_is_raised_even_when_no_row_would_match(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE empty (id INTEGER PRIMARY KEY, name TEXT)")
    with pytest.raises(TypeMismatchError):
        db.execute("SELECT * FROM empty WHERE name = 1")


def test_integer_literal_is_converted_for_a_real_column(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, x REAL)")
    db.execute("INSERT INTO m VALUES (1, 5)")

    (value,) = db.execute("SELECT x FROM m").rows[0]
    assert value == 5.0 and type(value) is float


def test_extreme_numbers_round_trip(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, x REAL)")
    db.execute("INSERT INTO m VALUES (-9223372036854775808, 1.7976931348623157e308)")
    db.execute("INSERT INTO m VALUES (9223372036854775807, -5e-324)")

    assert db.execute("SELECT * FROM m ORDER BY id").rows == [
        (-(2**63), 1.7976931348623157e308),
        (2**63 - 1, -5e-324),
    ]
    assert db.execute("SELECT id FROM m WHERE id = 9223372036854775807").rows == [(2**63 - 1,)]


# ---------------------------------------------------------------------- constraints


def test_primary_key_must_be_unique_and_not_null(people: mvrs_db.Database) -> None:
    with pytest.raises(ConstraintViolationError, match="PRIMARY KEY.*people.id = 3"):
        people.execute("INSERT INTO people VALUES (3, 'dup', 1, 1.0, NULL)")
    with pytest.raises(ConstraintViolationError, match="NOT NULL.*people.id"):
        people.execute("INSERT INTO people VALUES (NULL, 'x', 1, 1.0, NULL)")
    with pytest.raises(ConstraintViolationError, match="NOT NULL.*people.id"):
        people.execute("INSERT INTO people (name) VALUES ('no id')")
    assert ids(people) == [1, 2, 3, 4, 5]


def test_not_null(people: mvrs_db.Database) -> None:
    with pytest.raises(ConstraintViolationError, match="NOT NULL.*people.name"):
        people.execute("INSERT INTO people VALUES (6, NULL, 1, 1.0, NULL)")
    with pytest.raises(ConstraintViolationError, match="NOT NULL.*people.name"):
        people.execute("UPDATE people SET name = NULL WHERE id = 1")
    assert people.execute("UPDATE people SET name = NULL WHERE id = 99").rowcount == 0


def test_unique_allows_many_nulls_but_no_duplicates(people: mvrs_db.Database) -> None:
    people.execute("INSERT INTO people VALUES (6, 'kim', 1, 1.0, NULL)")  # a third NULL email
    with pytest.raises(ConstraintViolationError, match="UNIQUE.*people.email"):
        people.execute("INSERT INTO people VALUES (7, 'dup', 1, 1.0, 'mei@x.io')")
    with pytest.raises(ConstraintViolationError, match="UNIQUE.*people.email"):
        people.execute("UPDATE people SET email = 'asha@x.io' WHERE id = 2")
    assert ids(people, "email IS NULL") == [2, 4, 6]


def test_failed_multi_row_insert_inserts_nothing(people: mvrs_db.Database) -> None:
    with pytest.raises(ConstraintViolationError):
        people.execute(
            "INSERT INTO people VALUES (6, 'a', 1, 1.0, NULL), (7, 'b', 1, 1.0, NULL), "
            "(1, 'dup', 1, 1.0, NULL)"
        )
    assert ids(people) == [1, 2, 3, 4, 5]
    people.check()


def test_duplicate_keys_within_one_insert_are_caught(people: mvrs_db.Database) -> None:
    with pytest.raises(ConstraintViolationError):
        people.execute("INSERT INTO people VALUES (8, 'a', 1, 1.0, NULL), (8, 'b', 1, 1.0, NULL)")
    assert ids(people) == [1, 2, 3, 4, 5]


def test_failed_multi_row_update_changes_nothing(people: mvrs_db.Database) -> None:
    with pytest.raises(ConstraintViolationError):
        people.execute("UPDATE people SET email = 'same@x.io'")  # the second row collides
    assert people.execute("SELECT email FROM people ORDER BY id").rows == [
        ("asha@x.io",),
        (None,),
        ("mei@x.io",),
        (None,),
        ("zoe@x.io",),
    ]
    people.check()


# ---------------------------------------------------------------------- UPDATE / DELETE


def test_update(people: mvrs_db.Database) -> None:
    assert people.execute("UPDATE people SET age = 26, score = 1 WHERE age = 25").rowcount == 2
    assert people.execute("SELECT id, age, score FROM people WHERE age = 26 ORDER BY id").rows == [
        (3, 26, 1.0),
        (5, 26, 1.0),
    ]
    assert people.execute("UPDATE people SET age = NULL").rowcount == 5
    assert ids(people, "age IS NULL") == [1, 2, 3, 4, 5]
    people.check()


def test_update_of_primary_key_and_unique_columns_keeps_indexes_right(
    people: mvrs_db.Database,
) -> None:
    people.execute("UPDATE people SET id = 10 WHERE id = 1")
    people.execute("UPDATE people SET email = 'new@x.io' WHERE id = 3")
    people.execute("UPDATE people SET email = NULL WHERE id = 5")
    people.execute("UPDATE people SET email = 'zoe@x.io' WHERE id = 2")  # the value is free again

    assert ids(people) == [2, 3, 4, 5, 10]
    assert people.execute("SELECT name FROM people WHERE id = 10").rows == [("asha",)]
    assert people.execute("SELECT id FROM people WHERE id = 1").rows == []
    assert people.execute("SELECT id FROM people WHERE email = 'new@x.io'").rows == [(3,)]
    assert people.execute("SELECT id FROM people WHERE email = 'mei@x.io'").rows == []
    assert people.execute("SELECT id FROM people WHERE email = 'zoe@x.io'").rows == [(2,)]
    with pytest.raises(ConstraintViolationError):
        people.execute("UPDATE people SET id = 2 WHERE id = 3")
    people.execute("UPDATE people SET id = 3 WHERE id = 3")  # to itself is fine
    people.check()


def test_update_that_grows_rows_past_their_page(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT, tag TEXT UNIQUE)")
    for i in range(60):
        db.execute(f"INSERT INTO docs VALUES ({i}, 'small', 'tag{i}')")
    big = "x" * 3000

    assert db.execute(f"UPDATE docs SET body = '{big}' WHERE id < 30").rowcount == 30

    assert db.execute("SELECT id FROM docs WHERE body = 'small' ORDER BY id").rows == [
        (i,) for i in range(30, 60)
    ]
    for i in (0, 15, 29):
        assert db.execute(f"SELECT body FROM docs WHERE id = {i}").rows == [(big,)]
        assert db.execute(f"SELECT id FROM docs WHERE tag = 'tag{i}'").rows == [(i,)]
    db.check()


def test_update_touches_each_row_once_even_if_it_moves(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    for i in range(200):
        db.execute(f"INSERT INTO t VALUES ({i}, 'a')")

    assert db.execute(f"UPDATE t SET body = '{'b' * 500}' WHERE body = 'a'").rowcount == 200
    assert db.execute("SELECT id FROM t WHERE body = 'a'").rows == []
    db.check()


def test_delete(people: mvrs_db.Database) -> None:
    assert people.execute("DELETE FROM people WHERE age = 25").rowcount == 2
    assert ids(people) == [1, 2, 4]
    assert people.execute("SELECT id FROM people WHERE id = 3").rows == []
    people.execute("INSERT INTO people VALUES (3, 'back', 1, 1.0, 'mei@x.io')")  # keys are free
    assert people.execute("DELETE FROM people").rowcount == 4
    assert ids(people) == []
    assert people.execute("DELETE FROM people").rowcount == 0
    people.check()


# ---------------------------------------------------------------------- DDL


def test_create_table_rules(db: mvrs_db.Database) -> None:
    with pytest.raises(CatalogError, match="exactly one PRIMARY KEY"):
        db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    with pytest.raises(CatalogError, match="exactly one PRIMARY KEY"):
        db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT PRIMARY KEY)")
    with pytest.raises(CatalogError, match="more than once"):
        db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, a TEXT)")
    columns = ", ".join(f"c{i} INTEGER" for i in range(64))
    with pytest.raises(CatalogError, match="at most 64"):
        db.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, {columns})")
    assert db.table_names() == []

    db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY)")
    with pytest.raises(TableExistsError):
        db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY)")
    assert db.table_names() == ["t"]


def test_text_and_real_primary_keys(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE codes (code TEXT PRIMARY KEY, n INTEGER)")
    db.execute("CREATE TABLE temps (t REAL PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO codes VALUES ('b', 2), ('a', 1), ('', 0), ('ab', 3)")
    db.execute("INSERT INTO temps VALUES (-1.5, 1), (0, 2), (2.25, 3)")

    assert db.execute("SELECT n FROM codes WHERE code = 'ab'").rows == [(3,)]
    assert db.execute("SELECT code FROM codes WHERE code >= 'a' ORDER BY code").rows == [
        ("a",),
        ("ab",),
        ("b",),
    ]
    assert db.execute("SELECT n FROM temps WHERE t = 0").rows == [(2,)]
    assert db.execute("SELECT n FROM temps WHERE t < 1 ORDER BY t").rows == [(1,), (2,)]
    with pytest.raises(ConstraintViolationError):
        db.execute("INSERT INTO temps VALUES (-0.0, 9)")  # equal to 0
    db.check()


def test_drop_table_frees_its_pages_for_reuse(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, body TEXT, tag TEXT UNIQUE)")
    db.execute("BEGIN")
    for i in range(2000):
        db.execute(f"INSERT INTO big VALUES ({i}, '{'x' * 100}', 't{i}')")
    db.execute("COMMIT")
    pages_with_table = db.page_count

    db.execute("DROP TABLE big")

    report = db.check()  # includes "no page leaked" (A2)
    assert report.tables == {}
    assert report.free_pages == pages_with_table - 2  # all but the header and the catalog page
    with pytest.raises(TableNotFoundError):
        db.execute("SELECT * FROM big")

    db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, body TEXT, tag TEXT UNIQUE)")
    db.execute("BEGIN")
    for i in range(2000):
        db.execute(f"INSERT INTO big VALUES ({i}, '{'x' * 100}', 't{i}')")
    db.execute("COMMIT")
    assert db.page_count == pages_with_table  # the file did not grow
    db.check()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM missing",
        "INSERT INTO missing VALUES (1)",
        "UPDATE missing SET a = 1",
        "DELETE FROM missing",
        "DROP TABLE missing",
        "EXPLAIN SELECT * FROM missing",
    ],
)
def test_unknown_table(db: mvrs_db.Database, statement: str) -> None:
    with pytest.raises(TableNotFoundError):
        db.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT nope FROM people",
        "SELECT * FROM people WHERE nope = 1",
        "SELECT * FROM people ORDER BY nope",
        "INSERT INTO people (id, nope) VALUES (9, 1)",
        "UPDATE people SET nope = 1",
        "DELETE FROM people WHERE nope IS NULL",
    ],
)
def test_unknown_column(people: mvrs_db.Database, statement: str) -> None:
    with pytest.raises(ColumnNotFoundError):
        people.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO people VALUES (9, 'x')",
        "INSERT INTO people (id, name) VALUES (9)",
        "INSERT INTO people (id, id) VALUES (9, 9)",
        "UPDATE people SET age = 1, age = 2",
    ],
)
def test_malformed_statements_are_rejected(people: mvrs_db.Database, statement: str) -> None:
    with pytest.raises(SqlError):
        people.execute(statement)
    assert ids(people) == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------- limits


def test_record_size_limit(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, body TEXT)")
    db.execute(f"INSERT INTO docs VALUES (1, '{'x' * 4000}')")

    with pytest.raises(RecordTooLargeError):
        db.execute(f"INSERT INTO docs VALUES (2, '{'x' * 4100}')")
    with pytest.raises(RecordTooLargeError):
        db.execute(f"UPDATE docs SET body = '{'x' * 4100}' WHERE id = 1")

    assert db.execute("SELECT id FROM docs").rows == [(1,)]
    db.check()


def test_index_key_size_limit(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE codes (code TEXT PRIMARY KEY)")
    db.execute(f"INSERT INTO codes VALUES ('{'k' * 255}')")

    with pytest.raises(KeyTooLargeError):
        db.execute(f"INSERT INTO codes VALUES ('{'k' * 256}')")

    # A longer search value cannot be a key, so the planner falls back to a scan.
    assert db.execute(f"SELECT * FROM codes WHERE code = '{'k' * 300}'").rows == []
    assert len(db.execute(f"SELECT * FROM codes WHERE code < '{'z' * 300}'").rows) == 1
    db.check()


# ---------------------------------------------------------------------- persistence and files


def test_everything_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "test.mvrs"
    with mvrs_db.connect(path) as db:
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
        db.execute("CREATE TABLE b (k TEXT PRIMARY KEY, x REAL)")
        db.execute("CREATE TABLE gone (id INTEGER PRIMARY KEY)")
        db.execute("BEGIN")
        for i in range(3000):
            db.execute(f"INSERT INTO a VALUES ({i}, 'value {i}')")
        db.execute("COMMIT")
        db.execute("INSERT INTO b VALUES ('pi', 3.14159), ('e', 2.71828)")
        db.execute("DELETE FROM a WHERE id >= 2000")
        db.execute("UPDATE a SET v = 'changed' WHERE id = 7")
        db.execute("DROP TABLE gone")

    with mvrs_db.connect(path, create=False) as db:
        assert db.table_names() == ["a", "b"]
        assert db.check().tables == {"a": 2000, "b": 2}
        assert db.execute("SELECT v FROM a WHERE id = 7").rows == [("changed",)]
        assert db.execute("SELECT id FROM a WHERE v = 'value 1999'").rows == [(1999,)]
        assert db.execute("SELECT id FROM a WHERE id >= 2000").rows == []
        assert db.execute("SELECT x FROM b WHERE k = 'pi'").rows == [(3.14159,)]
        db.execute("INSERT INTO a VALUES (5000, 'after reopen')")

    with mvrs_db.connect(path) as db:
        assert db.execute("SELECT v FROM a WHERE id = 5000").rows == [("after reopen",)]
        assert db.recovery.pages_restored == 0  # a clean close leaves nothing to recover


@pytest.mark.parametrize("page_size", [4096, 8192, 32768])
def test_page_sizes(tmp_path: Path, page_size: int) -> None:
    path = tmp_path / "test.mvrs"
    with mvrs_db.connect(path, page_size=page_size) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("BEGIN")
        for i in range(500):
            db.execute(f"INSERT INTO t VALUES ({i}, '{'x' * 200}')")
        db.execute("COMMIT")

    with mvrs_db.connect(path) as db:  # the page size comes from the file, not the argument
        assert db.page_size == page_size
        assert db.check().tables == {"t": 500}


def test_open_refuses_missing_and_foreign_files(tmp_path: Path) -> None:
    with pytest.raises(DatabaseNotFoundError):
        mvrs_db.connect(tmp_path / "missing.mvrs", create=False)
    assert list(tmp_path.iterdir()) == []

    other = tmp_path / "notes.txt"
    other.write_text("not a database")
    with pytest.raises(NotADatabaseError):
        mvrs_db.connect(other)
    assert other.read_text() == "not a database"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]  # no stray log or lock


def test_second_open_is_refused_until_the_first_closes(tmp_path: Path) -> None:
    path = tmp_path / "test.mvrs"
    first = mvrs_db.connect(path)

    with pytest.raises(DatabaseLockedError):
        mvrs_db.connect(path)

    first.close()
    mvrs_db.connect(path).close()


def test_use_after_close(tmp_path: Path) -> None:
    db = mvrs_db.connect(tmp_path / "test.mvrs")
    db.close()
    db.close()  # closing twice is harmless

    with pytest.raises(DatabaseClosedError):
        db.execute("SELECT * FROM t")
    with pytest.raises(DatabaseClosedError):
        db.connect()


def test_small_buffer_pool_still_works(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "test.mvrs", buffer_pages=16, synchronous=False) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        for start in range(0, 3000, 50):
            values = ", ".join(f"({i}, '{'x' * 150}')" for i in range(start, start + 50))
            db.execute(f"INSERT INTO t VALUES {values}")

        assert db.pool.stats.evictions > 0
        assert db.execute("SELECT id FROM t WHERE id = 2999").rows == [(2999,)]
        assert db.check().tables == {"t": 3000}
