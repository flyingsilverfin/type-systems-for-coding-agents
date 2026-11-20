# Task: ALTER TABLE

Add an `ALTER TABLE` statement to toydb that changes the columns of an existing table: it can add a column, drop
a column, or rename a column. An altered table must behave in every respect like a table that was created with
the resulting definition and holds the same rows: in queries, writes, constraints, indexes, foreign keys,
transactions, `schema` output and query plans.

This task is purely additive. Everything not described here keeps its current behaviour, and all existing golden
scripts must keep producing byte-identical output (some of them print raw stored keys and values, so the stored
form of existing tables, rows and index entries must not change).

A small example script is provided as the visible test. All examples below use the golden-script conventions:
`> statement` followed by its result rows, errors as `Error: <message>`, `schema` printing table definitions, and
`[plan]` printing the plan of a statement before executing it. Error messages are shown below without the
`Error: invalid input: ` prefix that the output adds to them (except where an example shows the full output line).

## Syntax

```
ALTER TABLE <table> ADD [COLUMN] <column definition>
ALTER TABLE <table> DROP [COLUMN] <column>
ALTER TABLE <table> RENAME [COLUMN] <column> TO <new column name>
```

* One action per statement. As for every statement, a single trailing `;` is allowed.
* The word `COLUMN` is optional in all three forms: `ALTER TABLE t ADD c INT` is the same as
  `ALTER TABLE t ADD COLUMN c INT`, `ALTER TABLE t DROP c` the same as `ALTER TABLE t DROP COLUMN c`.
* `<column definition>` is exactly what CREATE TABLE accepts for one column: a name, a data type
  (`BOOL`/`BOOLEAN`, `INT`/`INTEGER`, `FLOAT`/`DOUBLE`, `STRING`/`TEXT`/`VARCHAR`), and then the column
  constraints in any order (`PRIMARY KEY`, `NULL`, `NOT NULL`, `DEFAULT <expression>`, `UNIQUE`, `INDEX`,
  `REFERENCES <table>`), parsed with the same rules and the same errors as in CREATE TABLE.
* Table and column names are identifiers with the existing rules: unquoted names are case-insensitive (stored in
  lower case), double-quoted names are kept as written. As in CREATE TABLE, the parser accepts the empty quoted
  name `""`; an empty column name is then rejected by the checks below (`column name can't be empty`).

## Reserved words

`ALTER`, `ADD`, `COLUMN`, `RENAME` and `TO` become reserved keywords, with the same rules as the existing ones:

* They are case-insensitive: `alter table t add column c int`, `Alter Table T Rename A To B`.
* They can no longer be used as unquoted identifiers: `CREATE TABLE add (id INT PRIMARY KEY)` →
  `Error: invalid input: expected identifier, got ADD`; `SELECT 1 AS to` → `expected identifier, got TO`.
* Double-quoted they remain valid identifiers, and `schema` shows such names double-quoted, as it does for every
  other keyword: after `ALTER TABLE t ADD COLUMN "column" INT` the column is displayed as
  `"column" INTEGER DEFAULT NULL`.

If the code base has a test that pins the exact number or set of reserved keywords, update it for these five new
keywords; that is the only existing test expectation that may change.

## Syntax errors

The statement is read from left to right, and the first token that does not fit is reported, before anything
else (such as the existence of the table) is checked. Tokens are displayed in messages as the existing parser
errors display them: keywords in upper case, identifiers as their (lower-cased unless quoted) name, symbols as
written.

1. After `ALTER`, `TABLE` is required: `ALTER t ADD c INT` → `expected token TABLE, found t`.
2. The table name must be an identifier: `ALTER TABLE 1 DROP c` → `expected identifier, got 1`.
3. Then `ADD`, `DROP` or `RENAME` is required; any other token gives `unexpected token <token>`:
   `ALTER TABLE t MODIFY v INT` → `unexpected token modify`.
4. `ADD`: an optional `COLUMN`, then a column definition, read exactly as in CREATE TABLE (with its errors, e.g.
   `ALTER TABLE t ADD COLUMN c BLOB` → `unexpected token blob`, `ALTER TABLE t ADD c INT NULL NOT NULL` →
   `nullability already set for column c`, `ALTER TABLE t ADD c INT TO` → `unexpected keyword TO`).
5. `DROP`: an optional `COLUMN`, then the column name, an identifier.
6. `RENAME`: an optional `COLUMN`, the column name, then `TO` is required
   (`ALTER TABLE t RENAME COLUMN v w` → `expected token TO, found w`), then the new name, an identifier.
7. After the action, only an optional `;` may follow; anything else gives `unexpected token <token>`, e.g. a
   second action: `ALTER TABLE t ADD COLUMN c INT, ADD COLUMN d INT` → `unexpected token ,`.
8. If the input ends where a token is required: `unexpected end of input` (`ALTER TABLE t`, `ALTER TABLE t DROP`,
   `ALTER TABLE t RENAME v TO`).

Since `COLUMN` and `TO` are reserved, an identifier is required after the optional `COLUMN`:
`ALTER TABLE t ADD column INT` → `expected identifier, got INT`; `ALTER TABLE t DROP COLUMN column` →
`expected identifier, got COLUMN`; `ALTER TABLE t DROP COLUMN IF EXISTS v` → `expected identifier, got IF`;
`ALTER TABLE t RENAME TO u` → `expected identifier, got TO` (renaming tables is not supported).

## Checks and their errors

After parsing, an ALTER TABLE statement makes the checks below in the order listed, and reports the first one that
fails. In the messages, `<t>` is the table name and `<c>` the column name as stored (without quotes), and values
are displayed as in the existing error messages (strings in single quotes, e.g. `'x'`).

For every action, first:

1. The table must exist: `table <t> does not exist` (the existing message).

`ADD`:

2. The column can't be a primary key (a table has exactly one, and it already has it):
   `can't add primary key column <c> to table <t>`, also for an empty table.
3. The `DEFAULT` expression is evaluated as in CREATE TABLE, with the same errors (it must be constant:
   `ALTER TABLE t ADD c INT DEFAULT id` → `expression must be constant, found column id`).
4. No column of the table may have the same name: `column <c> already exists in table <t>`
   (`ALTER TABLE t ADD COLUMN ID STRING` on a table with a column `id` fails, `"ID"` does not).
5. The column definition must be valid, with exactly the checks and messages that CREATE TABLE applies to a
   column, in this order:
   * `column name can't be empty` (`ALTER TABLE t ADD COLUMN "" INT`),
   * `invalid default type <type> for <column type> column <c>` (e.g. `invalid default type INTEGER for FLOAT
     column c` for `c FLOAT DEFAULT 1`),
   * `invalid NULL default for non-nullable column <c>` (`c INT NOT NULL DEFAULT NULL`),
   * `unknown table <x> referenced by column <c>` (`REFERENCES x` where table x does not exist; a column may
     reference its own table),
   * `can't reference <type> primary key of <x> from <column type> column <c>` (the column's type must be the
     type of the referenced table's primary key).
6. Only if the table has at least one row, the value that the existing rows will get (see below) must be valid
   for them, in this order:
   * A `NOT NULL` column without a default can't be added:
     `non-empty table <t> requires a default value for NOT NULL column <c>`.
   * A `REFERENCES` column with a non-NULL default: the default must be the primary key of a row of the
     referenced table (of `<t>` itself for a self-reference): `reference <value> not in table <x>` (the existing
     message).
   * A `UNIQUE` column whose default is not NULL or NaN, on a table with two or more rows:
     `value <value> already in unique column <c>` (the existing message). As today, NULL and NaN values are never
     duplicates of each other.

   On an empty table none of these checks apply: `ALTER TABLE empty ADD c INT NOT NULL` succeeds (later inserts
   must then give a value for `c`, as for any NOT NULL column without default), and so does
   `ALTER TABLE empty ADD c INT DEFAULT 9 REFERENCES dept` when dept has no row 9 (the default is checked when an
   insert uses it, as today).

`DROP`:

2. The column must exist: `unknown column <c> in table <t>` (the same message as for an unknown column in an
   INSERT column list).
3. The primary key column can't be dropped: `can't drop primary key column <c> of table <t>`. Since every table
   has exactly one primary key and it can't be dropped, a table always keeps at least one column: dropping the
   only column of a table is this error too.

`RENAME`:

2. The column must exist: `unknown column <c> in table <t>`.
3. No column of the table may already have the new name: `column <new> already exists in table <t>`. This
   includes renaming a column to its own name (`RENAME COLUMN v TO v`, and also `RENAME COLUMN v TO V`, since the
   unquoted `V` is `v`).
4. The new name can't be empty, as for a column in CREATE TABLE: `ALTER TABLE t RENAME COLUMN v TO ""` →
   `column name can't be empty` (the existing message).

A statement that fails changes nothing: neither the definition nor any row nor any index entry. This also holds
inside an explicit transaction, where (as for every failed statement) the transaction stays open and usable: after
`BEGIN` and a failing `ALTER TABLE t ADD COLUMN u INT UNIQUE DEFAULT 7`, `SELECT * FROM t` and `schema t` show
exactly what they showed before the statement, and a later `ALTER TABLE t ADD COLUMN u INT INDEX` builds an index
that contains only the new column's values.

## Effects

### ADD

* The new column becomes the last column of the table. `SELECT *` shows it last, and an `INSERT` without a column
  list takes its value last (an `INSERT` that gives fewer values than there are columns fills the missing trailing
  columns with their defaults, as today).
* The column gets the same derived properties as in CREATE TABLE: it is nullable unless `NOT NULL` is given; a
  nullable column without `DEFAULT` gets the default NULL; a `NOT NULL` column without `DEFAULT` has no default;
  `UNIQUE` and `REFERENCES` imply `INDEX`. `schema` shows it exactly as a CREATE TABLE column with this definition
  is shown, e.g. `ALTER TABLE emp ADD COLUMN dept_id INT DEFAULT 1 REFERENCES dept` →
  `dept_id INTEGER DEFAULT 1 INDEX REFERENCES dept`.
* Every existing row gets the column's default value (NULL for a nullable column without `DEFAULT`). The value is
  stored: it is the row's value from now on, and later changes to the table do not change it.
* If the column is indexed (`INDEX`, `UNIQUE` or `REFERENCES`), the index covers the existing rows immediately:
  index lookups find them (`WHERE c = <default>` and `WHERE c IS NULL`), uniqueness is enforced against them (with
  `ALTER TABLE one ADD COLUMN u INT UNIQUE DEFAULT 0` on a table with one row, inserting another row with the
  default fails with `value 0 already in unique column u`), and a referenced row can't be deleted while an
  existing row references it through the new column.

### DROP

* The column and its values are removed from every row; the columns after it move one position to the left
  (`SELECT *`, `INSERT` without column list). The primary key stays the primary key wherever it is in the table.
* The column's constraints and its index disappear with it. If it was a `REFERENCES` column, its foreign key
  is gone: referenced rows can then be deleted, and the referenced table dropped (if nothing else references it).
* Dropped values never come back. A column that is added later with the same name is a new column: the existing
  rows get its default, and its index (if any) contains only its own values.
* Foreign keys always reference a primary key, which can't be dropped, so dropping a column never breaks a
  reference from another table.

### RENAME

* Only the name changes. The column keeps its position, type, nullability, default, `UNIQUE`, index,
  `REFERENCES` and all its values. Any column can be renamed, including the primary key and `REFERENCES`
  columns.
* From now on the column is only known by its new name: in queries (the old name is an unknown column, e.g.
  `unknown column salary`, `unknown column emp.salary`), in `INSERT` column lists, in `UPDATE ... SET`, in
  `schema` output, in result headers (`emp.pay`), in plans (`IndexLookup: emp.label ('a')`), and in error
  messages that name it (`value 'eng' already in unique column title`, `row referenced by emp.emp_no=10` where
  `emp_no` is the renamed primary key of the referencing table, `table dept is referenced from emp.dept` where
  `dept` is the renamed reference column).
* The index of a renamed column keeps working under the new name (lookups, uniqueness, foreign-key checks). The
  old name is free: a column added later under the old name is a new column whose index (if any) contains only its
  own values.
* Foreign keys reference tables, not column names, so renaming the primary key of a referenced table does not
  affect the references to it.

### Afterwards

The altered table behaves exactly like a table created with the resulting definition and holding the same rows,
in particular for queries of every kind (joins, GROUP BY, HAVING, aggregates, ORDER BY, LIMIT), for INSERT, UPDATE
and DELETE with all their constraint checks and error messages, and for the optimizer: the plans of statements on
the altered table (shown by `[plan]` or `EXPLAIN`) are those for the equivalent created table, e.g. a primary key
lookup is `KeyLookup: t (2)` and a lookup through the index of a renamed column `IndexLookup: emp.label ('a')`.

## Plan display

The plan of an ALTER TABLE statement (shown by `[plan]` before it is executed, and by `EXPLAIN`) is a single
line `AlterTable: <t>`, with the table name as stored, like the existing `CreateTable: <t>` and `DropTable: <t>`:

```
> CREATE TABLE movie (id INT PRIMARY KEY, title STRING)
[plan]> ALTER TABLE movie ADD COLUMN rating FLOAT DEFAULT 5.0
---
AlterTable: movie
```

Building the plan changes nothing; the table is altered only when the statement is executed. In particular
`EXPLAIN ALTER TABLE ...` leaves the table unchanged.

## Transactions

ALTER TABLE is transactional, like CREATE TABLE and DROP TABLE, and follows the existing snapshot-isolation
rules:

* It runs in the session's open transaction, or else in an implicit transaction of its own. `ROLLBACK` undoes it
  completely (definition, row values and index entries).
* Until the transaction commits, other transactions see the old definition and the old rows (in queries, index
  lookups and `schema` output).
* A transaction that began before the commit keeps seeing the old definition and the old rows until it ends;
  transactions that begin after the commit see the new ones.
* An ALTER TABLE (that passes all the checks above) of a table that another transaction has altered or dropped
  and not yet committed, or committed after the current transaction began, fails with the existing write-conflict
  error `Error: serialization failure, retry transaction` (this message has no `invalid input:` prefix).
* In a read-only transaction (`BEGIN READ ONLY`), an ALTER TABLE that passes all the checks above fails with the
  existing `Error: read-only transaction`; one that fails a check reports that check's error.

What happens when another transaction inserts, updates or deletes rows of a table while it is being altered
concurrently is not specified.

## Not part of this task

Renaming tables, changing a column's type, default or constraints, `IF [NOT] EXISTS` for columns, several actions
in one statement, and CREATE/DROP INDEX. The statement result of ALTER TABLE (what a client prints after it) is not
tested and may be anything reasonable; ALTER TABLE outputs no rows.
