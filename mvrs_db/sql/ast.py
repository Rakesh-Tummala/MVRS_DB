"""Syntax tree for the SQL subset in docs/06-sql-grammar.md.

Plain immutable data. The parser builds it; the planner and executor read it.
"""

from __future__ import annotations

from dataclasses import dataclass

Value = int | float | str | tuple[float, ...] | None

# ---------------------------------------------------------------- expressions


@dataclass(frozen=True, slots=True)
class Literal:
    value: Value


@dataclass(frozen=True, slots=True)
class ColumnRef:
    name: str


Operand = Literal | ColumnRef


@dataclass(frozen=True, slots=True)
class Compare:
    op: str  # one of = != < <= > >=
    left: Operand
    right: Operand


@dataclass(frozen=True, slots=True)
class IsNull:
    operand: Operand
    negated: bool


@dataclass(frozen=True, slots=True)
class Not:
    operand: Expr


@dataclass(frozen=True, slots=True)
class And:
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class Or:
    left: Expr
    right: Expr


Expr = Compare | IsNull | Not | And | Or

# ---------------------------------------------------------------- statements


@dataclass(frozen=True, slots=True)
class ColumnDef:
    name: str
    type_name: str  # INTEGER, REAL, TEXT or VECTOR
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False
    dimension: int = 0  # n in VECTOR(n); 0 for every other type
    hnsw: str = ""  # "", "l2" or "cosine": an approximate index on a vector column


@dataclass(frozen=True, slots=True)
class CreateTable:
    table: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True, slots=True)
class DropTable:
    table: str


@dataclass(frozen=True, slots=True)
class Insert:
    table: str
    columns: tuple[str, ...] | None  # None: every column, in table order
    rows: tuple[tuple[Value, ...], ...]


@dataclass(frozen=True, slots=True)
class Distance:
    """`column <-> [vector]` or `column <=> [vector]`: distance to a query vector."""

    column: str
    operator: str  # "<->" (Euclidean) or "<=>" (cosine)
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Aggregate:
    """COUNT(*), COUNT(column), SUM(column), AVG(column), MIN(column) or MAX(column)."""

    function: str  # COUNT, SUM, AVG, MIN or MAX
    column: str | None  # None only for COUNT(*)

    @property
    def label(self) -> str:
        """The text used as the result column's name, such as "sum(price)"."""
        return f"{self.function.lower()}({self.column or '*'})"


SelectItem = str | Distance | Aggregate


@dataclass(frozen=True, slots=True)
class OrderItem:
    column: SelectItem
    descending: bool = False


@dataclass(frozen=True, slots=True)
class Select:
    table: str
    columns: tuple[SelectItem, ...] | None  # None: SELECT *
    where: Expr | None = None
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = None
    group_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Update:
    table: str
    assignments: tuple[tuple[str, Value], ...]
    where: Expr | None = None


@dataclass(frozen=True, slots=True)
class Delete:
    table: str
    where: Expr | None = None


@dataclass(frozen=True, slots=True)
class Explain:
    statement: Select | Update | Delete


@dataclass(frozen=True, slots=True)
class Begin:
    pass


@dataclass(frozen=True, slots=True)
class Commit:
    pass


@dataclass(frozen=True, slots=True)
class Rollback:
    pass


Statement = (
    CreateTable
    | DropTable
    | Insert
    | Select
    | Update
    | Delete
    | Explain
    | Begin
    | Commit
    | Rollback
)
