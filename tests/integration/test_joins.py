"""Inner joins, qualified names and table aliases."""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import (
    ColumnNotFoundError,
    SqlError,
    SqlSyntaxError,
    TableNotFoundError,
    TypeMismatchError,
)
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "join.mvrs", synchronous=False)
    database.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "city TEXT, code TEXT UNIQUE)"
    )
    database.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, "
        "total REAL, city TEXT, ref TEXT)"
    )
    database.execute(
        "INSERT INTO customers VALUES "
        "(1, 'asha', 'pune', 'A'), (2, 'ravi', 'delhi', 'R'), "
        "(3, 'mei', 'pune', NULL), (4, 'omar', NULL, 'O')"
    )
    database.execute(
        "INSERT INTO orders VALUES "
        "(10, 1, 50.0, 'pune', 'A'), (11, 1, 20.0, 'delhi', 'A'), "
        "(12, 2, 75.5, 'delhi', 'R'), (13, NULL, 5.0, 'pune', NULL), "
        "(14, 9, 1.0, 'goa', 'Z'), (15, 3, 30.0, NULL, 'O')"
    )
    yield database
    database.close()


def rows(db: mvrs_db.Database, sql: str) -> list[tuple[object, ...]]:
    return db.execute(sql).rows


def plan(db: mvrs_db.Database, sql: str) -> str:
    return "\n".join(str(row[0]) for row in db.execute(f"EXPLAIN {sql}").rows)


JOIN = "FROM orders JOIN customers ON orders.customer_id = customers.id"


# ---------------------------------------------------------------------- parsing


def test_join_aliases_and_qualified_names_parse() -> None:
    statement = parse(
        "SELECT o.id, c.name, total FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
        "WHERE c.city = 'pune' AND o.total > 10 GROUP BY c.name ORDER BY o.id DESC LIMIT 5"
    )
    assert isinstance(statement, ast.Select)
    assert (statement.table, statement.alias) == ("orders", "o")
    assert statement.joins == (ast.Join("customers", "c", "o.customer_id", "c.id"),)
    assert statement.columns == ("o.id", "c.name", "total")
    assert statement.group_by == ("c.name",)
    assert statement.order_by == (ast.OrderItem("o.id", True),)
    assert parse("SELECT * FROM a JOIN b ON x = y").joins == (ast.Join("b", None, "x", "y"),)  # type: ignore[union-attr]
    assert parse("SELECT SUM(o.total) FROM orders o").columns == (  # type: ignore[union-attr]
        ast.Aggregate("SUM", "o.total"),
    )


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("SELECT * FROM a JOIN b", "expected ON"),
        ("SELECT * FROM a JOIN b ON a.x", "one equality"),
        ("SELECT * FROM a JOIN b ON a.x < b.y", "one equality"),
        ("SELECT * FROM a JOIN b ON a.x = 5", "expected a column name"),
        ("SELECT * FROM a JOIN b ON a.x = b.y AND a.z = b.w", "put other conditions in WHERE"),
        ("SELECT * FROM a INNER b ON a.x = b.y", "expected JOIN"),
        ("SELECT * FROM a LEFT b ON a.x = b.y", "expected JOIN"),
        ("SELECT * FROM a FULL OUTER b ON a.x = b.y", "expected JOIN"),
        ("SELECT * FROM a CROSS JOIN b", "CROSS is not supported"),
        ("SELECT * FROM a NATURAL JOIN b", "NATURAL is not supported"),
        ("SELECT * FROM a JOIN b USING (x)", "USING is not supported"),
        ("SELECT * FROM a x y", "expected end of statement"),
        ("SELECT a. FROM t", "a column name after the dot"),
        ("SELECT a.b.c FROM t", "not allowed here"),
        ("INSERT INTO t (t.a) VALUES (1)", "not allowed here"),
        ("UPDATE t SET t.a = 1", "not allowed here"),
    ],
)
def test_join_syntax_errors(statement: str, message: str) -> None:
    with pytest.raises(SqlSyntaxError, match=message):
        parse(statement)


# ---------------------------------------------------------------------- one table, qualified


def test_qualified_names_and_aliases_on_a_single_table(db: mvrs_db.Database) -> None:
    result = db.execute(
        "SELECT customers.id, name FROM customers WHERE customers.city = 'pune' "
        "ORDER BY customers.id DESC"
    )
    assert result.columns == ("customers.id", "name")  # named as written
    assert result.rows == [(3, "mei"), (1, "asha")]

    assert rows(db, "SELECT c.name FROM customers c WHERE c.id = 2") == [("ravi",)]
    assert rows(db, "SELECT COUNT(c.code), MAX(c.id) FROM customers c") == [(3, 4)]
    assert "IndexScan(customers.id: id = 2)" in plan(
        db, "SELECT c.name FROM customers c WHERE c.id = 2"
    )


def test_qualified_names_in_update_and_delete(db: mvrs_db.Database) -> None:
    assert db.execute("UPDATE orders SET total = 0 WHERE orders.id = 10").rowcount == 1
    assert db.execute("DELETE FROM orders WHERE orders.total = 0").rowcount == 1
    assert "IndexScan(orders.id: id = 11)" in plan(db, "DELETE FROM orders WHERE orders.id = 11")
    with pytest.raises(ColumnNotFoundError, match="customers is not a table in this query"):
        db.execute("DELETE FROM orders WHERE customers.id = 1")


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT orders.id FROM customers", "orders is not a table in this query"),
        ("SELECT customers.id FROM customers c", "customers is not a table in this query"),
        ("SELECT c.nope FROM customers c", "customers has no column named nope"),
        ("SELECT nope FROM customers", "customers has no column named nope"),
        ("SELECT id FROM customers WHERE x.id = 1", "x is not a table in this query"),
    ],
)
def test_unknown_qualifiers_and_columns(db: mvrs_db.Database, sql: str, message: str) -> None:
    with pytest.raises(ColumnNotFoundError, match=message):
        db.execute(sql)


# ---------------------------------------------------------------------- join results


def test_inner_join_returns_only_matching_pairs(db: mvrs_db.Database) -> None:
    result = db.execute(f"SELECT orders.id, customers.name, total {JOIN} ORDER BY orders.id")

    assert result.columns == ("orders.id", "customers.name", "total")
    assert result.rows == [
        (10, "asha", 50.0),
        (11, "asha", 20.0),
        (12, "ravi", 75.5),
        (15, "mei", 30.0),
    ]
    # Order 13 has a NULL customer_id and order 14 points at no customer: neither
    # appears. Customer 4 has no orders: it does not appear either.


def test_select_star_returns_every_column_of_both_tables(db: mvrs_db.Database) -> None:
    result = db.execute(f"SELECT * {JOIN} WHERE orders.id = 12")

    assert result.columns == (
        "orders.id",
        "orders.customer_id",
        "orders.total",
        "orders.city",
        "orders.ref",
        "customers.id",
        "customers.name",
        "customers.city",
        "customers.code",
    )
    assert result.rows == [(12, 2, 75.5, "delhi", "R", 2, "ravi", "delhi", "R")]


def test_either_table_may_be_written_first(db: mvrs_db.Database) -> None:
    forward = rows(db, f"SELECT orders.id, name {JOIN} ORDER BY orders.id")
    reverse = rows(
        db,
        "SELECT orders.id, name FROM customers JOIN orders ON customers.id = orders.customer_id "
        "ORDER BY orders.id",
    )
    flipped_condition = rows(
        db,
        "SELECT orders.id, name FROM orders JOIN customers ON customers.id = orders.customer_id "
        "ORDER BY orders.id",
    )

    assert forward == reverse == flipped_condition


def test_aliases(db: mvrs_db.Database) -> None:
    assert rows(
        db,
        "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id "
        "WHERE c.name = 'asha' ORDER BY o.id",
    ) == [(10, "asha"), (11, "asha")]


def test_unqualified_names_work_when_unambiguous(db: mvrs_db.Database) -> None:
    assert rows(db, f"SELECT name, total {JOIN} WHERE customer_id = 2 AND name = 'ravi'") == [
        ("ravi", 75.5)
    ]


def test_ambiguous_names_must_be_qualified(db: mvrs_db.Database) -> None:
    for sql in (
        f"SELECT id {JOIN}",
        f"SELECT name {JOIN} WHERE city = 'pune'",
        f"SELECT name {JOIN} ORDER BY id",
        f"SELECT COUNT(id) {JOIN}",
        "SELECT name FROM orders JOIN customers ON id = customer_id",
    ):
        with pytest.raises(SqlError, match="is ambiguous; write orders\\.\\w+ or customers\\."):
            db.execute(sql)


def test_where_conditions_on_one_table_both_tables_and_across_tables(
    db: mvrs_db.Database,
) -> None:
    assert rows(db, f"SELECT orders.id {JOIN} WHERE total >= 30 ORDER BY orders.id") == [
        (10,),
        (12,),
        (15,),
    ]
    assert rows(
        db,
        f"SELECT orders.id {JOIN} WHERE total >= 30 AND customers.city = 'pune' ORDER BY orders.id",
    ) == [(10,), (15,)]
    # A condition that compares columns of the two tables.
    assert rows(
        db, f"SELECT orders.id {JOIN} WHERE orders.city = customers.city ORDER BY orders.id"
    ) == [(10,), (12,)]
    assert rows(
        db,
        f"SELECT orders.id {JOIN} WHERE orders.city != customers.city OR total > 70 "
        "ORDER BY orders.id",
    ) == [(11,), (12,)]
    assert rows(db, f"SELECT orders.id {JOIN} WHERE total > 1000") == []


def test_null_keys_never_match(db: mvrs_db.Database) -> None:
    # Joining on text columns that both contain NULLs: NULL = NULL is not a match.
    assert rows(
        db,
        "SELECT orders.id, customers.id FROM orders JOIN customers ON orders.ref = customers.code "
        "ORDER BY orders.id",
    ) == [(10, 1), (11, 1), (12, 2), (15, 4)]
    assert rows(
        db,
        "SELECT o.id, c.id FROM orders o JOIN customers c ON o.city = c.city "
        "WHERE o.id = 13 OR o.id = 15 ORDER BY c.id",
    ) == [(13, 1), (13, 3)]  # order 15's NULL city matches nobody, not even customer 4's NULL


def test_join_on_a_non_unique_column_returns_every_pairing(db: mvrs_db.Database) -> None:
    result = rows(
        db,
        "SELECT o.id, c.id FROM orders o JOIN customers c ON o.city = c.city ORDER BY o.id, c.id",
    )

    assert result == [(10, 1), (10, 3), (11, 2), (12, 2), (13, 1), (13, 3)]


def test_integer_and_real_keys_join_by_value(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "num.mvrs") as db:
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, n INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, x REAL)")
        db.execute("INSERT INTO a VALUES (1, 2), (2, 3), (3, 4)")
        db.execute("INSERT INTO b VALUES (1, 2.0), (2, 3.5), (3, 4.0)")

        found = db.execute("SELECT a.id, b.id FROM a JOIN b ON a.n = b.x ORDER BY a.id").rows

        assert found == [(1, 1), (3, 3)]


def test_self_join_needs_aliases(db: mvrs_db.Database) -> None:
    with pytest.raises(SqlError, match="appears twice; give each one an alias"):
        db.execute("SELECT * FROM customers JOIN customers ON customers.id = customers.id")

    pairs = rows(
        db,
        "SELECT a.name, b.name FROM customers a JOIN customers b ON a.city = b.city "
        "WHERE a.id < b.id",
    )
    assert pairs == [("asha", "mei")]


@pytest.mark.parametrize(
    ("sql", "error", "message"),
    [
        (
            "SELECT * FROM orders JOIN customers ON orders.id = orders.customer_id",
            SqlError,
            "must compare a column of orders with a column of customers",
        ),
        (
            "SELECT * FROM orders JOIN customers ON orders.total = customers.name",
            TypeMismatchError,
            "cannot join orders.total \\(REAL\\) with customers.name \\(TEXT\\)",
        ),
        (
            "SELECT * FROM orders JOIN customers ON orders.nope = customers.id",
            ColumnNotFoundError,
            "orders has no column named nope",
        ),
        (f"SELECT orders.id {JOIN} WHERE name = 5", TypeMismatchError, "cannot compare"),
        (
            f"SELECT orders.id {JOIN} WHERE orders.city = customers.id",
            TypeMismatchError,
            "cannot compare",
        ),
    ],
)
def test_invalid_joins(
    db: mvrs_db.Database, sql: str, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        db.execute(sql)


def test_join_with_a_missing_table(db: mvrs_db.Database) -> None:
    with pytest.raises(TableNotFoundError, match="no such table: nope"):
        db.execute("SELECT * FROM orders JOIN nope ON orders.id = nope.id")
    with pytest.raises(TableNotFoundError, match="no such table: nope"):
        db.execute("SELECT * FROM nope JOIN orders ON orders.id = nope.id")


# ---------------------------------------------------------------------- with other features


def test_order_by_and_limit_on_joined_rows(db: mvrs_db.Database) -> None:
    assert rows(db, f"SELECT name, total {JOIN} ORDER BY total DESC LIMIT 2") == [
        ("ravi", 75.5),
        ("asha", 50.0),
    ]
    assert rows(
        db, f"SELECT customers.name, orders.id {JOIN} ORDER BY customers.name DESC, orders.id"
    ) == [("ravi", 12), ("mei", 15), ("asha", 10), ("asha", 11)]
    assert len(rows(db, f"SELECT name {JOIN} LIMIT 3")) == 3
    assert rows(db, f"SELECT name {JOIN} LIMIT 0") == []


def test_aggregates_over_a_join(db: mvrs_db.Database) -> None:
    result = db.execute(
        f"SELECT customers.name, COUNT(*), SUM(total) {JOIN} "
        "GROUP BY customers.name ORDER BY SUM(total) DESC"
    )

    assert result.columns == ("customers.name", "count(*)", "sum(total)")
    assert result.rows == [("ravi", 1, 75.5), ("asha", 2, 70.0), ("mei", 1, 30.0)]
    assert rows(db, f"SELECT COUNT(*), MAX(orders.id), MIN(customers.id) {JOIN}") == [(4, 15, 1)]
    assert rows(db, f"SELECT COUNT(*) {JOIN} WHERE total > 1000") == [(0,)]
    assert rows(
        db,
        f"SELECT customers.city, COUNT(*) {JOIN} WHERE total > 10 "
        "GROUP BY customers.city ORDER BY customers.city",
    ) == [("delhi", 1), ("pune", 3)]


def test_join_sees_the_transactions_own_changes_and_rolls_back(db: mvrs_db.Database) -> None:
    db.execute("BEGIN")
    db.execute("INSERT INTO customers VALUES (9, 'new', 'goa', 'N')")
    assert rows(db, f"SELECT name {JOIN} WHERE orders.id = 14") == [("new",)]
    db.execute("ROLLBACK")

    assert rows(db, f"SELECT name {JOIN} WHERE orders.id = 14") == []


def test_vector_distance_is_not_allowed_in_a_join(tmp_path: Path) -> None:
    with mvrs_db.connect(tmp_path / "v.mvrs") as db:
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, v VECTOR(2))")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
        for sql in (
            "SELECT a.id FROM a JOIN b ON a.id = b.id ORDER BY v <-> [1, 2] LIMIT 1",
            "SELECT v <-> [1, 2] FROM a JOIN b ON a.id = b.id",
        ):
            with pytest.raises(SqlError, match="vector distance cannot be used in a join"):
                db.execute(sql)


# ---------------------------------------------------------------------- plans


def test_filters_on_one_table_are_pushed_below_the_join(db: mvrs_db.Database) -> None:
    lines = [
        line.strip()
        for line in plan(
            db, f"SELECT name {JOIN} WHERE orders.id = 12 AND customers.city = 'delhi'"
        ).splitlines()
    ]

    # Each condition sits under the join, on its own table, and the one on
    # orders uses orders' primary-key index. (On tables this small a hash join
    # is the cheaper method; the index join is checked on larger tables below.)
    assert "Join(orders.customer_id = customers.id" in lines[1]
    assert lines[2].startswith("Filter(id = 12)")
    assert lines[3].startswith("IndexScan(orders.id: id = 12)")
    assert lines[4].startswith("Filter(city = 'delhi')")
    assert lines[5].startswith("SeqScan(customers)")


def test_conditions_across_tables_are_filtered_above_the_join(db: mvrs_db.Database) -> None:
    lines = [
        line.strip()
        for line in plan(
            db, f"SELECT name {JOIN} WHERE orders.city = customers.city AND total > 5"
        ).splitlines()
    ]

    assert lines[1].startswith("Filter(orders.city = customers.city)")
    assert "Join(orders.customer_id = customers.id" in lines[2]
    assert any(line.startswith("Filter(total > 5)") for line in lines[3:])


def test_join_without_a_usable_index_is_a_hash_join(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT o.id FROM orders o JOIN customers c ON o.city = c.city")

    assert "HashJoin(o.city = c.city; hash table on c)" in text  # the smaller table is built
    assert text.count("SeqScan") == 2


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path_factory.mktemp("bigjoin") / "big.mvrs", synchronous=False)
    database.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT, floor INTEGER)")
    database.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, dept_id INTEGER, pay INTEGER)")
    rng = random.Random(4)
    database.execute(
        "INSERT INTO dept VALUES " + ", ".join(f"({i}, 'd{i}', {i % 5})" for i in range(300))
    )
    for start in range(0, 6000, 500):
        database.execute(
            "INSERT INTO emp VALUES "
            + ", ".join(
                f"({i}, {rng.randrange(330)}, {rng.randrange(100)})"
                for i in range(start, start + 500)
            )
        )
    yield database
    database.close()


def test_planner_switches_join_method_with_the_size_of_the_outer_side(
    big: mvrs_db.Database,
) -> None:
    on = "FROM emp JOIN dept ON emp.dept_id = dept.id"
    few = plan(big, f"SELECT emp.id {on} WHERE emp.id < 20")
    many = plan(big, f"SELECT emp.id {on}")

    assert "IndexJoin(emp.dept_id = dept.id; look up dept.id)" in few
    assert "HashJoin(emp.dept_id = dept.id; hash table on dept)" in many

    # With an index join, a condition on the looked-up table is checked on each
    # fetched row, and the outer table's own condition still uses its index.
    lines = [
        line.strip()
        for line in plan(
            big, f"SELECT emp.id {on} WHERE emp.id = 4321 AND dept.floor = 2"
        ).splitlines()
    ]
    assert lines[1].startswith("IndexJoin(emp.dept_id = dept.id; look up dept.id; then floor = 2)")
    assert lines[2].startswith("Filter(id = 4321)")
    assert lines[3].startswith("IndexScan(emp.id: id = 4321)")


def test_every_join_plan_matches_a_nested_loop_reference(big: mvrs_db.Database) -> None:
    """Whatever method the planner picks, the rows must equal comparing every pair."""
    emp = big.execute("SELECT * FROM emp").rows
    dept = big.execute("SELECT * FROM dept").rows
    cases = [
        ("", lambda e, d: True),
        ("WHERE emp.id < 20", lambda e, d: e[0] < 20),
        ("WHERE dept.floor = 2", lambda e, d: d[2] == 2),
        ("WHERE pay > 90 AND floor < 2", lambda e, d: e[2] > 90 and d[2] < 2),
        ("WHERE emp.id = 4321", lambda e, d: e[0] == 4321),
        ("WHERE dept.id = 7", lambda e, d: d[0] == 7),
        ("WHERE pay = floor", lambda e, d: e[2] == d[2]),
        ("WHERE emp.id < 100 OR dept.floor = 4", lambda e, d: e[0] < 100 or d[2] == 4),
    ]
    by_dept = {d[0]: d for d in dept}
    for where, keep in cases:
        expected = sorted(
            (e[0], d[1]) for e in emp if (d := by_dept.get(e[1])) is not None and keep(e, d)
        )
        for order in (
            "emp JOIN dept ON emp.dept_id = dept.id",
            "dept JOIN emp ON dept.id = dept_id",
        ):
            found = big.execute(f"SELECT emp.id, dept.name FROM {order} {where}").rows
            assert sorted(found) == expected, (where, order)

    grouped = big.execute(
        "SELECT dept.floor, COUNT(*), SUM(pay) FROM emp JOIN dept ON dept_id = dept.id "
        "GROUP BY dept.floor ORDER BY dept.floor"
    ).rows
    reference: dict[int, list[int]] = {}
    for e in emp:
        d = by_dept.get(e[1])
        if d is not None:
            reference.setdefault(d[2], []).append(e[2])  # type: ignore[arg-type]
    assert grouped == [(f, len(p), sum(p)) for f, p in sorted(reference.items())]
