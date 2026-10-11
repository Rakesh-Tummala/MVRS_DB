"""The planner's choices, seen through EXPLAIN, and that every plan gives the same answer."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import mvrs_db

ROWS = 4000


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[mvrs_db.Database]:
    database = mvrs_db.connect(tmp_path_factory.mktemp("plan") / "plan.mvrs", synchronous=False)
    database.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, code TEXT UNIQUE, grp INTEGER, "
        "price REAL, note TEXT)"
    )
    database.execute("BEGIN")
    for i in range(ROWS):
        code = "NULL" if i % 10 == 0 else f"'c{i:05d}'"
        database.execute(
            f"INSERT INTO items VALUES ({i}, {code}, {i % 7}, {i * 0.5}, 'note {i % 13}')"
        )
    database.execute("COMMIT")
    yield database
    database.close()


def plan(db: mvrs_db.Database, sql: str) -> str:
    return "\n".join(row[0] for row in db.execute(f"EXPLAIN {sql}").rows)


def test_point_lookup_uses_the_primary_key_index(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT * FROM items WHERE id = 1234")
    assert "IndexScan(items.id: id = 1234)" in text
    assert "SeqScan" not in text


def test_lookup_on_a_unique_column_uses_its_index(db: mvrs_db.Database) -> None:
    assert "IndexScan(items.code: code = 'c00123')" in plan(
        db, "SELECT * FROM items WHERE code = 'c00123'"
    )


def test_literal_on_the_left_is_recognised(db: mvrs_db.Database) -> None:
    assert "IndexScan(items.id: id > 3990)" in plan(db, "SELECT * FROM items WHERE 3990 < id")


def test_narrow_range_uses_the_index_and_wide_range_scans(db: mvrs_db.Database) -> None:
    narrow = plan(db, "SELECT * FROM items WHERE id >= 100 AND id < 120")
    wide = plan(db, "SELECT * FROM items WHERE id >= 100")

    assert "IndexScan(items.id: id >= 100 AND id < 120)" in narrow
    assert "SeqScan(items)" in wide  # most of the table matches, so the index would cost more


def test_bounds_from_several_conditions_are_combined(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT * FROM items WHERE id > 10 AND id >= 50 AND id < 70 AND id <= 90")
    assert "IndexScan(items.id: id >= 50 AND id < 70)" in text


def test_unindexed_column_always_scans(db: mvrs_db.Database) -> None:
    assert "SeqScan(items)" in plan(db, "SELECT * FROM items WHERE grp = 3")


def test_or_cannot_use_an_index(db: mvrs_db.Database) -> None:
    assert "SeqScan(items)" in plan(db, "SELECT * FROM items WHERE id = 1 OR id = 2")


def test_filter_is_kept_above_an_index_scan(db: mvrs_db.Database) -> None:
    lines = plan(db, "SELECT * FROM items WHERE id = 5 AND grp = 5").splitlines()
    assert lines[1].strip().startswith("Filter(id = 5 AND grp = 5)")
    assert lines[2].strip().startswith("IndexScan(items.id: id = 5)")


def test_order_by_on_the_scanned_index_needs_no_sort(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT * FROM items WHERE id > 3900 ORDER BY id")
    assert "IndexScan" in text
    assert "Sort" not in text and "TopN" not in text


def test_order_by_with_limit_walks_the_index_and_stops_early(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT * FROM items ORDER BY id LIMIT 5")
    assert "IndexScan(items.id: all rows, in key order)" in text
    assert "Limit(5)" in text
    assert "TopN" not in text and "SeqScan" not in text


def test_order_by_other_column_with_limit_uses_top_n(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT * FROM items ORDER BY price DESC LIMIT 5")
    assert "TopN(5; price DESC)" in text
    assert "SeqScan(items)" in text


def test_order_by_without_limit_sorts(db: mvrs_db.Database) -> None:
    assert "Sort(grp ASC, id DESC)" in plan(db, "SELECT * FROM items ORDER BY grp, id DESC")


def test_ordered_index_walk_is_not_used_for_a_nullable_column(db: mvrs_db.Database) -> None:
    # NULL codes are not in the index, so walking it would silently drop those rows.
    text = plan(db, "SELECT * FROM items ORDER BY code LIMIT 5")
    assert "SeqScan(items)" in text
    rows = db.execute("SELECT id FROM items ORDER BY code, id LIMIT 3").rows
    assert rows == [(0,), (10,), (20,)]  # the NULLs come first


def test_explain_update_and_delete(db: mvrs_db.Database) -> None:
    update = plan(db, "UPDATE items SET note = 'x' WHERE id = 7")
    delete = plan(db, "DELETE FROM items WHERE id < 3")

    assert update.splitlines()[0] == "Update(items: note = 'x')"
    assert "IndexScan(items.id: id = 7)" in update
    assert delete.splitlines()[0] == "Delete(items)"
    assert "IndexScan(items.id: id < 3)" in delete
    assert db.execute("SELECT note FROM items WHERE id = 7").rows == [("note 7",)]  # not executed


def test_estimates_are_shown(db: mvrs_db.Database) -> None:
    text = plan(db, "SELECT * FROM items WHERE id >= 1000 AND id < 1100")
    assert "rows~100 " in text


QUERIES = [
    "id = 1234",
    "id = 999999",
    "id >= 100 AND id < 120",
    "id > 3990",
    "id < 5",
    "id <= 5 AND id >= 5",
    "id < 5 AND id > 10",
    "id >= 100",
    "3990 < id",
    "id > 10 AND id >= 50 AND id < 70 AND id <= 90",
    "id >= 100 AND id < 200 AND grp = 3",
    "code = 'c00123'",
    "code >= 'c03990'",
    "code < 'c00012' AND id > 3",
    "code IS NULL AND id < 100",
    "id = 5 OR id = 6",
    "id < 2.5",
    "id = 2.5",
    "price = 10",
    "NOT id > 5",
    "id != 7 AND id < 12",
    "id = NULL",
]


@pytest.mark.parametrize("where", QUERIES)
def test_every_plan_returns_exactly_the_rows_a_full_scan_would(
    db: mvrs_db.Database, where: str
) -> None:
    """The index is an optimisation. It must never change which rows come back."""

    def expected_row(i: int) -> tuple[int, str | None, int, float, str]:
        return (i, None if i % 10 == 0 else f"c{i:05d}", i % 7, i * 0.5, f"note {i % 13}")

    env_checks = {
        "id = 1234": lambda r: r[0] == 1234,
        "id = 999999": lambda r: False,
        "id >= 100 AND id < 120": lambda r: 100 <= r[0] < 120,
        "id > 3990": lambda r: r[0] > 3990,
        "id < 5": lambda r: r[0] < 5,
        "id <= 5 AND id >= 5": lambda r: r[0] == 5,
        "id < 5 AND id > 10": lambda r: False,
        "id >= 100": lambda r: r[0] >= 100,
        "3990 < id": lambda r: r[0] > 3990,
        "id > 10 AND id >= 50 AND id < 70 AND id <= 90": lambda r: 50 <= r[0] < 70,
        "id >= 100 AND id < 200 AND grp = 3": lambda r: 100 <= r[0] < 200 and r[2] == 3,
        "code = 'c00123'": lambda r: r[1] == "c00123",
        "code >= 'c03990'": lambda r: r[1] is not None and r[1] >= "c03990",
        "code < 'c00012' AND id > 3": lambda r: r[1] is not None and r[1] < "c00012" and r[0] > 3,
        "code IS NULL AND id < 100": lambda r: r[1] is None and r[0] < 100,
        "id = 5 OR id = 6": lambda r: r[0] in (5, 6),
        "id < 2.5": lambda r: r[0] < 2.5,
        "id = 2.5": lambda r: False,
        "price = 10": lambda r: r[3] == 10,
        "NOT id > 5": lambda r: r[0] <= 5,
        "id != 7 AND id < 12": lambda r: r[0] != 7 and r[0] < 12,
        "id = NULL": lambda r: False,
    }
    keep = env_checks[where]
    expected = [row for row in map(expected_row, range(ROWS)) if keep(row)]

    assert db.execute(f"SELECT * FROM items WHERE {where} ORDER BY id").rows == expected
    assert sorted(db.execute(f"SELECT * FROM items WHERE {where}").rows) == expected
    limited = db.execute(f"SELECT * FROM items WHERE {where} ORDER BY id LIMIT 3").rows
    assert limited == expected[:3]
