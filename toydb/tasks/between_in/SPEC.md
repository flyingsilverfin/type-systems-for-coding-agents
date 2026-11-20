# Task: BETWEEN and IN

Add two SQL predicates to toydb's expression language:

```
expr [NOT] BETWEEN low AND high
expr [NOT] IN (e1, e2, ..., eN)        -- N >= 1
```

They are ordinary expressions and must work everywhere an expression is allowed today: the SELECT list,
WHERE, JOIN ... ON, GROUP BY, HAVING, ORDER BY, INSERT ... VALUES, UPDATE ... SET, DEFAULT in CREATE TABLE,
inside the arguments of functions and aggregate functions, and as operands of other operators. Their own
operands (`expr`, `low`, `high`, every `ei`) are arbitrary expressions: constants, columns (qualified or not,
from any joined table), arithmetic, function calls, aggregate functions (where aggregates are allowed today),
and nested BETWEEN/IN.

This task is purely additive. Everything not described here keeps its current behaviour, and all existing
golden scripts must keep producing byte-identical output.

A small example script is provided as the visible test. All examples below use the golden-script conventions:
values are displayed as today (`TRUE`, `FALSE`, `NULL`, `1`, `1.5`, `'text'`), errors as `Error: <message>`.

## Meaning

The new forms are defined by expansion into existing operators, and behave **exactly** like the expansion, for
results, NULL handling and errors alike:

| Form | Means exactly |
|---|---|
| `x BETWEEN a AND b` | `x >= a AND x <= b` |
| `x NOT BETWEEN a AND b` | `NOT (x >= a AND x <= b)` |
| `x IN (e1, e2, ..., eN)` | `x = e1 OR x = e2 OR ... OR x = eN` |
| `x NOT IN (e1, e2, ..., eN)` | `NOT (x = e1 OR x = e2 OR ... OR x = eN)` |

where `x`, `a`, `b`, `ei` stand for the (parenthesized) operand expressions. Consequences worth spelling out:

* **Bounds are inclusive and never swapped.** `2 BETWEEN 1 AND 3`, `1 BETWEEN 1 AND 3` and `3 BETWEEN 1 AND 3`
  are `TRUE`; `2 BETWEEN 3 AND 1` is `FALSE` (and `2 NOT BETWEEN 3 AND 1` is `TRUE`).
* **Types** follow the existing comparison rules: integers and floats can be mixed (`1 IN (1.0)` and
  `2 BETWEEN 1.5 AND 2.0` are `TRUE`), strings compare with strings (exactly, case-sensitively), booleans with
  booleans (`FALSE < TRUE`). NaN compares false with everything, as today: `NAN IN (NAN)` is `FALSE`,
  `NAN BETWEEN 0 AND 1` is `FALSE` and `NAN NOT BETWEEN 0 AND 1` is `TRUE`.
* **Three-valued logic** is that of the existing `AND`, `OR`, `NOT` and comparisons:
  * `NULL IN (1, 2)`, `NULL NOT IN (1, 2)`, `NULL IN (NULL)` are `NULL`.
  * `1 IN (1, NULL)` is `TRUE`; `1 IN (2, NULL)` is `NULL`; `1 NOT IN (1, NULL)` is `FALSE`;
    `1 NOT IN (2, NULL)` is `NULL`.
  * `NULL BETWEEN 1 AND 3`, `2 BETWEEN NULL AND 3`, `2 BETWEEN 1 AND NULL` are `NULL` (and so are their
    `NOT BETWEEN` forms).
  * `5 BETWEEN NULL AND 3` and `0 BETWEEN 1 AND NULL` are `FALSE` (the other bound already decides), so
    `5 NOT BETWEEN NULL AND 3` and `0 NOT BETWEEN 1 AND NULL` are `TRUE`.
  * In WHERE, JOIN ... ON and HAVING a `NULL` result does not select the row, as today.
* **Errors are those of the expansion.** Comparing incompatible types fails as it does today, with the
  BETWEEN/IN left-hand operand first in the message:
  * `SELECT 1 IN ('a')` → `Error: invalid input: can't compare 1 and 'a'`
  * `SELECT 'a' NOT IN (TRUE)` → `Error: invalid input: can't compare 'a' and TRUE`
  * `SELECT 1 BETWEEN 'a' AND 2` → `Error: invalid input: can't compare 1 and 'a'`
  * `SELECT 'a' BETWEEN 1 AND 2` → `Error: invalid input: can't compare 'a' and 1`
  * A `NULL` left-hand operand never fails: `NULL IN ('a', 1)` and `NULL BETWEEN 'a' AND TRUE` are `NULL`.
* **No short-circuiting.** The existing `AND` and `OR` evaluate both operands, so every operand of BETWEEN and
  IN is evaluated even when the result is already determined, and an error in any of them is reported:
  * `SELECT 1 IN (1, 'a')` → `Error: invalid input: can't compare 1 and 'a'`
  * `SELECT 1 BETWEEN 2 AND 'a'` → `Error: invalid input: can't compare 1 and 'a'`
  * `SELECT 1 IN (1, 1 / 0)` → `Error: invalid input: can't divide by zero`
  * with a table whose first row has `s = 'a'`: `SELECT id FROM t WHERE s NOT IN ('a', 2)` →
    `Error: invalid input: can't compare 'a' and 2`
* Other existing errors surface unchanged, e.g. `WHERE x IN (1)` on a table without a column `x` →
  `Error: invalid input: unknown column x`, and `SELECT 2 BETWEEN 1 AND 3 AND 4` →
  `Error: invalid input: can't AND TRUE and 4`.
* Repeated list elements are allowed: `1 IN (1, 1, 1)` is `TRUE`.
* A result column computed by BETWEEN or IN has no name in the result header (like any other computed
  expression) unless it is given an alias with `AS`.
* Rows are returned in the order ORDER BY specifies. (Without ORDER BY, the order of rows, and the handling of
  a list that repeats a value when it is matched against a primary-key or indexed column, are whatever the
  expanded `OR` expression does today; nothing else is required.)

## Precedence and grouping

BETWEEN and IN (with or without NOT) are at the **same precedence level as `=`, `!=` and `LIKE`**, and are
left-associative with them. So, from tightest to loosest: arithmetic and postfix/prefix arithmetic operators;
`<`, `<=`, `>`, `>=`; then `=`, `!=`, `LIKE`, `IS`, `BETWEEN`, `IN`; then prefix `NOT`; then `AND`; then `OR`.

* The left-hand operand is everything to the left that binds tighter or equally: `1 + 1 BETWEEN 1 AND 3` is
  `(1 + 1) BETWEEN 1 AND 3`; `1 < 2 IN (TRUE)` is `(1 < 2) IN (TRUE)`; `1 = 1 IN (TRUE)` is
  `(1 = 1) IN (TRUE)`; `'a' LIKE 'a' IN (TRUE)` is `('a' LIKE 'a') IN (TRUE)`.
* `low` and `high` are parsed like the right-hand operand of `=`: they extend over arithmetic and
  `<`, `<=`, `>`, `>=`, but stop at `=`, `!=`, `LIKE`, `IS`, `BETWEEN`, `IN`, `NOT`, `AND`, `OR` (use parentheses
  for those). `5 BETWEEN 1 AND 2 + 4` is `5 BETWEEN 1 AND (2 + 4)`.
* The `AND` between the bounds belongs to BETWEEN; the next `AND` is a logical AND:
  `2 BETWEEN 1 AND 3 AND FALSE` is `(2 BETWEEN 1 AND 3) AND FALSE`.
* List elements are full expressions (anything, including `AND`/`OR`, up to the `,` or `)`).
* After a complete BETWEEN or IN the expression continues exactly as it would after an `=` comparison, whatever
  the left-hand operand was:
  * `1 + 1 IN (2) AND TRUE` is `((1 + 1) IN (2)) AND TRUE`, and `1 + 1 BETWEEN 1 AND 3 AND TRUE` is
    `((1 + 1) BETWEEN 1 AND 3) AND TRUE`; likewise with `OR`.
  * `1 IN (1) = TRUE` is `(1 IN (1)) = TRUE`; `1 IN (1) IN (TRUE)` is `(1 IN (1)) IN (TRUE)`;
    `1 BETWEEN 0 AND 2 = TRUE` is `(1 BETWEEN 0 AND 2) = TRUE`;
    `3 BETWEEN 1 AND 2 BETWEEN FALSE AND FALSE` is `(3 BETWEEN 1 AND 2) BETWEEN FALSE AND FALSE`.
  * `2 IN (NULL) IS NULL` is `(2 IN (NULL)) IS NULL`; `NULL IS NULL IN (TRUE)` is `(NULL IS NULL) IN (TRUE)`.
* A prefix `NOT` binds looser: `NOT 1 IN (2)` is `NOT (1 IN (2))`, `NOT 1 NOT IN (2)` is `NOT (1 NOT IN (2))`.

## Reserved words

`BETWEEN` and `IN` become reserved keywords, with the same rules as the existing ones (`AND`, `LIKE`, ...):

* They are case-insensitive: `2 between 1 and 3`, `2 nOt iN (2)`.
* They can no longer be used as unquoted identifiers:
  `CREATE TABLE between (id INT PRIMARY KEY)` → `Error: invalid input: expected identifier, got BETWEEN`;
  `CREATE TABLE t (id INT PRIMARY KEY, in INT)` and `SELECT 1 AS in` →
  `Error: invalid input: expected identifier, got IN`.
* Double-quoted they remain valid identifiers (`CREATE TABLE "in" (id INT PRIMARY KEY, "between" INT)`), and
  the `schema` output shows such names double-quoted, as it does for every other keyword:

  ```
  CREATE TABLE "in" (
    id INTEGER PRIMARY KEY,
    "between" INTEGER DEFAULT NULL
  )
  ```
* Identifiers that merely contain the words (`inbox`, `pin`, `between_`) are unaffected.

If the code base has a test that pins the exact number or set of reserved keywords, update it for these two new
keywords; that is the only existing test expectation that may change.

## Syntax errors

Tokens are displayed in messages as the existing parser errors display them (keywords in upper case, symbols and
numbers as written).

| Input | Error |
|---|---|
| empty list: `1 IN ()`, `1 NOT IN ()` | `Error: invalid input: expected expression atom, found )` |
| trailing comma: `1 IN (1, )` | `Error: invalid input: expected expression atom, found )` |
| list element followed by anything but `,` or `)`: `1 IN (1 2)`, `1 NOT IN (1, 2; 3)` | `Error: invalid input: expected token ), found 2` (the offending token; resp. `found ;`) |
| `IN` not followed by `(`: `1 IN 1`, `1 NOT IN 1, 2` | `Error: invalid input: expected token (, found 1` (the offending token) |
| low bound not followed by `AND`: `1 BETWEEN 1 OR 2`, `1 BETWEEN 1, 2`, `1 BETWEEN 1 2`, `... WHERE v BETWEEN 1 ORDER BY id` | `Error: invalid input: expected token AND, found OR` (resp. `found ,`, `found 2`, `found ORDER`) |
| input ends inside the construct: `1 IN`, `1 IN (1, 2`, `1 BETWEEN 1`, `1 BETWEEN 1 AND`, `1 NOT` | `Error: invalid input: unexpected end of input` |
| `NOT` directly after an operand, followed by anything but `BETWEEN` or `IN`: `1 NOT 2`, `1 NOT NULL`, `DELETE FROM t WHERE id NOT 1` | `Error: invalid input: unexpected token 2` (the token after `NOT`; resp. `NULL`, `1`). Exception: after a column DEFAULT expression, see below |
| no left-hand operand: `SELECT IN (1)`, `SELECT BETWEEN 1 AND 2`, `SELECT in.id FROM "in"` | `Error: invalid input: expected expression atom, found IN` (resp. `found BETWEEN`) |
| keyword after `table.`: `SELECT "in".between FROM "in"` | `Error: invalid input: expected identifier, got BETWEEN` |

A statement that fails to parse has no effect.

### NOT after a column DEFAULT expression

In `CREATE TABLE`, a column's `DEFAULT expr` can be followed by further column constraints, in particular
`NOT NULL` (`n INT DEFAULT 1 NOT NULL`). This keeps working exactly as today. Directly after a column's
default expression, a `NOT` belongs to that expression only if the next token is `BETWEEN` or `IN`; otherwise
the default expression ends before the `NOT`, which is then handled as the column constraint, as today:

* `n INT DEFAULT 1 NOT NULL` is a `NOT NULL` column with default `1`;
  `p BOOLEAN DEFAULT 2 NOT IN (1, 3) NOT NULL` is a `NOT NULL` column with default `TRUE`, and
  `q BOOLEAN DEFAULT 2 NOT BETWEEN 1 AND 3 NULL` is a nullable column with default `FALSE`.
* `n INT DEFAULT 1 NOT 2` → `Error: invalid input: expected token NULL, found 2` and
  `n INT DEFAULT 1 NOT NULL NOT NULL` → `Error: invalid input: nullability already set for column n`
  (both unchanged).
* This only applies where a column constraint can follow, i.e. not inside parentheses, function call arguments
  or an IN list within the default expression, where the general rule above holds:
  `n INT DEFAULT (1 NOT NULL)` and `n BOOLEAN DEFAULT 1 IN (1 NOT NULL)` →
  `Error: invalid input: unexpected token NULL`.

## Interaction with existing features (must all work)

* **WHERE** on any column, including primary-key columns and columns with `INDEX` or `UNIQUE`, with constant
  lists, lists containing `NULL` or `NAN`, lists and bounds that reference columns of the same row
  (`n BETWEEN lo AND hi`, `10 IN (n, lo, hi)`, `k IN (id * 10, 30)`), and combined with other predicates through
  `AND`/`OR`/`NOT`. The result must be the same whether or not an index exists.
* **SELECT list and ORDER BY**, including ORDER BY on a BETWEEN/IN expression over columns that are not in the
  SELECT list (`SELECT name FROM t ORDER BY score BETWEEN 10 AND 20 DESC, id DESC`), ORDER BY on an alias of
  such an expression, LIMIT and OFFSET, and SELECT without FROM (`SELECT 'yes' WHERE 3 IN (1, 2, 3)`).
* **Joins**: in ON conditions of inner and outer joins with columns from both sides
  (`a JOIN b ON a.x BETWEEN b.lo AND b.hi`, `ON d.id IN (e.dept, e.id)`), and in WHERE/SELECT over joined tables.
* **Aggregates, GROUP BY, HAVING**: aggregate functions as any operand
  (`SUM(v) BETWEEN 100 AND 110`, `20 BETWEEN MIN(v) AND MAX(v)`, `6 IN (COUNT(v), COUNT(*))`), BETWEEN/IN inside
  aggregate arguments (`COUNT(v IN (10, 20))`, `MAX(v BETWEEN 10 AND 20)`), in HAVING (also with aggregates
  that are not in the SELECT list, and with GROUP BY columns), in ORDER BY over aggregates that are not in the
  SELECT list, and as a GROUP BY expression that is repeated verbatim in the SELECT list
  (`SELECT v BETWEEN 10 AND 30, COUNT(*) FROM t GROUP BY v BETWEEN 10 AND 30`).
* **Writes**: `INSERT ... VALUES (5 IN (1, 2), ...)`, `UPDATE ... SET flag = v BETWEEN 15 AND 55`,
  `UPDATE`/`DELETE ... WHERE id IN (...)` / `WHERE v NOT BETWEEN ...`, and constant DEFAULT expressions
  (`flag BOOLEAN DEFAULT 2 BETWEEN 1 AND 3` is shown by `schema` as `flag BOOLEAN DEFAULT TRUE`).
  Transactions behave as today (snapshot isolation, write conflicts → `Error: serialization failure, retry
  transaction`).
