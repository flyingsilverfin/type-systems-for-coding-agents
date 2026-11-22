# Tier 4 extensions — documentation text (both implementations)

This file fixes the documentation that the tier-4 base carries, so that the Rust and the Python workspace document
their code identically. It is derived from `EXT4_SPEC.md` and adds nothing to it.

**Rules.**

* Every new module starts with the module doc comment given below, word for word, in the language's syntax: Rust
  `//!` lines at the top of the file; Python a module docstring. Line breaks may differ (wrap at the file's usual
  width); words, punctuation, list structure and code spans (backticks) don't.
* The items listed under a module (by role, not by name: the implementations name them as is natural in each
  language) carry the given doc comment, word for word: Rust `///`; Python the docstring of the function, class or
  method. Where an item has no single counterpart in one language (e.g. a Rust enum variant vs. a Python class),
  the text goes on the closest declaration.
* Additions to existing modules (new enum variants, struct fields, trait methods, keys, commands) carry the
  one-line doc comments of section 9, in the style of the surrounding code (Rust `///` on the variant or method,
  Python a docstring or a `#` comment on the class or field, as the existing port does for its neighbours).
* Every other new item gets either no doc comment or a one-line summary of what it is ("Formats a FROM item.",
  "The prepared statements of a session."). Such summaries, and ordinary code comments, must not state behavioural
  rules, orderings, special cases, examples or hints that are not in the texts below, and must not mention the
  benchmark tasks or constructs that toydb doesn't have.
* The README section of section 8 is added to both `README.md` files (after the tier-3 extensions), and the module
  table of `LAYOUT.md` gets the paths of section 0.

## 0. Modules

| Extension | Rust | Python |
|---|---|---|
| format | `src/sql/parser/format.rs` | `toydb/sql/parser/format.py` |
| views | `src/sql/types/view.rs`, `src/sql/execution/views.rs` | `toydb/sql/types/view.py`, `toydb/sql/execution/views.py` |
| describe | `src/sql/planner/describe.rs` | `toydb/sql/planner/describe.py` |
| estimate | `src/sql/planner/estimate.rs` | `toydb/sql/planner/estimate.py` |
| prepared | `src/sql/execution/prepared.rs` | `toydb/sql/execution/prepared.py` |
| lint | `src/sql/execution/lint.rs` | `toydb/sql/execution/lint.py` |
| changes | `src/sql/engine/changes.rs` | `toydb/sql/engine/changes.py` |

Unit tests: Rust in the `#[cfg(test)] mod tests` of each new file; Python in the mirrored `tests/sql/.../test_*.py`.
Test code has no doc-text requirements beyond the existing conventions.

---

## 1. `format` — `sql/parser/format`

**Module doc:**

> Canonical SQL text of statements and expressions, written from the AST. It is the output of `FORMAT`, the text
> that views store for their query, the statement text of `SHOW PREPARED`, and the text of expressions in lint
> messages and of views in the change log.
>
> * Keywords are upper case. Names are written so that they read back as the same name: unquoted only if they are
>   a lower-case identifier that isn't a keyword, otherwise between double quotes with `"` doubled. (Statement
>   digests and the schema display quote names differently.)
> * Literals are written as SQL text that reads back as the same literal: floats as displayed in query results,
>   `NAN` and `INFINITY`, strings between single quotes with `'` doubled, literals of other kinds as the SQL text of
>   their value. A minus sign is an operator, not part of a literal. Parameters are written `$n`.
> * Synonyms are written in one form, optional words are left out, aliases use `AS`, and operands are
>   parenthesized exactly where the parser needs it, as in statement digests. Every construct is written as it was
>   written.
> * A statement is a list of lines: each clause on a line of its own (`FROM`, `WHERE`, `GROUP BY`, `HAVING`,
>   `ORDER BY`, `LIMIT`, `OFFSET`, `SET`), joins on lines indented by two spaces, the rows of `VALUES` and the
>   columns of `CREATE TABLE` one per indented line, and a statement contained in another one on the following
>   lines. The one-line form joins the lines with single spaces.

**Items:**

* The function that returns the lines of a statement:
  > Returns the canonical text of a statement, as lines.
* The function that returns the one-line form:
  > Returns the canonical text of a statement on one line: its lines without indentation, joined by single spaces
  > (without spaces inside the parentheses of a column list).
* The function that writes an expression:
  > Returns the canonical text of an expression.
* The function that writes a name:
  > Writes a name so that the lexer reads it back as the same name, quoting it if necessary.

---

## 2. `views`

### 2.1 `sql/types/view`

**Module doc:**

> View definitions. A view is a named query, stored in the catalog as the canonical one-line text of its query
> together with its column names and the names of the tables and views that its query reads (its dependencies).
> The query is parsed and planned again every time a statement uses the view.

**Items:**

* The view definition type:
  > A view definition, as stored in the catalog.
* Its fields, in this order: name, columns, query, dependencies:
  > The view name. Views and tables share one namespace.
  >
  > The column names, in query column order.
  >
  > The canonical one-line text of the view's query.
  >
  > The tables and views that the query's FROM clause reads, by name.

### 2.2 `sql/execution/views`

**Module doc:**

> CREATE VIEW, DROP VIEW and SHOW VIEWS, and the dependency checks of views.
>
> * CREATE VIEW checks the name, plans the query (without optimizing or executing it), names the columns from the
>   column list or the query's result columns, and stores the definition.
> * A table or view that a view depends on can't be dropped, and no statement may change the definition of a table
>   that a view depends on.
> * SHOW VIEWS lists the views by name, with their columns and query.

**Items:**

* The function that executes CREATE VIEW:
  > Creates a view, after checking its name, its query and its column names.
* The function that executes DROP VIEW:
  > Drops a view, unless another view depends on it. Returns true if the view existed.
* The function that checks the dependencies of a table before it is dropped or its definition is changed:
  > Errors if a view depends on the given table.
* The function that returns SHOW VIEWS:
  > Returns the SHOW VIEWS result: one row per view, ordered by name.

---

## 3. `describe` — `sql/planner/describe`

**Module doc:**

> Result description for `DESCRIBE`: the result columns of a statement, or the columns of a table or view, with
> their data types and nullability.
>
> A statement is described from its plan before optimization, so no expression is evaluated.
>
> * The type of an expression is the data type of the values it returns when its evaluation succeeds. It is
>   unknown if that depends on the values of its operands, not only on their types, or if the expression can't
>   return a non-NULL value. The NULL constant only returns NULL; an operand of unknown type may return values of
>   any type.
> * An expression is nullable if it can return NULL: a nullable column, a column of the inner side of an outer
>   join, the NULL constant, an operator or function with a nullable operand (except `IS NULL`), and every
>   aggregate function except `count`.

**Items:**

* The function that describes a plan:
  > Describes the result columns of a plan: one entry per column of a query, none for other statements.
* The function that infers an expression's type:
  > Returns the data type of an expression, or None if it is unknown.
* The function that infers nullability:
  > Returns true if an expression can return NULL.
* The function that describes a table or view:
  > Describes the columns of a table or view.

---

## 4. `estimate` — `sql/planner/estimate`

**Module doc:**

> Row estimates for `ESTIMATE`: the estimated number of rows of every node of an optimized plan, computed from the
> column statistics of `ANALYZE` as the transaction sees them.
>
> * A table has the row count of its statistics, or 1000 without statistics.
> * Conditions have a selectivity: from the statistics of the columns they compare (distinct values and NULLs for
>   equality, interpolation between minimum and maximum for ranges of numbers), combined for AND, OR and NOT, and
>   fixed values otherwise.
> * Each node estimates its rows from its inputs; a column keeps the statistics of the table column it passes on
>   unchanged.
>
> Estimates are computed in floating point and rounded to the nearest integer (halves up) only for display.

**Items:**

* The function that estimates a plan:
  > Returns the estimated rows of every line of the plan display, in display order.
* The function that estimates a node:
  > Returns the estimated number of rows of a node.
* The function that estimates a condition:
  > Returns the estimated fraction of rows for which a condition is true.
* The function that traces a column to its statistics:
  > Returns the statistics of the table column that a column reference passes on unchanged, if any.

---

## 5. `prepared` — `sql/execution/prepared`

**Module doc:**

> Prepared statements: `PREPARE`, `EXECUTE`, `DEALLOCATE` and `SHOW PREPARED`.
>
> A prepared statement is a planned statement with typed parameters `$1`, `$2`, ..., kept by the session under a
> name. EXECUTE checks the arguments (their number, and that each is a constant of the parameter's type or NULL)
> and runs the statement with every parameter replaced by the value of its argument.
>
> Each prepared statement caches the plan of its last successful planning, with the argument values and the
> definitions of the tables and views that planning used. The cached plan is used again for identical arguments
> while those definitions are unchanged; otherwise the statement is planned anew.

**Items:**

* The prepared statement type:
  > A prepared statement: the parsed statement, its parameter types, its cached plan and its execution counts.
* The per-session collection:
  > The prepared statements of a session, by name.
* The function that binds arguments:
  > Checks the arguments of an EXECUTE and returns the statement with every parameter replaced by its value.
* The function that looks up or builds a plan:
  > Returns the cached plan if it is valid for the arguments and the current definitions, otherwise plans the
  > statement and caches the plan if planning succeeds.
* The function that returns SHOW PREPARED:
  > Returns the SHOW PREPARED result: one row per prepared statement, ordered by name.

---

## 6. `lint` — `sql/execution/lint`

**Module doc:**

> Statement lint for `LINT`: warnings about likely mistakes in a statement and the statements it contains,
> without planning or executing them. Each warning has a code and a message.
>
> * `unfiltered_write`: UPDATE or DELETE without WHERE.
> * `implicit_columns`: INSERT without a column list.
> * `null_comparison`: a comparison with the NULL constant.
> * `type_mismatch`: a column compared with a literal or parameter of a type it can't be compared with.
> * `like_without_wildcard`: LIKE with a pattern without `%` or `_`.
> * `division_by_zero`: division or remainder by the integer literal 0.
> * `constant_condition`: a WHERE, HAVING or ON condition without columns, aggregates or parameters.
> * `order_by_constant`: an ORDER BY expression without columns, aggregates or parameters.
>
> Column references are resolved against the tables and views the statement reads; references that don't resolve
> are not checked. Warnings are listed in the order of the statement's canonical text.

**Items:**

* The function that lints a statement:
  > Returns the warnings for a statement and the statements it contains, in canonical text order.
* The warning type:
  > A lint warning: its code and message.

---

## 7. `changes` — `sql/engine/changes`

**Module doc:**

> The change log of `SHOW CHANGES`: the changes made by committed transactions, in commit order.
>
> The local engine records each change as its transaction makes it — rows inserted, updated and deleted, tables and
> views created and dropped, statistics stored — and appends a transaction's changes to the log when it commits;
> a rollback discards them. The log is kept in memory, is not written to storage, and starts empty when the engine
> is created. In a cluster each node records the changes as it applies the Raft log, so all nodes have the same
> log.

**Items:**

* The change type:
  > A change of the database: the table or view it concerns, its kind, and, for row changes, the primary key and
  > the row.
* The log type:
  > The change log: the committed changes in commit order, and the pending changes of open transactions by version.
* The function that records a change:
  > Records a change of an open transaction.
* The function called at commit:
  > Appends the pending changes of a transaction to the log.
* The function called at rollback:
  > Discards the pending changes of a transaction.
* The function that returns SHOW CHANGES:
  > Returns the committed changes of transactions with a version above the given one, in commit order.

---

## 8. README section

Added to both `README.md` files, after the tier-3 extensions:

```markdown
Seven more extensions build on the SQL engine, the tier-3 extensions and each other:

* **SQL formatter.** `FORMAT statement` returns the statement's canonical SQL
  text, one clause per line: keywords in upper case, names quoted only where
  needed to read back as the same name, literals as written values, synonyms
  and optional words normalized. It doesn't check or execute the statement.

* **Views.** `CREATE VIEW name [(columns)] AS SELECT ...` stores a query, which
  is planned again whenever a statement reads the view in `FROM`; plans show it
  as a `View` node. `DROP VIEW [IF EXISTS] name` removes it, `SHOW VIEWS` lists
  the views. Tables and views that a view reads can't be dropped. Views are
  transactional and replicated like tables, and `EXPORT AS SQL` includes them.

* **Result description.** `DESCRIBE table_or_view` and `DESCRIBE statement`
  return the columns of a table or view, or the result columns of a statement,
  with their data types (NULL if unknown) and whether they can be NULL, without
  executing anything.

* **Row estimates.** `ESTIMATE statement` returns the statement's plan, one row
  per line, with the estimated number of rows of each node, computed from the
  column statistics of `ANALYZE` (1000 rows for a table without statistics).

* **Prepared statements.** `PREPARE name (types) AS statement` prepares a
  statement with parameters `$1`, `$2`, ...; `EXECUTE name (values)` runs it,
  `DEALLOCATE name` removes it and `SHOW PREPARED` lists the session's prepared
  statements with their execution and planning counts. A prepared statement
  reuses its cached plan for the same arguments while the definitions of the
  tables and views it uses are unchanged.

* **Statement lint.** `LINT statement` returns warnings about likely mistakes,
  such as comparisons with NULL, comparisons of a column with a value of another
  type, UPDATE or DELETE without WHERE, and constant conditions, without
  executing the statement.

* **Change log.** `SHOW CHANGES [SINCE version]` lists the changes of committed
  transactions in commit order — inserted, updated and deleted rows (as JSON),
  created and dropped tables and views, ANALYZE — with the version of their
  transaction. The log is kept in memory by each server.

`CHANGES`, `DEALLOCATE`, `DESCRIBE`, `ESTIMATE`, `EXECUTE`, `FORMAT`, `LINT`,
`PREPARE`, `PREPARED`, `SINCE`, `VIEW` and `VIEWS` are reserved keywords. The
golden scripts of these extensions are in the `ext4/` script directories.
```

In the README's layout table, each workspace lists its `ext4/` script directories next to `ext/`
(`src/sql/testscripts/ext4/`, `tests/scripts/ext4/` in Rust; `tests/sql/testscripts/ext4/`,
`tests/e2e/scripts/ext4/` in Python).

---

## 9. Doc comments of additions to existing modules

One line each, as in the surrounding code.

| Addition | Doc comment |
|---|---|
| keywords (lexer) | none (like the existing keywords) |
| the parameter token | `A parameter $n of a prepared statement.` |
| AST statement FORMAT | `FORMAT: returns the canonical SQL text of a statement.` |
| AST statement CREATE VIEW | `CREATE VIEW: creates a view.` (fields: `The view name.`, `The column names, if given.`, `The view's query, a SELECT statement.`) |
| AST statement DROP VIEW | `DROP VIEW: drops a view.` (fields: `The view to drop.`, `IF EXISTS: if true, don't error if the view doesn't exist.`) |
| AST statement SHOW VIEWS | `SHOW VIEWS: lists the views.` |
| AST statement DESCRIBE | `DESCRIBE: describes a table or view, or the result of a statement.` |
| AST statement ESTIMATE | `ESTIMATE: estimates the rows of a statement's plan.` |
| AST statement PREPARE | `PREPARE: prepares a statement with typed parameters.` (fields: `The prepared statement's name.`, `The parameter types, for $1, $2, ...`, `The statement.`) |
| AST statement EXECUTE | `EXECUTE: executes a prepared statement.` (fields: `The prepared statement's name.`, `The arguments, one per parameter.`) |
| AST statement DEALLOCATE | `DEALLOCATE: removes a prepared statement.` |
| AST statement SHOW PREPARED | `SHOW PREPARED: lists the session's prepared statements.` |
| AST statement LINT | `LINT: returns warnings about a statement.` |
| AST statement SHOW CHANGES | `SHOW CHANGES: lists the committed changes.` (field: `SINCE: only changes of transactions above this version.`) |
| AST parameter expression | `A parameter $n of a prepared statement.` |
| plan node View | `A view: emits the rows of the view's query plan, with the view's column names. The alias is only used for formatting.` |
| statement result Format | `The lines of FORMAT.` |
| statement results CreateView, DropView, Prepare, Deallocate | none (like the existing results) |
| catalog methods | `Creates a view. Errors if a table or view of that name exists.`, `Drops a view. Errors if it does not exist, unless if_exists is true. Returns true if the view existed.`, `Fetches a view definition, or None if it doesn't exist.`, `Returns a list of all view definitions.` |
| local engine key | `A view definition, by view name.` |
| Raft write commands | none (like the existing commands) |
| Raft read commands | none (like the existing commands) |
| the local engine's change log | `The change log of committed changes, shared by the engine's transactions.` |
| the session's prepared statements | `Prepared statements, by name.` |
