# Tier 4 extension layers — specification

Tier 4 is the complete tier-3 toydb (SQL engine, Raft, server, client, binaries, and the four tier-3 extensions of
`tiers/t3/EXT_SPEC.md`: JSON output, statement digests, column statistics, export/import) plus seven more
**extension layers**. Each is a realistic subsystem in its own module(s) that consumes toydb's core types — values
and data types, the AST, plan expressions, plan nodes and aggregates, statement results, the catalog's table and
column schemas, the Raft SQL engine's commands — and is reachable from SQL (and so from `toysql`, the client
library and both golden-script runners):

| § | Extension (id) | Reached through | Depends on |
|---|---|---|---|
| 2 | **SQL formatter** (`format`) — canonical, multi-line SQL text of any statement | `FORMAT` | – |
| 3 | **Views** (`views`) — stored queries, re-planned on use, with dependency checks | `CREATE VIEW`, `DROP VIEW`, `SHOW VIEWS`, views in `FROM` | 2 |
| 4 | **Result description** (`describe`) — result columns with inferred types and nullability | `DESCRIBE` | 3 |
| 5 | **Row estimates** (`estimate`) — estimated row counts of every plan node, from the column statistics | `ESTIMATE` | 3 |
| 6 | **Prepared statements** (`prepared`) — typed parameters `$1`, ..., per-session statements with a plan cache | `PREPARE`, `EXECUTE`, `DEALLOCATE`, `SHOW PREPARED` | 2, 3 |
| 7 | **Statement lint** (`lint`) — warnings about a statement, without executing it | `LINT` | 2, 3, 4, 6 |
| 8 | **Change log** (`changes`) — every committed change, in commit order | `SHOW CHANGES` | 2, 3 |

The ids name the script directories `ext4_scripts/<id>/`; the order of the table is the build order (an
extension may use the ones above it).

This document is language-neutral. Sections 1–9 define the user-facing behaviour completely; the extensions'
user documentation is derived from them. Section 10 lists the requirements for implementing the tier-4 base; it
is not user documentation. References of the form *EXT_SPEC 3.2* are to sections of `tiers/t3/EXT_SPEC.md`,
which stays in force unchanged except where section 9 extends it.

**Generic rules.** As in tier 3, every output is defined by rules stated in terms of SQL syntax, values, data
types, the plan display and the schema display — not by a closed list of cases. Where a rule lists the existing
cases, it also says how a case added to toydb later (a data type, a kind of literal, an expression, operator,
function or aggregate function, a plan node, a statement or a statement result, a change of a table's definition)
is handled. The correct output for such an addition follows from these rules together with the addition's own
definition: its SQL syntax, its meaning (types, NULL handling), its value display, plan display and schema display.
Two kinds of additions are named by the rules because their handling differs from the existing cases:

* A construct that toydb **defines as an abbreviation** of other constructs ("`x BETWEEN a AND b` means
  `x >= a AND x <= b`") is written as itself wherever SQL text is written (sections 2, 3, 6, 7, 8), but behaves
  as its expansion wherever a rule is about meaning (sections 4, 5, 7).
* A statement **contained in another statement** (the statement after `EXPLAIN`, the query of `CREATE VIEW`, ...)
  is handled as a statement in its own right by every rule.

**Conventions.** As in EXT_SPEC: examples use the golden-script conventions of the SQL runner (`> statement`, the
output below `---`, `Error: <message>`, `ok` for no output); *displayed as in query results* means the value
display of result rows (`NULL`, `TRUE`, `1`, `-0.5`, `1e100`, `NaN`, `inf`, `'text'`); the *plan display* is the
text that `[plan]` and `EXPLAIN` show; the *schema display* is the `CREATE TABLE` text of the `schema` command.
Error messages are complete output lines. "Code points" order compares names character by character by Unicode
code point (`"B"` before `a`).

---

## 1. Shared definitions

### 1.1 New statements and keywords

```
FORMAT <statement>
CREATE VIEW <name> [( <column>, ... )] AS <query>
DROP VIEW [IF EXISTS] <name>
SHOW VIEWS
DESCRIBE <name>
DESCRIBE <statement>
ESTIMATE <statement>
PREPARE <name> [( <type>, ... )] AS <statement>
EXECUTE <name> [( <expression>, ... )]
DEALLOCATE <name>
SHOW PREPARED
LINT <statement>
SHOW CHANGES [SINCE <version>]
```

`CHANGES`, `DEALLOCATE`, `DESCRIBE`, `ESTIMATE`, `EXECUTE`, `FORMAT`, `LINT`, `PREPARE`, `PREPARED`, `SINCE`, `VIEW`
and `VIEWS` are reserved keywords with the same rules as the existing ones (EXT_SPEC 7.2): case-insensitive, not
usable as unquoted identifiers (`CREATE TABLE view (id INT PRIMARY KEY)` →
`Error: invalid input: expected identifier, got VIEW`), valid when double-quoted, quoted by the schema display.
Names that merely contain them (`views_old`, `preview`) are unaffected. `<query>` is a SELECT statement;
`<statement>` is any statement, written without a trailing `;` (the single `;` that may end every statement
belongs to the outermost one). Keywords, names, and the parser's messages follow the existing rules.

### 1.2 Planned statements

A **planned statement** is a statement that has a plan, i.e. one that `EXPLAIN` shows: SELECT, INSERT, UPDATE,
DELETE, CREATE TABLE, DROP TABLE, and every statement added to toydb later that has a plan. In addition,
`EXPLAIN`, `DESCRIBE` and `ESTIMATE` accept `EXECUTE <name> [(...)]`, which stands for the prepared statement it
executes, with its arguments bound (section 6.4): `EXPLAIN EXECUTE p (1)`, `DESCRIBE EXECUTE p (1)`,
`ESTIMATE EXECUTE p (1)`. (A prepared statement can't be an EXECUTE.)

`DESCRIBE`, `ESTIMATE` and `PREPARE` accept only planned statements (and `DESCRIBE` a name). For any other
statement they fail with

```
Error: invalid input: <WORD> is not supported for <KEYWORD>
```

where `<WORD>` is `DESCRIBE`, `ESTIMATE` or `PREPARE`, and `<KEYWORD>` is the first keyword of the contained
statement, or its first two keywords where the first one also begins a planned statement (`CREATE VIEW`,
`DROP VIEW`): `ESTIMATE SHOW VIEWS` → `ESTIMATE is not supported for SHOW`, `PREPARE p AS BEGIN` →
`PREPARE is not supported for BEGIN`, `DESCRIBE CREATE VIEW v AS SELECT 1 AS a` →
`DESCRIBE is not supported for CREATE VIEW`, `PREPARE p AS EXECUTE q` → `PREPARE is not supported for EXECUTE`.
`EXPLAIN` of the new statements other than EXECUTE fails the same way, with the existing message of EXT_SPEC 7.3:
`EXPLAIN FORMAT SELECT 1` → `Error: invalid input: EXPLAIN is not supported for FORMAT`, `EXPLAIN DROP VIEW v` →
`Error: invalid input: EXPLAIN is not supported for DROP VIEW`. (EXPLAIN of BEGIN, COMMIT and ROLLBACK keeps its
current, unspecified behaviour.) These checks are made after parsing, before anything else.

### 1.3 Names in canonical SQL

Where the tier-4 extensions write SQL text (sections 2, 3, 6, 7, 8), a table, column, alias, view, prepared
statement or function name is written **so that it reads back as the same name**: unquoted exactly when the lexer
reads the unquoted text as that very name — it begins with a letter, continues with letters, digits and `_`,
contains no upper-case letter, and is not a keyword (including keywords added to toydb later) — and otherwise
between double quotes with every `"` doubled: `item`, `t2`, `"Item"`, `"order"`, `"view"`, `"a b"`, `"x""y"`,
`"_x"`, `"1a"`, `""`. (This differs from EXT_SPEC 1.6, which digests and the schema display keep using: they write
`"Item"` as `Item`.)

### 1.4 Literals in canonical SQL

A literal is written as SQL text that the parser reads as the same literal:

| Literal | Written |
|---|---|
| `NULL`, `TRUE`, `FALSE` | `NULL`, `TRUE`, `FALSE` |
| an integer | its decimal digits: `42`, `007` → `7` |
| a finite float | the value as displayed in query results: `1.5`, `1.50` → `1.5`, `2E3` → `2000.0`, `1e100`, `1.5e-7` |
| `INFINITY`, `NAN` | `INFINITY`, `NAN` |
| a string | between single quotes, each `'` doubled: `'it''s'`; nothing else is escaped |
| a literal of any other kind (added later) | the SQL literal of its value (EXT_SPEC 1.3), i.e. the value as the plan display shows it as a constant |

A minus sign is never part of a literal: `-1` is the prefix `-` applied to `1` and is written `-1` (EXT_SPEC 3.2
spacing). A parameter (section 6.1) is written as `$` and its number: `$1`.

### 1.5 The canonical text of an expression

The canonical text of an expression is written by the rules of EXT_SPEC 3.2 (keywords in upper case, synonyms in
canonical form, optional words left out, aliases with `AS`, spacing, and parentheses exactly where the parser
needs them, by the precedence table and associativity rules there), except that literals are written by 1.4
instead of `?`, parameters as `$n`, and names by 1.3. Every construct is written as it was written: an
abbreviation is written in its own syntax, `a >= b` stays `a >= b`, `a IS NOT NULL` stays `a IS NOT NULL`. A
construct added later is written by EXT_SPEC 3.2's rules for such constructs, with its literals by 1.4.

```
select ID, "Name" n from Item where not (A <> 1.50 or b is not null) and c >= -2E3 and d = 'it''s'
```

has the expressions `id`, `"Name"`, `NOT (a != 1.5 OR b IS NOT NULL) AND c >= -2000.0 AND d = 'it''s'`.

### 1.6 Results

The new statements that return rows (`SHOW VIEWS`, `DESCRIBE`, `ESTIMATE`, `SHOW PREPARED`, `LINT`,
`SHOW CHANGES`) return ordinary query results, with the column names given in their sections as unqualified
labels: the runners print their rows, `[header]` shows the names, the JSON form is a `select` result
(EXT_SPEC 2.1), and SHOW STATEMENTS counts their rows. `FORMAT` returns a list of text lines, which is printed like
the lines of `EXPORT` (one per output line, as they are, also without tags). `CREATE VIEW`, `DROP VIEW`,
`PREPARE` and `DEALLOCATE` print nothing without `[json]`. `EXECUTE` returns the result of the statement it
executes.

### 1.7 Transactions

| Statement | Outside an explicit transaction | Inside one |
|---|---|---|
| `FORMAT`, `PREPARE`, `DEALLOCATE`, `SHOW PREPARED`, `SHOW CHANGES` | no transaction | no transaction |
| `SHOW VIEWS`, `DESCRIBE`, `ESTIMATE`, `LINT` | an implicit read-only transaction (no version is allocated) | the session's |
| `CREATE VIEW`, `DROP VIEW` | an implicit read-write transaction (a version is allocated, also when the statement fails) | the session's |
| `EXECUTE` | that of the statement it executes, exactly as if that statement were issued | the session's |

All other statements behave exactly as before.

---

## 2. Extension: SQL formatter

```
FORMAT <statement>
```

returns the **canonical SQL text** of the statement, laid out on several lines. The statement is only parsed: it
is not checked against the database, not executed, and may name tables that don't exist. FORMAT uses no
transaction and works inside any transaction. Its result is a list of lines (1.6); the JSON form is
`{"result":"format","lines":[<string>,...]}`. A statement that fails to parse fails as usual.

### 2.1 Layout

The canonical text of a statement is a list of **lines**. Expressions are written by 1.5 and never broken across
lines. Lines are not indented, except where stated (two spaces). For each statement:

| Statement | Lines |
|---|---|
| `BEGIN` | one line: `BEGIN [READ ONLY] [AS OF SYSTEM TIME <n>]` |
| `COMMIT`, `ROLLBACK` | one line |
| `EXPLAIN <s>` | `EXPLAIN`, then the lines of `<s>` |
| `CREATE TABLE` | `CREATE TABLE <name> (`, then one line per column `  <column>`, followed by `,` on every column line but the last, then `)`. A column is written as in EXT_SPEC 3.2 (`<name> <type>` then its constraints in the order `PRIMARY KEY`, `NULL`/`NOT NULL` (only if written), `DEFAULT <expr>`, `UNIQUE`, `INDEX`, `REFERENCES <table>`), with the `DEFAULT` expression by 1.5. Only what was written is written: no default or constraint is added. |
| `DROP TABLE` | one line: `DROP TABLE [IF EXISTS] <name>` |
| `INSERT` | `INSERT INTO <table> [(<column>, ...)] VALUES`, then one line per row `  (<expr>, ...)`, followed by `,` on every row line but the last |
| `UPDATE` | `UPDATE <table>`, then `SET <column> = <expr or DEFAULT>, ...` (assignments in increasing order of column name by code points, as in EXT_SPEC 3.2), then `WHERE <expr>` if present |
| `DELETE` | `DELETE FROM <table>`, then `WHERE <expr>` if present |
| `SELECT` | `SELECT <item>, ...` (`*` or `<expr> [AS <alias>]`); then, for each clause that is present, in this order: `FROM ...` (see below), `WHERE <expr>`, `GROUP BY <expr>, ...`, `HAVING <expr>`, `ORDER BY <expr> [DESC], ...`, `LIMIT <expr>`, `OFFSET <expr>` — each clause on its own line |
| tier-3 statements | one line each, by EXT_SPEC 3.2 with literals by 1.4: `SHOW STATEMENTS`, `ANALYZE [<table>]`, `SHOW STATISTICS <table>`, `EXPORT [<table>] AS CSV` (`JSON`, `SQL`), `IMPORT INTO <table> FROM CSV '<line>', ...` (`JSON`) |
| `FORMAT <s>`, `DESCRIBE <s>`, `ESTIMATE <s>`, `LINT <s>` | the keyword on a line of its own, then the lines of `<s>` |
| `DESCRIBE <name>` | one line |
| `CREATE VIEW` | `CREATE VIEW <name> [(<column>, ...)] AS`, then the lines of the query |
| `DROP VIEW`, `SHOW VIEWS`, `SHOW PREPARED`, `DEALLOCATE <name>` | one line: `DROP VIEW [IF EXISTS] <name>` etc. |
| `PREPARE` | `PREPARE <name> [(<type>, ...)] AS` (types as the schema display names them), then the lines of the statement |
| `EXECUTE` | one line: `EXECUTE <name> [(<expr>, ...)]` |
| `SHOW CHANGES` | one line: `SHOW CHANGES [SINCE <n>]` |

**FROM.** The `FROM` line holds `FROM` and the FROM items, separated by `, `, written as in EXT_SPEC 3.2
(`<table> [AS <alias>]`, joins left to right as the parser nests them, `INNER`/`OUTER` left out) — except that
each join starts a new line, indented by two spaces, with its join keywords: `  JOIN <table> ON <expr>`,
`  LEFT JOIN ...`, `  RIGHT JOIN ...`, `  CROSS JOIN <table>`. A comma and the FROM item after it continue the
current line: `FROM a JOIN b ON x, c` is written as the two lines `FROM a` and `  JOIN b ON x, c`.

**Statements added later** are written by the same principles. Their keywords, names and operands are written in
the order their syntax defines, with optional words left out and synonyms in canonical form (EXT_SPEC 3.2), on one
line, except that:

* a complete statement contained in it is written on the following lines, laid out as that statement;
* a parenthesized list of column definitions, the rows of a VALUES list, and the clauses FROM (with its joins),
  WHERE, GROUP BY, HAVING, ORDER BY, LIMIT, OFFSET and SET are laid out as in the statements above;
* words that the syntax puts directly after a statement's leading keywords stay on that line (as `READ ONLY` after
  `BEGIN`, `IF EXISTS` after `DROP TABLE`).

So a statement `FROB TABLE t SET a = 1 WHERE b` would be written `FROB TABLE t`, `SET a = 1`, `WHERE b`. The same
holds for **options and clauses added later to an existing statement**: a word or option is written where its
syntax puts it, on the line of the part it belongs to (a word after `SELECT` on the `SELECT` line, a word after
`CREATE TABLE` on the `CREATE TABLE` line), a clause of its own on its own line, and a statement or query that
replaces a part (for example a query in place of the VALUES of an INSERT) on the following lines, laid out as that
statement.

### 2.2 The one-line form

The **one-line form** of a statement is its lines joined into one: each line without its indentation, joined
by single spaces, except that no space follows a line that ends with the `(` opening a list of column definitions
and no space precedes the line `)` closing it. It equals the statement's digest (EXT_SPEC 3.2) with literals by
1.4 and names by 1.3. Views (section 3), prepared statements (6), lint messages (7) and the change log (8) use it.

### 2.3 Examples

```
> FORMAT select id, NAME n from Item i left outer join "Order" o on o.item_id = i.id, extra where i.price >= 1.50 and not o.qty <> 0 group by id, n having count(*) > 1 order by n asc, 2 desc limit 10 offset 2
---
SELECT id, name AS n
FROM item AS i
  LEFT JOIN "Order" AS o ON o.item_id = i.id, extra
WHERE i.price >= 1.5 AND NOT o.qty != 0
GROUP BY id, n
HAVING count(*) > 1
ORDER BY n, 2 DESC
LIMIT 10
OFFSET 2

> FORMAT create table T (ID int primary key, v varchar not null default 'x' index, r int references T)
---
CREATE TABLE t (
  id INTEGER PRIMARY KEY,
  v STRING NOT NULL DEFAULT 'x' INDEX,
  r INTEGER REFERENCES t
)

> FORMAT explain insert into t (id, v) values (1, 'a'), (-2, NULL)
---
EXPLAIN
INSERT INTO t (id, v) VALUES
  (1, 'a'),
  (-2, NULL)
```

The one-line form of the last one is `EXPLAIN INSERT INTO t (id, v) VALUES (1, 'a'), (-2, NULL)`; of the
CREATE TABLE, `CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING NOT NULL DEFAULT 'x' INDEX, r INTEGER REFERENCES t)`.

Every statement that toydb accepts has a canonical text — the statements of the tier-3 and tier-4 extensions and
statements added later included — and the rules above determine it. Parsing the one-line form (or the lines)
again gives the same statement.

---

## 3. Extension: Views

```
CREATE VIEW <name> [( <column>, ... )] AS <query>
DROP VIEW [IF EXISTS] <name>
SHOW VIEWS
```

A view is a named query. It is stored in the catalog as the one-line form (2.2) of its query, and is planned
again, against the current table definitions, every time a statement uses it. Views and tables share one
namespace.

### 3.1 CREATE VIEW

`<query>` is a SELECT statement, with all its clauses; it may read tables and other views. The view's **columns**
are named by the column list, or otherwise by the query's result columns: a column reference `t.c` (header
`t.c`) is named `c`, an aliased column `... AS a` is named `a`.

The statement makes these checks, in this order, and reports the first that fails:

1. The name can't be empty: `Error: invalid input: view name can't be empty`.
2. No table or view may have the name: `Error: invalid input: table <name> already exists` (a table) or
   `Error: invalid input: view <name> already exists` (a view).
3. The query is planned in the statement's transaction, with the errors that planning it as a SELECT has
   (`Error: invalid input: table nosuch does not exist`, `Error: invalid input: unknown column x`, ...). It is
   neither optimized nor executed, so a constant expression that fails, such as `1 / 0`, is an error only when the
   view is used.
4. With a column list, its length must be the query's number of result columns:
   `Error: invalid input: wrong number of column names for view <name>: expected <m>, found <k>` (`<m>` the
   number of result columns, `<k>` the number of names).
5. The columns are checked in order; for each, the first applicable error is reported:
   `Error: invalid input: column <i> of view <name> has no name` (no column list is given, and the result column
   is neither a column reference nor aliased, so its header is empty; `<i>` counts from 1),
   `Error: invalid input: column name can't be empty` (an empty name in the column list, or an empty alias
   `AS ""`), `Error: invalid input: duplicate column <c> in view <name>` (the name of an earlier column, compared
   exactly).
6. In a read-only transaction: `Error: read-only transaction`.

The stored definition consists of the view's name, its column names, its query (the one-line form), and the
names of the tables and views that its query reads in its FROM clause (its **dependencies**: each name once, in the
order in which it first occurs in the FROM clause).

### 3.2 Using a view

A view can be used wherever a table can be used in a FROM clause — alone, with an alias, in joins of every kind,
in queries with WHERE, GROUP BY, aggregates, HAVING, ORDER BY, LIMIT and OFFSET, and in the query of another view.
A name in a FROM clause refers to the table of that name if there is one, otherwise to the view of that name;
if there is neither, the existing error `table <name> does not exist` is reported.

A view used in a statement behaves like a table whose rows are the rows of its query and whose columns are the
view's columns, qualified by the view's name or alias (`v.c`, `x.c`). Its query is parsed from the stored text and
planned in the statement's transaction, against what that transaction sees; it fails with any error that
planning it fails with. If its result has another number of columns than the view, the statement fails with
`Error: invalid input: wrong number of column names for view <name>: expected <m>, found <k>`. Result headers show view
columns like table columns (`v.c`, `x.c`).

Views are read-only. Every statement other than a query that names a view where a table is expected (INSERT,
UPDATE, DELETE, DROP TABLE, ANALYZE, SHOW STATISTICS, EXPORT, IMPORT, ...) fails exactly as for a name that is not a
table: `Error: invalid input: table <name> does not exist`. `CREATE TABLE <name>` with the name of a view fails
with `Error: invalid input: view <name> already exists` (instead of creating the table). The `schema` command and
the cluster runner's `tables` list tables only.

**Plans.** A use of a view appears in the plan as a node `View: <name>`, or `View: <name> as <alias>`, whose only
input is the plan of the view's query, exactly as the plan display shows the query on its own. No optimization
moves a filter, a join condition or a lookup into or out of a `View` node, and a `View` node is never replaced:
a condition on view columns stays above the node (as a `Filter`, or in a join), and the plan below the node is
the query's own optimized plan. Optimizations above the node treat it like a table scan into which nothing can be
pushed (so `WHERE FALSE` over a view still becomes `Nothing`). The JSON form (EXT_SPEC 2.2) of the node is
`{"node":"view","view":<name>,"alias":<alias or null>,"source":<node>}`.

```
> CREATE TABLE item (id INT PRIMARY KEY, name STRING INDEX, price FLOAT, qty INT, cat STRING)
> CREATE VIEW cheap AS SELECT id, name FROM item WHERE price < 2
[plan]> SELECT name FROM cheap WHERE id > 1
---
Projection: cheap.name
└─ Filter: cheap.id > 1
   └─ View: cheap
      └─ Projection: item.id, item.name
         └─ Scan: item (item.price < 2)
```

### 3.3 Dependencies and DROP VIEW

* `DROP TABLE <t>` (also with `IF EXISTS`) of a table that a view depends on fails with
  `Error: invalid input: table <t> is used by view <v>`, where `<v>` is the first such view by name (code points).
  This check comes after the existing ones (the table exists, it is not referenced by a foreign key of another
  table) and before anything is changed.
* **Every other statement that changes the definition of a table** — none exists in toydb today; for example, a
  statement added later that adds, drops or renames columns — fails with the same error when a view depends on the
  table. The check comes after all of the statement's own checks (when the statement would otherwise succeed),
  before anything is changed, and before the `read-only transaction` error.
* `DROP VIEW <v>` removes the view. It fails with `Error: invalid input: view <v> does not exist` if there is no
  such view (also if `<v>` is a table); with `IF EXISTS` it then does nothing. A view that another view depends on
  can't be dropped: `Error: invalid input: view <v> is used by view <w>` (`<w>` the first by name). In a read-only
  transaction, a DROP VIEW that passes these checks fails with `Error: read-only transaction`.
* Views are part of the catalog and transactional exactly like table definitions: they are seen by their own
  transaction at once, by others after commit under snapshot isolation, undone by ROLLBACK, replicated through Raft
  in a cluster. Two transactions that create or drop the same view concurrently conflict like two writes of the
  same row (`Error: serialization failure, retry transaction`). What happens when one transaction creates a view on
  a table that another transaction concurrently drops, or when two transactions concurrently create a table and a
  view of the same name, is not specified.

### 3.4 SHOW VIEWS

Returns one row per view, ordered by name (code points), with the columns `name` (STRING, as stored), `columns`
(STRING: the column names written by 1.3, separated by `, `) and `query` (STRING: the stored one-line form of the
query).

### 3.5 Views and the tier-3 extensions

* **Digests**: `CREATE VIEW <name> [(<column>, ...)] AS <digest of the query>`, `DROP VIEW [IF EXISTS] <name>`,
  `SHOW VIEWS` (names by EXT_SPEC 1.6).
* **JSON results**: `{"result":"create_view","view":<name>}`, `{"result":"drop_view","view":<name>,"existed":<bool>}`
  (`existed` is false when `IF EXISTS` found no view).
* **EXPORT AS SQL** (the whole database) writes, after all tables, one line per view:
  `CREATE VIEW <name> (<column>, ...) AS <one-line form of the query>;` (names by 1.3), always with the column
  list. Views come after the views they depend on: repeatedly, the next view is the first by name among the
  remaining views whose dependencies have all been exported (views never form cycles). `EXPORT <view> AS ...`
  fails as for any name that is not a table.
* **ANALYZE** without a table analyzes tables only.

---

## 4. Extension: Result description

```
DESCRIBE <name>
DESCRIBE <statement>
```

If the token after `DESCRIBE` is a name (an identifier, quoted or not), the statement describes the table or view
of that name (`Error: invalid input: table <name> does not exist` if there is neither). Otherwise it describes the result of the planned statement (1.2) that follows.

The result has one row per column, with the columns `column` (STRING), `type` (STRING) and `nullable` (BOOLEAN):

* For a **table**: each column in table order, with its name, its data type as the schema display names it, and
  whether it is nullable (FALSE for the primary key).
* For a **view**: each view column, with its name and the type and nullability of the corresponding result column
  of the view's query (planned again).
* For a **statement**: one row per result column of a query, in order — `column` is the column's header as
  `[header]` shows it (`item.id`, `n`), or NULL if it has none; a statement that returns no rows (INSERT, UPDATE,
  DELETE, CREATE TABLE, DROP TABLE, and every statement added later that returns no rows) has no rows.

`type` is NULL where the type is **unknown**. The statement is planned as by EXPLAIN — with the same errors, in
the session's transaction or an implicit read-only one — but it is neither optimized nor executed: no expression
is evaluated, except where planning itself evaluates one (LIMIT, OFFSET, column DEFAULTs). So
`DESCRIBE SELECT 1 / 0` succeeds, while `DESCRIBE SELECT * FROM t LIMIT 1 / 0` fails like EXPLAIN does.

### 4.1 Types

The type of an expression is **the data type of the values it returns when its evaluation succeeds** (NULL
aside). It is unknown if the expression returns values of different data types depending on the values (not only
the types) of its operands, and if it can't return any non-NULL value for operands of these types (because its
evaluation then always fails, or always gives NULL). In the rules, the NULL constant returns only NULL, and an
operand of unknown type may return values of any type, also where it can in fact only return NULL or fail: only
the constant `NULL` itself is the NULL constant, not `NULL + 1`, `-NULL`, `s + 1` for a STRING `s`, `MIN(NULL)`,
a view column of unknown type, or `NULL` in a query with `GROUP BY NULL` (which refers to the GROUP BY
expression). So `(NULL + 1) = 1` and `MIN(NULL) = 1` are BOOLEAN.

| Expression | Type |
|---|---|
| a constant | the data type of its value; the NULL constant: unknown |
| a column of a table | its data type |
| a column of a view | the type of the view's query column |
| `a AND b`, `a OR b` | BOOLEAN, unless an operand has a known type other than BOOLEAN, or both operands are the NULL constant (unknown) |
| `NOT a` | BOOLEAN, unless `a` has a known type other than BOOLEAN or is the NULL constant |
| `a = b`, `a != b`, `a < b`, `a <= b`, `a > b`, `a >= b` | BOOLEAN, unless an operand is the NULL constant, or both have known types that can't be compared (they differ and aren't both INTEGER or FLOAT) |
| `a LIKE b` | BOOLEAN, unless an operand is the NULL constant or has a known type other than STRING |
| `a IS NULL`, `a IS NOT NULL` | BOOLEAN |
| `a IS NAN`, `a IS NOT NAN` | BOOLEAN, unless `a` is the NULL constant or has a known type other than FLOAT |
| `a + b`, `a - b`, `a * b`, `a / b`, `a % b` | INTEGER if both are INTEGER; FLOAT if one is FLOAT and the other is INTEGER, FLOAT or of unknown type (not the NULL constant); otherwise unknown |
| `a ^ b` | FLOAT if one is FLOAT and the other is INTEGER, FLOAT or of unknown type (not the NULL constant); otherwise unknown (two INTEGERs give an INTEGER or, for a negative exponent, a FLOAT) |
| `-a`, `+a` | the type of `a` if it is INTEGER or FLOAT; otherwise unknown |
| `a!` | INTEGER if `a` is INTEGER or of unknown type (not the NULL constant); otherwise unknown |
| `sqrt(a)` | FLOAT if `a` is INTEGER, FLOAT or of unknown type (not the NULL constant); otherwise unknown |
| `count(...)` | INTEGER |
| `sum(a)`, `avg(a)` | INTEGER if `a` is INTEGER, FLOAT if `a` is FLOAT, otherwise unknown |
| `min(a)`, `max(a)` | the type of `a` |

* An expression, function or aggregate function **added later** has the type that follows from the principle and
  its definition: e.g. a function defined to return "an integer" for string arguments is INTEGER for a STRING
  argument and unknown for an INTEGER one (its evaluation always fails); an arithmetic operator defined for a data
  type added later has the type its definition gives for those operand types.
* An expression whose value is the value of one of several of its operands, chosen when it is evaluated, has the
  type of those operands if all of them other than NULL constants have the same known type, and is unknown
  otherwise (also when all of them are NULL constants).
* An abbreviation has the type of its expansion.
* DESCRIBE describes the planned statement, and toydb plans `a != b` as `NOT (a = b)`, `a >= b` as
  `a > b OR a = b`, `a <= b` as `a < b OR a = b`, and `a IS NOT NULL` / `a IS NOT NAN` as `NOT (a IS NULL)` /
  `NOT (a IS NAN)`. These forms therefore have the type of the operator they stand for also where they are
  written out (with the same operands `a` and `b`): `NOT (s = 1)` is described like `s != 1`, and
  `s > 1 OR s = 1` like `s >= 1` (both unknown for a STRING `s`). Other combinations follow their own rules:
  `s = 1 OR s > 1` and `NOT (NOT (s = 1))` are BOOLEAN.

### 4.2 Nullability

`nullable` is FALSE only where the value can never be NULL:

* A constant is nullable only if it is NULL.
* A table column is nullable if the table's column is nullable, and also if the column comes from a table that the
  query joins as the inner side of an outer join — the right side of a LEFT JOIN or the left side of a RIGHT JOIN,
  at any level of nesting — whose rows can be missing and filled in with NULLs. A view column is nullable if the
  view's query column is, and by the same outer-join rule.
* `a IS NULL` and `a IS NOT NULL` are never nullable; `count(...)` is never nullable; every other existing aggregate function (`sum`,
  `avg`, `min`, `max`) is always nullable.
* Every other existing operator and function is nullable exactly when one of its operands is.
* An expression whose value is the value of one of several operands, chosen at evaluation, is nullable if one of
  those operands is nullable or if it can return NULL without choosing one.
* An expression or function added later is nullable if one of its operands is, unless its definition says that it
  can return NULL for non-NULL operands (then it is always nullable) or that it never returns NULL (then it never
  is). An aggregate function added later is nullable unless its definition says that it never returns NULL. An
  abbreviation is nullable as its expansion.

Result columns are described from the expressions of the select list (after `*` is expanded to the columns of the
FROM tables and views). A column reference to a GROUP BY expression or an aggregate result is described like that
expression.

```
> CREATE TABLE emp (id INT PRIMARY KEY, name STRING NOT NULL, dept_id INT, salary FLOAT)
> CREATE TABLE dept (id INT PRIMARY KEY, name STRING NOT NULL)
[header]> DESCRIBE SELECT e.id, d.name, salary * 2 AS pay, e.id + 1, NULL, COUNT(*) FROM emp e LEFT JOIN dept d ON e.dept_id = d.id GROUP BY e.id, d.name, salary
---
column, type, nullable
'e.id', 'INTEGER', FALSE
'd.name', 'STRING', TRUE
'pay', 'FLOAT', TRUE
NULL, 'INTEGER', FALSE
NULL, NULL, TRUE
NULL, 'INTEGER', FALSE
```

`DESCRIBE INSERT INTO emp VALUES (1, 'a', NULL, NULL)`
returns no rows and inserts nothing.

---

## 5. Extension: Row estimates

```
ESTIMATE <statement>
```

plans and optimizes the planned statement (1.2) exactly as EXPLAIN does — same plan, same errors, in the session's
transaction or an implicit read-only one — without executing it, and estimates how many rows each line of the plan
display produces. It returns one row per line of the plan display, in order, with the columns `node` (STRING: the
line exactly as the plan display shows it, including its tree prefix such as `└─ ` and `│  `) and `rows`
(INTEGER: the estimate, rounded to the nearest integer, halves rounded up). (The plan display writes names as they
are; for a plan that shows a name containing a line break, the result of ESTIMATE is unspecified.)

Estimates are computed as real numbers (64-bit floating point), from the inputs upwards, without rounding in
between, using the column statistics of ANALYZE (EXT_SPEC 5) as the transaction sees them. They describe the table
as of its last ANALYZE: rows written since then are not counted.

### 5.1 Tables and columns

* The **row count** `R(t)` of a table is its statistics' `rows`, or **1000** if the table has no statistics.
* A column reference in a plan has **column statistics** if it traces to a column of a table that has statistics:
  a column traces through every node that passes it on unchanged — Filter, Order, Limit, Offset, View, a Remap, a
  Projection or Aggregate where the column is a projected or grouped plain column reference, joins (to the
  column of the input it comes from), and every node added later that passes its input's columns on unchanged — to
  a column of a table read by a `Scan`, `KeyLookup` or `IndexLookup` node. Columns computed by expressions,
  aggregate results and `Values` columns have no statistics, nor has a table column that the table's statistics
  don't include (one added to the table after its last ANALYZE). Below, `n`, `z`, `d`, `min`, `max` are the
  statistics' `rows`, `nulls`, `distinct`, `min` and `max` of the column.

### 5.2 Selectivity of conditions

The selectivity `s(p)` of a condition is the estimated fraction of rows for which it is TRUE:

| Condition | Selectivity |
|---|---|
| the constant TRUE / FALSE or NULL | 1 / 0 |
| `c = v` or `v = c`, `c` a column, `v` a constant other than NULL | with statistics: `(1 - z/n) / d`, or 0 if `n` or `d` is 0; without: 0.1 |
| `c = NULL`, `NULL = c` (any comparison with the NULL constant) | 0 |
| `c1 = c2`, two column references | with statistics for both: `1 / max(d1, d2)`, or 0 if both `d` are 0; otherwise 0.1 |
| `c IS NULL` | with statistics: `z/n` (0 if `n` is 0); without: 0.1 |
| `c < v`, `v > c` (`v` a number) | with statistics whose `min` and `max` are finite numbers with a finite difference `max - min` (in 64-bit floating point, so `-1e308` and `1e308` have none), and a finite `v`: `f × (1 - z/n)`, where `f = (v - min) / (max - min)` limited to the range 0..1, or, if `max = min`, 1 if `v > min` and 0 otherwise; otherwise 1/3 |
| `c > v`, `v < c` (`v` a number) | the same with `f = (max - v) / (max - min)` limited to 0..1, or, if `max = min`, 1 if `v < max` and 0 otherwise; otherwise 1/3 |
| `p AND q` | `s(p) × s(q)` |
| `p OR q` | `s(p) + s(q) - s(p) × s(q)` |
| `NOT p` | `1 - s(p)` |
| any other condition (`LIKE`, comparisons of other expressions, `IS NAN`, function calls, ..., and every kind of expression added later) | 0.25 |

The conditions are those of the plan, as it displays them (`v >= 1` is planned as `v > 1 OR v = 1`, `v != 1` as
`NOT v = 1`, so they are estimated as those combinations; `v != NULL` is `NOT v = NULL`, so 1). Constants on either
side are numbers of any numeric type; a range comparison of a column of another type (a string, a boolean, a data
type added later) uses 1/3.

### 5.3 Nodes

`E(x)` is the estimate of node `x`:

| Node | Estimate |
|---|---|
| `Scan: t [(p)]` | `R(t) × s(p)` (`R(t)` without a filter) |
| `KeyLookup: t (k1, ...)` | `min(k, R(t))` for `k` keys |
| `IndexLookup: t.c (v1, ...)` | the sum over the values: `R(t) × s(c IS NULL)` for NULL, `R(t) × s(c = v)` for every other value |
| `Filter: p` | `E(source) × s(p)` |
| `Projection`, `Order`, `Remap`, `View` | `E(source)` |
| `Limit: n` | `min(E(source), n)` |
| `Offset: n` | `max(E(source) - n, 0)` |
| `Aggregate` without GROUP BY expressions | 1 |
| `Aggregate` with GROUP BY expressions | `min(E(source), G)`, where `G` is the product over the GROUP BY expressions of `d + 1` (a column with statistics and `z > 0`), `d` (a column with statistics and `z = 0`), or 10 (any other expression) |
| `HashJoin: inner on l = r` | `E(left) × E(right) × s(l = r)` (the selectivity of the equality of the two columns, above) |
| `HashJoin: outer ...` | `max(E(left) × E(right) × s(l = r), E(left))` |
| `NestedLoopJoin: inner [on p]` | `E(left) × E(right) × s(p)` (`s` = 1 without a condition) |
| `NestedLoopJoin: outer [on p]` | `max(E(left) × E(right) × s(p), E(left))` |
| `Values: ...` | the number of rows |
| `Nothing` | 0 |
| a node added later | the estimate of its first input (the first one displayed below it), or 0 if it has no input |

The statement line of a plan that isn't a query: `Insert: t`, `Update: t (...)`, `Delete: t` estimate the rows of
the node below them (the rows written); `CreateTable: t`, `DropTable: t` are 0; the statement line of a plan added
later is the estimate of the node below it, or 0 if there is none.

```
> CREATE TABLE item (id INT PRIMARY KEY, name STRING INDEX, price FLOAT, qty INT)
> INSERT INTO item VALUES (1, 'a', 1.0, 10), (2, 'b', 2.0, 20), (3, 'c', 3.0, NULL), (4, 'd', 4.0, 20), (5, 'e', 5.0, 30)
> ANALYZE item
> ESTIMATE SELECT name FROM item WHERE price < 3.5 AND qty = 20
---
'Projection: item.name', 1
'└─ Scan: item (item.price < 3.5 AND item.qty = 20)', 1
```

(`price < 3.5`: `f = (3.5 - 1.0) / (5.0 - 1.0) = 0.625`; `qty = 20`: `(1 - 1/5) / 3 = 0.2667`;
`5 × 0.625 × 0.2667 = 0.83`, rounded to 1.)

---

## 6. Extension: Prepared statements

```
PREPARE <name> [( <type>, ... )] AS <statement>
EXECUTE <name> [( <expression>, ... )]
DEALLOCATE <name>
SHOW PREPARED
```

### 6.1 Parameters

A **parameter** `$<n>` — a `$` directly followed by one or more ASCII digits, the number `n` — is an expression
atom, usable wherever a literal is: `WHERE id = $1`, `VALUES ($1, $2)`, `LIMIT $3`, `SET v = $1 + 1`,
`DEFAULT $1`. Leading zeros are allowed (`$01` is `$1`); `$0`, and a number too large for an unsigned 64-bit integer
(above 18446744073709551615, the bound of `AS OF SYSTEM TIME` and `SINCE`), fail to parse with
`Error: invalid input: invalid parameter $<digits as written>`. A `$` that is not followed by a digit
is, as today, `Error: invalid input: unexpected character $`. In parser messages a parameter token is displayed as
`$<n>` (`unexpected token $1`).

A statement that contains a parameter, anywhere (also in a contained statement), can only be run as the statement
of a PREPARE. Any other statement containing a parameter fails after parsing, before anything else (also before
the checks of 1.2), with
`Error: invalid input: parameter $<n> can only be used in a prepared statement` (`<n>` the smallest parameter
number in it); FORMAT and LINT, which don't execute their statement, accept parameters anywhere.

### 6.2 PREPARE

Prepares a planned statement (1.2) under a name, for the session. The types are data types written as in
CREATE TABLE (`INT`, `TEXT`, ... are synonyms); the i-th type is the type of parameter `$i`. Checks, in order:

1. `Error: invalid input: prepared statement <name> already exists` if the session has a prepared statement of
   that name.
2. The statement must be a planned statement: `Error: invalid input: PREPARE is not supported for <KEYWORD>` (1.2).
3. Every parameter of the statement must have a type: `Error: invalid input: no type given for parameter $<n>`
   (`<n>` the smallest parameter number above the number of types). Types without a parameter are allowed.

PREPARE doesn't check the statement against the database and uses no transaction: errors of the statement itself
(unknown tables, columns, ...) are reported when it is executed. Prepared statements belong to the session (in a
cluster: to the client connection), end with it, and are not transactional (ROLLBACK doesn't undo a PREPARE or
DEALLOCATE).

### 6.3 EXECUTE

`EXECUTE <name>` without parentheses passes no arguments; with parentheses, at least one argument. Checks, in
order:

1. `Error: invalid input: prepared statement <name> does not exist`.
2. `Error: invalid input: wrong number of arguments for prepared statement <name>: expected <m>, found <k>`.
3. The arguments, in order: each is a constant expression and is evaluated like a VALUES expression of INSERT,
   with the same errors (`Error: invalid input: expression must be constant, found column x`,
   `Error: invalid input: can't divide by zero`, ...). Its value must be NULL or of the parameter's type (no
   conversion, also not between INTEGER and FLOAT):
   `Error: invalid input: invalid datatype <VALUE TYPE> for <PARAMETER TYPE> parameter $<i>`.

The prepared statement is then executed with every parameter `$i` replaced by the value of argument `i` as a
constant: it behaves exactly — result, errors, transaction, plan display — like the statement written with that
value in place of the parameter (a negative number as the negated literal, NaN as `NAN`, ...). The result of
EXECUTE is the result of that statement.

### 6.4 The plan cache

Each prepared statement keeps at most one **cached plan**: the optimized plan of its most recent execution that
planned it successfully, together with the argument values it was planned for and the definitions of the tables
and views that planning used (looked up). An EXECUTE whose checks pass uses the cached plan if

* its argument values are identical to those of the cached plan (the same data type and equal; NULL is identical
  to NULL and NaN to NaN, but `0.0` and `-0.0`, which are equal, are not identical: they are different values, shown
  differently), and
* every table and view that the cached plan used has exactly the same definition in the executing transaction as
  when it was planned (a table that was dropped and created again with the same definition counts as unchanged),

and otherwise plans the statement anew, replacing the cached plan if planning succeeds (a planning error keeps
the previous cached plan). Either way the plan is executed as usual.

`EXPLAIN EXECUTE`, `DESCRIBE EXECUTE` and `ESTIMATE EXECUTE` check and bind the arguments in the same way and work
on the bound statement, but neither use nor change the cache and don't count as executions.

### 6.5 DEALLOCATE and SHOW PREPARED

`DEALLOCATE <name>` removes the prepared statement and its cached plan
(`Error: invalid input: prepared statement <name> does not exist`).

`SHOW PREPARED` returns one row per prepared statement of the session, ordered by name (code points), with the
columns `name` (STRING), `parameters` (STRING: the types as the schema display names them, separated by `, `; `''`
without parameters), `statement` (STRING: the one-line form (2.2) of the prepared statement, parameters as `$n`),
`executions` (INTEGER: the EXECUTEs whose checks passed) and `plans` (INTEGER: how many of them planned the
statement instead of using the cached plan, whether or not planning succeeded).

```
> CREATE TABLE t (id INT PRIMARY KEY, v STRING)
> PREPARE get (INT) AS SELECT v FROM t WHERE id = $1
> INSERT INTO t VALUES (1, 'a'), (2, 'b')
> EXECUTE get (1)
> EXECUTE get (1)
> EXECUTE get (2)
!> EXECUTE get ('x')
> SHOW PREPARED
---
'a'
'a'
'b'
Error: invalid input: invalid datatype STRING for INTEGER parameter $1
'get', 'INTEGER', 'SELECT v FROM t WHERE id = $1', 3, 2
```

### 6.6 Prepared statements and the tier-3 extensions

* **Digests**: parameters are written as themselves (`$1`), not as `?`. `PREPARE <name> [(<type>, ...)] AS
  <digest>`, `EXECUTE <name> [(<expr>, ...)]` (`EXECUTE get (?)`), `DEALLOCATE <name>`, `SHOW PREPARED`. An EXECUTE
  is recorded as the EXECUTE statement only, with the rows of the statement it executed.
* **JSON results**: `{"result":"prepare","name":<name>}`, `{"result":"deallocate","name":<name>}`; EXECUTE has the
  JSON form of the executed statement's result.

---

## 7. Extension: Statement lint

```
LINT <statement>
```

checks the statement — and every statement contained in it, at any depth — for likely mistakes, without planning
or executing it, and returns one row per **warning**, with the columns `code` (STRING) and `message` (STRING). The
statement must parse; LINT fails for no other reason. It reads table and view definitions in the session's
transaction or an implicit read-only one; names that don't resolve are simply not checked by the rules that need
them.

In messages, `<expr>` is the canonical text (1.5) of the expression concerned, and names are written by 1.3.

### 7.1 Rules

| Code | Applies to | Message |
|---|---|---|
| `unfiltered_write` | an UPDATE or DELETE without WHERE (statements added later are not checked by this rule) | `UPDATE without WHERE changes every row of <table>` / `DELETE without WHERE deletes every row of <table>` |
| `implicit_columns` | an INSERT without a column list, in every form of INSERT (also forms added later) | `INSERT without a column list depends on the column order of <table>` |
| `null_comparison` | a comparison (`=`, `!=`, `<`, `<=`, `>`, `>=`) one of whose operands is the NULL constant | `comparison with NULL is never true: <expr>` |
| `type_mismatch` | a comparison (`=`, `!=`, `<`, `<=`, `>`, `>=`) between a column reference whose type is known and a literal — also with a prefix `-` or `+` — or a parameter (6.1), of a known type that can't be compared with it (the types differ and aren't both INTEGER or FLOAT; NULL is not a type) | `<TYPE> column <column> compared with <TYPE> value <literal>`, or `... with <TYPE> parameter $<n>` |
| `like_without_wildcard` | `a LIKE b` where `b` is a string literal that contains neither `%` nor `_` | `LIKE without wildcards matches like =: <expr>` |
| `division_by_zero` | `a / b` or `a % b` where `b` is the integer literal `0` | `division by zero: <expr>` |
| `constant_condition` | a WHERE, HAVING or JOIN ... ON condition (the whole condition) that contains no column reference, no aggregate function and no parameter | `condition is constant: <expr>` |
| `order_by_constant` | an ORDER BY expression that contains no column reference, no aggregate function and no parameter | `ORDER BY a constant has no effect: <expr>` |

* `<column>` in `type_mismatch` is the column reference as written (by 1.5: `id`, `e.id`); `<literal>` is the
  literal by 1.4, with its sign (`-1`); `<TYPE>`s are data type names as the schema display writes them. The column's type is found by
  resolving the reference against the tables and views that the statement reads: those of the FROM clause of a
  SELECT, and the table of an UPDATE or DELETE. Of these, only the tables and views that exist and whose columns
  DESCRIBE can describe are considered (a name that doesn't resolve is left out, as is a view whose query can't be
  planned). A qualified reference `x.c` resolves to the FROM item whose alias
  is `x`, or, for an item without alias, whose name is `x` (as the planner resolves it), if there is exactly one such
  item (`FROM t x, u x`, which the planner rejects, has two); an unqualified one to the
  only such table or view that has a column of that name. Select-list aliases are not considered. A reference that doesn't resolve this way (unknown,
  ambiguous, in an INSERT, ...) is not checked; a view column of unknown type (section 4) is not checked. The type
  of a parameter is its type in the PREPARE that contains it; a parameter outside a PREPARE is not checked.
* **Abbreviations** are checked as their expansions: a comparison, LIKE or division that is part of the expansion
  of a construct is checked like one written in the statement, but the warning concerns the construct as written:
  `<expr>` is the construct's canonical text, and the warning's position (7.2) is the construct's. (For example, a
  construct `x IN (1, NULL)` defined as `x = 1 OR x = NULL` gets the warning
  `comparison with NULL is never true: x IN (1, NULL)`.) One warning is given per comparison of the expansion that
  the rule applies to.
* The rules apply to every expression of the statement, wherever it appears (select items, conditions, GROUP BY,
  ORDER BY, LIMIT, OFFSET, VALUES, SET, DEFAULT, EXECUTE arguments, and the parts of constructs added later), and
  to constructs added later as stated: `null_comparison`, `type_mismatch` and `like_without_wildcard` concern
  exactly the operators named (and expansions into them); an operator or function added later is not one of
  them, whatever it means.

### 7.2 Order

Each warning concerns a part of the statement: `unfiltered_write` and `implicit_columns` the (contained)
statement, the other rules an expression. Warnings are listed in the order in which these parts begin in the
statement's canonical text (left to right in its one-line form); a statement before the expressions it contains,
an expression before the expressions it contains; warnings about the same part in the order of the table above,
and several `type_mismatch` warnings of one construct in the order of the comparisons in its expansion.

```
> CREATE TABLE t (id INT PRIMARY KEY, s STRING, n INT)
> LINT UPDATE t SET n = n / 0 WHERE s = 1 OR id = NULL
---
'division_by_zero', 'division by zero: n / 0'
'type_mismatch', 'STRING column s compared with INTEGER value 1'
'null_comparison', 'comparison with NULL is never true: id = NULL'
```

(The SET expression comes before the WHERE clause in the canonical text; the UPDATE has a WHERE clause, so
`unfiltered_write` doesn't apply.)

---

## 8. Extension: Change log

```
SHOW CHANGES [SINCE <version>]
```

Every server keeps a **change log**: a list of the changes made by committed transactions, in commit order.
SHOW CHANGES returns it, oldest first; with `SINCE <version>` (a non-negative integer literal) only the changes of
transactions whose version is greater than `<version>`.

### 8.1 Changes

Each change is one row, with the columns:

| Column | Type | Value |
|---|---|---|
| `version` | INTEGER | the version of the transaction that made the change (as `[json] BEGIN` shows it) |
| `table` | STRING | the table or view changed (as stored) |
| `op` | STRING | the kind of change |
| `key` | the type of the table's primary key | the primary key of the row, for row changes; NULL otherwise |
| `data` | STRING | as below, or NULL |

| Change | `op` | `data` |
|---|---|---|
| a row inserted (INSERT, IMPORT, and every statement that inserts rows) | `insert` | the row as a JSON object, as one line of `EXPORT ... AS JSON` writes it (EXT_SPEC 6.1) |
| a row updated without changing its primary key | `update` | the new row, as for `insert` |
| a row updated with a new primary key | – | recorded as `delete` of the old key, followed by `insert` of the new row |
| a row deleted | `delete` | NULL |
| a table created | `create_table` | the table's schema display in one line (its lines without indentation, joined by single spaces, without a space after the opening `(` and before the closing `)`) |
| a table dropped | `drop_table` | NULL (its rows, index entries and statistics are not recorded separately) |
| a table's statistics stored by ANALYZE | `analyze` | NULL |
| a view created | `create_view` | `CREATE VIEW <name> (<column>, ...) AS <query>`, as EXPORT AS SQL writes it (3.5), without the `;` |
| a view dropped | `drop_view` | NULL |
| a table's definition changed by a statement added later | the statement name (EXT_SPEC 2.1), e.g. `foo_table` | the table's new schema display in one line, as for `create_table` |

The rows that a statement added later writes are recorded as inserts, updates and deletes as above, except the
row changes that are part of a change of a table's definition (for example values filled in for an added column),
which are not recorded separately.

**Order.** Transactions in the order they committed; within a transaction, in the order the changes were made:
statement by statement; within a statement, INSERT in the order of its rows, UPDATE in primary key order (of the
old keys), DELETE in the order in which it found the rows (primary key order, except for a primary key lookup,
which finds them in the order of the keys of its plan, `KeyLookup: t (3, 1)`). UPDATE and DELETE record one change
for each row they count (the `count` of their result): a DELETE whose primary key lookup lists a key twice
(`WHERE id = 2 OR id = 2`, plan `KeyLookup: t (2, 2)`, `{"result":"delete","count":2}`) records two `delete`
changes of key 2, and an UPDATE with that lookup, which counts the row once, records one `update`.

* Only committed changes are recorded: a rolled-back transaction, a read-only one and a failed statement in an
  implicit transaction record nothing. Inside an explicit transaction, a failed statement keeps the writes it made
  before it failed (as today), and they are recorded when the transaction commits. A statement that changes
  nothing (an UPDATE that matches no row, `DROP TABLE IF EXISTS` of a missing table) records nothing.
* The log is not part of the stored data: it is kept in memory from the time the server (or, in the SQL runner,
  the test engine) started, is not written to storage (nothing that `dump`, `[ops]`, `status` or `toydump` show
  changes), and is lost on restart. In a cluster every node records the changes as it applies the Raft log, so all
  nodes have the same log, and SHOW CHANGES shows it through every node.
* SHOW CHANGES uses no transaction. It shows every change committed so far, also those committed after the
  session's transaction began, and never the uncommitted changes of any transaction, its own included.

Parse errors: `SHOW CHANGES SINCE` → `Error: invalid input: unexpected end of input`; `SHOW CHANGES SINCE x` →
`Error: invalid input: unexpected token x, wanted number` (as for `AS OF SYSTEM TIME`); anything after the
statement: `Error: invalid input: unexpected token <token>`.

```
> CREATE TABLE t (id INT PRIMARY KEY, v STRING)
> INSERT INTO t VALUES (1, 'a'), (2, NULL)
> UPDATE t SET v = 'b' WHERE id = 2
> DELETE FROM t WHERE id = 1
> SHOW CHANGES SINCE 1
---
2, 't', 'insert', 1, '{\"id\":1,\"v\":\"a\"}'
2, 't', 'insert', 2, '{\"id\":2,\"v\":null}'
3, 't', 'update', 2, '{\"id\":2,\"v\":\"b\"}'
4, 't', 'delete', 1, NULL
```

(Strings are displayed as in query results, which show `"` as `\"` and `'` as `\'`.)

---

## 9. Syntax summary and changes to the tier-3 extensions

### 9.1 Parsing

The statements of 1.1, with the existing parser messages for the first token that doesn't fit (tokens displayed
as usual):

| Input | Error |
|---|---|
| `FORMAT`, `DESCRIBE`, `ESTIMATE`, `LINT`, `CREATE VIEW v AS`, `PREPARE p AS`, `EXECUTE`, `DEALLOCATE`, `DROP VIEW`, `EXECUTE p (1,` | `Error: invalid input: unexpected end of input` |
| `FORMAT x`, `ESTIMATE 1` | `Error: invalid input: unexpected token x` (resp. `1`) |
| `CREATE VIEW 1 AS SELECT 1`, `DROP VIEW 1`, `PREPARE 1 AS SELECT 1`, `EXECUTE 1`, `DEALLOCATE 1` | `Error: invalid input: expected identifier, got 1` |
| `CREATE VIEW v SELECT 1` | `Error: invalid input: expected token AS, found SELECT` |
| `CREATE VIEW v AS INSERT INTO t VALUES (1)` | `Error: invalid input: expected token SELECT, found INSERT` |
| `CREATE VIEW v () AS SELECT 1`, `CREATE VIEW v (a,) AS SELECT 1` | `Error: invalid input: expected identifier, got )` |
| `CREATE VIEWS v AS SELECT 1`, `DROP x` | `Error: invalid input: expected token TABLE, found VIEWS` (resp. `found x`): after `CREATE` and `DROP`, anything but `VIEW` is read as today |
| `SHOW VIEW` | `Error: invalid input: unexpected token VIEW` (also `SHOW PREPARE`, `SHOW CHANGE`, ...) |
| `PREPARE p (BLOB) AS SELECT 1`, `PREPARE p () AS SELECT 1` | `Error: invalid input: unexpected token blob` (resp. `)`) |
| `PREPARE p SELECT 1`, `PREPARE p (INT) SELECT 1` | `Error: invalid input: expected token AS, found SELECT` |
| `EXECUTE p ()` | `Error: invalid input: expected expression atom, found )` |
| anything after a complete statement: `SHOW VIEWS x`, `DEALLOCATE p q`, `EXECUTE p (1) 2`, `DESCRIBE t u` | `Error: invalid input: unexpected token x` (resp. `q`, `2`, `u`) |

A statement that fails to parse has no effect.

### 9.2 Additions to the tier-3 extensions

* **Statement digests** (EXT_SPEC 3): every new statement has a digest by EXT_SPEC 3.2: `FORMAT <digest>`,
  `CREATE VIEW ...` (3.5), `DESCRIBE <name>`, `DESCRIBE <digest>`, `ESTIMATE <digest>`, `PREPARE ...` and
  `EXECUTE ...` (6.6), `LINT <digest>`, `SHOW CHANGES [SINCE ?]`. Parameters are written `$n`. A statement that
  fails after parsing (1.2, 6.1) is recorded as a failed call. `rows` counts the rows of the new statements that
  return rows (1.6) and is 0 for FORMAT, CREATE VIEW, DROP VIEW, PREPARE and DEALLOCATE.
* **JSON output** (EXT_SPEC 2): the new results of 2, 3.5 and 6.6; the `View` node (3.2).
* **EXPORT AS SQL**: views (3.5).
* **`toysql`**: FORMAT lines are printed as they are, like EXPORT lines.

---

## 10. Implementation requirements for the tier-4 base (not user documentation)

The experiment's fairness and documentation-parity rules are in `EXT4_DESIGN_NOTES.md` and `EXT4_DOCS.md`
(experimenter-only); the code of each language must be what a strong engineer naturally writes in that language,
and every new module carries the doc comment text of `EXT4_DOCS.md`.

1. **Nothing existing changes.** All 235 tier-3 golden script runs and all unit tests produce byte-identical
   output, except unit tests that pin the set or number of keywords (Python `tests/sql/parser/test_lexer.py`:
   75 → 87) and the lexer's handling of `$`. In particular no existing statement writes anything new to storage
   (views are written only by CREATE/DROP VIEW; the change log is in memory), and the plan display, schema display,
   value display, error messages and client protocol of existing results are unchanged.
2. **Keywords**: the twelve of 1.1. **Lexer**: the parameter token of 6.1.
3. **AST**: statements for FORMAT, CREATE VIEW (name, optional column list, query), DROP VIEW (name, if_exists),
   SHOW VIEWS, DESCRIBE (a name or a statement), ESTIMATE, PREPARE (name, types, statement), EXECUTE (name,
   argument expressions), DEALLOCATE, SHOW PREPARED, LINT, SHOW CHANGES (optional version); a parameter expression
   (number). The tier-3 desugaring pass handles all of them. The planner never sees a parameter.
4. **Statement results** appended after the existing variants (the enum is a wire format): Format (lines),
   CreateView (name), DropView (name, existed), Prepare (name), Deallocate (name). The row-returning statements
   return Select results. Python: the hand-written `StatementResult` serde gets the new variants.
5. **Views storage**: a view definition type (name, columns, query text, dependencies) with serde; a new local
   engine key appended after the existing key variants; catalog methods to create, drop, get and list views;
   matching Raft write commands (create/drop view) and read commands (get/list views), appended after the existing
   variants, applied by the state machine through the local engine; `encoding/format` (toydump, `dump`) formats the
   key and the commands readably. CREATE TABLE checks the view namespace. The DROP TABLE dependency check of 3.3.
6. **Planner**: FROM resolves views (parse the stored query, desugar, plan it as a SELECT in the same catalog) into
   a new plan node `View` (view name, alias, column names, source), handled by every exhaustive node function
   (columns, labels, transform, display, JSON, execution).
7. **Change log**: kept by the local engine (shared by its transactions), with per-transaction pending changes
   that are appended at commit and discarded at rollback; read through a Raft read command on the leader. Not
   written to storage.
8. **Session**: FORMAT, SHOW VIEWS, PREPARE/EXECUTE/DEALLOCATE/SHOW PREPARED (per-session prepared statements and
   plan cache), DESCRIBE, ESTIMATE, LINT and SHOW CHANGES; the parameter check of 6.1; EXPLAIN/DESCRIBE/ESTIMATE of
   EXECUTE; the "not supported" checks of 1.2. Digests recorded as in 9.2.
9. **Runners** (test code, both languages): FORMAT lines printed like EXPORT lines by both runners. The golden
   scripts of the tier-4 extensions live in a regression directory of each workspace (`src/sql/testscripts/ext4/`
   + `tests/scripts/ext4/` in Rust; `tests/sql/testscripts/ext4/` + `tests/e2e/scripts/ext4/` in Python, registered
   like `ext/`). They are the scripts of `ext4_scripts/*/sql/` and `ext4_scripts/*/cluster/`.
10. **toysql**: FORMAT lines printed as they are; short messages for CREATE/DROP VIEW, PREPARE, DEALLOCATE in text
    format (e.g. `Created view v`).
11. **Size**: about 10,000 lines of application code per language for the seven extensions (see the design notes
    for the split).
