# Tier 3 extension layers — specification

Tier 3 is the complete tier-2 toydb (SQL engine, Raft, server, client, binaries) plus four **extension layers**.
Each extension is a realistic subsystem that consumes toydb's core types — values and data types, the AST, plan
expressions, plan nodes and aggregates, the catalog's table and column schemas, statement results, the Raft SQL
engine's read and write commands — and is reachable from SQL or from the client:

| | Extension | Reached through |
|---|---|---|
| A | **JSON output** — a stable JSON encoding of statement results, query plans and values | the `[json]` tag of both golden-script runners; `toysql` (`!format json`) |
| B | **Statement digests** — per-session statement statistics, keyed by a normalized statement text | `SHOW STATEMENTS` |
| C | **Column statistics** — `ANALYZE` computes per-column statistics, stored transactionally in the catalog | `ANALYZE`, `SHOW STATISTICS` |
| D | **Export and import** — tables as CSV, JSON lines and SQL; import of CSV and JSON lines | `EXPORT`, `IMPORT` |

This document is language-neutral. Sections 1–7 define the user-facing behaviour completely; the extensions' user
documentation is derived from them. Section 8 lists requirements for implementing the tier-3 base; it is not user
documentation.

**Generic rules.** Every output is defined by rules stated in terms of values, data types, SQL syntax, the plan
display and the schema display — not by a closed list of cases. Where a rule lists the existing cases, it also says
how a case that is added to toydb later (a data type, a kind of literal, expression, plan node, aggregate function,
statement or statement result) is handled. The correct output for such an addition follows from the rules together
with the addition's own SQL syntax, value display, plan display and schema display.

**Conventions.** Examples use the golden-script conventions of the SQL runner: `> statement`, the output below
`---`, `Error: <message>` for errors, `ok` for no output. *Displayed as in query results* means the value display of
result rows (`NULL`, `TRUE`, `1`, `-0.5`, `1e100`, `NaN`, `inf`, `'text'`). The *plan display* is the text that
`[plan]` and `EXPLAIN` show; the *schema display* is the `CREATE TABLE` text that the `schema` command (and the
cluster runner's `table`) shows. Error messages are given as complete output lines.

---

## 1. Shared definitions

### 1.1 JSON text

All JSON written by the extensions is **compact** — no whitespace outside strings: `{"a":1,"b":[true,null]}`.
Object members are written in the order this document lists them; array elements in the order stated.

* A **string** is written between `"`, escaping `"` as `\"`, `\` as `\\`, U+0008 as `\b`, U+000C as `\f`, U+000A
  as `\n`, U+000D as `\r`, U+0009 as `\t`, and every other character below U+0020 as `\u00XX` with two lower-case
  hex digits (`\u0001`, `\u001b`). Every other character is written as itself: `/`, U+007F and all non-ASCII
  characters are not escaped (`"é ☃ 👋"`).
* An **integer** is written in decimal, with `-` if negative.
* `true`, `false`, `null`.

### 1.2 Values as JSON

| Value | JSON |
|---|---|
| `NULL` | `null` |
| `TRUE`, `FALSE` | `true`, `false` |
| an INTEGER | an integer: `42`, `-9223372036854775808` |
| a finite FLOAT | the value exactly as displayed in query results — always a valid JSON number: `1.0`, `-0.0`, `0.30000000000000004`, `1e100`, `1.5e-7`, `1.2345678901234568e20` |
| a STRING | a string: `'a"b'` is `"a\"b"` |
| a FLOAT that is NaN or infinite, and every value of any other data type | the **typed form** `{"type":<T>,"value":<S>}`: `<T>` is the value's data type as the schema display names it, `<S>` a string holding the value as displayed in query results |

So NaN is `{"type":"FLOAT","value":"NaN"}`, infinity `{"type":"FLOAT","value":"inf"}`, negative infinity
`{"type":"FLOAT","value":"-inf"}`. A data type added to toydb later has no native JSON form: its values always use
the typed form (a value displayed `x` of a type named `T` is `{"type":"T","value":"x"}`).

### 1.3 Values as SQL literals

The *SQL literal* of a value is SQL text that produces exactly that value when parsed and evaluated:

| Value | SQL literal |
|---|---|
| `NULL`, `TRUE`, `FALSE` | `NULL`, `TRUE`, `FALSE` |
| an INTEGER | its decimal digits, with `-` if negative (`42`, `-7`); the smallest integer, which has no single literal, is `-9223372036854775807 - 1` |
| a finite FLOAT | as displayed in query results (`1.0`, `0.0`, `-2.5`, `1e100`, `1.5e-7`) |
| a FLOAT NaN, infinity, negative infinity | `NAN`, `INFINITY`, `-INFINITY` |
| a STRING | between single quotes, with each `'` doubled (`'it''s'`); no other character is escaped |
| a value of any other data type | the value as the plan display shows it as a constant (the plan display shows constants of such types as the SQL text that produces them) |

### 1.4 Values as CSV fields

| Value | CSV field |
|---|---|
| `NULL` | the empty field (no characters) |
| a STRING | the string itself, quoted if necessary (4.2) |
| every other value, of any data type | the value as displayed in query results: `TRUE`, `-7`, `1.5`, `0.0`, `NaN`, `inf`, `-inf`, `1e100` |

### 1.5 Reading values from text

Import reads values from text (CSV fields, and the `"value"` of a typed JSON form). The text must be exactly one of
the following, depending on the column's data type; anything else is an invalid value.

| Data type | Accepted text | Value |
|---|---|---|
| BOOLEAN | `true` or `false` in any letter case (`TRUE`, `False`, `fAlSe`) | the boolean |
| INTEGER | an optional `-`, then one or more ASCII digits, within the 64-bit signed range (`007` is 7, `-0` is 0; `+1`, `1.0`, `1e3`, ` 1` are invalid) | the integer |
| FLOAT | `NaN`, `inf` or `-inf` (exactly these spellings); or an optional `-`, one or more ASCII digits, optionally `.` and one or more digits, optionally `e` or `E` followed by an optional `+` or `-` and one or more digits (`1`, `-0.5`, `2.5e-3`, `1E10`; `.5`, `5.`, `+1`, `Infinity`, `nan` are invalid) | the nearest 64-bit float (`-0` gives -0.0, which is stored as 0.0 like every -0.0), rounded as IEEE 754 rounds: a number beyond the range of FLOAT gives infinity or negative infinity (`1e400`, `-1e400`), one too small for it 0.0 (`1e-400`) |
| STRING | any text | the text |
| any other data type | the value as displayed in query results, for a valid value of the type | that value |

Text is never trimmed. For a data type added later, the accepted texts are exactly the query-result displays of the
type's valid values, so every value written by export is read back as the same value.

### 1.6 Names in SQL text

Where the extensions write SQL text (digests, SQL export), a table, column, alias or function name is written as
the schema display writes names: unquoted if, written without quotes, it would be read as a single identifier — it
begins with a letter, continues with letters, digits and `_`, and is not a keyword (letter case is not considered,
so a name created as `"Name"` is written `Name`, as in the schema display) — and otherwise between double quotes
with every `"` doubled: `"in"`, `"a b"`, `"x""y"`, `"show"`. Keywords added to toydb later are quoted like the
existing ones. CSV headers, JSON member names, and table names in JSON results are the stored names, never quoted
for SQL.

---

## 2. Extension A: JSON output

Every statement result has a JSON form, written as one line of compact JSON. It is used

* by **`toysql`**: `!format json` prints every result in its JSON form, `!format text` returns to the normal
  display (the default);
* by **both golden-script runners**, through the `[json]` tag: `[json]> statement` prints the JSON form of the
  statement's result **instead of** the runner's normal output for it (result rows, export lines, a plan, or
  nothing). The statement is executed exactly as without the tag. Errors are printed as usual (`Error: ...`). The
  tag combines with the other tags (`[plan]` output comes first; `[header]` has no effect on the JSON line).

### 2.1 Statement results

| Result of | JSON |
|---|---|
| `BEGIN` | `{"result":"begin","version":<v>,"read_only":<bool>}` — the version and mode of the new transaction (the values that `[result]` shows) |
| `COMMIT` | `{"result":"commit","version":<v>}` |
| `ROLLBACK` | `{"result":"rollback","version":<v>}` |
| `EXPLAIN` | `{"result":"explain","plan":<plan>}` (2.2) |
| `CREATE TABLE` | `{"result":"create_table","table":<name>}` |
| `DROP TABLE` | `{"result":"drop_table","table":<name>,"existed":<bool>}` |
| `INSERT`, `UPDATE`, `DELETE` | `{"result":"insert","count":<n>}`, `{"result":"update","count":<n>}`, `{"result":"delete","count":<n>}` |
| a statement that returns rows (`SELECT`, `SHOW STATEMENTS`, `SHOW STATISTICS`) | `{"result":"select","columns":[<label>,...],"rows":[[<value>,...],...]}` |
| `ANALYZE` | `{"result":"analyze","tables":[<name>,...]}` (section 5) |
| `EXPORT` | `{"result":"export","format":<"csv", "json" or "sql">,"lines":[<string>,...]}` (section 6) |
| `IMPORT` | `{"result":"insert","count":<n>}` — an import is an insert |
| a statement added later | `{"result":<statement name>}`, followed by `"table":<name>` if the statement acts on one named table |

* Names are the stored names. A column `<label>` is the column's header as `[header]` shows it (`"item.id"`,
  `"n"`), or `null` for a column without a name (an empty header). Values are written by 1.2.
* The *statement name* is the statement's leading keywords in lower case joined by `_`, without optional words and
  without the table name or other operands: `create_table`, `drop_table`, `insert`. A statement `FOO BAR t ...`
  added later has the name `foo_bar`.

```
> CREATE TABLE item (id INT PRIMARY KEY, name STRING INDEX, price FLOAT, qty INT)
> INSERT INTO item VALUES (1, 'apple', 1.5, 10), (2, 'pear', 2.25, 0), (3, 'fig', NULL, 7)
---
ok

[json]> SELECT id, name AS n, qty * 2 FROM item WHERE id < 3
---
{"result":"select","columns":["item.id","n",null],"rows":[[1,"apple",20],[2,"pear",0]]}

[json]> UPDATE item SET qty = 1 WHERE id = 2
---
{"result":"update","count":1}
```

### 2.2 Plans

The plan in an `EXPLAIN` result is the optimized plan that the plan display shows. Its JSON form describes the same
plan: everything the plan display shows is in it, in structured form.

**The plan** is an object whose first member is `"statement"`:

| Plan display | JSON |
|---|---|
| a node tree (a query) | `{"statement":"select","root":<node>}` |
| `Insert: t` over a node | `{"statement":"insert","table":"t","source":<node>}` |
| `Update: t (c1=e1, c2=e2)` over a node | `{"statement":"update","table":"t","set":[{"column":"c1","expr":<expr>},{"column":"c2","expr":<expr>}],"source":<node>}` (assignments in display order) |
| `Delete: t` over a node | `{"statement":"delete","table":"t","source":<node>}` |
| `CreateTable: t` | `{"statement":"create_table","table":"t"}` |
| `DropTable: t` | `{"statement":"drop_table","table":"t"}` |
| a plan added later, displayed `Name: t` | `{"statement":<Name in snake case>,"table":"t"}`, followed by `"source":<node>` if a node tree is displayed below it |

*Snake case* turns a CamelCase display name into lower-case words joined by `_`: `CreateTable` → `create_table`,
`HashJoin` → `hash_join`, `Distinct` → `distinct`.

**Nodes** are objects whose first member is `"node"` (the node's display name in snake case), followed by the
node's parameters, followed by its inputs (`"source"` for one input; `"left"`, `"right"` for two):

| Plan display | JSON |
|---|---|
| `Scan: t [as a] [(filter)]` | `{"node":"scan","table":"t","alias":<"a" or null>,"filter":<expr or null>}` |
| `KeyLookup: t [as a] (k1, ...)` | `{"node":"key_lookup","table":"t","alias":<...>,"keys":[<value>,...]}` |
| `IndexLookup: t.c [as a.c] (v1, ...)` | `{"node":"index_lookup","table":"t","alias":<...>,"column":"c","values":[<value>,...]}` |
| `Filter: p` | `{"node":"filter","predicate":<expr>,"source":<node>}` |
| `Projection: e1, e2 as x` | `{"node":"projection","expressions":[{"expr":<expr>,"alias":null},{"expr":<expr>,"alias":"x"}],"source":<node>}` |
| `Order: e1 desc, e2 asc` | `{"node":"order","key":[{"expr":<expr>,"direction":"desc"},{"expr":<expr>,"direction":"asc"}],"source":<node>}` |
| `Limit: n` | `{"node":"limit","limit":<n>,"source":<node>}` |
| `Offset: n` | `{"node":"offset","offset":<n>,"source":<node>}` |
| `Aggregate: g1, ..., f1(...), ...` | `{"node":"aggregate","group_by":[<expr>,...],"aggregates":[<expr>,...],"source":<node>}` |
| `HashJoin: inner on l = r` | `{"node":"hash_join","join":<"inner" or "outer">,"left_column":<column>,"right_column":<column>,"left":<node>,"right":<node>}` |
| `NestedLoopJoin: inner [on p]` | `{"node":"nested_loop_join","join":<"inner" or "outer">,"predicate":<expr or null>,"left":<node>,"right":<node>}` |
| `Remap: c1, c2 [(dropped: c3)]` | `{"node":"remap","columns":[<column or null>,...],"dropped":[<column>,...],"source":<node>}` |
| `Values: ...` | `{"node":"values","rows":[[<expr>,...],...]}` |
| `Nothing` | `{"node":"nothing"}` |
| a node added later, displayed `Name` or `Name: ...` | `{"node":<Name in snake case>}`, then one member per parameter that its display line shows (none if the line shows only the name), named and written like the members above, then its inputs |

* Lists are always complete, also where the display abbreviates them: `KeyLookup: t (12 keys)` has all 12 keys,
  `Values: 3 rows` all three rows, `IndexLookup: t.c (0 values)` is `"values":[]`, and `Values: blank row` is
  `"rows":[[]]`.
* `<column>` is `{"column":<i>,"label":<L>}` for column number `<i>` (from 0) of the relevant input, where `<L>` is
  the column's label as the plan display shows it (`"item.name"`, `"q"`), or `null` where the display shows `#<i>`
  (`left #<i>`, `right #<i>`). `left_column` is a column of the left input, `right_column` one of the right input.
  For `remap`, `columns` has one entry per output column, in order — the input column it comes from, or `null`
  where the display shows `Null` — and `dropped` lists the unused input columns in increasing order (`[]` when the
  display shows no `(dropped: ...)`).
* `"alias"` of a projection expression is the alias as displayed after `as`, or `null`.
* Column references inside expressions are resolved exactly as by the plan display, so their numbers and labels are
  the ones that the display resolves (e.g. a join's predicate counts the columns of both inputs, left first).

**Expressions.**

* A constant is `{"constant":<value>}` (value by 1.2).
* A column reference is `<column>`: `{"column":3,"label":"item.qty"}`, `{"column":1,"label":null}` for `#1`.
* Every other expression — every operator, function call, aggregate function call, and every kind of expression
  added later — is `{"op":<T>,"args":[<expr>,...]}`. `args` are its direct sub-expressions, in the order in which
  they appear in its plan display. `<T>` is the expression's own plan display with the display of each direct
  sub-expression replaced by `_`; parentheses that the display puts around a sub-expression belong to it and
  disappear with it. Everything else — keywords, symbols, function names, spaces — is kept exactly as displayed.
  The structure is that of the expression tree, which the display does not always show (`a - (b - 1)` and
  `a - b - 1` are both displayed `t.a - t.b - 1` but have different JSON forms).
* An expression whose plan display is that of a combination of other expressions (because it means exactly that
  combination) is written as that combination. A unary `+` is not displayed and is written as its operand.

| Plan display | `"op"` |
|---|---|
| `a AND b`, `a OR b`, `NOT a` | `_ AND _`, `_ OR _`, `NOT _` |
| `a = b`, `a > b`, `a < b` | `_ = _`, `_ > _`, `_ < _` |
| `a IS NULL`, `a IS NAN` | `_ IS NULL`, `_ IS NAN` |
| `a + b`, `a - b`, `a * b`, `a / b`, `a % b`, `a ^ b` | `_ + _`, `_ - _`, `_ * _`, `_ / _`, `_ % _`, `_ ^ _` |
| `a!`, `-a` | `_!`, `-_` |
| `sqrt(a)`, `sqrt((a + 1))` | `sqrt(_)` (both) |
| `a LIKE b` | `_ LIKE _` |
| `count(TRUE)`, `sum(t.v)`, `avg(...)`, `min(...)`, `max(...)` | `count(_)`, `sum(_)`, `avg(_)`, `min(_)`, `max(_)` |

Consequences: `v >= 1` is planned and displayed as `t.v > 1 OR t.v = 1` and written as `_ OR _` of two comparisons;
`v != 1` and `v IS NOT NULL` as `NOT _` of `_ = _` and of `_ IS NULL`. A construct displayed with several
keyword-separated parts has one `_` per part (an expression displayed `FROB t.a WITH t.b` would be
`{"op":"FROB _ WITH _","args":[...]}`); a function or aggregate added later is written like `sqrt` and `count`
(displayed `frob(t.v)`: `"op":"frob(_)"`; displayed with a word before the argument, `frob(all t.v)`:
`"op":"frob(all _)"`).

```
> EXPLAIN SELECT name, qty * 2 AS q FROM item WHERE qty > 5 ORDER BY price DESC
```

has the plan display

```
Remap: item.name, q (dropped: item.price)
└─ Order: item.price desc
   └─ Projection: item.name, item.qty * 2 as q, item.price
      └─ Scan: item (item.qty > 5)
```

and the JSON form

```
{"result":"explain","plan":{"statement":"select","root":{"node":"remap","columns":[{"column":0,"label":"item.name"},{"column":1,"label":"q"}],"dropped":[{"column":2,"label":"item.price"}],"source":{"node":"order","key":[{"expr":{"column":2,"label":"item.price"},"direction":"desc"}],"source":{"node":"projection","expressions":[{"expr":{"column":1,"label":"item.name"},"alias":null},{"expr":{"op":"_ * _","args":[{"column":3,"label":"item.qty"},{"constant":2}]},"alias":"q"},{"expr":{"column":2,"label":"item.price"},"alias":null}],"source":{"node":"scan","table":"item","alias":null,"filter":{"op":"_ > _","args":[{"column":3,"label":"item.qty"},{"constant":5}]}}}}}}}
```

(The SQL runner prints nothing for an `EXPLAIN` without `[json]`, as before; the cluster runner prints the plan
display, as before.)

---

## 3. Extension B: Statement digests

Each session keeps statistics about the statements it executes, grouped by **digest**: the statement's canonical
SQL text with every literal replaced by `?`. Statements that differ only in literal values, letter case,
whitespace, redundant parentheses, optional words or synonymous spellings have the same digest.

### 3.1 SHOW STATEMENTS

```
SHOW STATEMENTS
```

returns one row per digest, in the order in which the session first executed a statement with that digest. The
columns are `digest` (STRING) and `calls`, `errors`, `rows` (INTEGER):

* `calls` — the number of statements with this digest that the session executed, successfully or not;
* `errors` — how many of them failed;
* `rows` — the sum over the successful ones of the rows each produced: the number of rows returned by a statement
  that returns rows (SELECT, SHOW STATISTICS), the count of an INSERT, UPDATE or DELETE, the number of rows
  inserted by IMPORT, the number of table rows exported by EXPORT; 0 for every other statement, including
  statements added later that neither return rows nor report a count of this kind.

Rules:

* A statement is recorded after it is executed. A statement that fails to parse has no digest and is not recorded;
  `SHOW STATEMENTS` itself is never recorded.
* Only SQL statements executed by the session count. The golden-script runners' own commands (`schema`, `dump`,
  `table`, `tables`, `status`) are not statements, and the `[plan]`/`[opt]` tags record nothing beyond the
  statement itself.
* The statistics belong to the session (in a cluster: to the client connection) and end with it. Transactions
  don't affect them: statements of a rolled-back transaction stay counted, with their rows.
* `SHOW STATEMENTS` uses no transaction and works inside any transaction.

```
> CREATE TABLE t (id INT PRIMARY KEY, v STRING, n INT)
> INSERT INTO t VALUES (1, 'a', 10), (2, 'b', 20), (3, NULL, 30)
> select * from T where ID = 1
> SELECT * FROM t WHERE (id = 2)
> SELECT  *  FROM  t  WHERE  id  =  99
!> SELECT * FROM t WHERE v = 1
!> SELECT FROM t
[header]> SHOW STATEMENTS
---
1, 'a', 10
2, 'b', 20
Error: invalid input: can't compare 'a' and 1
Error: invalid input: expected expression atom, found FROM
digest, calls, errors, rows
'CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING, n INTEGER)', 1, 0, 0
'INSERT INTO t VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?)', 1, 0, 3
'SELECT * FROM t WHERE id = ?', 3, 0, 2
'SELECT * FROM t WHERE v = ?', 1, 1, 0
```

### 3.2 The canonical text

The digest is produced from the parsed statement by the following rules.

**Tokens and spacing.**

* Keywords are written in upper case. Table, column, alias and function names are written as parsed (unquoted names
  are lower case) and quoted by 1.6 where necessary. Function and aggregate names therefore appear in lower case:
  `count(*)`, `sqrt(?)`.
* Tokens are separated by exactly one space, except: no space after `(` or before `)`; no space before `,`; no space
  around the `.` of a qualified name (`t.id`); a function name is followed directly by `(`; a prefix `-` or `+` is
  written directly before its operand (`-?`, `--a`, `+-a`); a postfix `!` directly after its operand (`a!`). Every
  other `(` is preceded by a space (`INSERT INTO t (a, b) VALUES (?, ?)`).
* There is no trailing `;`.

**Literals.** Every literal is written `?`: numbers, strings, `TRUE`, `FALSE`, `NULL`, `INFINITY`, `NAN`, and every
kind of literal added later (a literal of another data type, however it is spelled, is a single `?`). A negative
number is the prefix `-` applied to a literal: `-?`. The `NULL` and `NAN` of `IS [NOT] NULL` and `IS [NOT] NAN`
belong to the operator: `a IS NULL`, `a IS NOT NAN`.

**Synonyms and optional words.** Synonymous spellings are written in one canonical form; optional words that don't
change the meaning are left out:

| Written | Canonical |
|---|---|
| `<>` | `!=` |
| `BOOL`, `INT`, `DOUBLE`, `TEXT`, `VARCHAR` (any spelling of a data type) | the data type's name as the schema display writes it: `BOOLEAN`, `INTEGER`, `FLOAT`, `STRING` |
| `BEGIN TRANSACTION`, `BEGIN READ WRITE` | `BEGIN` |
| `INNER JOIN` | `JOIN` |
| `LEFT OUTER JOIN`, `RIGHT OUTER JOIN` | `LEFT JOIN`, `RIGHT JOIN` |
| `ORDER BY e ASC` | `ORDER BY e` (`DESC` is kept) |
| an alias without `AS` (`SELECT a b FROM t x`) | with `AS` (`SELECT a AS b FROM t AS x`) |

Everything else is written as it was written: a construct is never replaced by another one that means the same. In
particular `a IS NOT NULL` stays `a IS NOT NULL` (and `NOT a IS NULL` stays `NOT a IS NULL`), `a >= b` stays
`a >= b`, `NOT a = b` stays `NOT a = b`, and a construct that toydb defines as an abbreviation of other constructs
is written in its own syntax, not as the constructs it abbreviates.

**Parentheses.** An operand is written in parentheses exactly when the parser would not read it as that operand
without them. The parser's precedence levels, from loosest to tightest, are:

| Level | Operators |
|---|---|
| 1 | `OR` |
| 2 | `AND` |
| 3 | prefix `NOT` |
| 4 | `=`, `!=`, `LIKE`, postfix `IS [NOT] NULL` / `IS [NOT] NAN` |
| 5 | `<`, `<=`, `>`, `>=` |
| 6 | `+`, `-` |
| 7 | `*`, `/`, `%` |
| 8 | `^` |
| 9 | postfix `!` |
| 10 | prefix `-`, `+` |
| 11 | atoms: `?`, column references, `*`, function calls, and every construct that is itself an atom |

and the rules are:

* An operand of a left-associative binary operator at level *p* (all binary operators except `^`): the left operand
  is parenthesized if its level is below *p*, the right operand if its level is *p* or below. So `a - (b - c)` keeps
  its parentheses, `(a - b) - c` is written `a - b - c`, `(p = q) = p` is `p = q = p`, `p = (q = p)` keeps them.
* `^` (level 8, right-associative): the left operand is parenthesized if its level is 8 or below, the right operand
  if it is below 8. `(a ^ b) ^ c` keeps them, `a ^ (b ^ c)` is `a ^ b ^ c`.
* Prefix operators: the operand of `NOT` is parenthesized if its level is below 3, that of a prefix `-` or `+` if
  below 10. Postfix operators: the operand of `!` if below 9, that of `IS ...` if below 4. So `(-a) ^ 2` is
  `-a ^ ?`, `-(a ^ 2)` keeps them, `(a!)!` is `a!!`, `-(a!)` keeps them, `(NOT p) = q` keeps them, `NOT (p = q)` is
  `NOT p = q`, `(p = q) IS NULL` is `p = q IS NULL`, `p = (q IS NULL)` keeps them.
* Parts that the syntax reads as complete expressions up to a closing keyword or symbol of the construct — function
  and aggregate arguments, `VALUES` elements, `SET` values, select items, `WHERE`/`ON`/`HAVING` conditions,
  `GROUP BY` and `ORDER BY` items, `LIMIT`, `OFFSET` and `DEFAULT` expressions, and such parts of constructs added
  later (list elements, keyword-delimited parts) — are never parenthesized as a whole: `sqrt((a + b))` is
  `sqrt(a + b)`.
* A construct added later has the level that its syntax gives it, and its operands follow the same principle: an
  operand that the parser reads like the left operand of a binary operator at the construct's level follows the
  left-operand rule, one that it reads like the right operand follows the right-operand rule.

**Statements.** Each statement is written in this form; square brackets mark parts that appear only if present in
the statement:

| Statement | Canonical form |
|---|---|
| BEGIN | `BEGIN [READ ONLY] [AS OF SYSTEM TIME ?]` |
| COMMIT, ROLLBACK | `COMMIT`, `ROLLBACK` |
| EXPLAIN | `EXPLAIN <canonical text of the explained statement>` |
| CREATE TABLE | `CREATE TABLE <name> (<column>, ...)`; a column is `<name> <type>` followed by its constraints in this order: `PRIMARY KEY`, `NULL` or `NOT NULL` (only if written), `DEFAULT <expr>`, `UNIQUE`, `INDEX`, `REFERENCES <table>` — whatever order they were written in |
| DROP TABLE | `DROP TABLE [IF EXISTS] <name>` |
| DELETE | `DELETE FROM <table> [WHERE <expr>]` |
| INSERT | `INSERT INTO <table> [(<column>, ...)] VALUES (<expr>, ...), (<expr>, ...)` |
| UPDATE | `UPDATE <table> SET <column> = <expr or DEFAULT>, ... [WHERE <expr>]`, with the assignments in increasing order of column name (by code points), whatever order they were written in |
| SELECT | `SELECT <item>, ... [FROM <from>, ...] [WHERE <expr>] [GROUP BY <expr>, ...] [HAVING <expr>] [ORDER BY <expr> [DESC], ...] [LIMIT <expr>] [OFFSET <expr>]`; an item is `*` or `<expr> [AS <alias>]`; a FROM item is `<table> [AS <alias>]` or a join written left to right as the parser nests it: `<from> JOIN <from> ON <expr>`, `<from> LEFT JOIN <from> ON <expr>`, `<from> RIGHT JOIN <from> ON <expr>`, `<from> CROSS JOIN <from>` |
| ANALYZE | `ANALYZE [<table>]` |
| SHOW STATISTICS | `SHOW STATISTICS <table>` |
| EXPORT | `EXPORT [<table>] AS CSV`, `... AS JSON`, `... AS SQL` |
| IMPORT | `IMPORT INTO <table> FROM CSV ?, ?, ...` and `... FROM JSON ?, ...`, one `?` per line |
| a statement added later | its SQL syntax, with its keywords, clauses and operands in the order its syntax defines, by the rules above: optional words left out, synonyms in canonical form, literals as `?`, column definitions written as in CREATE TABLE |

| Statement | Digest |
|---|---|
| `select ID, v as "the name" from T x where x.id >= 2 and not (v <> 'a' or v is not null) order by 1 asc, v desc limit 5 offset 1` | `SELECT id, v AS "the name" FROM t AS x WHERE x.id >= ? AND NOT (v != ? OR v IS NOT NULL) ORDER BY ?, v DESC LIMIT ? OFFSET ?` |
| `SELECT t.v, u.w FROM t INNER JOIN u ON u.t_id = t.id LEFT OUTER JOIN t t2 ON t2.id = u.id` | `SELECT t.v, u.w FROM t JOIN u ON u.t_id = t.id LEFT JOIN t AS t2 ON t2.id = u.id` |
| `SELECT COUNT(*), Sum(n) FROM t GROUP BY n % 2` | `SELECT count(*), sum(n) FROM t GROUP BY n % ?` |
| `insert into T values (1, 'x', 15), (2, NULL, -1)` | `INSERT INTO t VALUES (?, ?, ?), (?, ?, -?)` |
| `update t set v = default, n = n + 1 where id = 3` | `UPDATE t SET n = n + ?, v = DEFAULT WHERE id = ?` |
| `CREATE TABLE u (k INT NOT NULL DEFAULT 1 PRIMARY KEY, r INT INDEX REFERENCES u, b BOOL NULL UNIQUE)` | `CREATE TABLE u (k INTEGER PRIMARY KEY NOT NULL DEFAULT ?, r INTEGER INDEX REFERENCES u, b BOOLEAN NULL UNIQUE)` |
| `BEGIN TRANSACTION READ ONLY AS OF SYSTEM TIME 3` | `BEGIN READ ONLY AS OF SYSTEM TIME ?` |

Every statement that toydb accepts has a digest — the statements of the other extensions and statements added to
toydb later included — and these rules determine it.

---

## 4. Shared rules for CSV

### 4.1 Lines

CSV text is handled as a list of **lines**. Export produces a list of lines (one per record); import takes one SQL
string literal per line. Line breaks never separate records: a string value that contains a line break is quoted
and stays within its line.

### 4.2 Writing

The fields of a line are joined with `,`. A field is **quoted** — written between `"` with every `"` in it doubled —
exactly when its text is empty but it must not be read as NULL (an empty STRING), or when it contains `,`, `"`,
U+000A or U+000D. Every other field is written as is (so `NULL` is an empty field, `'x y'` is `x y`, `'a,b'` is
`"a,b"`, `''` is `""`, `'say "hi"'` is `"say ""hi"""`). Header fields (column names) follow the same rule.

### 4.3 Reading

A line is split into fields at the commas that are not inside a quoted field:

* A field that begins with `"` is quoted: it extends to the matching closing `"`, where `""` inside stands for one
  `"`. The closing `"` must be followed by `,` or the end of the line.
* Any other field extends to the next `,` or the end of the line and must not contain `"`.
* A line has one more field than it has separating commas: `''` is one empty field, `'a,'` two fields.

A line that doesn't satisfy these rules (an unterminated quoted field, a `"` inside an unquoted field, a character
after a closing quote) is `Error: invalid input: invalid CSV on line <n>`.

An **unquoted empty field is NULL**, for a column of any data type. Every other field gives its text (a quoted
field without its quotes, `""` undoubled), which is read by 1.5 for the column's data type; so `""` is the empty
string for a STRING column and invalid for every other type, and `"12"` is 12 for an INTEGER column.

---

## 5. Extension C: Column statistics

### 5.1 ANALYZE

```
ANALYZE [<table>]
```

computes statistics for the named table, or for every table if no table is named, and stores them, replacing the
table's previous statistics.

* The statistics are computed from the rows that the statement's transaction sees (including its own uncommitted
  writes).
* Without a table name, the tables are analyzed in increasing order of their names (by code points: `"B"` before
  `a`). With no tables, nothing is analyzed.
* The result is `{"result":"analyze","tables":[...]}` in JSON, listing the analyzed tables in that order; the
  runners print nothing for it without `[json]`.
* Errors: `Error: invalid input: table <t> does not exist` for an unknown table (nothing is analyzed); in a
  read-only transaction `Error: read-only transaction`, always: this is checked first (`ANALYZE nosuch` in a
  read-only transaction is a read-only error), also when there is no table to analyze.

### 5.2 Storage and transactions

Statistics are part of the catalog and are transactional, exactly like table schemas and rows:

* ANALYZE runs in the session's transaction, or in an implicit read-write transaction of its own. Statistics
  written by an uncommitted transaction are seen only by that transaction; `ROLLBACK` discards them; other
  transactions see them once committed, under snapshot isolation (a transaction that began before the commit keeps
  seeing the earlier statistics, `AS OF SYSTEM TIME` sees those committed as of that version).
* Two transactions that write statistics of the same table concurrently conflict like two writes of the same row:
  the later writer fails with `Error: serialization failure, retry transaction`.
* In a cluster, statistics are replicated like every other write and visible through every node.
* Statistics describe the table as it was when it was analyzed: INSERT, UPDATE, DELETE and IMPORT do not change
  them. `DROP TABLE` removes them; a table created later with the same name has none.
* Statistics belong to the column they describe, not to its name or position. If the definition of an analyzed
  table changes, they follow the columns: a column that is renamed keeps its statistics under its new name, the
  statistics of a column that is removed are removed with it, and a column that is added has none until the table
  is analyzed again. The other columns keep theirs.

### 5.3 SHOW STATISTICS

```
SHOW STATISTICS <table>
```

returns one row per column of the table, in table column order, with these columns:

| Column | Type | Value |
|---|---|---|
| `column` | STRING | the column's name as stored |
| `rows` | INTEGER | the number of rows of the table when it was analyzed |
| `nulls` | INTEGER | how many of them had NULL in this column |
| `distinct` | INTEGER | the number of distinct non-NULL values; values are distinct exactly when GROUP BY puts them in different groups (NaN equals NaN) |
| `min`, `max` | the column's type | the smallest and largest non-NULL value in the order of ORDER BY (for FLOAT, NaN sorts after infinity); NULL if there is none |
| `top` | the column's type | the most frequent non-NULL value; among equally frequent values the smallest in ORDER BY order; NULL if there is none |
| `top_count` | INTEGER | the number of rows with `top`; NULL if there is none |
| `avg_length` | FLOAT | for a STRING column, the average number of characters (Unicode code points) of its non-NULL values; NULL for columns of every other type and when there is no non-NULL value |

A column without statistics (the table was never analyzed, its statistics were removed, or the column was added
after the last ANALYZE) has its name and NULL in every other column. These rules apply to every data type, including
types added later (whose values are ordered and grouped as ORDER BY and GROUP BY do); only STRING columns have an
`avg_length`.

* SHOW STATISTICS runs in the session's transaction or an implicit read-only one; it reads the statistics that the
  transaction sees. It is a statement that returns rows: the runners print its rows, `[header]` shows
  `column, rows, nulls, distinct, min, max, top, top_count, avg_length`, and its JSON form is a `select` result.
* `Error: invalid input: table <t> does not exist` for an unknown table.

```
> CREATE TABLE item (id INT PRIMARY KEY, name STRING INDEX, price FLOAT, qty INT, ok BOOLEAN)
> INSERT INTO item VALUES (1, 'apple', 1.5, 10, TRUE), (2, 'pear', 2.25, 0, FALSE), (3, 'fig', NULL, 7, TRUE), (4, 'apple', 0.5, 10, NULL), (5, 'kiwi', 2.25, NULL, TRUE)
> ANALYZE item
> SHOW STATISTICS item
---
'id', 5, 0, 5, 1, 5, 1, 1, NULL
'name', 5, 0, 4, 'apple', 'pear', 'apple', 2, 4.2
'price', 5, 1, 3, 0.5, 2.25, 2.25, 2, NULL
'qty', 5, 1, 3, 0, 10, 10, 2, NULL
'ok', 5, 1, 2, FALSE, TRUE, TRUE, 3, NULL
```

---

## 6. Extension D: Export and import

### 6.1 EXPORT

```
EXPORT <table> AS CSV
EXPORT <table> AS JSON
EXPORT <table> AS SQL
EXPORT AS SQL
```

returns the table's rows (or the whole database) as a list of text lines. The runners print the lines one per
output line, as they are (also without any tag); `toysql` prints them likewise; the JSON form is
`{"result":"export","format":"csv"|"json"|"sql","lines":[...]}`.

EXPORT runs in the session's transaction or an implicit read-only one and exports what the transaction sees. It
changes nothing. Rows are exported in primary key order, columns in table order.

* **`AS CSV`**: a header line with the column names (as stored), then one line per row, the values written by 1.4
  and quoted by 4.2. A table without rows exports only the header.
* **`AS JSON`**: one line per row: a JSON object whose members are the table's columns in table order, named as
  stored, with the values written by 1.2: `{"id":1,"name":"ann","score":{"type":"FLOAT","value":"NaN"}}`. A table
  without rows exports no lines.
* **`AS SQL`**: the lines of the table's schema display, except that default values are written as SQL literals
  (1.3) and `;` is appended to the last line (`);`); then, for each row, `INSERT INTO <table> VALUES (<v1>, ...,
  <vn>);` with the table name written by 1.6 and the values as SQL literals (1.3), separated by `, `. Executing the
  lines re-creates the table with the same definition and rows — up to what the schema display can express: names
  are written as it writes them, so a name with upper-case letters (`"Item"` is written `Item`) is re-created in
  lower case, and the table named by `REFERENCES` is written as the schema display writes it, as stored and never
  quoted (`REFERENCES select`, which doesn't parse).
* **`EXPORT AS SQL`** without a table exports every table in turn (its CREATE TABLE lines, then its INSERT lines),
  ordered so that a table comes after the tables it references: repeatedly, the next table is the first by name
  (code points) among the remaining tables whose referenced tables (other than itself) have all been exported; if
  there is none (a cycle of references), it is the first remaining table by name. With no tables there are no lines.

Errors: `Error: invalid input: table <t> does not exist`; `EXPORT AS CSV` and `EXPORT AS JSON` without a table:
`Error: invalid input: EXPORT without a table requires AS SQL`.

### 6.2 IMPORT

```
IMPORT INTO <table> FROM CSV <string> [, <string> ...]
IMPORT INTO <table> FROM JSON <string> [, <string> ...]
```

Each string literal is one line. The rows are inserted into the table; the result is that of an INSERT
(`{"result":"insert","count":<n>}`, nothing printed without `[json]`).

* **CSV**: the first line is the header: a list of column names (matched exactly, without letter-case folding; a
  column may appear only once) that the following lines give values for, in that order. Each following line is
  one row; it must have as many fields as the header, and its fields are read by 4.3 for the header's columns. A
  header without further lines imports nothing.
* **JSON**: each line is one JSON object (RFC 8259 JSON text; `NaN`, `Infinity` and anything after the object are
  invalid, and so is a string with the escape of an unpaired surrogate, `"\ud800"`, which is not text). Its members
  name columns (matched exactly) and give their values; members are taken in the order written, and a member given
  more than once counts with its last value, at the position of its first occurrence (only that value is read and
  checked).
  An object without members is a row of default values. The value of a member is read as follows:
  * `null` is NULL, for a column of any type;
  * a BOOLEAN column accepts `true` and `false`;
  * an INTEGER column accepts a JSON number written without fraction and exponent, within the 64-bit range
    (`2.0`, `2e0` are invalid);
  * a FLOAT column accepts any JSON number (the nearest float, also for integers, rounded as in 1.5: `1e400` is
    infinity) and the typed forms of NaN, infinity and negative infinity (1.2);
  * a STRING column accepts a JSON string;
  * a column of any other data type accepts the typed form (1.2) with its own type name and a `"value"` read by 1.5;
  * a typed form is an object with exactly the members `type` and `value` (in either order), both strings; it is
    accepted only where stated above. Everything else is an invalid value.
* **Inserting.** Each row is inserted exactly as `INSERT INTO <table> (<columns>) VALUES (<values>)` would insert
  it — `<columns>` being the CSV header, or the JSON object's member names in order, and `<values>` the values read
  — with the same defaults for the other columns, checks and errors (`NULL value not allowed for column <c>`,
  `primary key <k> already exists`, `no value given for column <c> with no default`, reference and uniqueness
  errors). All rows of one IMPORT are inserted by one statement, as by an INSERT of several rows: first every row
  is completed with the defaults of the columns it doesn't give, in line order (the first row without a value for a
  column that has no default fails with `no value given for column <c> with no default`, before any row is
  inserted), then the rows are inserted in line order, with the checks of INSERT; rows inserted earlier by the same
  IMPORT count for the checks of later ones. A failing IMPORT has the effect of a failing INSERT: outside an explicit
  transaction it inserts nothing; inside one, the rows that it inserted before the failing row stay written in the
  transaction, exactly as for a failing INSERT.

**Order of checks.** The table must exist (`Error: invalid input: table <t> does not exist`). Then every line is
read and checked, in order, before any row is inserted; within a line, from left to right. The first problem is
reported:

| Problem | Error |
|---|---|
| a CSV line that is not valid CSV (4.3) | `Error: invalid input: invalid CSV on line <n>` |
| a header name that is not a column of the table | `Error: invalid input: unknown column <name> in table <t>` |
| a header name given twice | `Error: invalid input: column <name> given multiple times` |
| a CSV data line with another number of fields than the header | `Error: invalid input: wrong number of fields on line <n>: expected <m>, found <k>` |
| a JSON line that is not valid JSON | `Error: invalid input: invalid JSON on line <n>` |
| a JSON line that is not an object | `Error: invalid input: line <n> is not a JSON object` |
| a JSON member that is not a column of the table | `Error: invalid input: unknown column <name> in table <t>` |
| an invalid value (CSV field or JSON member) | `Error: invalid input: invalid value for <TYPE> column <name> on line <n>` (`<TYPE>` as the schema display names it) |

Lines are numbered from 1 in the order given; for CSV, line 1 is the header. For each JSON member, its name is
checked before its value. After all lines, the rows are inserted, which can fail with the errors of INSERT.

---

## 7. Syntax summary

### 7.1 Statements

```
SHOW STATEMENTS
ANALYZE [<table>]
SHOW STATISTICS <table>
EXPORT [<table>] AS { CSV | JSON | SQL }
IMPORT INTO <table> FROM { CSV | JSON } <string> [, <string> ...]
```

Keywords are case-insensitive, names follow the identifier rules of the other statements, and a single trailing `;`
is allowed, as for every statement. Parsing reports the first token that does not fit, with the existing parser
messages (tokens displayed as usual: keywords in upper case, identifiers as their name, strings as their content):

| Input | Error |
|---|---|
| `SHOW` | `Error: invalid input: unexpected end of input` |
| `SHOW` followed by anything but `STATEMENTS` or `STATISTICS`: `SHOW TABLES` | `Error: invalid input: unexpected token tables` |
| `SHOW STATISTICS`, `EXPORT`, `EXPORT t`, `EXPORT t AS`, `IMPORT`, `IMPORT INTO t FROM CSV`, `IMPORT INTO t FROM CSV 'a',` | `Error: invalid input: unexpected end of input` |
| `SHOW STATISTICS 1`, `IMPORT INTO 1 FROM CSV 'a'` | `Error: invalid input: expected identifier, got 1` |
| `EXPORT t CSV`, `EXPORT 1 AS CSV` | `Error: invalid input: expected token AS, found CSV` (resp. `found 1`) |
| a format other than `CSV`, `JSON`, `SQL` (for IMPORT: `CSV`, `JSON`): `EXPORT t AS XML`, `IMPORT INTO t FROM SQL 'a'` | `Error: invalid input: unexpected token xml` (resp. `unexpected token SQL`) |
| `IMPORT t FROM CSV 'a'` | `Error: invalid input: expected token INTO, found t` |
| `IMPORT INTO t CSV 'a'` | `Error: invalid input: expected token FROM, found CSV` |
| `IMPORT INTO t FROM CSV a` (a line that is not a string literal) | `Error: invalid input: expected string, found a` |
| anything after a complete statement: `SHOW STATEMENTS t`, `ANALYZE 1`, `ANALYZE t u`, `ANALYZE t, u`, `EXPORT t AS CSV, JSON`, `IMPORT INTO t FROM CSV 'a' 'b'` | `Error: invalid input: unexpected token t` (resp. `1`, `u`, `,`, `,`, `b`) |

A statement that fails to parse has no effect.

### 7.2 Keywords

`ANALYZE`, `CSV`, `EXPORT`, `IMPORT`, `JSON`, `SHOW`, `SQL`, `STATEMENTS` and `STATISTICS` are reserved keywords with
the same rules as the existing ones: case-insensitive; not usable as unquoted identifiers
(`CREATE TABLE show (id INT PRIMARY KEY)` → `Error: invalid input: expected identifier, got SHOW`,
`SELECT 1 AS sql` → `Error: invalid input: expected identifier, got SQL`); valid when double-quoted, and quoted by
the schema display (`"show"`). Names that merely contain them (`shows`, `csv_file`) are unaffected.

### 7.3 EXPLAIN

The statements of this section are not planned and can't be explained: `EXPLAIN ANALYZE t`,
`EXPLAIN SHOW STATEMENTS`, `EXPLAIN EXPORT t AS CSV`, ... fail with
`Error: invalid input: EXPLAIN is not supported for <KEYWORD>`, where `<KEYWORD>` is the statement's first keyword
(`ANALYZE`, `SHOW`, `EXPORT`, `IMPORT`).

### 7.4 Transactions used

| Statement | Outside an explicit transaction |
|---|---|
| `SHOW STATEMENTS` | no transaction |
| `SHOW STATISTICS`, `EXPORT` | an implicit read-only transaction (no version is allocated) |
| `ANALYZE`, `IMPORT` | an implicit read-write transaction (a version is allocated, as for INSERT) |

Inside an explicit transaction, all of them use it. All other statements behave exactly as before.

---

## 8. Implementation requirements for the tier-3 base (not user documentation)

These requirements keep the tier-2 behaviour intact and make the extensions consistent across the two workspaces.
The experiment's fairness rules for writing the extension code are in `EXT_DESIGN_NOTES.md` (experimenter-only);
the code of each language must be what a strong engineer naturally writes in that language.

1. **Nothing existing changes.** All 203 existing golden script runs and all unit tests produce byte-identical
   output, except the unit tests that pin the set or number of keywords (Python `tests/sql/parser/test_lexer.py`:
   66 → 75), and AST debug-dump expectations affected by item 4. In particular: no existing statement writes
   anything new to storage (raw `dump`/`[ops]` output and cluster `status` key counts and sizes stay identical:
   statistics are written only by ANALYZE, and DROP TABLE deletes a statistics key only if it exists); the plan
   display, schema display, value display, error messages and the client protocol of existing results are
   unchanged.
2. **Keywords**: the nine keywords of 7.2, added like the existing ones (lexer keyword enum/table, display).
3. **AST**: statements for `SHOW STATEMENTS`, `ANALYZE`, `SHOW STATISTICS`, `EXPORT` (table: optional; format:
   CSV/JSON/SQL), `IMPORT` (table, format CSV/JSON, lines).
4. **AST fidelity for digests**: the parser currently turns `a IS NOT NULL` / `IS NOT NAN` into `NOT (a IS ...)` in
   the AST; the digest must distinguish them, so the AST records `IS NOT` (e.g. an `IsNot` operator), and the
   planner lowers it to `NOT (a IS ...)` before it uses the statement (e.g. a desugaring pass at the start of
   planning). Everything that compares AST expressions — GROUP BY and aggregate matching in particular — and every
   plan therefore behaves exactly as today. No other AST change is needed: `<>`/`!=`, the type synonyms, optional
   words and aliases without `AS` are synonyms by the rules of 3.2, and parentheses need not be recorded.
5. **Session**: per-session digest statistics (statements recorded after execution, by the rules of 3.1);
   `SHOW STATEMENTS` handled by the session; the other extension statements executed by the session (or the
   executor) in the transactions of 7.4; `EXPLAIN` of them rejected with the message of 7.3.
6. **StatementResult**: new results for ANALYZE (`tables`) and EXPORT (`format`, `lines`), appended **after** the
   existing variants (the enum is a wire format; appending keeps existing encodings). SHOW results are `Select`
   results with the columns and labels of 3.1 / 5.3 (unqualified labels). IMPORT returns the `Insert` result. Python:
   the hand-written `StatementResult` serde gets the two new variants.
7. **Statistics storage**: a new local-engine key for a table's statistics (appended after the existing key
   variants, so existing keys encode identically), holding the statistics of each column keyed by column; written
   and read through two new catalog methods. The Raft SQL engine gets a matching write command (to store
   statistics in a transaction) and read command, appended after the existing variants, applied by the state
   machine on every node via the local engine; `DROP TABLE` in the local engine removes the key if present. The
   log/storage formatters (`encoding/format`, used by `toydump` and `dump`) format the new key and the new write
   command readably. Python: serde for the statistics and the new commands.
8. **IMPORT** inserts through the normal INSERT path, so that defaults, checks and messages are those of INSERT: each
   row is completed by INSERT's own row building (with the header/member names as column list), and all rows are
   inserted by one insert plan with constant rows (6.2).
9. **JSON**: Rust may move `serde_json` from dev-dependencies to dependencies (with `preserve_order`, needed for
   member order when parsing JSON lines) or write its own JSON code; Python uses the standard `json` module where it
   fits, with the spec's number formatting (the existing `debug_f64`) and strict parsing (reject `NaN`/`Infinity`
   constants; dict order = member order).
10. **toysql**: `!format json` / `!format text` (listed by `!help`); EXPORT lines printed as is; a short message
    for ANALYZE (e.g. `Analyzed tables a, b`) in text format.
11. **Runners** (test code, both languages): the `[json]` tag in the SQL runner and the cluster runner; EXPORT
    results printed line by line by both runners without tags. Golden scripts of the extensions live in a
    regression directory of each workspace (`src/sql/testscripts/ext/` + `tests/scripts/ext/` in Rust, registered
    like the other directories; `tests/sql/testscripts/ext/` + `tests/e2e/scripts/ext/` in Python, registered in
    `REGISTRY` and the SQL test module's directory list). The scripts in `ext_scripts/sql/` and
    `ext_scripts/cluster/` are those scripts.
12. **Size**: about 3,000 lines of application code in Rust and 3,500 in Python for the four extensions (see the
    design notes for the split).
