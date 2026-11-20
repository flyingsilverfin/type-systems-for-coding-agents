# Task: release notes (six small changes)

The next toydb release contains six small, independent changes, listed below. Implement all six.

1. `NOT LIKE`
2. The scalar functions `ABS`, `LENGTH`, `UPPER` and `LOWER`
3. The aggregate functions `BOOL_AND` and `BOOL_OR`
4. Key and index lookups look up each value only once
5. `CREATE TABLE IF NOT EXISTS`
6. `INSERT ... SELECT`

The changes are purely additive. Everything not described here keeps its current behaviour, and all existing
golden scripts must keep producing byte-identical output. If the code base has unit tests that pin the exact
internal form of parsed statements or of expressions (for example a debug dump of a parsed `CREATE TABLE` or
`INSERT` statement, or a binary encoding of an expression), you may update their expectations to match your
change; those are the only existing test expectations that may change.

A small example script is provided as the visible test. All examples below use the golden-script conventions:
values are displayed as today (`TRUE`, `FALSE`, `NULL`, `1`, `2.0`, `-0.0`, `inf`, `NaN`, `'text'`), errors as
`Error: <message>`, `[plan]` shows the optimized plan of a statement before its result, and `[result]` shows a
write statement's result (e.g. `Insert { count: 2 }`). A result column that is computed by an expression has
no name in a `[header]` line unless it is given an alias with `AS`, as today.

---

## 1. NOT LIKE

```
a NOT LIKE b
```

means exactly `NOT (a LIKE b)`, for results, NULL handling and errors alike:

* `'abc' NOT LIKE 'a%'` is `FALSE`, `'abc' NOT LIKE 'b%'` is `TRUE`, `'abc' NOT LIKE 'ABC'` is `TRUE` (LIKE is
  case-sensitive), `'' NOT LIKE '%'` is `FALSE`.
* `NULL NOT LIKE 'a'`, `'a' NOT LIKE NULL` and `NULL NOT LIKE NULL` are `NULL`; in WHERE, JOIN ... ON and HAVING a
  `NULL` result does not select the row, as today.
* Errors are those of LIKE, with the operands in the same order:
  `1 NOT LIKE 'a'` → `Error: invalid input: can't LIKE 1 and 'a'`,
  `'a' NOT LIKE 1` → `Error: invalid input: can't LIKE 'a' and 1`,
  `TRUE NOT LIKE NULL` → `Error: invalid input: can't LIKE TRUE and NULL`.
* `NOT` and `LIKE` are keywords, case-insensitive as always (`'a' not like 'b'`, `'a' Not Like 'b'`).

It is an ordinary expression, usable wherever an expression is allowed (SELECT list, WHERE, JOIN ... ON,
GROUP BY, HAVING, ORDER BY, UPDATE ... SET, DEFAULT, function and aggregate arguments, ...), with arbitrary
operands (columns of any joined table, constants, other expressions).

**Precedence.** `NOT LIKE` has the precedence of `=`, `!=`, `LIKE` and `IS`, and is left-associative with them;
arithmetic and `<`, `<=`, `>`, `>=` bind tighter, prefix `NOT`, `AND` and `OR` bind looser:

* `'b' NOT LIKE 'a' = TRUE` is `('b' NOT LIKE 'a') = TRUE`, i.e. `TRUE`.
* `'a' = 'a' NOT LIKE 'b'` is `('a' = 'a') NOT LIKE 'b'` → `Error: invalid input: can't LIKE TRUE and 'b'`;
  `'a' NOT LIKE 'a' NOT LIKE 'b'` → `Error: invalid input: can't LIKE FALSE and 'b'`;
  `'a' NOT LIKE 'b' LIKE 'c'` → `Error: invalid input: can't LIKE TRUE and 'c'`;
  `1 + 1 NOT LIKE 'a'` → `Error: invalid input: can't LIKE 2 and 'a'`.
* `'ab' NOT LIKE 'a' AND FALSE` is `('ab' NOT LIKE 'a') AND FALSE`; `NOT 'ab' NOT LIKE 'a%'` is
  `NOT ('ab' NOT LIKE 'a%')`; `'x' NOT LIKE 'y' IS NULL` is `('x' NOT LIKE 'y') IS NULL`, i.e. `FALSE`.

**`NOT` is only part of `NOT LIKE` when the very next token is `LIKE`.** In every other position after an
operand, a `NOT` is left alone and the statement behaves exactly as today:

* `SELECT 1 NOT 2`, `SELECT 'a' NOT`, `SELECT 'a' NOT NOT LIKE 'b'` and `SELECT * FROM t WHERE s NOT 'a'` →
  `Error: invalid input: unexpected token NOT` (as today).
* `SELECT NOT LIKE 'a'` → `Error: invalid input: expected expression atom, found LIKE` (as today), and
  `SELECT 'a' NOT LIKE` → `Error: invalid input: unexpected end of input`.
* In particular, a column's `DEFAULT` expression can still be followed by the column constraint `NOT NULL`, also
  when the default expression itself uses `NOT LIKE`:
  `CREATE TABLE d (id INT PRIMARY KEY, n INT DEFAULT 1 NOT NULL, b BOOLEAN DEFAULT 'abc' NOT LIKE 'a%' NOT NULL,
  c BOOLEAN DEFAULT 'abc' NOT LIKE 'x%' NULL)` creates a table that `schema` shows as

  ```
  CREATE TABLE d (
    id INTEGER PRIMARY KEY,
    n INTEGER NOT NULL DEFAULT 1,
    b BOOLEAN NOT NULL DEFAULT FALSE,
    c BOOLEAN DEFAULT TRUE
  )
  ```

  (so `INSERT INTO d (id, b) VALUES (2, NULL)` → `Error: invalid input: NULL value not allowed for column b`),
  and `n INT DEFAULT 1 NOT 2` → `Error: invalid input: expected token NULL, found 2`,
  `n INT DEFAULT 1 NOT NULL NOT NULL` → `Error: invalid input: nullability already set for column n` (both as
  today).

**Plans** show `a NOT LIKE b` exactly as `NOT (a LIKE b)` is shown today, i.e. as `NOT a LIKE b`:

```
> SELECT id FROM t WHERE s NOT LIKE 'a%'
Projection: t.id
└─ Scan: t (NOT t.s LIKE 'a%')
```

and `SELECT s NOT LIKE 'b%' AND id > 0 FROM t` has the projection `NOT t.s LIKE 'b%' AND t.id > 0`.

---

## 2. Scalar functions ABS, LENGTH, UPPER, LOWER

Four new functions, each taking exactly one argument. Function names are case-insensitive, as today
(`abs`, `ABS`, `Abs`). Like the existing `sqrt`, they can be used wherever an expression is allowed, with any
expression as the argument (including aggregate functions where aggregates are allowed, e.g. `ABS(SUM(x))`),
and inside aggregate arguments (`SUM(ABS(x))`).

| Function | Argument | Result |
|---|---|---|
| `ABS(x)` | integer | the absolute value, an integer: `ABS(-5)` = `5`, `ABS(-9223372036854775807)` = `9223372036854775807` |
| | float | the absolute value, a float: `ABS(-2.5)` = `2.5`, `ABS(-0.0)` = `0.0`, `ABS(-INFINITY)` = `inf`, `ABS(NAN)` = `NaN` |
| `LENGTH(s)` | string | the number of characters (Unicode code points, not bytes), an integer: `LENGTH('')` = `0`, `LENGTH('héllo')` = `5`, `LENGTH('👋👋')` = `2` |
| `UPPER(s)` | string | the string with every letter converted to upper case using the Unicode case mapping; other characters unchanged: `UPPER('aBc-1 é')` = `'ABC-1 É'`, `UPPER('straße')` = `'STRASSE'` |
| `LOWER(s)` | string | the string with every letter converted to lower case using the Unicode case mapping; other characters unchanged: `LOWER('AbC-1 É')` = `'abc-1 é'`, `LOWER('ÅNGSTRÖM')` = `'ångström'` |

* A `NULL` argument gives `NULL` for all four functions.
* Errors (the value is displayed as usual):
  * `ABS` of the smallest integer, e.g. `ABS(-9223372036854775807 - 1)` → `Error: invalid input: integer overflow`.
  * `ABS` of a string or boolean → `Error: invalid input: can't take absolute value of 'a'` (resp. `... of TRUE`).
  * `LENGTH` of a non-string → `Error: invalid input: can't take length of 1` (resp. `... of 2.5`, `... of FALSE`).
  * `UPPER` of a non-string → `Error: invalid input: can't uppercase 1` (resp. `... TRUE`).
  * `LOWER` of a non-string → `Error: invalid input: can't lowercase 1.5` (resp. `... FALSE`).
  * Any other number of arguments is an unknown function, as today:
    `ABS()` → `Error: invalid input: unknown function abs with 0 arguments`,
    `ABS(1, 2)` → `Error: invalid input: unknown function abs with 2 arguments`, and likewise
    `unknown function length with 2 arguments`, `unknown function upper with 0 arguments`,
    `unknown function lower with 3 arguments`.
  * When the argument depends on a row, the error is that of the first row (in the order rows are processed)
    whose value is invalid, e.g. `SELECT ABS(name) FROM p` with first row name `'Ann'` →
    `Error: invalid input: can't take absolute value of 'Ann'`.

**Plans** display the calls exactly like the existing `sqrt(...)`: the function name in lower case, then the
argument in parentheses — `abs(p.delta)`, `length(p.name)`, `upper(p.name)`, `lower(p.name)`,
`length(lower(p.name))`. An argument that is not a column, a constant or a function call is shown with one more
pair of parentheses around it, as `sqrt` does: `abs((p.delta + 1))`. A function call used as an operand is never
parenthesized: `-abs(p.x)`, `length(lower(p.name)) * 2`.

A call whose argument contains no column reference is a constant expression and is folded like any other
constant expression (e.g. `WHERE LENGTH(name) > ABS(-3)` is shown as `Scan: p (length(p.name) > 3)`, and
`SELECT LOWER('ABC')` projects `'abc'`). A `DEFAULT` can use the functions; it stores the computed value
(`n INT DEFAULT ABS(-7)` is shown by `schema` as `n INTEGER DEFAULT 7`).

---

## 3. Aggregate functions BOOL_AND and BOOL_OR

Two new aggregate functions, usable like the existing ones (`COUNT`, `SUM`, `MIN`, `MAX`, `AVG`): in the SELECT
list, HAVING and ORDER BY, with or without GROUP BY, with any expression as the argument, combined with other
aggregates, and inside other expressions (`NOT BOOL_OR(f)`, `BOOL_AND(f) OR BOOL_OR(n = 8)`). Names are
case-insensitive.

* `NULL` values are ignored, like for every aggregate.
* `BOOL_AND(x)` is `TRUE` if every non-NULL value is `TRUE`, `FALSE` if at least one value is `FALSE`.
* `BOOL_OR(x)` is `TRUE` if at least one value is `TRUE`, `FALSE` if every non-NULL value is `FALSE`.
* If there are no non-NULL values (only NULLs, or no rows at all), the result is `NULL`. Without GROUP BY, an
  aggregation over no rows returns one row, as today: `SELECT BOOL_AND(f), BOOL_OR(f), COUNT(*) FROM b WHERE
  FALSE` → `NULL, NULL, 0`.
* Every non-NULL value must be a boolean. Otherwise the query fails, reporting the first such value in the order
  rows are aggregated: `Error: invalid input: can't BOOL_AND 1`, `Error: invalid input: can't BOOL_OR 'x'`
  (e.g. `BOOL_AND(1.5)` → `can't BOOL_AND 1.5`). An expression whose values are all NULL is no error
  (`BOOL_AND(n + NULL)` is `NULL`).
* The existing aggregate rules apply, with the existing messages (the name in lower case):
  `BOOL_AND()` and `BOOL_AND(f, f)` → `Error: invalid input: bool_and takes 1 argument`;
  `BOOL_OR(BOOL_AND(f))` → `Error: invalid input: aggregate functions can't be nested`;
  `BOOL_AND(*)` → `Error: invalid input: unsupported use of *`.

**Plans** show them in the Aggregate node like the other aggregates, in lower case with the argument as an
expression: `Aggregate: b.grp, bool_and(b.f), bool_or(b.n > 3)`.

---

## 4. Key and index lookups look up each value only once

When the optimizer executes a condition as a primary key lookup or a secondary index lookup (plan nodes
`KeyLookup: <table> (...)` and `IndexLookup: <table>.<column> (...)`, built today from conditions like
`id = 1 OR id = 2`, `v IS NULL`, `f IS NAN`, including lookups derived through a join), a value that occurs more
than once is now looked up only once:

* The node's value list contains each value once, in the order of its **first** occurrence:
  `WHERE id = 3 OR id = 1 OR id = 3 OR id = 1` → `KeyLookup: k (3, 1)`;
  `WHERE v = 'a' OR v = 'c' OR v = 'a'` → `IndexLookup: k.v ('a', 'c')`.
* Values are the same if they are equal; strings are compared case-sensitively (`('a', 'A')` stays as is), `NULL`
  (from `IS NULL`) equals `NULL`, `NaN` (from `IS NAN`) equals `NaN`, and `-0.0` equals `0.0` (the first one written
  is kept: `f = -0.0 OR f = 0.0` → `(-0.0)`). (Whether an integer and an equal float, e.g. `1` and `1.0`, count as
  the same value is not specified.) `= NULL` and `= NAN` contribute no value, as today.
* The display rule is unchanged but applies to the distinct values: 1–9 values are listed, otherwise the count is
  shown (`(10 keys)`, `(0 values)`). So ten conditions with nine distinct keys show all nine keys.
* Rows: a primary key lookup returns each matching row once, in the order of the (deduplicated) key list, as
  today for distinct keys: `SELECT id FROM k WHERE id = 3 OR id = 1 OR id = 3` returns `3`, then `1`. A secondary
  index lookup returns each matching row once in primary key order, as today.
* This also applies to UPDATE and DELETE: `DELETE FROM k WHERE id = 5 OR id = 5` deletes one row and reports
  `Delete { count: 1 }` (its plan is `Delete: k` above `KeyLookup: k (5)`).
* Nothing else about lookups changes (which conditions become lookups, remaining filters, joins).

---

## 5. CREATE TABLE IF NOT EXISTS

```
CREATE TABLE IF NOT EXISTS <name> ( <column definitions> )
```

* If no table with that name is visible to the transaction, the statement is exactly `CREATE TABLE <name> (...)`:
  same checks, same errors, same result.
* If a table with that name exists, the statement does nothing and succeeds: the existing table, its definition
  and its rows are unchanged, whatever the new definition says. More precisely: with an existing table, the
  statement reports exactly the error that the same statement without `IF NOT EXISTS` reports today, except that
  `table <name> already exists` is replaced by success. So these errors are still reported for an existing table
  (they are reported today instead of "already exists"):
  * syntax errors, e.g. `Error: invalid input: nullability already set for column v`;
  * `Error: invalid input: no primary key for table t`;
  * `Error: invalid input: multiple primary keys for table t`;
  * errors from evaluating a `DEFAULT` expression, e.g. `DEFAULT 1 / 0` → `Error: invalid input: can't divide by
    zero`, `DEFAULT id` → `Error: invalid input: expression must be constant, found column id`.

  All other definition errors are not reported for an existing table, because today "already exists" takes
  precedence over them, e.g. `invalid default type STRING for INTEGER column v` or
  `unknown table missing referenced by column r`. For a new table they are reported as usual (e.g.
  `Error: invalid input: invalid default type STRING for INTEGER column v`,
  `Error: invalid input: unknown table missing referenced by column r`,
  `Error: invalid input: primary key id cannot be nullable`), and the table is not created (a later
  `SELECT * FROM u` fails with `Error: invalid input: table u does not exist`). A new table may reference an
  existing one, with the usual reference checks (`Error: invalid input: reference 2 not in table t`).
* Without `IF NOT EXISTS` nothing changes: `Error: invalid input: table t already exists`.
* Transactions: the check sees what the transaction sees (its snapshot plus its own changes). A table created
  earlier in the same transaction exists; after `DROP TABLE u` in a transaction, `CREATE TABLE IF NOT EXISTS u`
  creates the new table. A table that another session has created but not committed, or committed after this
  transaction began, is not visible, so the statement tries to create it, exactly like `CREATE TABLE`, which fails
  with the existing write conflict error `Error: serialization failure, retry transaction`. A creation is undone by
  ROLLBACK and visible to other sessions after COMMIT, as for `CREATE TABLE`. The same holds in a read-only
  transaction (`BEGIN READ ONLY`): for a table it sees, `CREATE TABLE` reports `table <name> already exists` today,
  so `CREATE TABLE IF NOT EXISTS` succeeds; for a missing table both fail with `Error: read-only transaction`.
* Syntax: `IF`, `NOT`, `EXISTS` are the existing keywords (case-insensitive) and must come in this order directly
  after `TABLE`. Errors:
  * `CREATE TABLE IF EXISTS v (...)` → `Error: invalid input: expected token NOT, found EXISTS`
  * `CREATE TABLE IF v (...)` → `Error: invalid input: expected token NOT, found v`
  * `CREATE TABLE IF NOT v (...)` → `Error: invalid input: expected token EXISTS, found v`
  * `CREATE TABLE IF NOT EXISTS (id INT PRIMARY KEY)` → `Error: invalid input: expected identifier, got (`
  * `CREATE TABLE v IF NOT EXISTS (...)` → `Error: invalid input: expected token (, found IF`
  * `CREATE TABLE IF NOT EXISTS` → `Error: invalid input: unexpected end of input`
  * A quoted `"if"` is a table name as today: `CREATE TABLE IF NOT EXISTS "if" (id INT PRIMARY KEY)` works.
* Plans: the statement's plan is shown as for CREATE TABLE, `CreateTable: <name>`, whether or not the table exists.
* `schema` output, `DROP TABLE [IF EXISTS]` and everything else are unchanged.

---

## 6. INSERT ... SELECT

```
INSERT INTO <table> [ ( <column>, ... ) ] SELECT ...
```

where `SELECT ...` is any SELECT statement accepted today, with all its clauses (FROM with joins, WHERE, GROUP BY,
HAVING, ORDER BY, LIMIT, OFFSET; also without FROM). It is not parenthesized.

* Each row the SELECT returns is inserted exactly as if it had been written as one row of a `VALUES` list, in
  the order the SELECT returns them: with a column list, the values go to the listed columns and the other
  columns get their defaults; without one, the values go to the table's columns in order and missing trailing
  columns get their defaults. Rows are validated as for VALUES, with the same errors (datatype, NULL, primary
  key, unique, reference), e.g. `Error: invalid input: invalid datatype STRING for INTEGER column id`,
  `Error: invalid input: NULL value not allowed for column name`,
  `Error: invalid input: value 5 already in unique column score`,
  `Error: invalid input: primary key 1 already exists`. Rows inserted earlier by the same statement count
  (a SELECT returning the same key twice fails with `primary key ... already exists`).
* The number of values is checked for each returned row, as for VALUES rows, with the existing messages:
  `Error: invalid input: too many values for table dst`,
  `Error: invalid input: column and value counts do not match` (column list given),
  `Error: invalid input: no value given for column name with no default`.
  A SELECT that returns no rows inserts nothing and never fails these checks (e.g.
  `INSERT INTO dst SELECT id, name, score, 1 FROM src WHERE FALSE` succeeds on a 3-column table).
* The SELECT is evaluated completely, against what the statement's transaction sees before the statement, and
  only then are the rows inserted. So `INSERT INTO dst (id, name) SELECT id + 100, name FROM dst WHERE id < 20 OR
  id > 100` copies each qualifying existing row exactly once; the rows it inserts are not selected again.
  Because the SELECT is complete before any row is inserted, an error of the SELECT takes precedence over the
  checks of the rows it returns (value counts, datatypes, NULL, keys), even when an earlier row would fail them:
  with `src` rows with id 1, 2, 3 and a 3-column `dst`, `INSERT INTO dst SELECT id, name, score, 1 / (id - 2)
  FROM src` → `Error: invalid input: can't divide by zero` (not `too many values for table dst`, although the
  first row has 4 values).
* A failing INSERT ... SELECT outside an explicit transaction has no effect, like a failing INSERT ... VALUES.
* Transactions behave as for any INSERT: inside an explicit transaction the SELECT sees the transaction's own
  earlier writes but not other sessions' uncommitted writes; ROLLBACK undoes the inserted rows; other sessions see
  them after COMMIT, in transactions that begin after the commit (snapshot isolation); two concurrent
  transactions inserting the same primary key conflict with `Error: serialization failure, retry transaction`.
* Errors in the SELECT are those of the SELECT on its own, e.g. `Error: invalid input: unknown column missing`,
  `Error: invalid input: table nosuch does not exist`, `Error: invalid input: can't divide by zero`,
  `Error: invalid input: column name must be used in an aggregate or GROUP BY expression`.
  Errors about the target table and its column list are reported first, with the existing messages:
  `INSERT INTO nosuch SELECT ...` → `Error: invalid input: table nosuch does not exist`;
  `INSERT INTO dst (id, nope) SELECT ...` → `Error: invalid input: unknown column nope in table dst` (also when
  the SELECT itself is invalid); `INSERT INTO dst (id, id) SELECT ...` →
  `Error: invalid input: column id given multiple times`.
* Syntax errors:
  * `INSERT INTO dst (SELECT * FROM src)` → `Error: invalid input: expected identifier, got SELECT`
  * `INSERT INTO dst SELECT` → `Error: invalid input: unexpected end of input`
  * `INSERT INTO dst SELECT * FROM src VALUES (1)` → `Error: invalid input: unexpected token VALUES`
  * `INSERT INTO dst VALUES (4, 'd', 4) SELECT 1` → `Error: invalid input: unexpected token SELECT`
  * `INSERT INTO dst SELECT * FROM src;;` → `Error: invalid input: unexpected token ;`
* The result is `Insert { count: N }` with the number of inserted rows (`Insert { count: 0 }` for no rows).
* Plans: `Insert: <table>` with the SELECT's optimized plan (as `[plan]` shows it for the SELECT on its own) as its
  only child:

  ```
  > INSERT INTO dst (id, name) SELECT id + 200, name FROM src ORDER BY score DESC LIMIT 1
  Insert: dst
  └─ Remap: #0, src.name (dropped: src.score)
     └─ Limit: 1
        └─ Order: src.score desc
           └─ Projection: src.id + 200, src.name, src.score
              └─ Scan: src
  ```

`INSERT ... VALUES` is unchanged.
