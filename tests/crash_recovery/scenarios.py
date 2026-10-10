"""Deterministic workloads shared by the crash worker (child) and the tests (parent).

Each workload is a numbered sequence of transactions. Transaction k leaves the
database in a state that a pure-Python model can compute from k alone, and it
bumps a counter row to k + 1 as part of the same transaction. After a crash the
parent reads the counter to learn how many transactions are visible and then
checks that the database matches the model for exactly that many: every
visible transaction complete, and nothing of any later one.
"""

from __future__ import annotations

import kestreldb

ROWS_PER_TXN = 8


def _body(k: int, j: int) -> str:
    # Lengths from about 200 to 900 bytes, so pages fill unevenly and split often.
    return f"{k}-{j}-" + "x" * (200 + (k * 37 + j * 101) % 700)


# ---------------------------------------------------------------------- "rows"


def rows_setup(db: kestreldb.Database) -> None:
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    db.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, tag TEXT UNIQUE, body TEXT)")


def rows_txn(db: kestreldb.Database, k: int) -> None:
    """Inserts, deletes and updates across several pages and two indexes."""
    db.execute("BEGIN")
    for j in range(ROWS_PER_TXN):
        db.execute(f"INSERT INTO data VALUES ({k * 100 + j}, 'tag-{k}-{j}', '{_body(k, j)}')")
    if k >= 2:
        low = (k - 2) * 100
        db.execute(f"DELETE FROM data WHERE id >= {low} AND id < {low + 4}")
    if k >= 1:
        db.execute(f"UPDATE data SET body = 'updated by {k}' WHERE id = {(k - 1) * 100 + 5}")
    db.execute(f"UPDATE counter SET n = {k + 1} WHERE id = 1")
    db.execute("COMMIT")


def rows_model(visible: int) -> list[tuple[int, str, str]]:
    """What `data` must contain once transactions 0 .. visible-1 have committed."""
    rows: dict[int, tuple[int, str, str]] = {}
    for k in range(visible):
        for j in range(ROWS_PER_TXN):
            rows[k * 100 + j] = (k * 100 + j, f"tag-{k}-{j}", _body(k, j))
        if k >= 2:
            for j in range(4):
                del rows[(k - 2) * 100 + j]
        if k >= 1:
            key = (k - 1) * 100 + 5
            rows[key] = (key, f"tag-{k - 1}-5", f"updated by {k}")
    return [rows[key] for key in sorted(rows)]


def rows_verify(db: kestreldb.Database, visible: int) -> None:
    assert db.execute("SELECT * FROM data ORDER BY id").rows == rows_model(visible)
    assert db.table_names() == ["counter", "data"]


# ---------------------------------------------------------------------- "ddl"


def ddl_setup(db: kestreldb.Database) -> None:
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    db.execute("INSERT INTO counter VALUES (1, 0)")


def ddl_txn(db: kestreldb.Database, k: int) -> None:
    """Structural changes: create a table, fill it, drop an older one."""
    db.execute("BEGIN")
    db.execute(f"CREATE TABLE t{k} (id INTEGER PRIMARY KEY, v TEXT UNIQUE, body TEXT)")
    values = ", ".join(f"({i}, 't{k}-{i}', '{'y' * 300}')" for i in range(40))
    db.execute(f"INSERT INTO t{k} VALUES {values}")
    if k >= 2:
        db.execute(f"DROP TABLE t{k - 2}")
    db.execute(f"UPDATE counter SET n = {k + 1} WHERE id = 1")
    db.execute("COMMIT")


def ddl_verify(db: kestreldb.Database, visible: int) -> None:
    live = [k for k in (visible - 2, visible - 1) if k >= 0]
    assert db.table_names() == sorted(["counter", *(f"t{k}" for k in live)])
    for k in live:
        expected = [(i, f"t{k}-{i}", "y" * 300) for i in range(40)]
        assert db.execute(f"SELECT * FROM t{k} ORDER BY id").rows == expected


SCENARIOS = {
    "rows": (rows_setup, rows_txn, rows_verify),
    "ddl": (ddl_setup, ddl_txn, ddl_verify),
}


def visible_transactions(db: kestreldb.Database) -> int | None:
    """The counter's value, or None if the crash came before setup finished."""
    if "counter" not in db.table_names():
        return None
    rows = db.execute("SELECT n FROM counter WHERE id = 1").rows
    if not rows:
        return None
    value = rows[0][0]
    assert isinstance(value, int)
    return value
