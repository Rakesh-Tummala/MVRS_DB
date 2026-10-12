# 06 — SQL subset (grammar version 1)

Anything not derivable from this grammar is rejected with a parse error that names the unexpected token and its position. Nothing is silently ignored.

## Lexical rules

* **Keywords** are case-insensitive.
* **Identifiers** match `[A-Za-z_][A-Za-z0-9_]*`, are at most 63 bytes, and are folded to lower case. Quoted identifiers are not supported. A keyword cannot be used as an identifier.
* **Integer literal:** digits, optionally signed. Must fit in 64 bits signed.
* **Real literal:** digits with a decimal point and/or exponent, optionally signed (`1.5`, `-2e3`).
* **String literal:** single quotes; a quote inside a string is written `''`. No backslash escapes.
* **Comments:** `--` to end of line.
* **Whitespace** separates tokens and is otherwise ignored.
* One statement per call. A trailing `;` is optional.

## Grammar

```
statement     ::= ( create_table | drop_table | insert | select
                  | update | delete | explain | txn_control ) [ ";" ]

create_table  ::= "CREATE" "TABLE" ident "(" column_def { "," column_def } ")"
column_def    ::= ident type { constraint }
type          ::= "INTEGER" | "INT" | "REAL" | "FLOAT" | "TEXT"
                | "VECTOR" "(" integer ")"
constraint    ::= "PRIMARY" "KEY" | "NOT" "NULL" | "UNIQUE"
                | "HNSW" [ "COSINE" ]            -- VECTOR columns only

drop_table    ::= "DROP" "TABLE" ident

insert        ::= "INSERT" "INTO" ident [ "(" ident { "," ident } ")" ]
                  "VALUES" row { "," row }
row           ::= "(" literal { "," literal } ")"

select        ::= "SELECT" select_list "FROM" table_ref { join }
                  [ "WHERE" expr ]
                  [ "GROUP" "BY" ident { "," ident } ]
                  [ "ORDER" "BY" order_item { "," order_item } ]
                  [ "LIMIT" integer ]
table_ref     ::= ident [ ident ]                       -- table, optional alias
join          ::= [ "INNER" | ( "LEFT" | "RIGHT" | "FULL" ) [ "OUTER" ] ]
                  "JOIN" table_ref "ON" column "=" column
column        ::= ident [ "." ident ]                   -- optionally qualified
select_list   ::= "*" | select_item { "," select_item }
select_item   ::= ident | distance | aggregate
order_item    ::= select_item [ "ASC" | "DESC" ]
aggregate     ::= "COUNT" "(" ( "*" | ident ) ")"
                | ( "SUM" | "AVG" | "MIN" | "MAX" ) "(" ident ")"
distance      ::= ident ( "<->" | "<=>" ) vector

update        ::= "UPDATE" ident "SET" assignment { "," assignment } [ "WHERE" expr ]
assignment    ::= ident "=" literal

delete        ::= "DELETE" "FROM" ident [ "WHERE" expr ]

explain       ::= "EXPLAIN" ( select | update | delete )

txn_control   ::= "BEGIN" | "COMMIT" | "ROLLBACK"          -- Phase 5

expr          ::= or_expr
or_expr       ::= and_expr { "OR" and_expr }
and_expr      ::= not_expr { "AND" not_expr }
not_expr      ::= "NOT" not_expr | predicate
predicate     ::= operand comp_op operand
                | operand "IS" [ "NOT" ] "NULL"
                | "(" expr ")"
comp_op       ::= "=" | "!=" | "<>" | "<" | "<=" | ">" | ">="
operand       ::= ident | literal
literal       ::= integer | real | string | "NULL" | vector
vector        ::= "[" number { "," number } "]"
number        ::= integer | real
```

Precedence, loosest to tightest: `OR`, `AND`, `NOT`, comparison. This falls out of the grammar's nesting, which is how a recursive-descent parser encodes precedence.

## Semantic rules

**Tables**

* Every table has exactly one `PRIMARY KEY` column. It is implicitly `NOT NULL`.
* Column names are unique within a table. At most 64 columns.
* `INT` and `FLOAT` are spellings of `INTEGER` and `REAL`.

**Types**

| Situation | Rule |
|---|---|
| Integer literal into a `REAL` column | Allowed; converted |
| Real literal into an `INTEGER` column | Error |
| `INTEGER` compared with `REAL` | Allowed; compared exactly as numbers |
| `TEXT` compared with a number | Error at planning time, before any row is read |
| `TEXT` ordering | By Unicode code point; no collation |
| Real `NaN` | Cannot be written as a literal and is rejected by the record encoder |

In `select`, every place the rules below say `ident` for a column (select items, aggregate arguments, `GROUP BY`, `ORDER BY`, operands in `WHERE`) accepts a `column`, that is, an optional `table.` or `alias.` prefix.

**Joins** (details in `12-aggregates-and-joins.md`)

* Any number of tables: `FROM a JOIN b ON .. LEFT JOIN c ON ..` is `((a JOIN b) LEFT JOIN c)`. Kinds: inner, `LEFT`, `RIGHT`, `FULL` (`OUTER` is optional).
* Each `ON` is one equality between a column of the new table and a column of any earlier table.
* A table may be given an alias; once it has one, its columns are qualified by the alias, not the table name.
* A bare column name is allowed when only one table has it. A name both have must be qualified.
* A NULL join value matches nothing.
* `SELECT *` returns each table's columns in the order the tables are written, named `alias.column`.
* An outer join keeps the rows of a side that found no partner, with NULL for the other side's columns.
* `CROSS`, `NATURAL`, `USING`, and vector distance in a join are not supported.

**Aggregates and GROUP BY** (details in `12-aggregates-and-joins.md`)

* `COUNT(*)`, `COUNT(col)`, `SUM(col)`, `AVG(col)`, `MIN(col)`, `MAX(col)`. `SUM` and `AVG` need a numeric column.
* Every selected column must be listed in `GROUP BY` or be inside an aggregate. `SELECT *` cannot be combined with either.
* All aggregates except `COUNT(*)` ignore NULLs. With no `GROUP BY`, a query over no rows still returns one row: `COUNT` is 0, the others NULL.
* In `GROUP BY`, NULLs form one group.
* `ORDER BY` may name a `GROUP BY` column or an aggregate, including one that is not selected.
* `HAVING`, `DISTINCT` and expressions over aggregates are not supported.

**Vectors**

* `VECTOR(n)` holds exactly `n` numbers, `n` from 1 to 4096. A vector of any other length is a type error.
* A vector literal is written `[0.1, 0.2, 0.3]`. Integers inside it are read as reals. It cannot be empty or nested.
* Elements are stored as 32-bit floats, so a value read back is rounded to that precision.
* A vector column cannot be `PRIMARY KEY` or `UNIQUE`, cannot appear in `ORDER BY`, and cannot be compared with `=`, `<` and so on. `IS NULL` and `IS NOT NULL` work.
* `HNSW` after a vector column's type declares an approximate index for Euclidean distance; `HNSW COSINE` declares one for cosine distance. Queries do not change; the planner uses the index when it can, and `EXPLAIN` shows whether a search is exact or approximate.
* Nearest-neighbour search: `ORDER BY column <-> [vector] LIMIT k` (Euclidean) or `<=>` (cosine). The same expression may appear in the `SELECT` list, where its column is named `distance`. A distance must be the only `ORDER BY` item and cannot be used in `WHERE`. Rows with no distance come last. See `11-vector-search.md`.

**NULL (three-valued logic)**

* Any comparison with NULL is UNKNOWN, including `x = NULL`. Use `IS NULL`.
* `NOT UNKNOWN` is UNKNOWN. `AND`/`OR` follow SQL's truth tables (`FALSE AND UNKNOWN` is FALSE; `TRUE OR UNKNOWN` is TRUE).
* `WHERE` keeps a row only when the expression is TRUE.
* `ORDER BY` puts NULLs first ascending and last descending.
* A `UNIQUE` column may hold many NULLs; NULLs are not indexed.

**Statements**

* `INSERT` without a column list supplies every column in table order. With a list, unlisted columns are NULL.
* `SELECT` without `ORDER BY` returns rows in no promised order.
* `LIMIT` takes a non-negative integer.
* `UPDATE` may change the primary key; uniqueness is checked.
* A constraint violation fails the whole statement: a multi-row `INSERT` or `UPDATE` that fails on its third row leaves no trace of the first two. Inside `BEGIN` ... `COMMIT`, a statement that fails after changing something rolls back the whole transaction (see `10-transactions.md`).
* `SET` accepts literals only (`SET n = 5`, not `SET n = n + 1`).
* `table.column` names are accepted in `SELECT`, and in the `WHERE` clause of `UPDATE` and `DELETE`. They are not accepted in `INSERT` column lists, `SET`, or `CREATE TABLE`.

## Stretch (not in version 1)

Joins (`FROM a JOIN b ON a.x = b.y`), which add qualified names (`a.x`) to `operand`.

## Examples

```sql
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, score REAL);
INSERT INTO users VALUES (1, 'asha', 9.5), (2, 'ravi', NULL);
SELECT name, score FROM users WHERE score >= 5 AND NOT (name = 'ravi') ORDER BY score DESC LIMIT 10;
UPDATE users SET score = 7 WHERE id = 2;
DELETE FROM users WHERE score IS NULL;
EXPLAIN SELECT * FROM users WHERE id = 1;
```

## Parameters

A `?` stands for a value that is supplied separately:

```python
db.execute("SELECT name FROM users WHERE id = ? AND city = ?", (7, "pune"))
db.execute("INSERT INTO users VALUES (?, ?, ?)", (8, "o'brien", None))
find = conn.prepare("SELECT name FROM users WHERE id = ?")  # parsed once
find.execute((7,))
find.execute((9,))
```

* **Where:** anywhere a literal can be: `INSERT` values, `SET` values, the operands of a comparison in `WHERE`, `LIMIT`, and a vector after `<->` or `<=>` (the whole vector, or its elements: `[?, ?]`). Not for names: a table or column cannot be a parameter.
* **Numbering:** by position, in the order the `?`s are written. The number of parameters must equal the number of `?`s, or `ParameterError` is raised before anything runs.
* **Python values:** `None` is NULL; `int` must fit 64 bits; `float` must be finite; `str`; a list or tuple of numbers is a vector. A `bool`, `bytes`, `NaN`, `inf` and anything else are refused with `ParameterError`, the same rule as for a literal.
* **Safety:** a parameter is never turned back into SQL text, so a value such as `x'); DROP TABLE t; --` is just a string. There is nothing to escape.
* **Speed:** the statement is parsed once and the parse is cached by its text (256 statements, each at most 4,096 characters, least recently used out), so `db.execute(sql, params)` and `prepare()` both skip parsing on repeats. Planning still happens on every run.
* `parse()` (the plain parser) refuses a statement with `?`; `parse_template()` returns the tree with `Param` nodes and their count, and `sql/binding.py: bind()` fills them in.
