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
constraint    ::= "PRIMARY" "KEY" | "NOT" "NULL" | "UNIQUE"

drop_table    ::= "DROP" "TABLE" ident

insert        ::= "INSERT" "INTO" ident [ "(" ident { "," ident } ")" ]
                  "VALUES" row { "," row }
row           ::= "(" literal { "," literal } ")"

select        ::= "SELECT" select_list "FROM" ident
                  [ "WHERE" expr ]
                  [ "ORDER" "BY" order_item { "," order_item } ]
                  [ "LIMIT" integer ]
select_list   ::= "*" | ident { "," ident }
order_item    ::= ident [ "ASC" | "DESC" ]

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
literal       ::= integer | real | string | "NULL"
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
* A constraint violation fails the whole statement. Before Phase 4, the executor validates every row of a statement before changing anything; from Phase 4, the statement's transaction is rolled back.

## Stretch (not in version 1)

Inner joins (`FROM a JOIN b ON a.x = b.y`), which would add qualified names (`a.x`) to `operand`.

## Examples

```sql
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, score REAL);
INSERT INTO users VALUES (1, 'asha', 9.5), (2, 'ravi', NULL);
SELECT name, score FROM users WHERE score >= 5 AND NOT (name = 'ravi') ORDER BY score DESC LIMIT 10;
UPDATE users SET score = 7 WHERE id = 2;
DELETE FROM users WHERE score IS NULL;
EXPLAIN SELECT * FROM users WHERE id = 1;
```
