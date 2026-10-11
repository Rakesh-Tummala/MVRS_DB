"""Command-line interface and the demo script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

typer_testing = pytest.importorskip("typer.testing")

from mvrs_db.cli import app, format_result, split_statements  # noqa: E402
from mvrs_db.execution.executor import Result  # noqa: E402

runner = typer_testing.CliRunner()


def test_exec_creates_a_database_and_prints_tables(tmp_path: Path) -> None:
    path = tmp_path / "cli.mvrs"

    result = runner.invoke(
        app,
        [
            "exec",
            str(path),
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT); "
            "INSERT INTO t VALUES (1, 'a;b'), (2, NULL); -- a comment; with a semicolon\n"
            "SELECT * FROM t ORDER BY id",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ok (2 rows affected)" in result.output
    assert "id | name" in result.output
    assert "1  | a;b" in result.output
    assert "2  | NULL" in result.output
    assert "(2 rows)" in result.output


def test_exec_reports_errors_with_a_nonzero_exit_code(tmp_path: Path) -> None:
    result = runner.invoke(app, ["exec", str(tmp_path / "cli.mvrs"), "SELECT * FROM missing"])

    assert result.exit_code == 1
    assert "no such table: missing" in result.output


def test_explain_is_printed_as_a_tree(tmp_path: Path) -> None:
    path = tmp_path / "cli.mvrs"
    runner.invoke(app, ["exec", str(path), "CREATE TABLE t (id INTEGER PRIMARY KEY)"])

    result = runner.invoke(app, ["exec", str(path), "EXPLAIN SELECT * FROM t WHERE id = 1"])

    assert result.exit_code == 0
    assert result.output.splitlines()[0].startswith("Project(id)")
    assert "  Filter(id = 1)" in result.output


def test_info_and_check(tmp_path: Path) -> None:
    path = tmp_path / "cli.mvrs"
    runner.invoke(
        app,
        ["exec", str(path), "CREATE TABLE t (id INTEGER PRIMARY KEY); INSERT INTO t VALUES (1)"],
    )

    info = runner.invoke(app, ["info", str(path)])
    check = runner.invoke(app, ["check", str(path)])

    assert info.exit_code == 0 and "table        t: 1 rows" in info.output
    assert "page size    4096 bytes" in info.output
    assert check.exit_code == 0 and check.output.startswith("ok:")


def test_info_refuses_missing_and_foreign_files(tmp_path: Path) -> None:
    other = tmp_path / "notes.txt"
    other.write_text("hello")

    assert runner.invoke(app, ["info", str(tmp_path / "missing.mvrs")]).exit_code == 1
    result = runner.invoke(app, ["check", str(other)])

    assert result.exit_code == 1
    assert "magic number" in result.output
    assert other.read_text() == "hello"


def test_check_reports_corruption(tmp_path: Path) -> None:
    path = tmp_path / "cli.mvrs"
    values = ", ".join(f"({i}, '{'x' * 200}')" for i in range(200))
    runner.invoke(
        app,
        [
            "exec",
            str(path),
            f"CREATE TABLE t (id INTEGER PRIMARY KEY, b TEXT); INSERT INTO t VALUES {values}",
        ],
    )
    raw = bytearray(path.read_bytes())
    raw[5 * 4096 + 2000] ^= 0xFF
    path.write_bytes(raw)

    result = runner.invoke(app, ["check", str(path)])

    assert result.exit_code == 2
    assert "corrupt:" in result.output and "checksum" in result.output


def test_shell_session(tmp_path: Path) -> None:
    script = "\n".join(
        [
            "CREATE TABLE t (id INTEGER PRIMARY KEY,",
            "  name TEXT);",
            "INSERT INTO t VALUES (1, 'x');",
            ".tables",
            "SELECT * FROM nope;",
            "SELECT name FROM t;",
            ".check",
            ".bogus",
            ".quit",
        ]
    )

    result = runner.invoke(app, ["shell", str(tmp_path / "cli.mvrs")], input=script + "\n")

    assert result.exit_code == 0, result.output
    assert "no such table: nope" in result.output
    assert "(1 row)" in result.output
    assert "ok: " in result.output
    assert "unknown command .bogus" in result.output


def test_split_statements() -> None:
    assert split_statements("a; b ;; c") == ["a", "b", "c"]
    assert split_statements("INSERT INTO t VALUES ('x;y', 'it''s;'); SELECT 1") == [
        "INSERT INTO t VALUES ('x;y', 'it''s;')",
        "SELECT 1",
    ]
    assert split_statements("SELECT 1 -- note; still a comment\n; SELECT 2") == [
        "SELECT 1",
        "SELECT 2",
    ]
    assert split_statements("  \n ; ") == []


def test_format_result() -> None:
    assert format_result(Result(rowcount=1)) == "ok (1 row affected)"
    table = format_result(Result(("id", "name"), [(1, "long name"), (22, None)], 2))
    assert table.splitlines() == [
        "id | name     ",
        "---+----------",
        "1  | long name",
        "22 | NULL     ",
        "(2 rows)",
    ]


def test_demo_script_runs_and_shows_recovery() -> None:
    demo = Path(__file__).resolve().parents[2] / "examples" / "demo.py"

    completed = subprocess.run(
        [sys.executable, str(demo)], capture_output=True, text=True, timeout=120
    )

    assert completed.returncode == 0, completed.stderr
    assert "*** killed here ***" in completed.stdout
    assert "(1, 'asha', 400)" in completed.stdout  # the committed transfer survived
    assert "total money: 1250" in completed.stdout  # the half-finished one did not
    assert "integrity check passed" in completed.stdout
    assert not list(demo.parent.glob("mvrs_db-demo-*"))  # it cleaned up after itself
