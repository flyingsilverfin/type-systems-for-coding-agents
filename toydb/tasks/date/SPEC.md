# Task: a DATE data type

Add a calendar date type to toydb: a column type `DATE`, a literal `DATE 'YYYY-MM-DD'`, comparison and ordering of
dates, and day arithmetic between dates and integers. Dates must work everywhere values work today: in every
clause of a query, as primary keys, in indexes, unique and foreign-key columns, as column defaults, in joins,
grouping, aggregates, writes and transactions.

This task is purely additive. Everything not described here keeps its current behaviour, and all existing golden
scripts must keep producing byte-identical output (some of them print raw stored keys and values, so the stored
form of the existing types and values must not change).

A small example script is provided as the visible test. All examples below use the golden-script conventions:
`> statement` followed by its result rows, errors as `Error: <message>`, `schema` printing table definitions, and
`[plan]` printing the optimized plan of a statement before its result.

## Dates

* A date is a day of the proleptic Gregorian calendar (the Gregorian rules applied to all years), from
  `0001-01-01` to `9999-12-31` inclusive. There is no time of day and no time zone.
* Leap years are the years divisible by 4, except those divisible by 100 but not by 400 (2024, 2000, 1600 and
  0004 are leap years; 2023, 1900 and 2100 are not). February has 29 days in leap years and 28 otherwise.
* `DATE` is a data type of its own. There are no implicit conversions between dates and strings, integers or
  any other type, in either direction.

## The literal `DATE 'YYYY-MM-DD'`

A date literal is the keyword `DATE` followed by a string literal. It is an expression atom, like a number or a
string, and can be used wherever an expression is allowed (it can be parenthesized, be an operand of any operator,
a function argument, a `DEFAULT`, a `VALUES` element, ...). `DATE` is case-insensitive like every keyword:
`date '2024-01-15'`.

The string must be exactly ten characters of the form `YYYY-MM-DD`: four ASCII digits, `-`, two ASCII digits, `-`,
two ASCII digits, and must name an existing day in the valid range (year `0001`..`9999`, month `01`..`12`, day
`01`..the length of that month in that year). Nothing else is accepted: no missing leading zeros, no other
separators, no sign, no surrounding whitespace, no time part.

* If the string is not a valid date, the statement fails with `Error: invalid input: invalid date '<string>'`,
  where `<string>` is the content of the string literal:

  | Statement | Error |
  |---|---|
  | `SELECT DATE '2023-02-29'` | `Error: invalid input: invalid date '2023-02-29'` |
  | `SELECT DATE '2024-13-01'`, `'2024-00-10'`, `'2024-01-00'`, `'2024-01-32'`, `'2024-04-31'`, `'1900-02-29'` | `Error: invalid input: invalid date '2024-13-01'` etc. |
  | `SELECT DATE '0000-01-01'` (year 0 is out of range) | `Error: invalid input: invalid date '0000-01-01'` |
  | `SELECT DATE '2024-1-15'`, `'24-01-15'`, `'2024/01/15'`, `'20240115'`, `'+024-01-15'`, `'2024-01-15T00:00:00'` | `Error: invalid input: invalid date '2024-1-15'` etc. |
  | `SELECT DATE '2024-01-15 '` (trailing space) | `Error: invalid input: invalid date '2024-01-15 '` |
  | `SELECT DATE 'today'`, `SELECT DATE ''` | `Error: invalid input: invalid date 'today'`, `Error: invalid input: invalid date ''` |

* If the token after `DATE` (in an expression) is not a string literal, the statement fails with
  `Error: invalid input: expected date string, found <token>`, with the token displayed as the existing parser
  errors display tokens: `SELECT DATE 20240115` → `... found 20240115`; `SELECT DATE d` → `... found d`;
  `SELECT DATE ('2024-01-15')` → `... found (`; `SELECT DATE NULL` → `... found NULL`; `SELECT DATE FROM t` →
  `... found FROM`. If the input ends after `DATE`, the error is the existing `Error: invalid input: unexpected
  end of input`.

* Both errors are reported like syntax errors: when the statement is parsed, before anything else about the
  statement is checked or executed, and wherever the literal appears, even if it would never be evaluated.
  `UPDATE t SET d = DATE '2024-13-01' WHERE FALSE` fails with `Error: invalid input: invalid date '2024-13-01'`, and
  so does `CREATE TABLE t (id INT PRIMARY KEY, d DATE DEFAULT DATE '2024-02-30')` (with `'2024-02-30'`). As with
  every failed statement, nothing is changed.

* The literal is checked at the point where the parser reads it, so as with syntax errors the first error in
  the statement (reading left to right) is the one reported: `SELECT DATE '2024-13-01' +` and
  `SELECT DATE '2024-13-01', DATE 'today'` fail with `invalid date '2024-13-01'`, while
  `SELECT FROM DATE '2024-13-01'` fails with the existing `expected expression atom, found FROM`. `<string>` is
  shown exactly as the string's content, without escaping: `SELECT DATE 'it''s'` →
  `Error: invalid input: invalid date 'it's'`.

## `DATE` is a reserved word

`DATE` becomes a reserved keyword, with the same rules as the existing ones:

* It can no longer be used as an unquoted identifier: `CREATE TABLE date (id INT PRIMARY KEY)` and
  `CREATE TABLE t (id INT PRIMARY KEY, date DATE)` → `Error: invalid input: expected identifier, got DATE`.
* Double-quoted it remains a valid identifier, and `schema` shows such names double-quoted, as it does for every
  other keyword: `CREATE TABLE "date" (id INT PRIMARY KEY, "date" DATE)` is displayed as

  ```
  CREATE TABLE "date" (
    id INTEGER PRIMARY KEY,
    "date" DATE DEFAULT NULL
  )
  ```

If the code base has unit tests that pin the exact number or set of reserved keywords or of data types (and their
encodings), update them for the one new keyword and the one new data type; those are the only existing test
expectations that may change.

## Display

A date is displayed in two ways, depending on where it appears:

* **As a value: `YYYY-MM-DD`**, always ten characters, zero-padded, without quotes. This form is used in query
  results and in every error message that mentions a value.

  ```
  > SELECT DATE '2024-01-15', DATE '0033-04-03', '2024-01-15'
  ---
  2024-01-15, 0033-04-03, '2024-01-15'
  ```

  (The third column is a string and is displayed as strings are today.) In error messages:
  `Error: invalid input: primary key 2024-01-15 already exists`, `Error: invalid input: can't compare 2024-01-15
  and 1`.

* **As SQL text: `DATE 'YYYY-MM-DD'`**, i.e. as the literal that produces the value. This form is used in the
  `schema` output (column defaults) and in query plans (constants in expressions, `Values` rows, and the keys or
  values of `KeyLookup` and `IndexLookup` nodes).

The data type is displayed as `DATE` (in `schema` and in error messages that name a type).

## DATE columns

`CREATE TABLE` accepts `DATE` as a column type, with every existing column option: `PRIMARY KEY`, `NULL` /
`NOT NULL`, `DEFAULT`, `UNIQUE`, `INDEX` and `REFERENCES`. Example:

```
> CREATE TABLE trip (id INT PRIMARY KEY, start DATE INDEX, finish DATE, cost INT)
> CREATE TABLE holiday (day DATE PRIMARY KEY, name STRING NOT NULL, moved DATE DEFAULT DATE '2024-12-24' + 2, seen DATE UNIQUE)
schema
---
CREATE TABLE holiday (
  day DATE PRIMARY KEY,
  name STRING NOT NULL,
  moved DATE DEFAULT DATE '2024-12-26',
  seen DATE DEFAULT NULL UNIQUE INDEX
)
CREATE TABLE trip (
  id INTEGER PRIMARY KEY,
  start DATE DEFAULT NULL INDEX,
  finish DATE DEFAULT NULL,
  cost INTEGER DEFAULT NULL
)
```

* **Defaults** are constant expressions as today, and are evaluated when the table is created (so `DATE
  '2024-12-24' + 2` is stored and shown as `DATE '2024-12-26'`). The existing check applies with the new type:
  `d DATE DEFAULT '2024-01-15'` → `Error: invalid input: invalid default type STRING for DATE column d`;
  `d DATE DEFAULT 20240115` → `... invalid default type INTEGER for DATE column d`;
  `n INT DEFAULT DATE '2024-01-15'` → `... invalid default type DATE for INTEGER column n`. Errors while
  evaluating the default are reported as usual (`d DATE DEFAULT DATE '9999-12-31' + 1` →
  `Error: invalid input: date out of range`). `INSERT` without a value and `UPDATE ... SET d = DEFAULT` use the
  default as today.
* **Type checking on INSERT and UPDATE** is the existing check with the new type; a `DATE` column only accepts
  dates and (if nullable) `NULL`, and other columns do not accept dates:

  | Statement (`d` is a DATE column, `n` an INT column) | Error |
  |---|---|
  | `INSERT INTO t (id, d) VALUES (3, '2024-03-01')`, `UPDATE t SET d = '2024-05-05'` | `Error: invalid input: invalid datatype STRING for DATE column d` |
  | `INSERT INTO t (id, d) VALUES (3, 20240301)`, `UPDATE t SET d = d - DATE '2024-01-01'` | `Error: invalid input: invalid datatype INTEGER for DATE column d` |
  | `INSERT INTO t (id, n) VALUES (3, DATE '2024-03-01')`, `UPDATE t SET n = d + 1` | `Error: invalid input: invalid datatype DATE for INTEGER column n` |

  The other existing checks are unchanged (`NULL value not allowed for column d`, `invalid primary key NULL`, ...).
* **Primary keys.** A `DATE` column can be the primary key. A scan of such a table returns the rows in ascending
  date order (as every table is returned in primary-key order today), for the whole range of dates.
  A duplicate key fails with `Error: invalid input: primary key 2024-01-15 already exists`. Lookups, updates
  (including updates that change the key) and deletes by key work as for the other key types.
* **INDEX and UNIQUE** columns of type `DATE` work as for the other types: lookups by `=`, by `IS NULL` and by
  `OR` combinations of them return the same rows as without an index, and stay correct after updates and deletes.
  A duplicate non-NULL date in a unique column fails with
  `Error: invalid input: value 2024-01-02 already in unique column u`; any number of NULLs is allowed.
* **REFERENCES.** A `DATE` column can reference a table whose primary key is a `DATE` column, with the existing
  rules and messages: `Error: invalid input: can't reference DATE primary key of cal from INTEGER column d` (and
  `can't reference INTEGER primary key of trip from DATE column d`), `Error: invalid input: reference 2024-01-03
  not in table cal`, `Error: invalid input: row referenced by booking.id=1`.
* **Transactions** behave as today with dates in keys and values (snapshot isolation; two transactions writing
  the same date key conflict with `Error: serialization failure, retry transaction`).

## Comparisons

The comparison operators `=`, `!=`, `<`, `<=`, `>`, `>=` work between two dates and compare them in calendar
order. If either operand is `NULL` the result is `NULL`, as today. `IS NULL` and `IS NOT NULL` work on dates.

```
> SELECT DATE '2024-01-15' = DATE '2024-01-15', DATE '2024-12-31' < DATE '2025-01-01', DATE '0999-12-31' < DATE '1000-01-01', DATE '2024-01-15' > NULL
---
TRUE, TRUE, TRUE, NULL
```

Comparing a date with a value of any other type (other than `NULL`) is an error, by the existing rule for mixed
types, with both values displayed as values, left operand first:

| Expression | Error |
|---|---|
| `DATE '2024-01-15' = '2024-01-15'` | `Error: invalid input: can't compare 2024-01-15 and '2024-01-15'` |
| `'2024-01-15' < DATE '2024-01-15'` | `Error: invalid input: can't compare '2024-01-15' and 2024-01-15` |
| `DATE '2024-01-15' > 20240115` | `Error: invalid input: can't compare 2024-01-15 and 20240115` |
| `DATE '2024-01-15' != TRUE` | `Error: invalid input: can't compare 2024-01-15 and TRUE` |
| `1.5 = DATE '2024-01-15'` | `Error: invalid input: can't compare 1.5 and 2024-01-15` |

The same holds for columns: with a DATE column `d` and an INT column `n` that are neither primary keys nor
indexed, `SELECT id FROM ev WHERE d = '2024-01-01'` fails with `can't compare 2024-01-01 and '2024-01-01'` and
`... WHERE d >= n` with `can't compare 2024-01-01 and 10` (for the first row, in which `d` is 2024-01-01 and `n`
is 10).

Existing optimizer behaviour carries over unchanged where the optimizer replaces a comparison by a lookup, which
finds values instead of comparing them, so a value of another type is simply not found, without an error:

* When the optimizer turns `column = constant` on a primary key or an indexed column into a key or index lookup,
  a constant of another type matches nothing. `SELECT * FROM holiday WHERE day = '2024-12-25'` (`day` is the
  DATE primary key) therefore returns no rows, and so does a lookup with an integer.
* Likewise `column IS NAN` on a DATE primary key or indexed DATE column is a lookup of `NaN` and returns no rows
  (`SELECT * FROM holiday WHERE day IS NAN`), while on any other DATE column, or on a date expression, it fails
  with the error below.
* An equality join between a DATE column and a column of another type that the optimizer runs as a hash join
  (`trip JOIN holiday ON trip.id = holiday.day`, `id` being an INT column) returns no matching rows, while
  join conditions evaluated row by row (any other condition, e.g. `ON trip.id < holiday.day`) fail with
  `can't compare`.

The other existing operators and predicates do not accept dates and fail with their existing messages, e.g.
`DATE '2024-01-15' IS NAN` → `Error: invalid input: IS NAN can't be used with DATE`;
`DATE '2024-01-15' LIKE '2024%'` → `Error: invalid input: can't LIKE 2024-01-15 and '2024%'`;
`SELECT id FROM ev WHERE d` → `Error: invalid input: filter returned 2024-01-01, expected boolean`.

## Arithmetic

Exactly four operations are defined on dates. In this table, *date* is a date value and *int* an integer value:

| Expression | Result |
|---|---|
| *date* `+` *int*, *int* `+` *date* | the date that many days later (earlier if the integer is negative) |
| *date* `-` *int* | the date that many days earlier (later if the integer is negative) |
| *date* `-` *date* | an integer: the number of days from the right operand to the left one (negative if the left date is earlier, `0` if they are equal) |
| *date* `+ NULL`, `NULL +` *date*, *date* `- NULL`, `NULL -` *date* | `NULL` |

```
> SELECT DATE '2024-01-15' + 1, 1 + DATE '2024-01-15', DATE '2024-01-15' - 1, DATE '2024-01-15' - -17, DATE '2024-02-28' + 2, DATE '1900-02-28' + 1
---
2024-01-16, 2024-01-16, 2024-01-14, 2024-02-01, 2024-03-01, 1900-03-01

> SELECT DATE '2024-01-15' - DATE '2024-01-01', DATE '2024-01-01' - DATE '2024-01-15', DATE '2024-03-01' - DATE '2024-02-01', DATE '9999-12-31' - DATE '0001-01-01'
---
14, -14, 29, 3652058
```

* The results are ordinary dates and integers: they can be compared, stored, used in further arithmetic, grouped
  on, etc. `+` and `-` keep their existing precedence and associativity (`DATE '2024-01-01' + 2 * 3` is
  `2024-01-07`; `DATE '2024-01-10' - 1 - 2` is `2024-01-07`; `DATE '2024-01-10' - DATE '2024-01-01' + 1` is `10`;
  `1 + DATE '2024-01-10' - DATE '2024-01-01'` is `10`).
* **Range.** If the resulting date would be before `0001-01-01` or after `9999-12-31`, the operation fails with
  `Error: invalid input: date out of range`. This holds for every integer operand, however large or small,
  including the largest and smallest 64-bit integers (never an integer overflow error, and never a crash):
  `DATE '9999-12-31' + 1`, `1 + DATE '9999-12-31'`, `DATE '0001-01-01' - 1`, `DATE '2024-01-15' + 3000000`,
  `DATE '2024-01-15' + 9223372036854775807`, `DATE '2024-01-15' - 9223372036854775807` and
  `DATE '2024-01-15' - (-9223372036854775807 - 1)` all fail with it, while `DATE '9999-12-30' + 1` is `9999-12-31`
  and `DATE '0001-01-01' + 3652058` is `9999-12-31`.
* **Everything else is an error**, with the existing messages of the operators (values displayed as values, left
  operand first). In particular an integer minus a date, a date plus a date, and floats, booleans or strings as
  the other operand of `+` or `-` are errors; so are all other arithmetic operators and functions, also when the
  other operand is `NULL` (as for strings and booleans today):

  | Expression | Error |
  |---|---|
  | `DATE '2024-01-15' + DATE '2024-01-16'` | `Error: invalid input: can't add 2024-01-15 and 2024-01-16` |
  | `1 - DATE '2024-01-15'` | `Error: invalid input: can't subtract 1 and 2024-01-15` |
  | `DATE '2024-01-15' + 1.0`, `1.5 + DATE '2024-01-15'` | `Error: invalid input: can't add 2024-01-15 and 1.0`, `... can't add 1.5 and 2024-01-15` |
  | `DATE '2024-01-15' - 0.5` | `Error: invalid input: can't subtract 2024-01-15 and 0.5` |
  | `DATE '2024-01-15' + TRUE`, `DATE '2024-01-15' + '1'` | `Error: invalid input: can't add 2024-01-15 and TRUE`, `... can't add 2024-01-15 and '1'` |
  | `DATE '2024-01-15' * 2`, `2 * DATE '2024-01-15'` | `Error: invalid input: can't multiply 2024-01-15 and 2`, `... can't multiply 2 and 2024-01-15` |
  | `DATE '2024-01-15' / 2` | `Error: invalid input: can't divide 2024-01-15 and 2` |
  | `DATE '2024-01-15' % 7` | `Error: invalid input: can't take remainder of 2024-01-15 and 7` |
  | `DATE '2024-01-15' ^ 2` | `Error: invalid input: can't exponentiate 2024-01-15 and 2` |
  | `-DATE '2024-01-15'` | `Error: invalid input: can't negate 2024-01-15` |
  | `+DATE '2024-01-15'` | `Error: invalid input: can't take the identity of 2024-01-15` |
  | `DATE '2024-01-15'!` | `Error: invalid input: can't take factorial of 2024-01-15` |
  | `sqrt(DATE '2024-01-15')` | `Error: invalid input: can't take square root of 2024-01-15` |
  | `DATE '2024-01-15' * NULL` | `Error: invalid input: can't multiply 2024-01-15 and NULL` |
  | `NULL / DATE '2024-01-15'` | `Error: invalid input: can't divide NULL and 2024-01-15` |

## ORDER BY, GROUP BY and aggregate functions

* **ORDER BY** sorts dates in calendar order; `NULL` sorts before every date in ascending order (and after in
  descending order), as for the other types.
* **GROUP BY** on a date column or date expression puts equal dates in the same group and NULLs in a group of
  their own, as for the other types. `SELECT d FROM ev GROUP BY d` therefore returns each distinct date once.
* **COUNT, MIN and MAX** work on dates. `MIN` and `MAX` return the earliest and latest non-NULL date (`NULL` if
  there is none); their results are ordinary dates (`MAX(d) - MIN(d)` is an integer, `MIN(d) + 1` a date).
* **SUM and AVG can't be used with dates.** As soon as `SUM` or `AVG` is given a date value, the statement fails
  with `Error: invalid input: SUM can't be used with DATE` or `Error: invalid input: AVG can't be used with DATE`.
  This is the case whenever at least one input row gives the aggregate a non-NULL date, also if it is a single
  row, also with GROUP BY, and for date expressions and constants (`AVG(d + 1)`, `SUM(DATE '2024-01-01')`).
  NULLs are ignored by aggregates as today, so with no input rows, or only NULL inputs, `SUM(d)` and `AVG(d)` are
  `NULL` without an error. `SUM` and `AVG` of integer expressions built from dates work as for any integer
  (`SUM(d - DATE '2024-01-01')`).

HAVING, LIMIT, OFFSET, joins (on equality of dates and on any other date condition; NULL dates never match) and
every other part of a query need no special rules: dates behave like any other value there.

## Query plans

In plans, a date constant is displayed as SQL text, `DATE 'YYYY-MM-DD'`, wherever it appears. The optimizer
treats dates like the values of any other type; nothing new is optimized and nothing is optimized differently:

* constant sub-expressions are folded where they are folded today (so `DATE '2024-02-28' + 2` appears as
  `DATE '2024-03-01'` and `DATE '2024-03-01' - DATE '2024-02-01'` as `29`), and not where they are not folded
  today (`UPDATE ... SET` expressions are shown as written);
* `column = date constant` (and `OR`s of them, and `IS NULL`) on a primary key or indexed column becomes a
  `KeyLookup` / `IndexLookup` exactly as for the other types;
* filters are pushed down and equality joins become hash joins exactly as for the other types.

Examples, for the tables `trip` and `holiday` defined above:

```
[plan]> SELECT DATE '2024-02-28' + 2, DATE '2024-03-01' - DATE '2024-02-01', DATE '2024-01-01' < DATE '2024-01-02'
---
Projection: DATE '2024-03-01', 29, TRUE
└─ Values: blank row
2024-03-01, 29, TRUE

[plan]> SELECT id, finish - start, finish + 1 FROM trip WHERE finish >= DATE '2024-05-01' + 9 ORDER BY finish DESC
---
Remap: trip.id, #1, #2 (dropped: trip.finish)
└─ Order: trip.finish desc
   └─ Projection: trip.id, trip.finish - trip.start, trip.finish + 1, trip.finish
      └─ Scan: trip (trip.finish > DATE '2024-05-10' OR trip.finish = DATE '2024-05-10')
2, 14, 2025-01-04
1, 9, 2024-05-11

[plan]> SELECT * FROM holiday WHERE day = DATE '2024-12-25' OR day = DATE '2024-04-30' + 1
---
KeyLookup: holiday (DATE '2024-12-25', DATE '2024-05-01')
2024-12-25, 'xmas', 2024-12-26, NULL
2024-05-01, 'may day', 2024-12-26, NULL

[plan]> SELECT id FROM trip WHERE start = DATE '2024-05-01' AND cost > 50
---
Projection: trip.id
└─ Filter: trip.cost > 50
   └─ IndexLookup: trip.start (DATE '2024-05-01')
1

[plan]> SELECT trip.id, holiday.name FROM trip JOIN holiday ON trip.start = holiday.day
---
Projection: trip.id, holiday.name
└─ HashJoin: inner on trip.start = holiday.day
   ├─ Scan: trip
   └─ Scan: holiday
1, 'may day'
3, 'may day'

[plan]> INSERT INTO holiday (day, name) VALUES (DATE '2025-01-01', 'new year')
---
Insert: holiday
└─ Values: DATE '2025-01-01', 'new year'

[plan]> UPDATE trip SET finish = start + 7 WHERE finish IS NULL
---
Update: trip (finish=trip.start + 7)
└─ Scan: trip (trip.finish IS NULL)

[plan]> DELETE FROM holiday WHERE day = DATE '2025-01-01'
---
Delete: holiday
└─ KeyLookup: holiday (DATE '2025-01-01')
```

(The rows used in these examples: `trip` holds `(1, 2024-05-01, 2024-05-10, 100)`, `(2, 2024-12-20, 2025-01-03,
250)` and `(3, 2024-05-01, NULL, NULL)`; `holiday` holds `2024-12-25 'xmas'` and `2024-05-01 'may day'`, both with
the default `moved` and a NULL `seen`.)

The plans of statements without dates do not change.

## Summary of the new error messages

| Situation | Error |
|---|---|
| a `DATE` literal whose string is not a valid `YYYY-MM-DD` date in the range `0001-01-01`..`9999-12-31` | `Error: invalid input: invalid date '<string>'` |
| `DATE` in an expression not followed by a string literal | `Error: invalid input: expected date string, found <token>` |
| date arithmetic whose result is outside `0001-01-01`..`9999-12-31` | `Error: invalid input: date out of range` |
| `SUM` / `AVG` given a non-NULL date | `Error: invalid input: SUM can't be used with DATE` / `Error: invalid input: AVG can't be used with DATE` |

All other errors involving dates are existing messages, with dates displayed as `YYYY-MM-DD` and the type as
`DATE`, as listed in the sections above. A statement that fails changes nothing, as today.
