"""Hand-written lexer: SQL text to a list of tokens.

The lexer makes one pass over the text, looking at the current character to
decide which kind of token starts there. Every token remembers its position so
that errors can point at the exact place in the statement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mvrs_db import config
from mvrs_db.errors import QueryTooLargeError, SqlSyntaxError


class TokenKind(Enum):
    KEYWORD = "keyword"
    IDENT = "identifier"
    INTEGER = "integer"
    REAL = "real"
    STRING = "string"
    SYMBOL = "symbol"
    EOF = "end of statement"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    position: int


# Words the grammar uses.
KEYWORDS = frozenset(
    """
    CREATE TABLE DROP INSERT INTO VALUES SELECT FROM WHERE ORDER BY ASC DESC LIMIT
    UPDATE SET DELETE EXPLAIN BEGIN COMMIT ROLLBACK AND OR NOT IS NULL
    PRIMARY KEY UNIQUE INTEGER INT REAL FLOAT TEXT VECTOR HNSW COSINE
    COUNT SUM AVG MIN MAX GROUP JOIN INNER ON
    """.split()
)

# Words that are SQL but outside the supported subset. Recognising them lets the
# parser say "JOIN is not supported" instead of a confusing generic error, and
# keeps them from being silently read as table or column names.
UNSUPPORTED_KEYWORDS = frozenset(
    """
    LEFT RIGHT OUTER FULL CROSS NATURAL USING HAVING DISTINCT UNION INTERSECT
    EXCEPT OFFSET AS LIKE IN BETWEEN EXISTS CASE WHEN THEN ELSE END ALTER INDEX VIEW
    TRIGGER FOREIGN REFERENCES DEFAULT CHECK WITH ALL ANY TRUE FALSE BOOLEAN BLOB
    VARCHAR CHAR DATE TRANSACTION SAVEPOINT ANALYZE VACUUM
    """.split()
)

# Vector distance operators: <-> is Euclidean, <=> is cosine. They must be
# tried before the two-character symbols, or "<=>" would be read as "<=" ">".
_THREE_CHAR_SYMBOLS = ("<->", "<=>")
_TWO_CHAR_SYMBOLS = ("<=", ">=", "!=", "<>")
# "." joins a table name or alias to a column name: orders.total.
# "[" and "]" delimit vector literals: [0.1, 0.2, 0.3].
_ONE_CHAR_SYMBOLS = "(),;*=<>+-.[]"
_IDENT_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_PART = _IDENT_START | frozenset("0123456789")
_DIGITS = frozenset("0123456789")


def describe_position(text: str, position: int) -> str:
    line = text.count("\n", 0, position) + 1
    column = position - (text.rfind("\n", 0, position) + 1) + 1
    return f"line {line}, column {column}"


def tokenize(text: str) -> list[Token]:
    """Split a statement into tokens, ending with an EOF token."""
    if len(text.encode("utf-8", errors="surrogatepass")) > config.MAX_SQL_LENGTH:
        raise QueryTooLargeError(
            f"statement exceeds the {config.MAX_SQL_LENGTH}-byte limit on SQL text"
        )
    tokens: list[Token] = []
    length = len(text)
    i = 0
    while i < length:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "-" and text.startswith("--", i):
            newline = text.find("\n", i)
            i = length if newline == -1 else newline + 1
        elif ch in _IDENT_START:
            start = i
            while i < length and text[i] in _IDENT_PART:
                i += 1
            word = text[start:i]
            upper = word.upper()
            if upper in KEYWORDS or upper in UNSUPPORTED_KEYWORDS:
                tokens.append(Token(TokenKind.KEYWORD, upper, start))
            else:
                if len(word) > config.MAX_IDENTIFIER_LENGTH:
                    raise SqlSyntaxError(
                        f"identifier is longer than {config.MAX_IDENTIFIER_LENGTH} characters "
                        f"at {describe_position(text, start)}"
                    )
                tokens.append(Token(TokenKind.IDENT, word.lower(), start))
        elif ch in _DIGITS or (ch == "." and i + 1 < length and text[i + 1] in _DIGITS):
            i = _number(text, i, tokens)
        elif ch == "'":
            i = _string(text, i, tokens)
        elif text[i : i + 3] in _THREE_CHAR_SYMBOLS:
            tokens.append(Token(TokenKind.SYMBOL, text[i : i + 3], i))
            i += 3
        elif text[i : i + 2] in _TWO_CHAR_SYMBOLS:
            tokens.append(Token(TokenKind.SYMBOL, text[i : i + 2], i))
            i += 2
        elif ch in _ONE_CHAR_SYMBOLS:
            tokens.append(Token(TokenKind.SYMBOL, ch, i))
            i += 1
        elif ch == '"' or ch == "`":
            raise SqlSyntaxError(
                f"quoted identifiers are not supported, at {describe_position(text, i)}"
            )
        else:
            raise SqlSyntaxError(f"unexpected character {ch!r} at {describe_position(text, i)}")
    tokens.append(Token(TokenKind.EOF, "", length))
    return tokens


def _number(text: str, start: int, tokens: list[Token]) -> int:
    length = len(text)
    i = start
    is_real = False
    while i < length and text[i] in _DIGITS:
        i += 1
    if i < length and text[i] == ".":
        is_real = True
        i += 1
        while i < length and text[i] in _DIGITS:
            i += 1
    if i < length and text[i] in "eE":
        exponent = i + 1
        if exponent < length and text[exponent] in "+-":
            exponent += 1
        if exponent < length and text[exponent] in _DIGITS:
            is_real = True
            i = exponent
            while i < length and text[i] in _DIGITS:
                i += 1
    if i < length and text[i] in _IDENT_PART:
        raise SqlSyntaxError(
            f"malformed number {text[start : i + 1]!r} at {describe_position(text, start)}"
        )
    kind = TokenKind.REAL if is_real else TokenKind.INTEGER
    tokens.append(Token(kind, text[start:i], start))
    return i


def _string(text: str, start: int, tokens: list[Token]) -> int:
    """A single-quoted string. Two quotes in a row stand for one quote."""
    length = len(text)
    parts: list[str] = []
    i = start + 1
    while True:
        end = text.find("'", i)
        if end == -1:
            raise SqlSyntaxError(
                f"string starting at {describe_position(text, start)} is never closed"
            )
        parts.append(text[i:end])
        if end + 1 < length and text[end + 1] == "'":
            parts.append("'")
            i = end + 2
        else:
            tokens.append(Token(TokenKind.STRING, "".join(parts), start))
            return end + 1
