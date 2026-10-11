"""Saved vector indexes: loaded when valid, ignored (never trusted) when not."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import mvrs_db
from mvrs_db.vector import distance, snapshot
from mvrs_db.vector.hnsw import HnswIndex

np = pytest.importorskip("numpy")

DIM = 6


def vec(rng: random.Random) -> tuple[float, ...]:
    return distance.as_float32([rng.gauss(0, 1) for _ in range(DIM)])


def literal(v: tuple[float, ...]) -> str:
    return "[" + ", ".join(repr(float(x)) for x in v) + "]"


def make_index(n: int, metric: str = distance.L2, removed: int = 0) -> HnswIndex:
    rng = random.Random(3)
    index = HnswIndex(DIM, metric)
    for i in range(n):
        index.add((i // 7 + 1, i % 7), vec(rng))
    for i in range(removed):
        index.remove((i // 7 + 1, i % 7))
    return index


def roundtrip(index: HnswIndex) -> HnswIndex:
    key = snapshot.SnapshotKey(1, 2, 3, 1, DIM, index.metric, (4, 5, 6))
    decoded_key, arrays = snapshot.decode(snapshot.encode(key, index.to_arrays()))
    assert decoded_key == key
    return HnswIndex.from_arrays(DIM, index.metric, arrays)


# ------------------------------------------------------------ the graph round trip


@pytest.mark.parametrize("metric", [distance.L2, distance.COSINE])
def test_round_trip_gives_identical_search_results(metric: str) -> None:
    index = make_index(400, metric, removed=40)
    loaded = roundtrip(index)
    rng = random.Random(8)
    for _ in range(25):
        q = vec(rng)
        assert loaded.search(q, 10, 80) == index.search(q, 10, 80)
    assert len(loaded) == len(index)
    assert loaded.dead_fraction == index.dead_fraction
    assert not loaded.dirty


def test_a_loaded_index_accepts_more_inserts_and_removals() -> None:
    loaded = roundtrip(make_index(200))
    rng = random.Random(1)
    new = vec(rng)
    assert loaded.add((900, 1), new)
    assert loaded.dirty
    assert loaded.search(new, 1, 50) == [(900, 1)]
    assert loaded.remove((900, 1))
    assert loaded.search(new, 1, 50) != [(900, 1)]


def test_empty_index_round_trips() -> None:
    loaded = roundtrip(HnswIndex(DIM, distance.L2))
    assert len(loaded) == 0 and loaded.search(vec(random.Random(0)), 3, 10) == []


# ------------------------------------------------------------ hostile input


def encoded() -> bytes:
    key = snapshot.SnapshotKey(1, 2, 3, 1, DIM, distance.L2, (4, 5, 6))
    return snapshot.encode(key, make_index(120, removed=10).to_arrays())


def load_or_reject(data: bytes) -> None:
    """Decoding must either succeed with a safe index or raise the two expected errors."""
    try:
        key, arrays = snapshot.decode(data)
        index = HnswIndex.from_arrays(key.dimension, key.metric, arrays)
    except (snapshot.SnapshotError, ValueError):
        return
    index.search(vec(random.Random(0)), 5, 20)  # must not crash or loop


def test_every_truncation_is_rejected() -> None:
    data = encoded()
    for cut in list(range(0, 200)) + list(range(200, len(data), 97)):
        with pytest.raises(snapshot.SnapshotError):
            snapshot.decode(data[:cut])


def test_any_flipped_byte_is_caught_by_the_checksum() -> None:
    data = bytearray(encoded())
    rng = random.Random(5)
    for _ in range(300):
        broken = bytearray(data)
        broken[rng.randrange(len(broken))] ^= 1 << rng.randrange(8)
        with pytest.raises(snapshot.SnapshotError):
            snapshot.decode(bytes(broken))


def test_a_valid_checksum_over_a_nonsense_graph_is_still_rejected() -> None:
    """The checksum only proves the file is what was written; the graph must still make sense."""
    import struct
    import zlib

    key = snapshot.SnapshotKey(1, 2, 3, 1, DIM, distance.L2, (4, 5, 6))
    base = make_index(60).to_arrays()
    mutations = {
        "link out of range": lambda a: a["neighbors"].__setitem__(0, 10**6),
        "entry out of range": lambda a: a.__setitem__("entry", 10**6),
        "wrong top layer": lambda a: a.__setitem__("top_layer", a["top_layer"] + 3),
        "bad dead flag": lambda a: a["dead"].__setitem__(0, 7),
        "zero layers": lambda a: a["node_layers"].__setitem__(1, 0),
        "nan vector": lambda a: a["vectors"].__setitem__((0, 0), float("nan")),
    }
    for name, mutate in mutations.items():
        arrays = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in base.items()}
        mutate(arrays)
        data = snapshot.encode(key, arrays)
        # re-sign in case encode changed lengths (it does not), to be sure only the graph is wrong
        assert zlib.crc32(data[:-4]) == struct.unpack("<I", data[-4:])[0]
        decoded_key, decoded = snapshot.decode(data)
        with pytest.raises(ValueError):
            HnswIndex.from_arrays(decoded_key.dimension, decoded_key.metric, decoded), name


def test_random_garbage_never_crashes_the_loader() -> None:
    rng = random.Random(2)
    for _ in range(200):
        load_or_reject(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300))))
    good = encoded()
    for _ in range(200):
        load_or_reject(
            good[: rng.randrange(len(good))] + bytes(rng.randrange(256) for _ in range(40))
        )


# ------------------------------------------------------------ through the database


def fill(path: Path, n: int = 400) -> dict[int, tuple[float, ...]]:
    rng = random.Random(4)
    rows = {i: vec(rng) for i in range(n)}
    with mvrs_db.connect(path) as db:
        db.execute(f"CREATE TABLE docs (id INTEGER PRIMARY KEY, v VECTOR({DIM}) HNSW)")
        values = ", ".join(f"({i}, {literal(v)})" for i, v in rows.items())
        db.execute(f"INSERT INTO docs VALUES {values}")
    return rows


def ask(db: mvrs_db.Database, q: tuple[float, ...], k: int = 5) -> list[int]:
    db.vector.min_rows = 0
    db.vector.ef_search = 300
    sql = f"SELECT id FROM docs ORDER BY v <-> {literal(q)} LIMIT {k}"
    return [r[0] for r in db.execute(sql).rows]  # type: ignore[misc]


def explain(db: mvrs_db.Database, q: tuple[float, ...]) -> str:
    sql = f"EXPLAIN SELECT id FROM docs ORDER BY v <-> {literal(q)} LIMIT 5"
    return "\n".join(r[0] for r in db.execute(sql).rows)  # type: ignore[misc]


def snapshots(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".ann.*"))


def test_snapshot_is_saved_on_close_and_used_without_rebuilding(tmp_path: Path) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    q = rows[10]
    with mvrs_db.connect(path) as db:
        built = ask(db, q)
    assert len(snapshots(path)) == 1

    with mvrs_db.connect(path) as db:
        table = db._catalog.get("docs")
        db.vector.min_rows = 0
        assert "saved copy on disk" in explain(db, q)
        calls = []
        original = HnswIndex.add
        HnswIndex.add = lambda self, *a, **k: calls.append(1) or original(self, *a, **k)  # type: ignore[method-assign]
        try:
            assert ask(db, q) == built
        finally:
            HnswIndex.add = original  # type: ignore[method-assign]
        assert calls == []  # loaded, not rebuilt
        assert table.ann_built(1)


def test_a_change_after_saving_makes_the_snapshot_stale(tmp_path: Path) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    with mvrs_db.connect(path) as db:
        ask(db, rows[1])
    # Change the table without the index being loaded: the saved copy is now wrong.
    with mvrs_db.connect(path) as db:
        db.execute("DELETE FROM docs WHERE id = 1")
    with mvrs_db.connect(path) as db:
        found = ask(db, rows[1], 20)
        assert 1 not in found  # a stale snapshot would still return the deleted row's id


def test_index_updates_are_saved_and_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    new = vec(random.Random(77))
    with mvrs_db.connect(path) as db:
        ask(db, new)
        db.execute(f"INSERT INTO docs VALUES (9000, {literal(new)})")
        db.execute("DELETE FROM docs WHERE id = 5")
    with mvrs_db.connect(path) as db:
        assert ask(db, new, 1) == [9000]
        assert 5 not in ask(db, rows[5], 20)
        assert "saved copy" not in explain(db, new)  # already in memory now
        assert db.check().tables == {"docs": 400}


def test_rolled_back_changes_are_not_saved(tmp_path: Path) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    ghost = vec(random.Random(78))
    with mvrs_db.connect(path) as db:
        ask(db, ghost)
        db.execute("BEGIN")
        db.execute(f"INSERT INTO docs VALUES (9100, {literal(ghost)})")
        db.execute("ROLLBACK")
    with mvrs_db.connect(path) as db:
        assert 9100 not in ask(db, ghost, 10)
        assert ask(db, rows[3], 1) == [3]


def test_open_transaction_at_close_saves_nothing_stale(tmp_path: Path) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    ghost = vec(random.Random(79))
    db = mvrs_db.connect(path)
    ask(db, ghost)
    db.execute("BEGIN")
    db.execute(f"INSERT INTO docs VALUES (9200, {literal(ghost)})")
    db.close()  # abandons the transaction
    with mvrs_db.connect(path) as db2:
        assert 9200 not in ask(db2, ghost, 10)
        assert ask(db2, rows[2], 1) == [2]


@pytest.mark.parametrize("damage", ["truncate", "flip", "empty", "delete", "garbage"])
def test_a_damaged_snapshot_falls_back_to_rebuilding(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    with mvrs_db.connect(path) as db:
        expected = ask(db, rows[7])
    (file,) = snapshots(path)
    data = bytearray(file.read_bytes())
    if damage == "truncate":
        file.write_bytes(bytes(data[: len(data) // 2]))
    elif damage == "flip":
        data[len(data) // 2] ^= 0xFF
        file.write_bytes(bytes(data))
    elif damage == "empty":
        file.write_bytes(b"")
    elif damage == "garbage":
        file.write_bytes(b"x" * 500)
    else:
        file.unlink()
    with mvrs_db.connect(path) as db:
        assert ask(db, rows[7]) == expected


def test_a_snapshot_from_another_database_is_ignored(tmp_path: Path) -> None:
    a, b = tmp_path / "a.mvrs", tmp_path / "b.mvrs"
    rows_a = fill(a)
    fill(b, 400)
    with mvrs_db.connect(a) as db:
        ask(db, rows_a[0])
    (snap_a,) = snapshots(a)
    snapshots(b)
    # Put A's snapshot where B would look for its own.
    target = b.parent / (b.name + snap_a.name[len(a.name) :])
    target.write_bytes(snap_a.read_bytes())
    with mvrs_db.connect(b) as db:
        db.vector.min_rows = 0
        assert "saved copy" in explain(db, rows_a[0])  # the file exists...
        calls = []
        original = HnswIndex.add
        HnswIndex.add = lambda self, *a, **k: calls.append(1) or original(self, *a, **k)  # type: ignore[method-assign]
        try:
            ask(db, rows_a[0])
        finally:
            HnswIndex.add = original  # type: ignore[method-assign]
        assert len(calls) == 400  # ...but its db_id differs, so it was rebuilt
        assert db.check().tables == {"docs": 400}


def test_dropping_a_table_deletes_its_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    with mvrs_db.connect(path) as db:
        ask(db, rows[0])
    assert snapshots(path)
    with mvrs_db.connect(path) as db:
        db.execute("DROP TABLE docs")
    assert snapshots(path) == []


def test_save_vector_indexes_can_be_called_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "s.mvrs"
    rows = fill(path)
    with mvrs_db.connect(path) as db:
        ask(db, rows[0])
        assert db.save_vector_indexes() == 1
        assert snapshots(path)
        assert db.save_vector_indexes() == 0  # nothing changed since
        db.execute("BEGIN")
        assert db.save_vector_indexes() == 0  # never inside a transaction
        db.execute("ROLLBACK")
