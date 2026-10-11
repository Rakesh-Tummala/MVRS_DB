"""Outer joins and joins of three or more tables.

The strongest test here compares the engine with a few lines of nested-loop Python
over random tables, random join kinds and random WHERE clauses (with NULLs). The
reference shares no code with the planner, so a wrong plan shows up as a
difference in rows.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.errors import SqlError, SqlSyntaxError, TypeMismatchError
from mvrs_db.planner.joins import join_order, narrow_kinds, null_supplied, rejects_null
from mvrs_db.sql import ast
from mvrs_db.sql.parser import parse

# ---------------------------------------------------------------------- the pure logic


def where(sql: str) -> ast.Expr:
    statement = parse(f"SELECT * FROM t WHERE {sql}")
    assert isinstance(statement, ast.Select) and statement.where is not None
    return statement.where


def in_b(name: str) -> bool:
    return name.startswith("b.")


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("b.x = 1", True),
        ("1 < b.x", True),
        ("a.x = b.x", True),
        ("a.x = 1", False),
        ("b.x IS NOT NULL", True),
        ("b.x IS NULL", False),  # true exactly for the NULL-extended rows
        ("b.x = 1 OR b.y = 2", True),
        ("b.x = 1 OR a.y = 2", False),  # the second branch can be true
        ("b.x = 1 AND a.y = 2", True),
        ("NOT b.x = 1", False),  # conservative: NOT is never reported
        ("NOT (b.x IS NULL)", False),
        ("b.x = 1 OR b.x IS NULL", False),
    ],
)
def test_rejects_null(condition: str, expected: bool) -> None:
    assert rejects_null(where(condition), in_b) is expected


def table_of(name: str) -> int:
    return "abcd".index(name[0])


def test_narrowing_follows_the_side_that_is_filtered() -> None:
    def narrow(kinds: list[str], *conditions: str) -> list[str]:
        return narrow_kinds(kinds, [where(c) for c in conditions], table_of)

    assert narrow(["LEFT"], "b.x = 1") == ["INNER"]
    assert narrow(["LEFT"], "a.x = 1") == ["LEFT"]  # the preserved side: nothing to narrow
    assert narrow(["LEFT"], "b.x IS NULL") == ["LEFT"]  # the anti-join idiom must stay outer
    assert narrow(["RIGHT"], "a.x = 1") == ["INNER"]
    assert narrow(["RIGHT"], "b.x = 1") == ["RIGHT"]
    assert narrow(["FULL"], "b.x = 1") == ["RIGHT"]  # left-only rows had b NULL, so they go
    assert narrow(["FULL"], "a.x = 1") == ["LEFT"]
    assert narrow(["FULL"], "a.x = 1", "b.x = 1") == ["INNER"]
    # In a chain, "left" means every table so far.
    assert narrow(["LEFT", "LEFT"], "c.x = 1") == ["LEFT", "INNER"]
    assert narrow(["INNER", "RIGHT"], "a.x = 1") == ["INNER", "INNER"]
    assert narrow(["LEFT", "RIGHT"], "b.x = 1") == ["INNER", "INNER"]


def test_null_supplied_tables() -> None:
    assert null_supplied(["INNER", "INNER"]) == [False, False, False]
    assert null_supplied(["LEFT"]) == [False, True]
    assert null_supplied(["RIGHT"]) == [True, False]
    assert null_supplied(["FULL"]) == [True, True]
    assert null_supplied(["INNER", "RIGHT"]) == [True, True, False]
    assert null_supplied(["LEFT", "INNER"]) == [False, True, False]


def test_join_order_starts_small_and_stays_connected() -> None:
    sizes = [1000.0, 10.0, 500.0, 5.0]
    # a - b - c, and c - d: a chain written a, b, c, d
    pairs = [(0, 1), (1, 2), (2, 3)]
    order = join_order(sizes, pairs, lambda acc, chosen, t: acc)
    assert order[0] == 3  # smallest first
    assert sorted(order) == [0, 1, 2, 3]
    for i, t in enumerate(order[1:], start=1):
        assert any((t, o) in pairs or (o, t) in pairs for o in order[:i])  # never a cross product


# ---------------------------------------------------------------------- hand-checked results


@pytest.fixture
def db(tmp_path: Path) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path / "outer.mvrs", synchronous=False)
    database.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, dept INTEGER, name TEXT)")
    database.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT, site INTEGER)")
    database.execute("CREATE TABLE site (id INTEGER PRIMARY KEY, city TEXT)")
    database.execute(
        "INSERT INTO emp VALUES (1, 10, 'ann'), (2, 10, 'bob'), (3, 20, 'cy'), "
        "(4, NULL, 'di'), (5, 99, 'ed')"
    )
    database.execute(
        "INSERT INTO dept VALUES (10, 'eng', 1), (20, 'ops', 2), (30, 'hr', NULL), (40, 'law', 7)"
    )
    database.execute("INSERT INTO site VALUES (1, 'pune'), (2, 'delhi'), (3, 'goa')")
    yield database
    database.close()


def rows(db: mvrs_db.Database, sql: str) -> list[tuple[object, ...]]:
    return sorted(db.execute(sql).rows, key=repr)


def test_left_join_keeps_unmatched_left_rows(db: mvrs_db.Database) -> None:
    got = rows(db, "SELECT e.name, d.name FROM emp e LEFT JOIN dept d ON e.dept = d.id")
    assert got == sorted(
        [("ann", "eng"), ("bob", "eng"), ("cy", "ops"), ("di", None), ("ed", None)], key=repr
    )


def test_right_join_keeps_unmatched_right_rows(db: mvrs_db.Database) -> None:
    got = rows(db, "SELECT e.name, d.name FROM emp e RIGHT JOIN dept d ON e.dept = d.id")
    assert got == sorted(
        [("ann", "eng"), ("bob", "eng"), ("cy", "ops"), (None, "hr"), (None, "law")], key=repr
    )


def test_full_join_keeps_both(db: mvrs_db.Database) -> None:
    got = rows(db, "SELECT e.name, d.name FROM emp e FULL OUTER JOIN dept d ON e.dept = d.id")
    assert got == sorted(
        [
            ("ann", "eng"),
            ("bob", "eng"),
            ("cy", "ops"),
            ("di", None),
            ("ed", None),
            (None, "hr"),
            (None, "law"),
        ],
        key=repr,
    )


def test_outer_keyword_is_optional(db: mvrs_db.Database) -> None:
    a = rows(db, "SELECT e.id, d.id FROM emp e LEFT JOIN dept d ON e.dept = d.id")
    b = rows(db, "SELECT e.id, d.id FROM emp e LEFT OUTER JOIN dept d ON e.dept = d.id")
    assert a == b


def test_null_keys_never_match_each_other(db: mvrs_db.Database) -> None:
    db.execute("CREATE TABLE p (a INTEGER PRIMARY KEY, k INTEGER)")
    db.execute("CREATE TABLE q (b INTEGER PRIMARY KEY, k INTEGER)")
    db.execute("INSERT INTO p VALUES (1, NULL), (2, 5)")
    db.execute("INSERT INTO q VALUES (1, NULL), (2, 5)")
    got = rows(db, "SELECT p.a, q.b FROM p FULL JOIN q ON p.k = q.k")
    assert got == sorted([(2, 2), (1, None), (None, 1)], key=repr)


def test_three_table_inner_join(db: mvrs_db.Database) -> None:
    got = rows(
        db,
        "SELECT e.name, d.name, s.city FROM emp e JOIN dept d ON e.dept = d.id "
        "JOIN site s ON d.site = s.id",
    )
    assert got == sorted(
        [("ann", "eng", "pune"), ("bob", "eng", "pune"), ("cy", "ops", "delhi")], key=repr
    )


def test_three_table_chain_of_left_joins(db: mvrs_db.Database) -> None:
    got = rows(
        db,
        "SELECT e.name, s.city FROM emp e LEFT JOIN dept d ON e.dept = d.id "
        "LEFT JOIN site s ON d.site = s.id",
    )
    assert got == sorted(
        [("ann", "pune"), ("bob", "pune"), ("cy", "delhi"), ("di", None), ("ed", None)], key=repr
    )


def test_second_condition_may_name_any_earlier_table(db: mvrs_db.Database) -> None:
    # site joins to emp's dept column, skipping dept
    got = rows(
        db,
        "SELECT e.name, s.city FROM emp e JOIN dept d ON e.dept = d.id JOIN site s ON e.id = s.id",
    )
    assert got == sorted([("ann", "pune"), ("bob", "delhi"), ("cy", "goa")], key=repr)


def test_where_on_the_null_extended_side_is_applied_after_the_join(db: mvrs_db.Database) -> None:
    # IS NULL must see the NULLs the join added: this finds employees with no department.
    got = rows(
        db,
        "SELECT e.name FROM emp e LEFT JOIN dept d ON e.dept = d.id WHERE d.id IS NULL",
    )
    assert got == [("di",), ("ed",)]
    # A condition on the preserved side is safe to push down and keeps unmatched rows.
    got = rows(
        db,
        "SELECT e.name, d.name FROM emp e LEFT JOIN dept d ON e.dept = d.id WHERE e.id > 3",
    )
    assert got == [("di", None), ("ed", None)]


def test_where_that_rejects_nulls_turns_the_join_into_an_inner_join(db: mvrs_db.Database) -> None:
    text = "\n".join(
        str(r[0])
        for r in db.execute(
            "EXPLAIN SELECT e.name FROM emp e LEFT JOIN dept d ON e.dept = d.id "
            "WHERE d.name = 'eng'"
        ).rows
    )
    assert "LEFT" not in text
    outer = "\n".join(
        str(r[0])
        for r in db.execute(
            "EXPLAIN SELECT e.name FROM emp e LEFT JOIN dept d ON e.dept = d.id"
        ).rows
    )
    assert "LEFT" in outer


def test_select_star_lists_tables_in_the_order_written(db: mvrs_db.Database) -> None:
    result = db.execute(
        "SELECT * FROM site s JOIN dept d ON d.site = s.id JOIN emp e ON e.dept = d.id"
    )
    assert result.columns == (
        "s.id", "s.city", "d.id", "d.name", "d.site", "e.id", "e.dept", "e.name",
    )  # fmt: skip
    assert sorted(result.rows) == sorted(
        [(1, "pune", 10, "eng", 1, 1, 10, "ann"), (1, "pune", 10, "eng", 1, 2, 10, "bob"),
         (2, "delhi", 20, "ops", 2, 3, 20, "cy")]
    )  # fmt: skip


def test_aggregates_over_outer_joins(db: mvrs_db.Database) -> None:
    got = rows(
        db,
        "SELECT d.name, COUNT(e.id) FROM dept d LEFT JOIN emp e ON e.dept = d.id GROUP BY d.name",
    )
    assert got == sorted([("eng", 2), ("ops", 1), ("hr", 0), ("law", 0)], key=repr)
    assert rows(db, "SELECT COUNT(*) FROM emp e FULL JOIN dept d ON e.dept = d.id") == [(7,)]


def test_order_by_and_limit_work_on_joins_of_three(db: mvrs_db.Database) -> None:
    got = db.execute(
        "SELECT e.id, d.name, s.city FROM emp e LEFT JOIN dept d ON e.dept = d.id "
        "LEFT JOIN site s ON d.site = s.id ORDER BY e.id DESC LIMIT 3"
    ).rows
    assert got == [(5, None, None), (4, None, None), (3, "ops", "delhi")]


@pytest.mark.parametrize(
    ("sql", "error", "message"),
    [
        (
            "SELECT * FROM emp e JOIN dept d ON e.id = e.dept",
            SqlError,
            "must compare a column of e with a column of d",
        ),
        (
            "SELECT * FROM emp e JOIN dept d ON e.dept = d.id JOIN site s ON e.id = d.id",
            SqlError,
            "must compare a column of e or d with a column of s",
        ),
        (
            "SELECT * FROM emp e JOIN dept d ON e.dept = d.id JOIN site s ON s.id = e.name",
            TypeMismatchError,
            "cannot join s.id",
        ),
        (
            "SELECT * FROM emp e LEFT JOIN dept d ON e.dept = d.id WHERE x.id = 1",
            mvrs_db.errors.ColumnNotFoundError,
            "x is not a table",
        ),
        (
            "SELECT id FROM emp e JOIN dept d ON e.dept = d.id JOIN site s ON d.site = s.id",
            SqlError,
            "ambiguous",
        ),
        (
            "SELECT * FROM emp JOIN emp ON emp.id = emp.id",
            SqlError,
            "appears twice",
        ),
    ],
)
def test_errors(db: mvrs_db.Database, sql: str, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        db.execute(sql)


def test_a_table_may_be_joined_to_itself_under_two_aliases(db: mvrs_db.Database) -> None:
    got = rows(
        db,
        "SELECT a.name, b.name FROM emp a LEFT JOIN emp b ON a.dept = b.id",
    )
    assert len(got) == 5  # nobody's dept equals an emp id except by chance; all kept by LEFT
    assert all(len(row) == 2 for row in got)


def test_parse_shapes() -> None:
    statement = parse(
        "SELECT * FROM a LEFT OUTER JOIN b ON a.x = b.y RIGHT JOIN c ON b.y = c.z "
        "FULL JOIN d ON c.z = d.w JOIN e ON d.w = e.v"
    )
    assert isinstance(statement, ast.Select)
    assert [j.kind for j in statement.joins] == ["LEFT", "RIGHT", "FULL", "INNER"]
    with pytest.raises(SqlSyntaxError):
        parse("SELECT * FROM a LEFT OUTER b ON a.x = b.y")


# ---------------------------------------------------------------------- against a reference

NULL = None
Row = tuple[object, ...]


def reference_join(
    data: list[list[Row]],
    joins: list[tuple[str, int, int, int]],
) -> list[Row]:
    """Nested-loop evaluation: joins is [(kind, earlier table, its column, new column)]."""
    widths = [len(table[0]) if table else 4 for table in data]
    widths = [4] * len(data)
    acc: list[list[Row]] = [[row] for row in data[0]]
    for k, (kind, old, old_column, new_column) in enumerate(joins):
        right = data[k + 1]
        matched_right: set[int] = set()
        grown: list[list[Row]] = []
        for left in acc:
            value = left[old][old_column]
            hits = [
                i
                for i, row in enumerate(right)
                if value is not None and row[new_column] is not None and row[new_column] == value
            ]
            for i in hits:
                grown.append([*left, right[i]])
                matched_right.add(i)
            if not hits and kind in ("LEFT", "FULL"):
                grown.append([*left, (NULL,) * widths[k + 1]])
        if kind in ("RIGHT", "FULL"):
            for i, row in enumerate(right):
                if i not in matched_right:
                    grown.append([*([(NULL,) * widths[t] for t in range(k + 1)]), row])
        acc = grown
    return [tuple(value for row in parts for value in row) for parts in acc]


# A WHERE tree: ("cmp", op, (table, column), constant | (table, column)), ("null", (t, c), negated),
# ("and"/"or", a, b), ("not", a)
Tree = tuple[object, ...]
ALIASES = "abcd"
COLUMNS = ("id", "k", "u", "v")


def render(tree: Tree) -> str:
    kind = tree[0]
    if kind == "cmp":
        _, op, left, right = tree
        r = f"{ALIASES[right[0]]}.{COLUMNS[right[1]]}" if isinstance(right, tuple) else str(right)  # type: ignore[index]
        return f"{ALIASES[left[0]]}.{COLUMNS[left[1]]} {op} {r}"  # type: ignore[index]
    if kind == "null":
        _, ref, negated = tree
        return f"{ALIASES[ref[0]]}.{COLUMNS[ref[1]]} IS {'NOT ' if negated else ''}NULL"  # type: ignore[index]
    if kind == "not":
        return f"NOT ({render(tree[1])})"  # type: ignore[arg-type]
    return f"({render(tree[1])} {str(kind).upper()} {render(tree[2])})"  # type: ignore[arg-type]


COMPARE: dict[str, Callable[[int, int], bool]] = {
    "=": lambda a, b: a == b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "!=": lambda a, b: a != b,
}


def evaluate(tree: Tree, row: Row, offsets: list[int]) -> bool | None:
    kind = tree[0]
    if kind == "cmp":
        _, op, left, right = tree
        a = row[offsets[left[0]] + left[1]]  # type: ignore[index]
        b = row[offsets[right[0]] + right[1]] if isinstance(right, tuple) else right  # type: ignore[index]
        if a is None or b is None:
            return None
        return COMPARE[op](a, b)  # type: ignore[arg-type,index]
    if kind == "null":
        _, ref, negated = tree
        is_null = row[offsets[ref[0]] + ref[1]] is None  # type: ignore[index]
        return (not is_null) if negated else is_null
    if kind == "not":
        value = evaluate(tree[1], row, offsets)  # type: ignore[arg-type]
        return None if value is None else not value
    a = evaluate(tree[1], row, offsets)  # type: ignore[arg-type]
    b = evaluate(tree[2], row, offsets)  # type: ignore[arg-type]
    if kind == "and":
        if a is False or b is False:
            return False
        return None if a is None or b is None else True
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def random_tree(rng: random.Random, tables: int, depth: int = 0) -> Tree:
    choice = rng.random()
    if depth < 2 and choice < 0.35:
        return (
            rng.choice(["and", "or"]),
            random_tree(rng, tables, depth + 1),
            random_tree(rng, tables, depth + 1),
        )
    if depth < 2 and choice < 0.45:
        return ("not", random_tree(rng, tables, depth + 1))
    ref = (rng.randrange(tables), rng.randrange(len(COLUMNS)))
    if choice < 0.6:
        return ("null", ref, rng.random() < 0.5)
    if choice < 0.75 and tables > 1:
        other = (rng.randrange(tables), rng.randrange(len(COLUMNS)))
        return ("cmp", rng.choice(list(COMPARE)), ref, other)
    return ("cmp", rng.choice(list(COMPARE)), ref, rng.randrange(0, 8))


def make_world(rng: random.Random, db: mvrs_db.Database, tables: int) -> list[list[Row]]:
    """Create tables t0..; id is the primary key, u is UNIQUE (nullable), k and v are plain."""
    data: list[list[Row]] = []
    for t in range(tables):
        db.execute(
            f"CREATE TABLE t{t} (id INTEGER PRIMARY KEY, k INTEGER, u INTEGER UNIQUE, v INTEGER)"
        )
        size = rng.choice([0, 1, 2, 3, 5, 8, 12])
        ids = rng.sample(range(8), min(size, 8))
        spare = rng.sample(range(8), 8)
        rows_: list[Row] = []
        for i, identifier in enumerate(ids):
            unique = None if rng.random() < 0.25 else spare[i]
            rows_.append(
                (
                    identifier,
                    None if rng.random() < 0.2 else rng.randrange(6),
                    unique,
                    None if rng.random() < 0.2 else rng.randrange(6),
                )
            )
        for row in rows_:
            values = ", ".join("NULL" if x is None else str(x) for x in row)
            db.execute(f"INSERT INTO t{t} VALUES ({values})")
        data.append(rows_)
    return data


def run_case(
    rng: random.Random, db: mvrs_db.Database, data: list[list[Row]], *, all_inner: bool
) -> str:
    tables = len(data)
    kinds = [
        "INNER" if all_inner else rng.choice(["INNER", "LEFT", "RIGHT", "FULL", "LEFT"])
        for _ in range(tables - 1)
    ]
    joins: list[tuple[str, int, int, int]] = []
    from_sql = f"FROM t0 {ALIASES[0]}"
    for k, kind in enumerate(kinds):
        old = rng.randrange(k + 1)
        old_column, new_column = rng.randrange(4), rng.randrange(4)
        joins.append((kind, old, old_column, new_column))
        left = f"{ALIASES[old]}.{COLUMNS[old_column]}"
        right = f"{ALIASES[k + 1]}.{COLUMNS[new_column]}"
        condition = f"{left} = {right}" if rng.random() < 0.5 else f"{right} = {left}"
        word = {
            "INNER": "JOIN",
            "LEFT": "LEFT JOIN",
            "RIGHT": "RIGHT OUTER JOIN",
            "FULL": "FULL JOIN",
        }[kind]
        from_sql += f" {word} t{k + 1} {ALIASES[k + 1]} ON {condition}"
    joined = reference_join(data, joins)
    tree = random_tree(rng, tables) if rng.random() < 0.8 else None
    offsets = [4 * t for t in range(tables)]
    expected = [row for row in joined if tree is None or evaluate(tree, row, offsets) is True]
    suffix = "" if tree is None else f" WHERE {render(tree)}"
    sql = f"SELECT * {from_sql}{suffix}"
    got = db.execute(sql).rows
    assert Counter(got) == Counter(expected), sql
    count = db.execute(f"SELECT COUNT(*) {from_sql}{suffix}").rows
    assert count == [(len(expected),)], sql
    return sql


@pytest.mark.parametrize("tables", [2, 3, 4])
@pytest.mark.parametrize("all_inner", [False, True])
def test_random_queries_match_a_nested_loop_reference(
    tmp_path: Path, tables: int, all_inner: bool
) -> None:
    rng = random.Random(1000 * tables + all_inner)
    for world in range(12):
        db = mvrs_db.connect(tmp_path / f"w{world}.mvrs", synchronous=False)
        try:
            data = make_world(rng, db, tables)
            for _ in range(14):
                run_case(rng, db, data, all_inner=all_inner)
        finally:
            db.close()


def test_the_planner_actually_uses_each_method_in_the_random_workload(tmp_path: Path) -> None:
    """Guards the differential test: it only means something if every operator ran."""
    rng = random.Random(5)
    seen: set[str] = set()
    db = mvrs_db.connect(tmp_path / "methods.mvrs", synchronous=False)
    try:
        for t in range(3):
            db.execute(
                f"CREATE TABLE t{t} "
                "(id INTEGER PRIMARY KEY, k INTEGER, u INTEGER UNIQUE, v INTEGER)"
            )
            sizes = [3, 400, 40]
            db.execute(
                f"INSERT INTO t{t} VALUES "
                + ", ".join(
                    f"({i}, {rng.randrange(9)}, {i}, {rng.randrange(9)})" for i in range(sizes[t])
                )
            )
        for kind in ("JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"):
            for column in ("id", "k"):
                text = "\n".join(
                    str(r[0])
                    for r in db.execute(
                        f"EXPLAIN SELECT * FROM t0 a {kind} t1 b ON a.{column} = b.{column} "
                        f"{kind} t2 c ON b.{column} = c.{column}"
                    ).rows
                )
                seen.update(
                    word
                    for word in (
                        "HashJoin(LEFT",
                        "HashJoin(RIGHT",
                        "HashJoin(FULL",
                        "IndexJoin",
                        "HashJoin(a",
                        "HashJoin(b",
                    )
                    if word in text
                )
    finally:
        db.close()
    assert {"HashJoin(LEFT", "HashJoin(RIGHT", "HashJoin(FULL", "IndexJoin"} <= seen, seen


def test_left_join_by_index_keeps_unmatched_rows(tmp_path: Path) -> None:
    db = mvrs_db.connect(tmp_path / "lj.mvrs", synchronous=False)
    try:
        db.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, dept INTEGER)")
        db.execute("INSERT INTO dept VALUES " + ", ".join(f"({i}, 'd{i}')" for i in range(300)))
        db.execute(
            "INSERT INTO emp VALUES "
            + ", ".join(f"({i}, {i * 7 % 340 if i % 5 else 'NULL'})" for i in range(400))
        )
        sql = "SELECT e.id, d.name FROM emp e LEFT JOIN dept d ON e.dept = d.id WHERE e.id < 12"
        text = "\n".join(str(r[0]) for r in db.execute(f"EXPLAIN {sql}").rows)
        assert "IndexJoin(LEFT, e.dept = d.id; look up dept.id)" in text
        got = sorted(db.execute(sql).rows, key=repr)
        expected = sorted(
            [
                (i, None if (i % 5 == 0 or i * 7 % 340 >= 300) else f"d{i * 7 % 340}")
                for i in range(12)
            ],
            key=repr,
        )
        assert got == expected
    finally:
        db.close()


def test_an_inner_chain_starts_from_the_smallest_table(tmp_path: Path) -> None:
    db = mvrs_db.connect(tmp_path / "order.mvrs", synchronous=False)
    try:
        db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, m INTEGER)")
        db.execute("CREATE TABLE mid (id INTEGER PRIMARY KEY, s INTEGER)")
        db.execute("CREATE TABLE tiny (id INTEGER PRIMARY KEY, tag TEXT)")
        db.execute("INSERT INTO big VALUES " + ", ".join(f"({i}, {i % 50})" for i in range(1500)))
        db.execute("INSERT INTO mid VALUES " + ", ".join(f"({i}, {i % 5})" for i in range(50)))
        db.execute("INSERT INTO tiny VALUES (0, 'x'), (1, 'y'), (2, 'z')")
        sql = (
            "SELECT * FROM big b JOIN mid m ON b.m = m.id JOIN tiny t ON m.s = t.id "
            "WHERE t.tag = 'x'"
        )
        text = [str(r[0]) for r in db.execute(f"EXPLAIN {sql}").rows]
        scans = [line.strip() for line in text if "Scan(" in line]
        assert "tiny" in scans[0], text  # the filtered, smallest table is read first
        got = db.execute(sql)
        assert got.columns[:2] == ("b.id", "b.m")  # still listed in the order written
        assert len(got.rows) == 300  # m.s = 0 for 10 of 50 mids, each with 30 bigs
        assert all(row[-1] == "x" for row in got.rows)
    finally:
        db.close()
