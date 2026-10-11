"""Aggregate functions and GROUP BY."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import ColumnNotFoundError, SqlError, SqlSyntaxError, TypeMismatchError
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "agg.mvrs", synchronous=False)
    database.execute(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, product TEXT, "
        "qty INTEGER, price REAL, code TEXT UNIQUE, v VECTOR(2))"
    )
    database.execute(
        "INSERT INTO sales VALUES "
        "(1, 'north', 'pen', 10, 1.5, 'a', [1, 0]), "
        "(2, 'north', 'book', 2, 12.0, 'b', NULL), "
        "(3, 'south', 'pen', 5, 1.5, NULL, NULL), "
        "(4, 'south', 'lamp', NULL, 30.0, 'd', NULL), "
        "(5, NULL, 'pen', 7, NULL, 'e', NULL), "
        "(6, 'north', 'pen', 3, 2.0, NULL, NULL), "
        "(7, NULL, 'book', NULL, NULL, 'g', NULL)"
    )
    yield database
    database.close()


def rows(db: mvrs_db.Database, sql: str) -> list[tuple[object, ...]]:
    return db.execute(sql).rows


def plan(db: mvrs_db.Database, sql: str) -> str:
    return "\n".join(str(row[0]) for row in db.execute(f"EXPLAIN {sql}").rows)


# ---------------------------------------------------------------------- parsing


def test_aggregates_and_group_by_parse() -> None:
    statement = parse(
        "SELECT region, count(*), SUM(qty), avg(price), MIN(id), MAX(id) FROM sales "
        "WHERE id > 0 GROUP BY region, product ORDER BY SUM(qty) DESC, region LIMIT 3"
    )
    assert isinstance(statement, ast.Select)
    assert statement.columns == (
        "region",
        ast.Aggregate("COUNT", None),
        ast.Aggregate("SUM", "qty"),
        ast.Aggregate("AVG", "price"),
        ast.Aggregate("MIN", "id"),
        ast.Aggregate("MAX", "id"),
    )
    assert statement.group_by == ("region", "product")
    assert statement.order_by == (
        ast.OrderItem(ast.Aggregate("SUM", "qty"), True),
        ast.OrderItem("region", False),
    )
    assert statement.limit == 3
    assert ast.Aggregate("COUNT", None).label == "count(*)"
    assert ast.Aggregate("AVG", "price").label == "avg(price)"


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("SELECT SUM(*) FROM t", "only COUNT can take \\*"),
        ("SELECT MAX(*) FROM t", "only COUNT can take \\*"),
        ("SELECT COUNT FROM t", "expected '\\('"),
        ("SELECT COUNT() FROM t", "expected a column name or \\*"),
        ("SELECT COUNT(a, b) FROM t", "expected '\\)'"),
        ("SELECT SUM(1) FROM t", "expected a column name"),
        ("SELECT SUM(SUM(a)) FROM t", "reserved word"),
        ("SELECT a FROM t GROUP a", "expected BY"),
        ("SELECT a FROM t GROUP BY", "expected column name"),
        ("SELECT a FROM t GROUP BY COUNT(*)", "reserved word"),
        ("SELECT a FROM t WHERE COUNT(*) > 1", "expected a value"),
        ("SELECT a FROM t ORDER BY a GROUP BY a", "expected end of statement"),
        ("CREATE TABLE count (id INT PRIMARY KEY)", "reserved word"),
    ],
)
def test_aggregate_syntax_errors(statement: str, message: str) -> None:
    with pytest.raises(SqlSyntaxError, match=message):
        parse(statement)


# ---------------------------------------------------------------------- whole-table aggregates


def test_every_function_over_the_whole_table(db: mvrs_db.Database) -> None:
    result = db.execute(
        "SELECT COUNT(*), COUNT(qty), SUM(qty), AVG(qty), MIN(qty), MAX(qty) FROM sales"
    )

    assert result.columns == (
        "count(*)",
        "count(qty)",
        "sum(qty)",
        "avg(qty)",
        "min(qty)",
        "max(qty)",
    )
    assert result.rows == [(7, 5, 27, 5.4, 2, 10)]
    assert result.rowcount == 1


def test_result_types(db: mvrs_db.Database) -> None:
    (row,) = rows(db, "SELECT SUM(qty), SUM(price), AVG(qty), COUNT(*), MIN(price) FROM sales")

    assert [type(value) for value in row] == [int, float, float, int, float]
    assert row[1] == pytest.approx(47.0)


def test_nulls_are_ignored_except_by_count_star(db: mvrs_db.Database) -> None:
    assert rows(db, "SELECT COUNT(*), COUNT(region), COUNT(code), COUNT(v) FROM sales") == [
        (7, 5, 5, 1)
    ]
    # AVG divides by the number of non-NULL values, not by the number of rows.
    assert rows(db, "SELECT AVG(price) FROM sales") == [(pytest.approx(47.0 / 5),)]


def test_min_and_max_of_text(db: mvrs_db.Database) -> None:
    assert rows(db, "SELECT MIN(product), MAX(product), MIN(region) FROM sales") == [
        ("book", "pen", "north")
    ]


def test_where_is_applied_before_aggregating(db: mvrs_db.Database) -> None:
    assert rows(db, "SELECT COUNT(*), SUM(qty) FROM sales WHERE product = 'pen'") == [(4, 25)]
    assert rows(db, "SELECT COUNT(*) FROM sales WHERE id >= 3 AND id <= 5") == [(3,)]
    assert rows(db, "SELECT MAX(id) FROM sales WHERE region IS NULL") == [(7,)]


def test_aggregates_over_no_rows(db: mvrs_db.Database) -> None:
    result = rows(
        db,
        "SELECT COUNT(*), COUNT(qty), SUM(qty), AVG(qty), MIN(qty), MAX(qty) "
        "FROM sales WHERE id > 100",
    )
    assert result == [(0, 0, None, None, None, None)]  # one row, even from nothing

    assert rows(db, "SELECT SUM(qty), AVG(qty) FROM sales WHERE id = 4 OR id = 7") == [
        (None, None)  # rows exist, but every qty is NULL
    ]


def test_aggregates_on_an_empty_table(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "empty.mvrs") as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")

        assert db.execute("SELECT COUNT(*), MIN(id), MAX(id), SUM(n) FROM t").rows == [
            (0, None, None, None)
        ]
        assert db.execute("SELECT n, COUNT(*) FROM t GROUP BY n").rows == []


def test_the_same_aggregate_may_appear_twice(db: mvrs_db.Database) -> None:
    result = db.execute("SELECT COUNT(*), COUNT(*), SUM(qty) FROM sales")

    assert result.columns == ("count(*)", "count(*)", "sum(qty)")
    assert result.rows == [(7, 7, 27)]


def test_integer_sum_does_not_overflow(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "big.mvrs") as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 9223372036854775807), (2, 9223372036854775807)")

        assert db.execute("SELECT SUM(n) FROM t").rows == [(2 * (2**63 - 1),)]
        assert db.execute("SELECT AVG(n) FROM t").rows == [(float(2**63 - 1),)]


# ---------------------------------------------------------------------- GROUP BY


def test_group_by_one_column(db: mvrs_db.Database) -> None:
    result = db.execute(
        "SELECT region, COUNT(*), SUM(qty), MAX(price) FROM sales GROUP BY region ORDER BY region"
    )

    assert result.columns == ("region", "count(*)", "sum(qty)", "max(price)")
    assert result.rows == [
        (None, 2, 7, None),  # NULL regions form one group; NULL sorts first
        ("north", 3, 15, 12.0),
        ("south", 2, 5, 30.0),
    ]


def test_group_by_two_columns(db: mvrs_db.Database) -> None:
    assert rows(
        db,
        "SELECT region, product, COUNT(*) FROM sales WHERE region IS NOT NULL "
        "GROUP BY region, product ORDER BY region, product",
    ) == [
        ("north", "book", 1),
        ("north", "pen", 2),
        ("south", "lamp", 1),
        ("south", "pen", 1),
    ]


def test_group_columns_can_be_selected_in_any_order_or_not_at_all(db: mvrs_db.Database) -> None:
    assert rows(db, "SELECT COUNT(*), product FROM sales GROUP BY product ORDER BY product") == [
        (2, "book"),
        (1, "lamp"),
        (4, "pen"),
    ]
    assert sorted(rows(db, "SELECT COUNT(*) FROM sales GROUP BY product")) == [(1,), (2,), (4,)]
    assert sorted(rows(db, "SELECT product FROM sales GROUP BY product")) == [
        ("book",),
        ("lamp",),
        ("pen",),
    ]


def test_order_by_an_aggregate_and_limit(db: mvrs_db.Database) -> None:
    assert rows(
        db,
        "SELECT product, SUM(qty) FROM sales GROUP BY product ORDER BY SUM(qty) DESC LIMIT 2",
    ) == [("pen", 25), ("book", 2)]
    # Ordering by an aggregate that is not selected.
    assert rows(db, "SELECT product FROM sales GROUP BY product ORDER BY COUNT(*), product") == [
        ("lamp",),
        ("book",),
        ("pen",),
    ]
    # A NULL aggregate sorts first ascending, like any NULL.
    assert rows(db, "SELECT product, SUM(qty) FROM sales GROUP BY product ORDER BY SUM(qty)") == [
        ("lamp", None),
        ("book", 2),
        ("pen", 25),
    ]
    assert rows(db, "SELECT product FROM sales GROUP BY product ORDER BY product LIMIT 0") == []


def test_group_by_a_numeric_column_and_a_unique_column(db: mvrs_db.Database) -> None:
    assert rows(db, "SELECT price, COUNT(*) FROM sales GROUP BY price ORDER BY price") == [
        (None, 2),
        (1.5, 2),
        (2.0, 1),
        (12.0, 1),
        (30.0, 1),
    ]
    assert len(rows(db, "SELECT id, COUNT(*) FROM sales GROUP BY id")) == 7


def test_group_by_with_where_that_removes_every_row(db: mvrs_db.Database) -> None:
    assert rows(db, "SELECT region, COUNT(*) FROM sales WHERE id > 100 GROUP BY region") == []


@pytest.mark.parametrize(
    ("sql", "error", "message"),
    [
        ("SELECT region, COUNT(*) FROM sales", SqlError, "region must appear in GROUP BY"),
        ("SELECT product, COUNT(*) FROM sales GROUP BY region", SqlError, "product must appear"),
        ("SELECT * FROM sales GROUP BY region", SqlError, "SELECT \\* cannot be used"),
        ("SELECT region FROM sales GROUP BY region ORDER BY qty", SqlError, "qty must appear"),
        ("SELECT COUNT(*) FROM sales GROUP BY region, region", SqlError, "more than once"),
        ("SELECT SUM(product) FROM sales", TypeMismatchError, "SUM needs a numeric column"),
        ("SELECT AVG(region) FROM sales", TypeMismatchError, "AVG needs a numeric column"),
        ("SELECT SUM(v) FROM sales", TypeMismatchError, "SUM needs a numeric column"),
        ("SELECT MIN(v) FROM sales", TypeMismatchError, "MIN cannot be applied to a vector"),
        ("SELECT COUNT(*) FROM sales GROUP BY v", TypeMismatchError, "cannot GROUP BY v"),
        ("SELECT COUNT(nope) FROM sales", ColumnNotFoundError, "no column named nope"),
        ("SELECT COUNT(*) FROM sales GROUP BY nope", ColumnNotFoundError, "no column named nope"),
        (
            "SELECT COUNT(*), v <-> [1, 2] FROM sales",
            SqlError,
            "vector distance cannot be combined",
        ),
        (
            "SELECT COUNT(*) FROM sales ORDER BY v <-> [1, 2]",
            SqlError,
            "vector distance cannot be combined",
        ),
    ],
)
def test_invalid_aggregate_queries(
    db: mvrs_db.Database, sql: str, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        db.execute(sql)


# ---------------------------------------------------------------------- plans


def test_count_star_without_where_reads_no_rows(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT COUNT(*) FROM sales")

    assert "MetadataAggregate(sales: count(*) from heap page slot counts)" in text
    assert "Scan" not in text
    assert rows(db, "SELECT COUNT(*) FROM sales") == [(7,)]


def test_min_and_max_of_indexed_columns_come_from_the_index(db: mvrs_db.Database) -> None:
    sql = "SELECT MIN(id), MAX(id), MAX(code), COUNT(*) FROM sales"
    text = plan(db, sql)

    assert "MetadataAggregate" in text and "min(id) from index edge" in text
    assert rows(db, sql) == [(1, 7, "g", 7)]

    db.execute("DELETE FROM sales WHERE id = 7 OR id = 1")
    assert rows(db, sql) == [(2, 6, "e", 5)]  # still right after the edges are deleted


def test_other_queries_use_a_real_aggregation(db: mvrs_db.Database) -> None:
    for sql in (
        "SELECT COUNT(*) FROM sales WHERE id > 2",  # a WHERE clause
        "SELECT MIN(qty) FROM sales",  # no index on qty
        "SELECT COUNT(qty) FROM sales",  # needs to look at values
        "SELECT MIN(id), SUM(qty) FROM sales",  # one of them cannot use metadata
    ):
        text = plan(db, sql)
        assert "Aggregate(" in text and "MetadataAggregate" not in text, sql

    text = plan(db, "SELECT COUNT(*) FROM sales WHERE id >= 2 AND id < 4")
    assert "Aggregate(count(*))" in text and "IndexScan(sales.id" in text


def test_explain_group_by(db: mvrs_db.Database) -> None:
    lines = plan(
        db,
        "SELECT product, SUM(qty) FROM sales WHERE qty > 1 GROUP BY product "
        "ORDER BY SUM(qty) DESC LIMIT 2",
    ).splitlines()

    assert lines[0].startswith("Project(product, sum(qty))")
    assert lines[1].strip().startswith("TopN(2; sum(qty) DESC)")
    assert lines[2].strip().startswith("HashAggregate(group by product; sum(qty))")
    assert lines[3].strip().startswith("Filter(qty > 1)")
    assert lines[4].strip().startswith("SeqScan(sales)")


# ---------------------------------------------------------------------- against a reference


def test_group_by_matches_a_python_reference_on_random_data(tmp_path: Path) -> None:
    rng = random.Random(3)
    data = [
        (
            i,
            rng.choice(["a", "b", "c", "d", None]),
            rng.choice([1, 2, 3, None]),
            rng.choice([None, *range(-50, 50)]),
            rng.choice([None, 0.5, 1.25, -3.0, 10.0]),
        )
        for i in range(3000)
    ]

    def literal(value: object) -> str:
        return "NULL" if value is None else f"'{value}'" if isinstance(value, str) else str(value)

    with mvrs_db.connect(tmp_path / "ref.mvrs", synchronous=False) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k TEXT, g INTEGER, n INTEGER, x REAL)")
        for start in range(0, len(data), 300):
            values = ", ".join(
                "(" + ", ".join(literal(v) for v in row) + ")" for row in data[start : start + 300]
            )
            db.execute(f"INSERT INTO t VALUES {values}")

        groups: dict[tuple[object, object], list[tuple[object, ...]]] = defaultdict(list)
        for row in data:
            if row[0] % 7 != 0:  # type: ignore[operator]
                groups[(row[1], row[2])].append(row)
        expected = {}
        for key, members in groups.items():
            ns = [m[3] for m in members if m[3] is not None]
            xs = [m[4] for m in members if m[4] is not None]
            expected[key] = (
                len(members),
                len(ns),
                sum(ns) if ns else None,  # type: ignore[arg-type]
                min(ns) if ns else None,  # type: ignore[type-var]
                max(ns) if ns else None,  # type: ignore[type-var]
                sum(xs) / len(xs) if xs else None,  # type: ignore[arg-type]
            )

        # id values 0, 7, 14, ... are excluded with a WHERE the model mirrors.
        excluded = " AND ".join(f"id != {i}" for i in range(0, 3000, 7))
        found = db.execute(
            "SELECT k, g, COUNT(*), COUNT(n), SUM(n), MIN(n), MAX(n), AVG(x) FROM t "
            f"WHERE {excluded} GROUP BY k, g"
        ).rows

        assert len(found) == len(expected) == 20
        for k, g, *values in found:
            want = expected[(k, g)]
            assert values[:5] == list(want[:5]), (k, g)
            assert values[5] == (None if want[5] is None else pytest.approx(want[5])), (k, g)


def test_aggregates_see_uncommitted_changes_of_their_own_transaction(
    db: mvrs_db.Database,
) -> None:
    db.execute("BEGIN")
    db.execute("DELETE FROM sales WHERE region = 'north'")
    assert rows(db, "SELECT COUNT(*), SUM(qty) FROM sales") == [(4, 12)]
    assert rows(db, "SELECT COUNT(*) FROM sales WHERE id > 0") == [(4,)]
    db.execute("ROLLBACK")

    assert rows(db, "SELECT COUNT(*), SUM(qty) FROM sales") == [(7, 27)]
