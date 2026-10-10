"""Command-line interface.

    kestrel shell  mydata.kdb          interactive SQL prompt
    kestrel exec   mydata.kdb "SQL"    run statements and print the results
    kestrel info   mydata.kdb          file facts, tables and row counts
    kestrel check  mydata.kdb          verify every invariant

The CLI is a thin layer over the public API in kestreldb/database.py. The engine
does not import it.
"""

from __future__ import annotations

from pathlib import Path

import typer

import kestreldb
from kestreldb.errors import KestrelError
from kestreldb.execution.executor import Result

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="KestrelDB: a small relational database written from scratch in Python.",
)


def format_result(result: Result) -> str:
    """Render a result as an aligned text table, or a one-line summary."""
    if not result.columns:
        return f"ok ({result.rowcount} row{'s' if result.rowcount != 1 else ''} affected)"
    if result.columns == ("plan",):
        return "\n".join(str(row[0]) for row in result.rows)
    cells = [["NULL" if value is None else str(value) for value in row] for row in result.rows]
    widths = [len(name) for name in result.columns]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        " | ".join(name.ljust(width) for name, width in zip(result.columns, widths, strict=True)),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(
        " | ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        for row in cells
    )
    lines.append(f"({len(cells)} row{'s' if len(cells) != 1 else ''})")
    return "\n".join(lines)


def split_statements(text: str) -> list[str]:
    """Split on semicolons that are outside string literals and comments."""
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == "'":
                in_string = False
        elif ch == "'":
            in_string = True
            current.append(ch)
        elif ch == "-" and text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        elif ch == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    statements.append("".join(current))
    return [statement.strip() for statement in statements if statement.strip()]


def _open(path: Path, *, create: bool) -> kestreldb.Database:
    try:
        return kestreldb.connect(path, create=create)
    except KestrelError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None


@app.command("exec")
def exec_command(
    database: Path = typer.Argument(..., help="Database file (created if missing)."),
    sql: str = typer.Argument(..., help="One or more statements separated by semicolons."),
) -> None:
    """Run SQL statements and print their results."""
    with _open(database, create=True) as db:
        for statement in split_statements(sql):
            try:
                typer.echo(format_result(db.execute(statement)))
            except KestrelError as exc:
                typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1) from None


@app.command()
def shell(
    database: Path = typer.Argument(..., help="Database file (created if missing)."),
) -> None:
    """Interactive prompt. End a statement with a semicolon; .help lists commands."""
    with _open(database, create=True) as db:
        typer.echo(f"KestrelDB {kestreldb.__version__} - {database}")
        typer.echo("Type SQL ending with ';'  |  .tables  .check  .help  .quit")
        buffer = ""
        while True:
            try:
                line = input("kestrel> " if not buffer else "    ...> ")
            except (EOFError, KeyboardInterrupt):
                typer.echo()
                break
            stripped = line.strip()
            if not buffer and stripped.startswith("."):
                if stripped in (".quit", ".exit"):
                    break
                _dot_command(db, stripped)
                continue
            buffer += line + "\n"
            if ";" not in line:
                continue
            for statement in split_statements(buffer):
                try:
                    typer.echo(format_result(db.execute(statement)))
                except KestrelError as exc:
                    typer.secho(f"error: {exc}", fg=typer.colors.RED)
            buffer = ""


def _dot_command(db: kestreldb.Database, command: str) -> None:
    if command == ".tables":
        typer.echo("\n".join(db.table_names()) or "(no tables)")
    elif command == ".check":
        try:
            report = db.check()
            typer.echo(f"ok: {report.pages} pages, {report.free_pages} free, {report.tables}")
        except KestrelError as exc:
            typer.secho(f"corrupt: {exc}", fg=typer.colors.RED)
    elif command == ".help":
        typer.echo(".tables  list tables\n.check   verify the database\n.quit    leave")
    else:
        typer.echo(f"unknown command {command}; try .help")


@app.command()
def info(database: Path = typer.Argument(..., help="Database file.")) -> None:
    """Show file facts, tables and row counts."""
    with _open(database, create=False) as db:
        report = db.check()
        typer.echo(f"file         {database}")
        typer.echo(f"page size    {db.page_size} bytes")
        typer.echo(f"pages        {report.pages} ({report.free_pages} free)")
        typer.echo(f"size         {report.pages * db.page_size} bytes")
        typer.echo(f"recovered    {db.recovery.pages_restored} pages on this open")
        for name, rows in report.tables.items():
            typer.echo(f"table        {name}: {rows} rows")


@app.command()
def check(database: Path = typer.Argument(..., help="Database file.")) -> None:
    """Verify checksums, page layouts, indexes, and that no page is leaked."""
    with _open(database, create=False) as db:
        try:
            report = db.check()
        except KestrelError as exc:
            typer.secho(f"corrupt: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from None
        typer.echo(f"ok: {report.pages} pages, {sum(report.tables.values())} rows")


if __name__ == "__main__":
    app()
