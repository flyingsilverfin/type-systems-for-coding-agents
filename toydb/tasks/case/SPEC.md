# Task: CASE expressions

Add the SQL `CASE` expression to toydb's expression language, in its two standard forms:

```
CASE WHEN condition THEN result [WHEN condition THEN result ...] [ELSE result] END            -- searched form
CASE operand WHEN value THEN result [WHEN value THEN result ...] [ELSE result] END            -- simple form
```

A CASE expression is an ordinary expression and must work everywhere an expression is allowed today: the SELECT
list, WHERE, JOIN ... ON, GROUP BY, HAVING, ORDER BY, LIMIT, OFFSET, INSERT ... VALUES, UPDATE ... SET, DEFAULT in
CREATE TABLE, inside the arguments of functions and of aggregate functions, as an operand of any operator, and
inside another CASE expression. Its own parts (operand, conditions, values, results) are arbitrary expressions:
constants, columns (qualified or not, from any joined table), operators, function calls, aggregate functions
(where aggregates are allowed today), and nested CASE expressions.

This task is purely additive. Everything not described here keeps its current behaviour, and all existing golden
scripts must keep producing byte-identical output.

A small example script is provided as the visible test. All examples below use the golden-script conventions:
values are displayed as today (`TRUE`, `FALSE`, `NULL`, `1`, `1.5`, `'text'`), errors as `Error: <message>`, and
`[plan]` shows the optimized plan of a statement before its result.

## Syntax

* `CASE`, `WHEN`, `THEN`, `ELSE` and `END` are keywords (case-insensitive, like all keywords).
* There is at least one `WHEN ... THEN ...` branch, and at most one `ELSE`, which comes last.
* If the token after `CASE` is `WHEN`, the expression is in the searched form. Otherwise it is in the simple form
  and an operand expression follows `CASE`.
* Every part (operand, condition, value, result, ELSE result) is a full expression: it may use any operator,
  including `AND` and `OR`, without parentheses, and extends up to the next `WHEN`, `THEN`, `ELSE` or `END` that
  belongs to the CASE. Parentheses inside the parts work as usual.
* The whole `CASE ... END` is an expression atom, like a parenthesized expression or a function call. Operators
  apply to it as to any other atom: `CASE WHEN TRUE THEN 1 END + 1` is `2`, `-CASE WHEN TRUE THEN 1 END` is `-1`,
  `CASE WHEN TRUE THEN 3 END!` is `6`, `CASE WHEN FALSE THEN 1 END IS NULL` is `TRUE`, `10 * CASE WHEN FALSE THEN 1
  ELSE 2 END` is `20`.

## Meaning

### Searched form

The conditions are evaluated in order, one at a time:

* If a condition evaluates to `TRUE`, the result of that branch is evaluated and is the value of the CASE. Nothing
  further is evaluated.
* If a condition evaluates to `FALSE` or `NULL`, the next branch is tried.
* If a condition evaluates to anything else, the evaluation fails with
  `Error: invalid input: CASE condition returned <value>, expected boolean`, where `<value>` is displayed like a
  result value: `SELECT CASE WHEN 1 THEN 'a' END` → `Error: invalid input: CASE condition returned 1, expected
  boolean`; with `'true'` → `... returned 'true', expected boolean`; with `0.0` → `... returned 0.0, expected
  boolean`; with `1 + 1` → `... returned 2, expected boolean`.
* If no branch is chosen, the value is the ELSE result, or `NULL` if there is no ELSE.

### Simple form

`CASE x WHEN v1 THEN r1 WHEN v2 THEN r2 ... [ELSE e] END` has exactly the meaning (results, NULL handling and
errors) of the searched form `CASE WHEN x = v1 THEN r1 WHEN x = v2 THEN r2 ... [ELSE e] END`, with `=` being the
existing equality operator and the operand on its left-hand side. Consequences worth spelling out:

* The first branch whose value equals the operand is chosen: `CASE 1 WHEN 1 THEN 'a' WHEN 1 THEN 'b' END` is `'a'`.
* Integers and floats can be mixed (`CASE 1 WHEN 1.0 THEN 'eq' END` is `'eq'`, `CASE 0.0 WHEN -0.0 THEN 'zero' END`
  is `'zero'`), strings compare exactly and case-sensitively, booleans compare with booleans
  (`CASE 2 > 1 WHEN TRUE THEN 'yes' ELSE 'no' END` is `'yes'`, `CASE FALSE WHEN FALSE THEN 'f' END` is `'f'`).
* `NULL` never matches, on either side: `CASE NULL WHEN NULL THEN 'null' ELSE 'other' END` is `'other'`,
  `CASE 1 WHEN NULL THEN 'a' WHEN 1 THEN 'b' END` is `'b'`. `NAN` never matches either:
  `CASE NAN WHEN NAN THEN 'nan' ELSE 'other' END` is `'other'`.
* Comparing incompatible types fails as `=` does today, with the operand first in the message:
  `SELECT CASE 1 WHEN 'a' THEN 1 END` → `Error: invalid input: can't compare 1 and 'a'`;
  `SELECT CASE 'a' WHEN 1 THEN 1 END` → `Error: invalid input: can't compare 'a' and 1`;
  `SELECT CASE TRUE WHEN 1 THEN 'x' ELSE 'y' END` → `Error: invalid input: can't compare TRUE and 1`.
  As with `=`, a `NULL` operand is never a type error: `CASE NULL WHEN 'a' THEN 1 WHEN 2 THEN 2 ELSE 4 END` is `4`.
* The operand is evaluated first, so an error in it is always reported:
  `SELECT CASE 1 / 0 WHEN 1 THEN 1 END` → `Error: invalid input: can't divide by zero`.
* The values are evaluated and compared in order until one matches, also when the operand is `NULL`:
  `SELECT CASE NULL WHEN 1 / 0 THEN 'a' ELSE 'b' END` → `Error: invalid input: can't divide by zero`.

### Lazy evaluation

Only the parts that the rules above call for are evaluated; a part that is not evaluated can never cause an error,
whatever it contains. This holds for constant parts as well as for parts that reference columns, and in every
place a CASE can be used.

* Results of branches that are not chosen, and the ELSE result when a branch is chosen, are not evaluated:
  `CASE WHEN TRUE THEN 1 ELSE 1 / 0 END` is `1`; `CASE WHEN FALSE THEN 1 / 0 ELSE 2 END` is `2`;
  `CASE WHEN FALSE THEN 1 / 0 END` is `NULL`; `CASE 2 WHEN 1 THEN 1 / 0 WHEN 2 THEN 'b' END` is `'b'`.
* Conditions (or values) after the chosen branch are not evaluated:
  `CASE WHEN TRUE THEN 1 WHEN 1 / 0 = 1 THEN 2 END` is `1`; `CASE WHEN TRUE THEN 'ok' WHEN 1 THEN 'bad' END` is `'ok'`
  (the non-boolean condition is never evaluated); `CASE 1 WHEN 1 THEN 'a' WHEN 1 / 0 THEN 'b' END` is `'a'`;
  `CASE 1 WHEN 1 THEN 'x' WHEN 'two' THEN 'y' END` is `'x'` (but `CASE 2 WHEN 1 THEN 'x' WHEN 'two' THEN 'y' END` →
  `Error: invalid input: can't compare 2 and 'two'`).
* Parts that are evaluated fail as usual: `SELECT CASE WHEN TRUE THEN 1 / 0 ELSE 1 END`,
  `SELECT CASE WHEN FALSE THEN 1 ELSE 1 / 0 END` and `SELECT CASE WHEN 1 / 0 = 1 THEN 1 ELSE 2 END` →
  `Error: invalid input: can't divide by zero`.
* With rows, each row is evaluated on its own. `SELECT id, CASE WHEN d = 0 THEN NULL ELSE n / d END FROM r` never
  divides by zero. `SELECT CASE WHEN id > 0 THEN 'pos' ELSE 1 / 0 END FROM r` succeeds if every row has `id > 0`
  (and also if there are no rows), although `1 / 0` is a constant sub-expression; it fails with
  `Error: invalid input: can't divide by zero` only if some row actually reaches the ELSE.
* Outside of CASE nothing changes. In particular a failing constant expression that is not inside a CASE is still
  an error even if no row is processed, as today: `SELECT 1 / 0 FROM r WHERE id > 100` and
  `SELECT id FROM r WHERE id > 100 AND 1 / 0 = 1` → `Error: invalid input: can't divide by zero`.
* Aggregate functions are not affected by laziness: as today, every aggregate function that appears anywhere in
  the SELECT list, HAVING or ORDER BY (including inside any part of a CASE) is computed for every group before
  those expressions are evaluated. The CASE is then evaluated lazily over the aggregate results, so
  `CASE WHEN COUNT(v) = 0 THEN 0 ELSE SUM(id) / COUNT(v) END` never divides by zero.

### Result values

* There is no type checking or conversion between branches: results can be of any type and can differ from branch
  to branch (`CASE WHEN v IS NULL THEN 'none' ELSE v END` gives a string for some rows and an integer for others).
  The value of the CASE is exactly the value of the chosen result (`NULL`, `NAN`, `-INFINITY`, ... included).
* A result column computed by a CASE has no name in the result header (like any other computed expression) unless
  it is given an alias with `AS`: `[header]> SELECT CASE WHEN TRUE THEN 1 END, CASE WHEN TRUE THEN 2 END AS two`
  prints the header line `, two`.

## Interaction with existing features (must all work)

* **WHERE, JOIN ... ON, HAVING**: a CASE can be the whole predicate or a part of it. As today, a row is selected
  only if the predicate is `TRUE` (`FALSE` and `NULL` do not select it), and a predicate that evaluates to another
  type fails as today, e.g. `SELECT id FROM item WHERE CASE WHEN price > 10 THEN 1 ELSE 0 END` →
  `Error: invalid input: filter returned 0, expected boolean` (for the first row, whose price is 5) and
  `... WHERE CASE WHEN TRUE THEN 'yes' END` → `Error: invalid input: filter returned 'yes', expected boolean`.
  Results must be the same whether or not the columns involved are primary keys or have indexes
  (`WHERE id = CASE kind WHEN 'veg' THEN 3 ELSE 1 END`, `WHERE CASE WHEN id = 1 THEN TRUE WHEN id = 3 THEN TRUE END`).
* **SELECT list, ORDER BY, LIMIT, OFFSET**: including ORDER BY on a CASE over columns that are not in the SELECT
  list (`SELECT id FROM t ORDER BY CASE g WHEN 'b' THEN 0 WHEN 'a' THEN 1 ELSE 2 END, id DESC`), ORDER BY on an alias
  of a CASE and on a CASE over an alias, `SELECT *, CASE ... END FROM t`, SELECT without FROM, and constant CASE
  expressions as LIMIT and OFFSET (`LIMIT CASE WHEN TRUE THEN 2 ELSE 1 / 0 END`).
* **Joins**: columns of every joined table, qualified or not, in any part of a CASE, in the SELECT list, in ON
  conditions of inner and outer joins and in WHERE; NULLs produced by outer joins are ordinary NULLs
  (`CASE WHEN d.id IS NULL THEN 'none' ELSE d.name END`).
* **Aggregates**: CASE inside aggregate function arguments (`SUM(CASE WHEN v > 10 THEN 1 ELSE 0 END)`,
  `COUNT(CASE WHEN v > 10 THEN 1 END)` counts the rows where the CASE is not NULL), aggregate functions in any part
  of a CASE (`CASE WHEN COUNT(*) > 1 THEN 'many' ELSE 'one' END`, `CASE COUNT(v) WHEN 4 THEN 'four' ELSE 'other' END`,
  `CASE 6 WHEN COUNT(v) THEN MIN(v) WHEN COUNT(*) THEN MAX(v) END`), both at once, and together with GROUP BY columns
  (`CASE g WHEN 'a' THEN SUM(v) WHEN 'b' THEN COUNT(*) END ... GROUP BY g`).
* **GROUP BY** a CASE expression. As for every GROUP BY expression today, the SELECT list, HAVING and ORDER BY can
  use the same expression again (written the same way; keyword case and whitespace do not matter), also as part of
  a larger expression:
  `SELECT CASE WHEN v < 15 THEN 'low' ELSE 'high' END, COUNT(*) FROM s GROUP BY CASE WHEN v < 15 THEN 'low' ELSE 'high' END`.
* **HAVING and ORDER BY of aggregate queries** with a CASE over aggregates and GROUP BY columns, also ones that
  are not in the SELECT list (`SELECT g FROM s GROUP BY g ORDER BY CASE WHEN SUM(v) IS NULL THEN 0 ELSE 1 END, MAX(id) DESC`).
* **Writes**: `INSERT ... VALUES (CASE ... END, ...)`, `UPDATE ... SET bal = CASE WHEN bal = 0 THEN 0 ELSE 2000 / bal END`
  (evaluated lazily for each row), `UPDATE`/`DELETE ... WHERE CASE ... END`, and constant CASE expressions as
  column DEFAULT (`kind STRING DEFAULT CASE WHEN 1 > 2 THEN 'vip' ELSE 'std' END` is shown by `schema` as
  `kind STRING DEFAULT 'std'`). Type checks on stored values and transactions behave as today.

Existing errors surface unchanged when their cause is inside a CASE (the hidden tests use these):

| Situation | Error |
|---|---|
| unknown column: `SELECT CASE WHEN x > 1 THEN 1 END FROM t`, `SELECT CASE x WHEN 1 THEN 1 END FROM t` | `Error: invalid input: unknown column x` |
| unknown qualified column, also in a part that would never be evaluated: `SELECT CASE WHEN TRUE THEN 1 WHEN id = 1 THEN t.x END FROM t` | `Error: invalid input: unknown column t.x` |
| unknown table: `... ORDER BY CASE WHEN TRUE THEN 1 ELSE u.v END` | `Error: invalid input: unknown table u` |
| ambiguous column in a join: `SELECT CASE WHEN id > 1 THEN 1 END FROM emp e JOIN dept d ON e.dept_id = d.id` | `Error: invalid input: ambiguous column id` |
| column where only a constant expression is allowed: `SELECT CASE WHEN TRUE THEN 1 ELSE v END` (no FROM), `x INT DEFAULT CASE WHEN id > 1 THEN 1 END` | `Error: invalid input: expression must be constant, found column v` (resp. `column id`) |
| aggregate function nested in an aggregate function: `SELECT SUM(CASE WHEN COUNT(*) > 1 THEN 1 END) FROM s` | `Error: invalid input: aggregate functions can't be nested` |
| column that is neither aggregated nor grouped, in any part of a CASE: `SELECT CASE WHEN v > 1 THEN COUNT(*) END FROM s`, `SELECT g, CASE id WHEN 1 THEN COUNT(*) END FROM s GROUP BY g`, or a CASE that differs from the GROUP BY expression: `SELECT CASE WHEN v < 15 THEN 'low' END, COUNT(*) FROM s GROUP BY CASE WHEN v < 15 THEN 'low' ELSE 'high' END` | `Error: invalid input: column v must be used in an aggregate or GROUP BY expression` (resp. `column id`) |
| HAVING without aggregation: `SELECT id FROM s HAVING CASE WHEN id > 1 THEN TRUE END` | `Error: invalid input: HAVING requires GROUP BY or aggregate function` |
| wrong type stored in a column: `UPDATE acct SET bal = CASE id WHEN 3 THEN 'three' ELSE 7 END` (`bal` is an INT column) | `Error: invalid input: invalid datatype STRING for INTEGER column bal` |

A statement that fails changes nothing, as today.

## Reserved words

`CASE`, `WHEN`, `THEN`, `ELSE` and `END` become reserved keywords, with the same rules as the existing ones:

* They are case-insensitive: `select case when true then 1 else 2 end`, `CaSe WhEn FALSE tHeN 1 eLsE 2 EnD`.
* They can no longer be used as unquoted identifiers: `CREATE TABLE x (id INT PRIMARY KEY, end INT)` →
  `Error: invalid input: expected identifier, got END`; `CREATE TABLE case (id INT PRIMARY KEY)` →
  `Error: invalid input: expected identifier, got CASE`; `SELECT "case".else FROM "case"` →
  `Error: invalid input: expected identifier, got ELSE`.
* Double-quoted they remain valid identifiers
  (`CREATE TABLE "case" (id INT PRIMARY KEY, "when" INT, "then" STRING, "else" STRING, "end" INT)`,
  `SELECT CASE "when" WHEN 1 THEN "then" ELSE "else" END FROM "case"`), and the `schema` output shows such names
  double-quoted, as it does for every other keyword:

  ```
  CREATE TABLE "case" (
    id INTEGER PRIMARY KEY,
    "when" INTEGER DEFAULT NULL,
    "then" STRING DEFAULT NULL,
    "else" STRING DEFAULT NULL,
    "end" INTEGER DEFAULT NULL
  )
  ```

If the code base has a test that pins the exact number or set of reserved keywords, update it for these five new
keywords; that is the only existing test expectation that may change.

## Syntax errors

A CASE expression is read from left to right as follows, and the first token that does not fit is reported.
Tokens are displayed in messages as the existing parser errors display them (keywords in upper case, symbols and
numbers as written).

1. After `CASE`: if the next token is not `WHEN`, an operand expression is read.
2. `WHEN` is required: otherwise `expected token WHEN, found <token>`.
3. A condition (or value) expression is read, then `THEN` is required (`expected token THEN, found <token>`), then
   a result expression is read. If the next token is `WHEN`, this step is repeated.
4. If the next token is `ELSE`, the ELSE result expression is read.
5. `END` is required: otherwise `expected token END, found <token>`.

Where an expression is to be read but none starts, the existing error
`expected expression atom, found <token>` is reported, and if the input ends too early, the existing
`unexpected end of input`.

| Input | Error |
|---|---|
| `SELECT CASE END` | `Error: invalid input: expected expression atom, found END` |
| `SELECT CASE 1 END` | `Error: invalid input: expected token WHEN, found END` |
| `SELECT CASE 1 ELSE 2 END` | `Error: invalid input: expected token WHEN, found ELSE` |
| `SELECT CASE WHEN TRUE END` | `Error: invalid input: expected token THEN, found END` |
| `SELECT CASE WHEN THEN 1 END` | `Error: invalid input: expected expression atom, found THEN` |
| `SELECT CASE WHEN TRUE THEN 1 2 END` | `Error: invalid input: expected token END, found 2` |
| `SELECT CASE WHEN TRUE THEN 1 ELSE 2 WHEN FALSE THEN 3 END` | `Error: invalid input: expected token END, found WHEN` |
| `SELECT CASE WHEN TRUE THEN 1 ELSE 2, 3 END` | `Error: invalid input: expected token END, found ,` |
| `SELECT (CASE WHEN TRUE THEN 1) END` | `Error: invalid input: expected token END, found )` |
| `SELECT CASE 1 WHEN 1 THEN 2 ELSE 3 FROM t` | `Error: invalid input: expected token END, found FROM` |
| `SELECT CASE WHEN TRUE THEN 1` | `Error: invalid input: unexpected end of input` |
| a keyword of CASE where an expression must start: `SELECT WHEN TRUE THEN 1 END` | `Error: invalid input: expected expression atom, found WHEN` |
| a keyword of CASE left over after a complete statement: `SELECT 1 END` | `Error: invalid input: unexpected token END` |

A statement that fails to parse has no effect.

## Query plans

### Display

In plans (`[plan]`, `EXPLAIN`), a CASE expression is displayed in the form in which it was written:

```
CASE WHEN <condition> THEN <result> [WHEN <condition> THEN <result> ...] [ELSE <result>] END
CASE <operand> WHEN <value> THEN <result> [WHEN <value> THEN <result> ...] [ELSE <result>] END
```

* The keywords are in upper case and everything is separated by exactly one space. The simple form stays in the
  simple form, and a missing ELSE stays missing.
* Each part is displayed exactly as that expression is displayed today on its own (column names as `t.v` or with
  the table alias, constants as values, `v >= 20` as `t.v > 20 OR t.v = 20`, `g != 'a'` as `NOT t.g = 'a'`, inner
  parentheses only where precedence requires them, aggregate results as `#1` etc.). A part is never wrapped in
  parentheses as a whole, whatever operators it uses.
* The CASE expression itself is never wrapped in parentheses, whatever it is an operand of.

Examples, for a table `t (id INT PRIMARY KEY, g STRING, v INT)`:

```
[plan]> SELECT id, CASE WHEN v > 10 THEN 'big' WHEN v > 5 THEN 'mid' ELSE 'small' END FROM t
Projection: t.id, CASE WHEN t.v > 10 THEN 'big' WHEN t.v > 5 THEN 'mid' ELSE 'small' END
└─ Scan: t

[plan]> SELECT CASE WHEN v > 10 OR g = 'a' AND id < 3 THEN v + 1 ELSE -v * 2 END, CASE v + id WHEN 11 THEN TRUE ELSE g = 'a' OR g = 'b' END FROM t
Projection: CASE WHEN t.v > 10 OR t.g = 'a' AND t.id < 3 THEN t.v + 1 ELSE -t.v * 2 END, CASE t.v + t.id WHEN 11 THEN TRUE ELSE t.g = 'a' OR t.g = 'b' END
└─ Scan: t

[plan]> SELECT CASE WHEN v >= 20 THEN (v + 1) * 2 WHEN g != 'a' THEN -(v + id) END FROM t
Projection: CASE WHEN t.v > 20 OR t.v = 20 THEN (t.v + 1) * 2 WHEN NOT t.g = 'a' THEN -(t.v + t.id) END
└─ Scan: t

[plan]> SELECT CASE WHEN v > 10 THEN 1 ELSE 2 END + 1, 2 * CASE g WHEN 'a' THEN 1 END, -CASE WHEN v > 10 THEN 1 END, NOT CASE WHEN v > 10 THEN TRUE END FROM t
Projection: CASE WHEN t.v > 10 THEN 1 ELSE 2 END + 1, 2 * CASE t.g WHEN 'a' THEN 1 END, -CASE WHEN t.v > 10 THEN 1 END, NOT CASE WHEN t.v > 10 THEN TRUE END
└─ Scan: t

[plan]> SELECT CASE WHEN v > 10 THEN 1 END IS NULL, CASE WHEN id > 3 THEN 3 END!, sqrt(CASE WHEN id > 1 THEN id * id END) FROM t
Projection: CASE WHEN t.v > 10 THEN 1 END IS NULL, CASE WHEN t.id > 3 THEN 3 END!, sqrt(CASE WHEN t.id > 1 THEN t.id * t.id END)
└─ Scan: t

[plan]> SELECT CASE WHEN x.v > 10 THEN 'big' END AS size FROM t AS x
Projection: CASE WHEN x.v > 10 THEN 'big' END as size
└─ Scan: t as x

[plan]> SELECT g, SUM(CASE WHEN v > 10 THEN 1 ELSE 0 END), CASE WHEN COUNT(*) > 1 THEN 'many' ELSE 'one' END FROM t GROUP BY g
Projection: t.g, #1, CASE WHEN #2 > 1 THEN 'many' ELSE 'one' END
└─ Aggregate: t.g, sum(CASE WHEN t.v > 10 THEN 1 ELSE 0 END), count(TRUE)
   └─ Scan: t

[plan]> UPDATE t SET v = CASE WHEN v IS NULL THEN 0 ELSE v + 1 END WHERE CASE id WHEN 4 THEN TRUE ELSE FALSE END
Update: t (v=CASE WHEN t.v IS NULL THEN 0 ELSE t.v + 1 END)
└─ Scan: t (CASE t.id WHEN 4 THEN TRUE ELSE FALSE END)
```

Nested CASE expressions follow the same rules:
`CASE WHEN CASE t.g WHEN 'a' THEN TRUE END THEN CASE WHEN t.v > 10 THEN 'x' ELSE 'y' END ELSE CASE t.id WHEN 5 THEN 'z' END END`.

### Optimizations

Today the optimizer pre-evaluates ("folds") constant expressions in the SELECT list, WHERE, JOIN ... ON, HAVING,
ORDER BY and VALUES, and the plan shows the folded value. For CASE expressions the rules are:

1. **A CASE expression without any column reference is a constant expression and is folded into its value**,
   computed with the lazy rules above (so `CASE WHEN TRUE THEN 1 ELSE 1 / 0 END` folds to `1`). From then on it
   behaves exactly like that literal value, in folding of the surrounding expression and in all other
   optimizations:

   ```
   [plan]> SELECT CASE WHEN TRUE THEN 1 ELSE 1 / 0 END, CASE 2 WHEN 1 THEN 'one' WHEN 2 THEN 'two' END
   Projection: 1, 'two'
   └─ Values: blank row

   [plan]> SELECT id, CASE WHEN 1 + 1 = 2 THEN 'two' END, 1 + CASE 3 WHEN 3 THEN 10 END, v + CASE WHEN FALSE THEN 1 ELSE 2 END FROM t WHERE id = 1
   Projection: t.id, 'two', 11, t.v + 2
   └─ KeyLookup: t (1)

   [plan]> SELECT * FROM t WHERE id = CASE WHEN TRUE THEN 2 ELSE 3 END
   KeyLookup: t (2)

   [plan]> SELECT * FROM t WHERE g = CASE 1 WHEN 1 THEN 'b' END          -- g has an index
   IndexLookup: t.g ('b')

   [plan]> SELECT * FROM t WHERE CASE WHEN FALSE THEN TRUE END
   Nothing

   [plan]> SELECT id FROM t WHERE CASE WHEN 1 < 2 THEN TRUE END
   Projection: t.id
   └─ Scan: t

   [plan]> SELECT id FROM t WHERE v > 15 AND CASE WHEN TRUE THEN TRUE ELSE FALSE END
   Projection: t.id
   └─ Scan: t (t.v > 15)

   [plan]> INSERT INTO t VALUES (9, CASE WHEN TRUE THEN 'z' ELSE 1 / 0 END, CASE 1 WHEN 2 THEN 0 END)
   Insert: t
   └─ Values: 9, 'z', NULL
   ```

   If evaluating such a constant CASE fails, the statement fails with that error, like every failing constant
   expression today, even if no row is processed:
   `SELECT CASE WHEN TRUE THEN 1 / 0 END FROM t WHERE id > 100` → `Error: invalid input: can't divide by zero`.

2. **A CASE expression that references at least one column is left exactly as it was written; nothing inside it
   is folded or simplified**, not constant sub-expressions, not nested constant CASE expressions, not conditions
   that are always true or false. (An aggregate function result counts as a column reference.) Such a CASE never
   causes an error while the statement is planned; errors inside it occur only when the evaluation of a row
   reaches the failing part (see "Lazy evaluation"): `SELECT CASE WHEN TRUE THEN COUNT(*) ELSE 1 / 0 END FROM s`
   returns the count.

   The same holds for the other rewrites that the optimizer applies today to the predicates it pushes down or
   turns into lookups (such as rewriting `NOT (x AND y)` into `NOT x OR NOT y`): they apply around a CASE as
   today, but never inside it. `SELECT a.id, b.id FROM t a JOIN t b ON CASE WHEN NOT (a.v > 10 AND b.v > 10) THEN
   a.id = b.id END` shows `NestedLoopJoin: inner on CASE WHEN NOT (a.v > 10 AND b.v > 10) THEN a.id = b.id END`.

   ```
   [plan]> SELECT CASE WHEN id > 1 - 1 THEN 1 + 1 ELSE 1 / 0 END FROM t
   Projection: CASE WHEN t.id > 1 - 1 THEN 1 + 1 ELSE 1 / 0 END
   └─ Scan: t

   [plan]> SELECT CASE 1 + 1 WHEN id THEN 2 * 3 WHEN 2 THEN 0 END, CASE WHEN TRUE THEN id ELSE 0 END FROM t
   Projection: CASE 1 + 1 WHEN t.id THEN 2 * 3 WHEN 2 THEN 0 END, CASE WHEN TRUE THEN t.id ELSE 0 END
   └─ Scan: t

   [plan]> SELECT CASE WHEN v > 10 THEN CASE WHEN TRUE THEN 1 END WHEN TRUE AND FALSE THEN 2 END FROM t
   Projection: CASE WHEN t.v > 10 THEN CASE WHEN TRUE THEN 1 END WHEN TRUE AND FALSE THEN 2 END
   └─ Scan: t
   ```

3. **Expressions around such a CASE are folded as today**, with the CASE as an opaque operand (like a column):

   ```
   [plan]> SELECT CASE WHEN v > 10 THEN 1 ELSE 0 END + (2 + 3), 2 * 3 = 6 AND CASE WHEN v > 2 * 5 THEN TRUE END FROM t
   Projection: CASE WHEN t.v > 10 THEN 1 ELSE 0 END + 5, CASE WHEN t.v > 2 * 5 THEN TRUE END
   └─ Scan: t
   ```

   (`TRUE AND x` is simplified to `x` today.)

4. **Where nothing is folded today, nothing is folded now**: aggregate function arguments, GROUP BY expressions
   and UPDATE ... SET expressions are shown as written, also when they are constant
   (`SELECT SUM(CASE WHEN TRUE THEN 1 ELSE 0 END) FROM t` shows `Aggregate: sum(CASE WHEN TRUE THEN 1 ELSE 0 END)`).

5. **All other optimizations treat a CASE that is left in the plan like any other expression that is not a
   comparison of a column with a constant.** A predicate consisting of or containing a CASE is pushed down
   exactly where the same predicate with, say, `t.v + 1 > 10` in place of the CASE would be pushed down, and a
   CASE is never turned into a key or index lookup by itself:

   ```
   [plan]> SELECT id FROM t WHERE CASE WHEN v > 2 * 5 THEN TRUE ELSE 1 = 1 END
   Projection: t.id
   └─ Scan: t (CASE WHEN t.v > 2 * 5 THEN TRUE ELSE 1 = 1 END)

   [plan]> SELECT * FROM t WHERE id = 1 + 1 AND CASE WHEN v > 10 THEN TRUE ELSE FALSE END
   Filter: CASE WHEN t.v > 10 THEN TRUE ELSE FALSE END
   └─ KeyLookup: t (2)

   [plan]> SELECT a.id, b.id FROM t a JOIN t b ON a.id = b.id WHERE CASE WHEN b.v > 10 THEN TRUE ELSE FALSE END AND CASE a.g WHEN 'a' THEN TRUE END
   Projection: a.id, b.id
   └─ HashJoin: inner on a.id = b.id
      ├─ Scan: t as a (CASE a.g WHEN 'a' THEN TRUE END)
      └─ Scan: t as b (CASE WHEN b.v > 10 THEN TRUE ELSE FALSE END)

   [plan]> SELECT a.id, b.id FROM t a JOIN t b ON CASE WHEN a.v > b.v THEN a.g = b.g END
   Projection: a.id, b.id
   └─ NestedLoopJoin: inner on CASE WHEN a.v > b.v THEN a.g = b.g END
      ├─ Scan: t as a
      └─ Scan: t as b

   [plan]> SELECT id FROM t ORDER BY CASE g WHEN 'b' THEN 0 ELSE 1 END DESC, id
   Remap: t.id (dropped: t.g)
   └─ Order: CASE t.g WHEN 'b' THEN 0 ELSE 1 END desc, t.id asc
      └─ Projection: t.id, t.g
         └─ Scan: t
   ```

The plans of statements without CASE expressions do not change.
