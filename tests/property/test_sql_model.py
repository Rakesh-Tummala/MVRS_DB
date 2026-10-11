"""Model-based test of the whole engine through SQL.

Hypothesis generates random sequences of statements, transactions and
close/reopen cycles. Every result is compared with a plain Python dictionary
that implements the same rules. After each sequence the database's own
integrity check must pass.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import mvrs_db
from mvrs_db.errors import ConstraintViolationError

Row = tuple[int, str | None, int | None]

IDS = st.integers(0, 25)
NAMES = st.one_of(st.none(), st.sampled_from(["a", "b", "c", "d", "e", "long" * 40]))
NUMBERS = st.one_of(st.none(), st.integers(-3, 3))

OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("insert"), IDS, NAMES, NUMBERS),
        st.tuples(st.just("rename"), IDS, NAMES),
        st.tuples(st.just("renumber"), IDS, IDS, NUMBERS),
        st.tuples(st.just("delete"), IDS, IDS),
        st.tuples(st.just("select"), st.integers(0, 7), IDS, NUMBERS),
        st.tuples(st.just("begin")),
        st.tuples(st.just("commit")),
        st.tuples(st.just("rollback")),
        st.tuples(st.just("reopen")),
    ),
    max_size=60,
)


def literal(value: object) -> str:
    if value is None:
        return "NULL"
    return f"'{value}'" if isinstance(value, str) else str(value)


def null_first(value: int | None) -> tuple[bool, int]:
    return (value is not None, value or 0)


@settings(max_examples=120, deadline=None)
@given(operations=OPERATIONS)
def test_engine_matches_a_dictionary_model(
    tmp_path_factory: pytest.TempPathFactory,
    operations: list[tuple],  # type: ignore[type-arg]
) -> None:
    path = tmp_path_factory.mktemp("model") / "model.mvrs"
    db = mvrs_db.connect(path, synchronous=False, buffer_pages=16)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT UNIQUE, n INTEGER)")
    model: dict[int, Row] = {}
    saved: dict[int, Row] | None = None  # the model as of BEGIN, while a transaction is open

    for operation in operations:
        kind = operation[0]
        if kind == "insert":
            _, key, name, number = operation
            taken = key in model or (name is not None and any(r[1] == name for r in model.values()))
            statement = f"INSERT INTO t VALUES ({key}, {literal(name)}, {literal(number)})"
            if taken:
                with pytest.raises(ConstraintViolationError):
                    db.execute(statement)
            else:
                assert db.execute(statement).rowcount == 1
                model[key] = (key, name, number)

        elif kind == "rename":
            _, key, name = operation
            statement = f"UPDATE t SET name = {literal(name)} WHERE id = {key}"
            clash = (
                key in model
                and name is not None
                and any(r[1] == name and r[0] != key for r in model.values())
            )
            if clash:
                with pytest.raises(ConstraintViolationError):
                    db.execute(statement)
            else:
                assert db.execute(statement).rowcount == (1 if key in model else 0)
                if key in model:
                    model[key] = (key, name, model[key][2])

        elif kind == "renumber":
            _, low, high, number = operation
            result = db.execute(
                f"UPDATE t SET n = {literal(number)} WHERE id >= {low} AND id <= {high}"
            )
            targets = [k for k in model if low <= k <= high]
            assert result.rowcount == len(targets)
            for k in targets:
                model[k] = (k, model[k][1], number)

        elif kind == "delete":
            _, low, high = operation
            result = db.execute(f"DELETE FROM t WHERE id >= {low} AND id < {high}")
            targets = [k for k in model if low <= k < high]
            assert result.rowcount == len(targets)
            for k in targets:
                del model[k]

        elif kind == "select":
            _, variant, key, number = operation
            rows = [model[k] for k in sorted(model)]
            if variant == 0:
                assert db.execute("SELECT * FROM t ORDER BY id").rows == rows
            elif variant == 1:
                expected = [r for r in rows if r[0] == key]
                assert db.execute(f"SELECT * FROM t WHERE id = {key}").rows == expected
            elif variant == 2:
                expected = [r for r in rows if r[0] > key]
                assert db.execute(f"SELECT * FROM t WHERE id > {key} ORDER BY id").rows == expected
            elif variant == 3:
                expected = [r for r in rows if r[2] is None]
                assert db.execute("SELECT * FROM t WHERE n IS NULL ORDER BY id").rows == expected
            elif variant == 4 and number is not None:
                expected = [r for r in rows if r[2] is not None and r[2] >= number]
                found = db.execute(f"SELECT * FROM t WHERE n >= {number} ORDER BY id").rows
                assert found == expected
            elif variant == 5:
                expected = sorted(rows, key=lambda r: (null_first(r[2]), r[0]))[:5]
                assert db.execute("SELECT * FROM t ORDER BY n, id LIMIT 5").rows == expected
            elif variant == 6:
                names = sorted(r[1] for r in rows if r[1] is not None)
                found = db.execute("SELECT name FROM t WHERE name >= '' ORDER BY name").rows
                assert [r[0] for r in found] == names
            else:
                expected = [r for r in rows if r[1] == "a" or (r[2] is not None and r[2] < 0)]
                found = db.execute("SELECT * FROM t WHERE name = 'a' OR n < 0 ORDER BY id").rows
                assert found == expected

        elif kind == "begin":
            if saved is None:
                db.execute("BEGIN")
                saved = dict(model)
        elif kind == "commit":
            if saved is not None:
                db.execute("COMMIT")
                saved = None
        elif kind == "rollback":
            if saved is not None:
                db.execute("ROLLBACK")
                model, saved = saved, None
        else:  # reopen: an open transaction is rolled back by close()
            db.close()
            if saved is not None:
                model, saved = saved, None
            db = mvrs_db.connect(path, synchronous=False, buffer_pages=16)

    if saved is not None:
        db.execute("COMMIT")
    assert db.execute("SELECT * FROM t ORDER BY id").rows == [model[k] for k in sorted(model)]
    assert db.check().tables == {"t": len(model)}
    db.close()
