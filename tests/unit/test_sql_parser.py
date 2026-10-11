"""Lexer and parser."""

from __future__ import annotations

import pytest

from mvrs_db.errors import QueryTooLargeError, SqlSyntaxError
from mvrs_db.sql import ast
from mvrs_db.sql.lexer import TokenKind, tokenize
from mvrs_db.sql.parser import parse

C, L = ast.ColumnRef, ast.Literal


def kinds_and_values(text: str) -> list[tuple[TokenKind, str]]:
    return [(t.kind, t.value) for t in tokenize(text)[:-1]]


# ---------------------------------------------------------------------- lexer


def test_token_kinds() -> None:
    assert kinds_and_values("select Name, 42 3.5 1e3 .5 'it''s' <= <> != ( ) * ; -") == [
        (TokenKind.KEYWORD, "SELECT"),
        (TokenKind.IDENT, "name"),
        (TokenKind.SYMBOL, ","),
        (TokenKind.INTEGER, "42"),
        (TokenKind.REAL, "3.5"),
        (TokenKind.REAL, "1e3"),
        (TokenKind.REAL, ".5"),
        (TokenKind.STRING, "it's"),
        (TokenKind.SYMBOL, "<="),
        (TokenKind.SYMBOL, "<>"),
        (TokenKind.SYMBOL, "!="),
        (TokenKind.SYMBOL, "("),
        (TokenKind.SYMBOL, ")"),
        (TokenKind.SYMBOL, "*"),
        (TokenKind.SYMBOL, ";"),
        (TokenKind.SYMBOL, "-"),
    ]


def test_comments_and_whitespace_are_skipped() -> None:
    assert kinds_and_values("SELECT -- the columns\n\t*  -- trailing") == [
        (TokenKind.KEYWORD, "SELECT"),
        (TokenKind.SYMBOL, "*"),
    ]


def test_strings_keep_their_contents_verbatim() -> None:
    assert kinds_and_values("'-- not a comment' 'line\nbreak' '' 'ünï'") == [
        (TokenKind.STRING, "-- not a comment"),
        (TokenKind.STRING, "line\nbreak"),
        (TokenKind.STRING, ""),
        (TokenKind.STRING, "ünï"),
    ]


def test_token_positions_and_eof() -> None:
    tokens = tokenize("ab  cd")
    assert [t.position for t in tokens] == [0, 4, 6]
    assert tokens[-1].kind is TokenKind.EOF


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("SELECT 'open", "never closed"),
        ("SELECT @x", "unexpected character '@'"),
        ('SELECT "name" FROM t', "quoted identifiers"),
        ("SELECT 12abc", "malformed number"),
        ("SELECT " + "a" * 64, "longer than 63"),
        ("SELECT *\nFROM t WHERE x = #", "line 2, column 18"),
    ],
)
def test_lexer_errors(text: str, message: str) -> None:
    with pytest.raises(SqlSyntaxError, match=message):
        tokenize(text)


def test_statement_size_limit() -> None:
    tokenize("SELECT '" + "x" * 1_000_000 + "'")
    with pytest.raises(QueryTooLargeError):
        tokenize("SELECT '" + "x" * 1_048_576 + "'")


# ---------------------------------------------------------------------- statements


def test_create_table() -> None:
    statement = parse(
        "create table Users (ID integer primary key, name TEXT NOT NULL UNIQUE, "
        "score float, n int);"
    )
    assert statement == ast.CreateTable(
        "users",
        (
            ast.ColumnDef("id", "INTEGER", primary_key=True),
            ast.ColumnDef("name", "TEXT", not_null=True, unique=True),
            ast.ColumnDef("score", "REAL"),
            ast.ColumnDef("n", "INTEGER"),
        ),
    )


def test_drop_table() -> None:
    assert parse("DROP TABLE users") == ast.DropTable("users")


def test_insert() -> None:
    assert parse("INSERT INTO t VALUES (1, 'a', NULL, -2.5), (+3, '', null, 1e2)") == ast.Insert(
        "t", None, ((1, "a", None, -2.5), (3, "", None, 100.0))
    )
    assert parse("INSERT INTO t (b, a) VALUES (1, 2)") == ast.Insert("t", ("b", "a"), ((1, 2),))


def test_select() -> None:
    assert parse("SELECT * FROM t") == ast.Select("t", None)
    assert parse("SELECT a, b FROM t WHERE a > 1 ORDER BY b DESC, a ASC, c LIMIT 5") == ast.Select(
        "t",
        ("a", "b"),
        ast.Compare(">", C("a"), L(1)),
        (ast.OrderItem("b", True), ast.OrderItem("a", False), ast.OrderItem("c", False)),
        5,
    )


def test_update_and_delete() -> None:
    assert parse("UPDATE t SET a = 1, b = NULL WHERE c = 'x'") == ast.Update(
        "t", (("a", 1), ("b", None)), ast.Compare("=", C("c"), L("x"))
    )
    assert parse("DELETE FROM t") == ast.Delete("t", None)
    assert parse("DELETE FROM t WHERE a IS NOT NULL") == ast.Delete(
        "t", ast.IsNull(C("a"), negated=True)
    )


def test_explain_and_transaction_control() -> None:
    assert parse("EXPLAIN SELECT * FROM t") == ast.Explain(ast.Select("t", None))
    assert isinstance(parse("EXPLAIN DELETE FROM t").statement, ast.Delete)  # type: ignore[union-attr]
    assert isinstance(parse("EXPLAIN UPDATE t SET a = 1").statement, ast.Update)  # type: ignore[union-attr]
    assert parse("begin") == ast.Begin()
    assert parse("COMMIT;") == ast.Commit()
    assert parse("Rollback") == ast.Rollback()


def test_integer_limits() -> None:
    assert parse("INSERT INTO t VALUES (-9223372036854775808, 9223372036854775807)").rows == (  # type: ignore[union-attr]
        (-(2**63), 2**63 - 1),
    )
    with pytest.raises(SqlSyntaxError, match="64 bits"):
        parse("INSERT INTO t VALUES (9223372036854775808)")
    with pytest.raises(SqlSyntaxError, match="out of range"):
        parse("INSERT INTO t VALUES (1e999)")


# ---------------------------------------------------------------------- expressions


def where(text: str) -> ast.Expr:
    statement = parse(f"SELECT * FROM t WHERE {text}")
    assert isinstance(statement, ast.Select) and statement.where is not None
    return statement.where


def test_and_binds_tighter_than_or() -> None:
    a, b, c = (ast.Compare("=", C(n), L(1)) for n in "abc")
    assert where("a = 1 OR b = 1 AND c = 1") == ast.Or(a, ast.And(b, c))
    assert where("a = 1 AND b = 1 OR c = 1") == ast.Or(ast.And(a, b), c)
    assert where("(a = 1 OR b = 1) AND c = 1") == ast.And(ast.Or(a, b), c)


def test_not_binds_tighter_than_and() -> None:
    a, b = (ast.Compare("=", C(n), L(1)) for n in "ab")
    assert where("NOT a = 1 AND b = 1") == ast.And(ast.Not(a), b)
    assert where("NOT (a = 1 AND b = 1)") == ast.Not(ast.And(a, b))
    assert where("NOT NOT a = 1") == ast.Not(ast.Not(a))


def test_and_or_are_left_associative() -> None:
    a, b, c = (ast.Compare("=", C(n), L(1)) for n in "abc")
    assert where("a = 1 AND b = 1 AND c = 1") == ast.And(ast.And(a, b), c)
    assert where("a = 1 OR b = 1 OR c = 1") == ast.Or(ast.Or(a, b), c)


def test_comparison_operators_and_operand_forms() -> None:
    assert where("a <> 1") == ast.Compare("!=", C("a"), L(1))
    assert where("1 <= a") == ast.Compare("<=", L(1), C("a"))
    assert where("a >= b") == ast.Compare(">=", C("a"), C("b"))
    assert where("a = NULL") == ast.Compare("=", C("a"), L(None))
    assert where("a IS NULL") == ast.IsNull(C("a"), negated=False)
    assert where("a > -5") == ast.Compare(">", C("a"), L(-5))


def test_deep_nesting_is_limited_not_a_crash() -> None:
    assert where("(" * 150 + "a = 1" + ")" * 150) == ast.Compare("=", C("a"), L(1))
    with pytest.raises(SqlSyntaxError, match="nested"):
        where("(" * 500 + "a = 1" + ")" * 500)
    with pytest.raises(SqlSyntaxError, match="nested"):
        where("NOT " * 500 + "a = 1")


# ---------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("   -- only a comment", "empty"),
        ("SELECT", "expected column name or \\*"),
        ("SELECT * FROM", "expected table name"),
        ("SELECT * FROM t WHERE", "expected a value"),
        ("SELECT * FROM t WHERE a", "expected a comparison operator or IS NULL"),
        ("SELECT * FROM t WHERE a = ", "expected a value"),
        ("SELECT * FROM t WHERE (a = 1", "expected '\\)'"),
        ("SELECT * FROM t LIMIT -1", "non-negative integer"),
        ("SELECT * FROM t LIMIT 1.5", "non-negative integer"),
        ("SELECT * FROM t alias extra", "expected end of statement"),
        ("SELECT * FROM t; SELECT * FROM t", "expected end of statement"),
        ("SELECT a b FROM t", "expected FROM"),
        ("INSERT INTO t VALUES ()", "expected a value"),
        ("INSERT INTO t VALUES (a)", "expected a value"),
        ("INSERT t VALUES (1)", "expected INTO"),
        ("CREATE TABLE t ()", "expected column name"),
        ("CREATE TABLE t (a)", "expected a column type"),
        ("CREATE TABLE t (a BLOB)", "BLOB is not supported"),
        ("CREATE TABLE select (a INT)", "reserved word"),
        ("UPDATE t SET a = b", "only literal values"),
        ("UPDATE t SET a 1", "expected '='"),
        ("EXPLAIN INSERT INTO t VALUES (1)", "expected SELECT, UPDATE or DELETE"),
        ("FROB t", "expected a statement"),
        ("42", "expected a statement"),
        ("SELECT * FROM t WHERE a = - 'x'", "a number after the sign"),
    ],
)
def test_syntax_errors(text: str, message: str) -> None:
    with pytest.raises(SqlSyntaxError, match=message):
        parse(text)


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("SELECT * FROM a LEFT JOIN b ON a.x = b.y", "LEFT"),
        ("SELECT DISTINCT a FROM t", "DISTINCT"),
        ("SELECT COUNT(DISTINCT a) FROM t", "DISTINCT"),
        ("SELECT a FROM t GROUP BY a HAVING a > 1", "HAVING"),
        ("SELECT * FROM t WHERE a IN (1, 2)", "IN"),
        ("SELECT * FROM t WHERE a LIKE 'x%'", "LIKE"),
        ("SELECT * FROM t WHERE a BETWEEN 1 AND 2", "BETWEEN"),
        ("SELECT * FROM t LIMIT 5 OFFSET 2", "OFFSET"),
        ("SELECT a AS b FROM t", "AS"),
        ("ALTER TABLE t ADD c INT", "ALTER"),
        ("CREATE INDEX i ON t (a)", "INDEX"),
        ("CREATE TABLE t (a INT DEFAULT 1)", "DEFAULT"),
        ("SELECT * FROM t WHERE a = TRUE", "TRUE"),
        ("BEGIN TRANSACTION", "TRANSACTION"),
    ],
)
def test_unsupported_sql_is_named_not_misread(text: str, word: str) -> None:
    with pytest.raises(SqlSyntaxError, match=f"{word} is not supported"):
        parse(text)


def test_qualified_names_are_kept_as_written_for_the_planner_to_resolve() -> None:
    statement = parse("SELECT t.a FROM t WHERE t.a = 1 AND 2 < t.b")
    assert isinstance(statement, ast.Select)
    assert statement.columns == ("t.a",)
    assert statement.where == ast.And(
        ast.Compare("=", C("t.a"), L(1)), ast.Compare("<", L(2), C("t.b"))
    )


def test_a_table_alias_is_recorded() -> None:
    statement = parse("SELECT x.a FROM t x WHERE x.a = 1")
    assert isinstance(statement, ast.Select)
    assert (statement.table, statement.alias) == ("t", "x")
    assert parse("SELECT a FROM t").alias is None  # type: ignore[union-attr]


def test_qualified_names_are_rejected_where_they_make_no_sense() -> None:
    for text in (
        "INSERT INTO t (t.a) VALUES (1)",
        "UPDATE t SET t.a = 1",
        "CREATE TABLE s.t (a INT PRIMARY KEY)",
    ):
        with pytest.raises(SqlSyntaxError, match="not allowed here"):
            parse(text)


def test_arithmetic_is_rejected() -> None:
    with pytest.raises(SqlSyntaxError):
        parse("SELECT * FROM t WHERE a + 1 = 2")
    with pytest.raises(SqlSyntaxError):
        parse("SELECT a * 2 FROM t")
