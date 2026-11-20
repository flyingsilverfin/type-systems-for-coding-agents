# Task: four bug reports

Users of toydb have filed the four bug reports below. Fix all four.

The reports describe what users observed and what they expected. Each report ends with the exact expected
behaviour. Once fixed, the behaviour must be exactly as described there. Everything not described here keeps
its current behaviour, and all existing golden scripts and unit tests must keep passing unchanged, with
byte-identical output.

A small example script is provided as the visible test. It reproduces each of the four bugs once. All examples
below use the golden-script conventions. Values are displayed as today (`TRUE`, `FALSE`, `NULL`, `1`, `1.5`,
`NaN`, `'text'`). Errors are shown as `Error: <message>`. A statement that returns no rows, or that succeeds
without output, is shown as `ok`. `[result]` shows a write statement's result (e.g. `Delete { count: 2 }`).
Without ORDER BY, a table scan returns rows in primary key order, as today.

All four reports use these tables:

```
> CREATE TABLE city (id INT PRIMARY KEY, name STRING, country STRING)
> CREATE TABLE flight (id INT PRIMARY KEY, origin INT REFERENCES city, dest INT REFERENCES city, direct BOOLEAN)
> INSERT INTO city (id, country, name) VALUES (1, 'Norway', 'Oslo'), (2, 'Italy', 'Rome')
> INSERT INTO flight VALUES (10, 1, 2, FALSE), (11, NULL, 1, TRUE)
```

---

## Bug 1: INSERT with a column list that names every column puts values into the wrong columns

**Report.** When the column list of an INSERT names every column of the table, but not in the table's own column
order, the values end up in the wrong columns. They are stored in table order, as if there were no column list.
Column lists that leave out at least one column work correctly when each row has one value per listed column.

```
> SELECT * FROM city
```

Actual:

```
1, 'Norway', 'Oslo'
2, 'Italy', 'Rome'
```

Expected:

```
1, 'Oslo', 'Norway'
2, 'Rome', 'Italy'
```

Depending on the column types, the INSERT can also fail with an error about the wrong column, or succeed when it
should fail. With `CREATE TABLE q (id INT PRIMARY KEY, name STRING)`:

* `INSERT INTO q (name, id) VALUES ('x', 1)` actually fails with
  `Error: invalid input: invalid datatype STRING for INTEGER column id`. It should succeed and store the row
  `1, 'x'`.
* `INSERT INTO q (name, id) VALUES (2, 'y')` actually succeeds and stores the row `2, 'y'`. It should fail with
  `Error: invalid input: invalid datatype STRING for INTEGER column id`.
* `INSERT INTO q (id) VALUES (3, 'z')` actually succeeds and stores the row `3, 'z'`. It should fail with
  `Error: invalid input: column and value counts do not match`.

**Expected behaviour.**

* In `INSERT INTO t (c1, c2, ..., cn) VALUES (v1, v2, ..., vn), ...`, the i-th value of each row is stored in
  column `ci`. This holds for every order of the listed columns, and whether or not every column of the table is
  listed. Columns that aren't listed get their default value, as today.
* Each row then behaves exactly like the same row inserted without a column list, with its values rearranged into
  the table's column order (and defaults filled in). This covers the result, the validation and the errors. As
  today, a row is checked as follows, and the first problem found is reported:
  1. the primary key value (`Error: invalid input: invalid primary key NULL`, `Error: invalid input: primary key 1
     already exists`);
  2. then each column in table column order: its datatype (`Error: invalid input: invalid datatype STRING for
     INTEGER column id`, `Error: invalid input: invalid datatype INTEGER for STRING column nick`), NULL
     (`Error: invalid input: NULL value not allowed for column name`), its reference (`Error: invalid input:
     reference 9 not in table dept`) and uniqueness (`Error: invalid input: value 'eng' already in unique column
     name`).

  For example, with `CREATE TABLE person (id INT PRIMARY KEY, name STRING, nick STRING, age INT)`,
  `INSERT INTO person (name, id, nick, age) VALUES (7, 'Grace', 'g', 1)` assigns `id = 'Grace'` and `name = 7`.
  It fails with `Error: invalid input: invalid datatype STRING for INTEGER column id`, because `id` comes before
  `name` in the table.
* The value counts are checked as today. A row with more values than the table has columns fails with
  `Error: invalid input: too many values for table person`. Otherwise, a row whose number of values differs from
  the number of listed columns fails with `Error: invalid input: column and value counts do not match`.
* If any row fails, the statement inserts nothing, as today. Inserted rows are found by primary key and secondary
  index lookups, joins etc. like any other row.
* INSERT without a column list is unchanged.

---

## Bug 2: `x OR NULL` returns FALSE instead of NULL

**Report.** `OR` with a NULL operand gives `FALSE` where it should give `NULL`, when the other operand is
`FALSE`. `SELECT FALSE OR NULL` correctly returns `NULL`, but with a column it goes wrong:

```
> SELECT id, direct OR NULL FROM flight
```

Actual:

```
10, FALSE
11, TRUE
```

Expected:

```
10, NULL
11, TRUE
```

This also makes `WHERE NOT (direct OR NULL)` return flight 10, where it should return no rows.

**Expected behaviour.**

* `OR` uses SQL's three-valued logic, with either operand order:

  | `a OR b` | b = TRUE | b = FALSE | b = NULL |
  |---|---|---|---|
  | **a = TRUE** | TRUE | TRUE | TRUE |
  | **a = FALSE** | TRUE | FALSE | NULL |
  | **a = NULL** | TRUE | NULL | NULL |

* This holds for every `OR` in every clause: the SELECT list, WHERE, JOIN ... ON, GROUP BY, HAVING, ORDER BY,
  aggregate function arguments, and UPDATE ... SET and the WHERE of UPDATE and DELETE. It holds for any operands:
  columns, comparisons and other expressions over columns, and constants. A constant expression that evaluates to
  NULL behaves exactly like `NULL`, e.g. `NULL = 1`, `1 + NULL > 0`, `NOT NULL` and `NULL AND TRUE`.
* The NULL result is visible to the enclosing expression, like any other NULL. For example,
  `(flag OR NULL) IS NULL` is `TRUE` and `NOT (flag OR NULL)` is `NULL` when `flag` is `FALSE`.
* As today, WHERE, JOIN ... ON and HAVING keep only the rows for which the predicate is `TRUE`. ORDER BY sorts
  `NULL` before `FALSE` and `TRUE` when ascending, and after them when descending. GROUP BY puts all NULLs into
  one group.
* Non-boolean operands are errors, as today, with the operands in their written order:
  `SELECT id OR NULL FROM t` → `Error: invalid input: can't OR 1 and NULL` (for a row with `id = 1`), and
  `SELECT id FROM t WHERE NULL OR n` → `Error: invalid input: can't OR NULL and 1` (for a row with `n = 1`).
* `AND`, `NOT` and the other operators are unchanged.

---

## Bug 3: LEFT and RIGHT JOIN lose rows whose join column is NULL

**Report.** A LEFT JOIN on an equality between two columns drops the left-hand rows whose join column is NULL. They
should come back with NULLs for the right-hand table. RIGHT JOIN has the same problem with right-hand rows.

```
> SELECT flight.id, city.name FROM flight LEFT JOIN city ON flight.origin = city.id
```

Actual:

```
10, 'Oslo'
```

Expected:

```
10, 'Oslo'
11, NULL
```

Counts are wrong too. `SELECT COUNT(*) FROM flight LEFT JOIN city ON flight.origin = city.id` returns `1`
instead of `2`.

**Expected behaviour.**

* `l LEFT [OUTER] JOIN r ON p` returns, for every row of `l`, one joined row for each row of `r` for which `p` is
  `TRUE`. If there is no such row of `r`, it returns exactly one row with the values of the `l` row and `NULL` in
  every column of `r`.
* A predicate that is `NULL` is not `TRUE`. In particular, an equality with a `NULL` (or `NaN`) operand never
  matches, not even `NULL = NULL` (or NaN with NaN). A row of `l` whose join column is `NULL` or `NaN` therefore
  always appears exactly once, with NULLs for `r`. This holds whichever way the equality is written
  (`l.x = r.y` or `r.y = l.x`), and also when the ON condition has further conditions on `r`, e.g.
  `flight LEFT JOIN city ON flight.origin = city.id AND city.name != 'Rome'`.
* This report covers ON conditions in which each part joined by `AND` refers to some column of `r`. An ON
  condition with a part that refers only to columns of `l` (e.g. `... ON flight.origin = city.id AND
  flight.id > 10`), and a WHERE clause over an outer join, keep their current behaviour.
* `l RIGHT [OUTER] JOIN r ON p` is the same, with the roles of `l` and `r` swapped. Its result columns are still
  those of `l` followed by those of `r`.
* The outer side may itself be a join, e.g. `a LEFT JOIN b ON ... LEFT JOIN c ON b.x = c.id`. Here NULLs that the
  first join filled in for `b` don't match anything in the second join, so those rows also get NULLs for `c`.
* Row order without ORDER BY: rows come in the order of the outer side's rows (for a table, primary key order).
  The joined rows of one outer row follow the order of the other side's rows. A NULL-padded row takes the place
  of its outer row. This is the order outer joins already produce for rows that match.
* Inner joins (`JOIN`, `INNER JOIN`, `FROM a, b WHERE ...`) never return rows for NULL join values, as today.
  Aggregates, GROUP BY, ORDER BY and LIMIT over outer joins work on the rows described above.

---

## Bug 4: a row referenced through a second foreign key column can be deleted

**Report.** If a table has several columns that reference the same table, only references through the first
such column prevent deleting the referenced row. City 2 is referenced by flight 10 as its destination, but it
can be deleted, leaving flight 10 with a dangling reference:

```
> DELETE FROM city WHERE id = 2
```

Actual: `ok`, and city 2 is gone. Expected:

```
Error: invalid input: row referenced by flight.id=10
```

Deleting city 1, which flight 10 references as its origin, is correctly rejected with the same error. Changing a
referenced primary key with UPDATE (`UPDATE city SET id = 9 WHERE id = 2`) has the same problem. So do tables
that reference themselves through several columns.

**Expected behaviour.**

* A row can't be deleted, and an UPDATE can't change its primary key, while some row references it. A row of
  table `p` is referenced when some row has a column declared `REFERENCES p` that holds the row's primary key
  value. This counts every such column of every table, including `p` itself. `NULL` references nothing.
* The statement then fails with `Error: invalid input: row referenced by <table>.<column>=<value>`, and changes
  nothing. Here `<table>` is the referencing table, `<column>` the name of its primary key column, and `<value>`
  the primary key of the referencing row. Examples: `row referenced by flight.id=10`,
  `row referenced by emp.id=3`, `row referenced by child.id=1`.
* As today, references from rows that the same DELETE statement deletes are ignored. This includes a row that
  references itself. For example, `DELETE FROM emp WHERE id = 4 OR id = 5` succeeds when rows 4 and 5 are only
  referenced by each other and by themselves.
* If there are several (non-ignored) referencing rows, the reported one is found like this. Referencing tables
  are taken in alphabetical order of their names. Within a table, its referencing columns are taken in table
  column order. The first column through which any of the affected rows is referenced determines the result: the
  smallest primary key among the rows that reference one of them through that column.
* Once the referencing rows are deleted, or their references are changed (e.g. set to NULL), the row can be
  deleted, and its primary key changed.
* `DROP TABLE`, and the reference checks of INSERT and UPDATE on the referencing table, are unchanged.
