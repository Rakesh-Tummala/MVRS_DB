"""Recursive-descent parser: tokens to a syntax tree.

Each grammar rule in docs/06-sql-grammar.md is one method. A method looks at the
current token to decide which alternative applies, consumes the tokens of that
alternative, and calls other methods for the rules it refers to.

Operator precedence comes from how the expression methods call each other:
_or() calls _and(), which calls _not(), which calls _predicate(). The deeper a
method sits in that chain, the tighter its operator binds.
"""

from __future__ import annotations

from collections.abc import Callable

from mvrs_db.errors import SqlSyntaxError
from mvrs_db.sql import ast
from mvrs_db.sql.lexer import (
    UNSUPPORTED_KEYWORDS,
    Token,
    TokenKind,
    describe_position,
    tokenize,
)

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_TYPE_NAMES = {
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "REAL": "REAL",
    "FLOAT": "REAL",
    "TEXT": "TEXT",
    "VECTOR": "VECTOR",
}
_COMPARISONS = {"=": "=", "!=": "!=", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
_MAX_NESTING = 200
_AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})


def parse(text: str) -> ast.Statement:
    """Parse exactly one statement."""
    return _Parser(text).statement()


class _Parser:
    def __init__(self, text: str) -> None:
        self._text = text
        self._tokens = tokenize(text)
        self._index = 0
        self._depth = 0

    # ------------------------------------------------------------------ token helpers

    @property
    def _current(self) -> Token:
        return self._tokens[self._index]

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        if token.kind is not TokenKind.EOF:
            self._index += 1
        return token

    def _error(self, expected: str, token: Token | None = None) -> SqlSyntaxError:
        token = token or self._current
        where = describe_position(self._text, token.position)
        if token.kind is TokenKind.KEYWORD and token.value in UNSUPPORTED_KEYWORDS:
            return SqlSyntaxError(f"{token.value} is not supported by MVRS_DB, at {where}")
        if token.kind is TokenKind.SYMBOL and token.value == ".":
            return SqlSyntaxError(
                f"a qualified name (table.column) is not allowed here, at {where}"
            )
        found = "end of statement" if token.kind is TokenKind.EOF else repr(token.value)
        return SqlSyntaxError(f"expected {expected} but found {found} at {where}")

    def _is_keyword(self, *words: str) -> bool:
        token = self._current
        return token.kind is TokenKind.KEYWORD and token.value in words

    def _accept_keyword(self, word: str) -> bool:
        if self._is_keyword(word):
            self._index += 1
            return True
        return False

    def _expect_keyword(self, word: str) -> None:
        if not self._accept_keyword(word):
            raise self._error(word)

    def _is_symbol(self, *symbols: str) -> bool:
        token = self._current
        return token.kind is TokenKind.SYMBOL and token.value in symbols

    def _accept_symbol(self, symbol: str) -> bool:
        if self._is_symbol(symbol):
            self._index += 1
            return True
        return False

    def _expect_symbol(self, symbol: str) -> None:
        if not self._accept_symbol(symbol):
            raise self._error(repr(symbol))

    def _identifier(self, what: str) -> str:
        token = self._current
        if token.kind is not TokenKind.IDENT:
            if token.kind is TokenKind.KEYWORD and token.value not in UNSUPPORTED_KEYWORDS:
                where = describe_position(self._text, token.position)
                raise SqlSyntaxError(
                    f"{token.value} is a reserved word and cannot be used as a {what}, at {where}"
                )
            raise self._error(what)
        self._index += 1
        return token.value

    # ------------------------------------------------------------------ statements

    def statement(self) -> ast.Statement:
        token = self._current
        if token.kind is TokenKind.EOF:
            raise SqlSyntaxError("the statement is empty")
        if token.kind is not TokenKind.KEYWORD:
            raise self._error("a statement")
        handlers: dict[str, Callable[[], ast.Statement]] = {
            "CREATE": self._create_table,
            "DROP": self._drop_table,
            "INSERT": self._insert,
            "SELECT": self._select,
            "UPDATE": self._update,
            "DELETE": self._delete,
            "EXPLAIN": self._explain,
            "BEGIN": lambda: self._simple(ast.Begin()),
            "COMMIT": lambda: self._simple(ast.Commit()),
            "ROLLBACK": lambda: self._simple(ast.Rollback()),
        }
        handler = handlers.get(token.value)
        if handler is None:
            raise self._error("a statement")
        result = handler()
        self._accept_symbol(";")
        if self._current.kind is not TokenKind.EOF:
            raise self._error("end of statement")
        return result

    def _simple(self, node: ast.Statement) -> ast.Statement:
        self._advance()
        return node

    def _create_table(self) -> ast.CreateTable:
        self._expect_keyword("CREATE")
        self._expect_keyword("TABLE")
        table = self._identifier("table name")
        self._expect_symbol("(")
        columns = [self._column_def()]
        while self._accept_symbol(","):
            columns.append(self._column_def())
        self._expect_symbol(")")
        return ast.CreateTable(table, tuple(columns))

    def _column_def(self) -> ast.ColumnDef:
        name = self._identifier("column name")
        token = self._current
        if token.kind is not TokenKind.KEYWORD or token.value not in _TYPE_NAMES:
            raise self._error("a column type (INTEGER, REAL, TEXT or VECTOR)")
        self._index += 1
        dimension = 0
        if token.value == "VECTOR":
            # VECTOR(n): the number of elements is part of the type.
            self._expect_symbol("(")
            size = self._current
            if size.kind is not TokenKind.INTEGER:
                raise self._error("the number of dimensions, as in VECTOR(3)")
            self._index += 1
            dimension = int(size.value)
            self._expect_symbol(")")
        primary_key = not_null = unique = False
        hnsw = ""
        while True:
            if self._accept_keyword("HNSW"):
                # HNSW builds the approximate index for Euclidean distance;
                # HNSW COSINE builds it for cosine distance.
                hnsw = "cosine" if self._accept_keyword("COSINE") else "l2"
            elif self._accept_keyword("PRIMARY"):
                self._expect_keyword("KEY")
                primary_key = True
            elif self._accept_keyword("NOT"):
                self._expect_keyword("NULL")
                not_null = True
            elif self._accept_keyword("UNIQUE"):
                unique = True
            else:
                break
        return ast.ColumnDef(
            name, _TYPE_NAMES[token.value], primary_key, not_null, unique, dimension, hnsw
        )

    def _drop_table(self) -> ast.DropTable:
        self._expect_keyword("DROP")
        self._expect_keyword("TABLE")
        return ast.DropTable(self._identifier("table name"))

    def _insert(self) -> ast.Insert:
        self._expect_keyword("INSERT")
        self._expect_keyword("INTO")
        table = self._identifier("table name")
        columns: tuple[str, ...] | None = None
        if self._accept_symbol("("):
            names = [self._identifier("column name")]
            while self._accept_symbol(","):
                names.append(self._identifier("column name"))
            self._expect_symbol(")")
            columns = tuple(names)
        self._expect_keyword("VALUES")
        rows = [self._row()]
        while self._accept_symbol(","):
            rows.append(self._row())
        return ast.Insert(table, columns, tuple(rows))

    def _row(self) -> tuple[ast.Value, ...]:
        self._expect_symbol("(")
        values = [self._literal()]
        while self._accept_symbol(","):
            values.append(self._literal())
        self._expect_symbol(")")
        return tuple(values)

    def _select(self) -> ast.Select:
        self._expect_keyword("SELECT")
        columns: tuple[ast.SelectItem, ...] | None
        if self._accept_symbol("*"):
            columns = None
        else:
            items = [self._select_item("column name or *")]
            while self._accept_symbol(","):
                items.append(self._select_item("column name"))
            columns = tuple(items)
        self._expect_keyword("FROM")
        table, alias = self._table_and_alias()
        joins: list[ast.Join] = []
        while (join := self._join()) is not None:
            joins.append(join)
        where = self._where()
        group_by: list[str] = []
        if self._accept_keyword("GROUP"):
            self._expect_keyword("BY")
            group_by.append(self._column_name("column name"))
            while self._accept_symbol(","):
                group_by.append(self._column_name("column name"))
        order_by: list[ast.OrderItem] = []
        if self._accept_keyword("ORDER"):
            self._expect_keyword("BY")
            order_by.append(self._order_item())
            while self._accept_symbol(","):
                order_by.append(self._order_item())
        limit: int | None = None
        if self._accept_keyword("LIMIT"):
            token = self._current
            if token.kind is not TokenKind.INTEGER:
                raise self._error("a non-negative integer after LIMIT")
            self._index += 1
            limit = int(token.value)
            if limit > _INT64_MAX:
                raise SqlSyntaxError("LIMIT is too large")
        return ast.Select(
            table, columns, where, tuple(order_by), limit, tuple(group_by), alias, tuple(joins)
        )

    def _table_and_alias(self) -> tuple[str, str | None]:
        """A table name, optionally followed by a short name for it: `orders o`."""
        table = self._identifier("table name")
        if self._current.kind is TokenKind.IDENT:
            return table, self._advance().value
        return table, None

    def _join(self) -> ast.Join | None:
        """[INNER | LEFT | RIGHT | FULL [OUTER]] JOIN table [alias] ON column = column."""
        kind = "INNER"
        if self._accept_keyword("INNER"):
            self._expect_keyword("JOIN")
        elif self._is_keyword("LEFT", "RIGHT", "FULL"):
            kind = self._advance().value
            self._accept_keyword("OUTER")
            self._expect_keyword("JOIN")
        elif not self._accept_keyword("JOIN"):
            return None
        table, alias = self._table_and_alias()
        self._expect_keyword("ON")
        left = self._column_name("a column name")
        if not self._accept_symbol("="):
            where = describe_position(self._text, self._current.position)
            raise SqlSyntaxError(
                f"a join condition must be one equality, column = column, at {where}"
            )
        right = self._column_name("a column name")
        if self._is_keyword("AND", "OR"):
            where = describe_position(self._text, self._current.position)
            raise SqlSyntaxError(
                f"a join condition must be one equality; put other conditions in WHERE, at {where}"
            )
        return ast.Join(table, alias, left, right, kind)

    def _column_name(self, what: str) -> str:
        """A column name, optionally qualified by a table or alias: `price` or `o.price`."""
        name = self._identifier(what)
        if self._accept_symbol("."):
            return f"{name}.{self._identifier('a column name after the dot')}"
        return name

    def _select_item(self, what: str) -> ast.SelectItem:
        """A column, a vector distance, or an aggregate such as COUNT(*) or SUM(price)."""
        token = self._current
        if token.kind is not TokenKind.KEYWORD or token.value not in _AGGREGATES:
            return self._column_or_distance(what)
        self._index += 1
        self._expect_symbol("(")
        if self._accept_symbol("*"):
            if token.value != "COUNT":
                where = describe_position(self._text, token.position)
                raise SqlSyntaxError(
                    f"{token.value}(*) is not valid; only COUNT can take *, at {where}"
                )
            column = None
        else:
            column = self._column_name("a column name or *")
        self._expect_symbol(")")
        return ast.Aggregate(token.value, column)

    def _column_or_distance(self, what: str) -> str | ast.Distance:
        """A column name, optionally followed by `<-> [vector]` or `<=> [vector]`."""
        column = self._column_name(what)
        if not self._is_symbol("<->", "<=>"):
            return column
        operator = self._advance().value
        if not self._is_symbol("["):
            raise self._error("a vector such as [0.1, 0.2, 0.3]")
        return ast.Distance(column, operator, self._vector())

    def _order_item(self) -> ast.OrderItem:
        column = self._select_item("column name")
        if self._accept_keyword("DESC"):
            return ast.OrderItem(column, True)
        self._accept_keyword("ASC")
        return ast.OrderItem(column, False)

    def _update(self) -> ast.Update:
        self._expect_keyword("UPDATE")
        table = self._identifier("table name")
        self._expect_keyword("SET")
        assignments = [self._assignment()]
        while self._accept_symbol(","):
            assignments.append(self._assignment())
        return ast.Update(table, tuple(assignments), self._where())

    def _assignment(self) -> tuple[str, ast.Value]:
        column = self._identifier("column name")
        self._expect_symbol("=")
        if self._current.kind is TokenKind.IDENT:
            where = describe_position(self._text, self._current.position)
            raise SqlSyntaxError(
                f"SET accepts only literal values; expressions are not supported, at {where}"
            )
        return column, self._literal()

    def _delete(self) -> ast.Delete:
        self._expect_keyword("DELETE")
        self._expect_keyword("FROM")
        table = self._identifier("table name")
        return ast.Delete(table, self._where())

    def _explain(self) -> ast.Explain:
        self._expect_keyword("EXPLAIN")
        if self._is_keyword("SELECT"):
            return ast.Explain(self._select())
        if self._is_keyword("UPDATE"):
            return ast.Explain(self._update())
        if self._is_keyword("DELETE"):
            return ast.Explain(self._delete())
        raise self._error("SELECT, UPDATE or DELETE after EXPLAIN")

    def _where(self) -> ast.Expr | None:
        if self._accept_keyword("WHERE"):
            return self._or()
        return None

    # ------------------------------------------------------------------ expressions

    def _or(self) -> ast.Expr:
        left = self._and()
        while self._accept_keyword("OR"):
            left = ast.Or(left, self._and())
        return left

    def _and(self) -> ast.Expr:
        left = self._not()
        while self._accept_keyword("AND"):
            left = ast.And(left, self._not())
        return left

    def _not(self) -> ast.Expr:
        if self._is_keyword("NOT"):
            self._enter()
            self._index += 1
            result: ast.Expr = ast.Not(self._not())
            self._depth -= 1
            return result
        return self._predicate()

    def _predicate(self) -> ast.Expr:
        if self._accept_symbol("("):
            self._enter()
            inner = self._or()
            self._depth -= 1
            self._expect_symbol(")")
            return inner
        left = self._operand()
        if self._accept_keyword("IS"):
            negated = self._accept_keyword("NOT")
            self._expect_keyword("NULL")
            return ast.IsNull(left, negated)
        token = self._current
        if token.kind is TokenKind.SYMBOL and token.value in _COMPARISONS:
            self._index += 1
            return ast.Compare(_COMPARISONS[token.value], left, self._operand())
        if self._is_symbol("<->", "<=>"):
            where = describe_position(self._text, token.position)
            raise SqlSyntaxError(
                f"a vector distance can be used in the SELECT list and in ORDER BY, "
                f"not in WHERE, at {where}"
            )
        raise self._error("a comparison operator or IS NULL")

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > _MAX_NESTING:
            raise SqlSyntaxError(f"expression is nested more than {_MAX_NESTING} levels deep")

    def _operand(self) -> ast.Operand:
        if self._current.kind is TokenKind.IDENT:
            return ast.ColumnRef(self._column_name("a column name"))
        return ast.Literal(self._literal())

    def _vector(self) -> tuple[float, ...]:
        """A vector literal: [number, number, ...]. Integers are read as reals."""
        self._expect_symbol("[")
        elements: list[float] = []
        while True:
            token = self._current
            value = self._literal()
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                where = describe_position(self._text, token.position)
                raise SqlSyntaxError(f"a vector may contain only numbers, at {where}")
            elements.append(float(value))
            if not self._accept_symbol(","):
                break
        self._expect_symbol("]")
        return tuple(elements)

    def _literal(self) -> ast.Value:
        token = self._current
        if token.kind is TokenKind.SYMBOL and token.value == "[":
            return self._vector()
        if token.kind is TokenKind.STRING:
            self._index += 1
            return token.value
        if token.kind is TokenKind.KEYWORD and token.value == "NULL":
            self._index += 1
            return None
        sign = 1
        if self._is_symbol("-", "+"):
            sign = -1 if token.value == "-" else 1
            self._index += 1
            token = self._current
            if token.kind not in (TokenKind.INTEGER, TokenKind.REAL):
                raise self._error("a number after the sign")
        if token.kind is TokenKind.INTEGER:
            self._index += 1
            value = sign * int(token.value)
            if not _INT64_MIN <= value <= _INT64_MAX:
                where = describe_position(self._text, token.position)
                raise SqlSyntaxError(f"integer {value} does not fit in 64 bits, at {where}")
            return value
        if token.kind is TokenKind.REAL:
            self._index += 1
            number = sign * float(token.value)
            if number in (float("inf"), float("-inf")):
                where = describe_position(self._text, token.position)
                raise SqlSyntaxError(f"real number {token.value} is out of range, at {where}")
            return number
        raise self._error("a value (number, string or NULL)")
