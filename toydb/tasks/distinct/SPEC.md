# Task: SELECT DISTINCT and DISTINCT aggregates

Add duplicate elimination to toydb's SQL dialect, in its two standard forms:

```
SELECT DISTINCT <select list> [FROM ...] [WHERE ...] [GROUP BY ...] [HAVING ...] [ORDER BY ...] [LIMIT ...] [OFFSET ...]
<aggregate>(DISTINCT <expression>)          -- <aggregate> is COUNT, SUM, AVG, MIN or MAX
```

`SELECT DISTINCT` removes duplicate rows from the result of a query. `COUNT(DISTINCT x)` and the other DISTINCT
aggregates compute the aggregate over the distinct values of `x` only.

This task is purely additive. Everything not described here keeps its current behaviour, and all existing golden
scripts must keep producing byte-identical output. Statements that don't use the word DISTINCT behave exactly as
today (except that `distinct` can no longer be an unquoted identifier, see "Reserved word").

A small example script is provided as the visible test. All examples below use the golden-script conventions:
values are displayed as today (`TRUE`, `FALSE`, `NULL`, `1`, `1.5`, `NaN`, `inf`, `'text'`), errors as
`Error: <message>`, and `[header]` shows the result column names before the rows.

## Duplicates

Both forms use the same notion of "duplicate": two values are duplicates exactly when GROUP BY puts them into the
same group today. In particular:

* `NULL` is a duplicate of `NULL` (although `NULL = NULL` is `NULL`), and `NaN` is a duplicate of `NaN`
  (although `NAN = NAN` is `FALSE`).
* Strings are duplicates only if they are exactly equal: `'a'` and `'A'` are different values, and so are `''` and
  `NULL`.
* `TRUE` is a duplicate of `TRUE`, `FALSE` of `FALSE`; numbers of equal numbers.

Two rows are duplicates when each value of one is a duplicate of the value in the same column of the other.

(Whether `0.0` and `-0.0` computed by an expression are duplicates is not specified; values read from a table are
never `-0.0`.)

## SELECT DISTINCT

### Syntax

* `DISTINCT` comes directly after `SELECT` and applies to the whole result row, i.e. to all items of the select
  list together. Parentheses don't change that: `SELECT DISTINCT (g), n FROM t` removes rows whose `g` **and** `n`
  are both duplicates.
* Everything that can follow `SELECT` today can follow `SELECT DISTINCT`: expressions with or without aliases,
  `*`, constants, aggregate functions; with or without FROM, WHERE, GROUP BY, HAVING, ORDER BY, LIMIT and OFFSET;
  joins of any kind. Keywords are case-insensitive: `select distinct g from t`.

### Meaning

A SELECT DISTINCT query is evaluated like the same query without DISTINCT, with one extra step. The steps are, in
this order:

1. FROM (including joins), WHERE;
2. GROUP BY and aggregate functions;
3. HAVING;
4. the select list is computed for each remaining row;
5. **duplicate rows are removed**: of each set of duplicate result rows only the first is kept, all later ones
   are dropped;
6. ORDER BY;
7. OFFSET and LIMIT.

Consequences:

* **Which rows, in which order.** Without ORDER BY, the result is exactly the result of the query without
  DISTINCT, with every row that is a duplicate of an earlier row removed. The rows that remain keep their order.
  For a table `t` with rows `(1, 'x')`, `(2, 'X')`, `(3, 'x')`, `(4, NULL)`, `(5, 'y')`, `(6, NULL)` (columns
  `id`, `g`), `SELECT DISTINCT g FROM t` returns `'x'`, `'X'`, `NULL`, `'y'`, in this order.
* **ORDER BY** then sorts these rows as today. Rows with equal sort keys keep their relative order, as today
  (e.g. `ORDER BY 1` keeps the order described in the previous bullet).
* **LIMIT and OFFSET** count rows after duplicates have been removed: `SELECT DISTINCT g FROM t LIMIT 2` returns
  two different values.
* **WHERE** is applied before duplicates are removed: `SELECT DISTINCT g FROM t WHERE id > 2` returns `'x'`,
  `NULL`, `'y'`.
* **GROUP BY and aggregates**: duplicates are removed from the aggregated result rows, not from the input rows:
  `SELECT DISTINCT COUNT(*) FROM t GROUP BY g` returns each different group size once (the groups come in the order
  in which GROUP BY returns them today). An aggregate query without GROUP BY returns one row, as today.
* **HAVING** removes groups before duplicates are removed, and only the select list decides what a duplicate is. A
  value that HAVING uses but that is not selected plays no part: for groups `a` (`COUNT(*)` 2, `MAX(v)` 5) and
  `b` (`COUNT(*)` 2, `MAX(v)` 9), `SELECT DISTINCT COUNT(*) FROM s GROUP BY g HAVING MAX(v) > 1` returns the single
  row `2`, and with `HAVING MAX(v) > 6` it also returns `2` (group `b`'s row; group `a` was removed by HAVING).
* **Joins**: the result rows of joins of any kind are handled like any other rows; the NULLs that outer joins
  produce for missing rows are duplicates of each other.
* **SELECT DISTINCT \*** works and considers all columns.
* **Without FROM**, the single result row is returned: `SELECT DISTINCT 1, 'a'` returns `1, 'a'`.
* **Result columns** have the same names as without DISTINCT (`[header]` output is unchanged), also when there are
  no rows.
* Errors that the query without DISTINCT would report are reported unchanged.

### ORDER BY in a SELECT DISTINCT query

After duplicates have been removed, a result row stands for possibly several original rows, so ORDER BY can only
use what the result row contains. In a SELECT DISTINCT query, **every ORDER BY expression must appear in the select
list**, in the following sense. An ORDER BY expression is allowed if it is

1. **identical to an expression of the select list** (its alias, if any, does not matter). "Identical" means
   written the same way, ignoring keyword and function-name case, whitespace and redundant parentheses: `COUNT(*)`
   and `count( * )` are identical, `a + 1` and `(a + 1)` are identical, but `a + 1` and `1 + a` are not, and
   neither are `a` and `t.a`;
2. or **the name of a result column**: an alias of the select list, or, for a select item that is a plain column
   reference without alias, that column's name (both unqualified and qualified with its table name or table alias,
   as usual). With `SELECT DISTINCT *`, all columns of the FROM tables are result columns;
3. or a **constant** (a literal);
4. or an **operator or a non-aggregate function** (such as `sqrt`) whose operands/arguments are each allowed by
   these four rules.

Anything else is not allowed, in particular a reference to a column that is not a result column, and an aggregate
function or GROUP BY expression that is not itself identical to an expression of the select list. The statement
then fails with

```
Error: invalid input: for SELECT DISTINCT, ORDER BY expressions must appear in select list
```

This error is reported only if the statement has no other error: an unknown column or table, an ungrouped column
in an aggregate query, etc. are reported exactly as they are without DISTINCT.

An allowed ORDER BY expression is evaluated exactly as it is without DISTINCT (names are resolved as today; for
example, in a query without GROUP BY and aggregates, an alias of the select list refers to that result column,
also inside a larger expression and also when a column of a FROM table has the same name), except for each part of
it that only rule 1 allows, i.e. that rules 2–4 applied to that part don't allow (because it uses a column that is
not a result column, or an aggregate function): such a part stands for the value of the identical select-list
expression. So:

* `SELECT DISTINCT -qty AS qty FROM s ORDER BY -qty`: `qty` is a result column (rule 2), so `-qty` is allowed by
  rule 4 and means `-` of that result column, i.e. the rows are sorted by the original `qty` values, exactly as
  `SELECT -qty AS qty FROM s ORDER BY -qty` sorts them today.
* `SELECT DISTINCT -qty AS x FROM s ORDER BY -qty`: `qty` is not a result column, so `-qty` is allowed only by
  rule 1 and stands for the result column `x`.

Examples, for `s (id INT PRIMARY KEY, cat STRING, sub STRING, qty INT)`:

| Statement | Result |
|---|---|
| `SELECT DISTINCT cat FROM s ORDER BY cat DESC` | allowed |
| `SELECT DISTINCT cat FROM s ORDER BY s.cat` | allowed (rule 2) |
| `SELECT DISTINCT cat AS c FROM s ORDER BY cat` | allowed (rule 1) |
| `SELECT DISTINCT * FROM s AS x ORDER BY x.cat` | allowed (rule 2) |
| `SELECT DISTINCT qty AS q FROM s ORDER BY q * -1` | allowed (rules 2, 3, 4) |
| `SELECT DISTINCT qty % 2 FROM s ORDER BY qty % 2 DESC` | allowed (rule 1) |
| `SELECT DISTINCT qty + 1 AS x FROM s ORDER BY (qty + 1) * 2` | allowed (rules 1, 3, 4) |
| `SELECT DISTINCT * FROM s ORDER BY sub, id DESC` | allowed (rule 2) |
| `SELECT DISTINCT cat, COUNT(*) AS n FROM s GROUP BY cat ORDER BY COUNT(*), n, cat` | allowed (rules 1, 2) |
| `SELECT DISTINCT cat, SUM(qty) * 2 FROM s GROUP BY cat ORDER BY SUM(qty) * 2` | allowed (rule 1) |
| `SELECT DISTINCT cat FROM s ORDER BY qty` | `Error: invalid input: for SELECT DISTINCT, ORDER BY expressions must appear in select list` |
| `SELECT DISTINCT cat FROM s ORDER BY cat, qty + 1` | same error (`qty` is not a result column) |
| `SELECT DISTINCT qty + 1 FROM s ORDER BY qty` | same error |
| `SELECT DISTINCT cat AS c FROM s ORDER BY sub` | same error |
| `SELECT DISTINCT cat AS c FROM s ORDER BY s.cat` | same error (`s.cat` is not identical to `cat`, and `cat` has an alias) |
| `SELECT DISTINCT cat FROM s GROUP BY cat ORDER BY COUNT(*)` | same error |
| `SELECT DISTINCT COUNT(*) FROM s GROUP BY cat ORDER BY cat` | same error |
| `SELECT DISTINCT cat FROM s GROUP BY cat HAVING COUNT(*) > 1 ORDER BY COUNT(*)` | same error (HAVING doesn't make `COUNT(*)` part of the select list) |
| `SELECT DISTINCT cat, SUM(qty) * 2 FROM s GROUP BY cat ORDER BY SUM(qty)` | same error |
| `SELECT DISTINCT cat FROM s ORDER BY nosuch` | `Error: invalid input: unknown column nosuch` (as without DISTINCT) |
| `SELECT DISTINCT cat FROM s ORDER BY x.cat` | `Error: invalid input: unknown table x` |
| `SELECT DISTINCT cat FROM s GROUP BY cat ORDER BY qty` | `Error: invalid input: column qty must be used in an aggregate or GROUP BY expression` |

Without DISTINCT nothing changes: `SELECT cat FROM s ORDER BY qty` works as today.

HAVING, WHERE, GROUP BY, LIMIT and OFFSET have no such restriction.

## DISTINCT aggregates

### Syntax

* In a call of one of the five aggregate functions `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` (function names are
  case-insensitive, as today), `DISTINCT` may come directly after the opening parenthesis, followed by the one
  argument expression: `COUNT(DISTINCT x)`, `sum(distinct a * 2)`, `COUNT( DISTINCT (x) )`.
* An expression must follow `DISTINCT`: `COUNT(DISTINCT)` →
  `Error: invalid input: expected expression atom, found )`.
* Aggregate functions take exactly one argument, also with DISTINCT: `COUNT(DISTINCT g, id)` →
  `Error: invalid input: count takes 1 argument`.
* `COUNT(DISTINCT *)` is not allowed. It fails like any other use of `*` as an aggregate argument other than
  `COUNT(*)` does today: `Error: invalid input: unsupported use of *`.
* `DISTINCT` in the call of any other function, known or unknown, with any number of arguments, fails with
  `Error: invalid input: DISTINCT specified, but <name> is not an aggregate function`, where `<name>` is the
  function name as it is shown in other errors today (lower case): `SELECT SQRT(DISTINCT id) FROM t` →
  `Error: invalid input: DISTINCT specified, but sqrt is not an aggregate function`; `SELECT foo(DISTINCT 1, 2)` →
  `Error: invalid input: DISTINCT specified, but foo is not an aggregate function`. This error is reported at the
  point where today an unknown function is reported, so errors reported before that point today still come first:
  `SELECT foo(DISTINCT 1) FROM nosuch` → `Error: invalid input: table nosuch does not exist` (as for
  `SELECT foo(1) FROM nosuch`).

### Meaning

`<aggregate>(DISTINCT x)` is computed, for each group, over the set of distinct non-`NULL` values of `x` in that
group (duplicates as defined above), each value taken once. Otherwise it behaves exactly like `<aggregate>(x)`:

* `COUNT(DISTINCT x)` is the number of distinct non-NULL values (`0` if there are none).
* `SUM(DISTINCT x)` and `AVG(DISTINCT x)` are `SUM` and `AVG` over the distinct values, with today's rules for
  types and results: e.g. the average of integers is computed like `AVG` today (for integers, integer division:
  `AVG(DISTINCT n)` over the values 4, 4, 2, 7, 2 is `(4 + 2 + 7) / 3` = `4`), floats give floats. Since every
  value is added only once, `SUM(DISTINCT 9223372036854775807)` over any number of rows is `9223372036854775807`,
  while `SUM(9223372036854775807)` over two or more rows fails with `Error: invalid input: integer overflow` as
  today.
* `MIN(DISTINCT x)` and `MAX(DISTINCT x)` give the same result as `MIN(x)` and `MAX(x)`.
* With no values (no rows, or only NULLs): `COUNT(DISTINCT x)` is `0`, the others are `NULL`, as today.
* Each group has its own set of values: `SELECT g, COUNT(DISTINCT v) FROM t GROUP BY g` counts, for every group,
  the different values of that group, regardless of which values occur in other groups.
* Errors are the same as without DISTINCT, e.g. `SUM(DISTINCT s)` over the string values `'b'`, `'b'` fails with
  `Error: invalid input: can't add 0 and 'b'`, `SELECT SUM(DISTINCT TRUE)` with
  `Error: invalid input: can't add 0 and TRUE`, and `COUNT(DISTINCT 10 / (n - 4))` with
  `Error: invalid input: can't divide by zero` when some row has `n = 4`.
* `<aggregate>(DISTINCT x)` and `<aggregate>(x)` are **different** aggregates, also in the same query and when
  written with the same argument: `SELECT COUNT(i), COUNT(DISTINCT i), COUNT(*) FROM c` returns three possibly
  different numbers, and `HAVING SUM(qty) != SUM(DISTINCT qty)` compares two different sums. The same DISTINCT
  aggregate written twice (in the same way, ignoring case and whitespace) is the same aggregate, as today.

### Where DISTINCT aggregates can be used

Exactly where aggregate functions can be used today, and in the same ways: in the select list, HAVING and ORDER
BY, alone or inside expressions (`COUNT(DISTINCT i) * 10 + COUNT(DISTINCT b)`), with an arbitrary argument
expression (`COUNT(DISTINCT i % 2)`), with or without GROUP BY (an expression GROUP BY too), with joins, WHERE,
SELECT DISTINCT, and in queries without FROM (`SELECT COUNT(DISTINCT 1)` is `1`). HAVING and ORDER BY can use
DISTINCT aggregates that are not in the select list. A result column computed by a DISTINCT aggregate has no name
(like any other aggregate), unless it is given an alias.

The existing rules and errors apply unchanged:

| Situation | Error |
|---|---|
| aggregate where aggregates aren't allowed: `SELECT id FROM t WHERE COUNT(DISTINCT g) > 1`, `SELECT COUNT(*) FROM t GROUP BY MAX(DISTINCT id)` | `Error: invalid input: unknown function count with 1 arguments` (resp. `max`) — the same error as without DISTINCT |
| nested aggregates: `SELECT MAX(DISTINCT COUNT(n)) FROM v`, `SELECT SUM(COUNT(DISTINCT n)) FROM v` | `Error: invalid input: aggregate functions can't be nested` |
| column that is neither aggregated nor grouped: `SELECT cust, COUNT(DISTINCT item) FROM o` | `Error: invalid input: column cust must be used in an aggregate or GROUP BY expression` |

## Reserved word

`DISTINCT` becomes a reserved keyword, with the same rules as the existing ones:

* It is case-insensitive.
* It can no longer be used as an unquoted identifier: `CREATE TABLE distinct (id INT PRIMARY KEY)` and
  `SELECT g AS distinct FROM t` → `Error: invalid input: expected identifier, got DISTINCT`.
* Double-quoted it remains a valid identifier (`CREATE TABLE "distinct" (id INT PRIMARY KEY, "distinct" STRING)`,
  `SELECT DISTINCT "distinct" FROM "distinct"`), and the `schema` output shows such names double-quoted, as it does
  for every other keyword:

  ```
  CREATE TABLE "distinct" (
    id INTEGER PRIMARY KEY,
    "distinct" STRING DEFAULT NULL
  )
  ```

If the code base has a test that pins the exact number or set of reserved keywords, update it for the new
keyword; that is the only existing test expectation that may change.

## Syntax errors

The first token that does not fit is reported, with tokens displayed as the existing parser errors display them
(keywords in upper case, symbols as written). Where an expression is required but none starts, the existing error
`expected expression atom, found <token>` is reported, and if the input ends too early, the existing
`unexpected end of input`.

| Input | Error |
|---|---|
| `SELECT DISTINCT` | `Error: invalid input: unexpected end of input` |
| `SELECT DISTINCT FROM t` | `Error: invalid input: expected expression atom, found FROM` |
| `SELECT DISTINCT DISTINCT id FROM t` | `Error: invalid input: expected expression atom, found DISTINCT` |
| `SELECT id, DISTINCT g FROM t` (DISTINCT only directly after SELECT) | `Error: invalid input: expected expression atom, found DISTINCT` |
| `SELECT COUNT(DISTINCT) FROM t` | `Error: invalid input: expected expression atom, found )` |
| `SELECT COUNT(DISTINCT DISTINCT g) FROM t` | `Error: invalid input: expected expression atom, found DISTINCT` |
| `SELECT COUNT(g DISTINCT) FROM t` (DISTINCT only directly after the parenthesis) | `Error: invalid input: expected token ,, found DISTINCT` |

A statement that fails to parse has no effect.

## Query plans

How DISTINCT is shown in query plans (`EXPLAIN`) is not specified. The plans of statements without DISTINCT don't
change.
