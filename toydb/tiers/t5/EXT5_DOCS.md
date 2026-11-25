# Tier 5 extensions — documentation text (both implementations)

This file fixes the documentation that the tier-5 base carries, so that the Rust and the Python workspace document
their code identically. It is derived from `EXT5_SPEC.md` and adds nothing to it. Agents see this text only as it
appears in the code (module and item doc comments) and in the README sections of section 9; they never see
`EXT5_SPEC.md`. Every rule that decides how a layer handles a construct added to toydb later is therefore stated
here, in the module doc of the layer.

**Rules** (as in `tiers/t4/EXT4_DOCS.md`).

* Every new module starts with the module doc comment given below, word for word, in the language's syntax: Rust
  `//!` lines at the top of the file; Python a module docstring. Line breaks may differ (wrap at the file's usual
  width); words, punctuation, list structure, tables and code spans (backticks) don't.
* The items listed under a module (by role, not by name: the implementations name them as is natural in each
  language) carry the given doc comment, word for word: Rust `///`; Python the docstring of the function, class or
  method. Where an item has no single counterpart in one language (e.g. a Rust enum variant vs. a Python class),
  the text goes on the closest declaration.
* Additions to existing modules (new enum variants, struct fields, trait methods, keys, commands, requests) carry the
  one-line doc comments of section 10, in the style of the surrounding code.
* Every other new item gets either no doc comment or a one-line summary of what it is ("Encodes a value.", "The
  sessions of a server."). Such summaries, and ordinary code comments, must not state behavioural rules, orderings,
  special cases, examples or hints that are not in the texts below, and must not mention the benchmark tasks or
  constructs that toydb doesn't have.
* The README sections of section 9 are added to both `README.md` files, after the tier-4 extensions; the module
  table of `LAYOUT.md` gets the paths of section 0.

## 0. Modules

| Extension | Rust | Python |
|---|---|---|
| users | `src/sql/types/user.rs`, `src/sql/execution/privileges.rs`, `src/sql/execution/users.rs` | `toydb/sql/types/user.py`, `toydb/sql/execution/privileges.py`, `toydb/sql/execution/users.py` |
| audit | `src/sql/execution/audit.rs` | `toydb/sql/execution/audit.py` |
| checks | `src/sql/types/check.rs`, `src/sql/execution/checks.rs` | `toydb/sql/types/check.py`, `toydb/sql/execution/checks.py` |
| policies | `src/sql/types/policy.rs`, `src/sql/execution/policies.rs` | `toydb/sql/types/policy.py`, `toydb/sql/execution/policies.py` |
| pgwire | `src/pgwire/mod.rs`, `src/pgwire/message.rs`, `src/pgwire/types.rs`, `src/pgwire/connection.rs` | `toydb/pgwire/__init__.py`, `toydb/pgwire/message.py`, `toydb/pgwire/types.py`, `toydb/pgwire/connection.py` |
| backup | `src/backup/mod.rs`, `src/backup/format.rs`, `src/bin/toybackup.rs` | `toydb/backup/__init__.py`, `toydb/backup/format.py`, `toydb/bin/toybackup.py` (+ the launcher `bin/toybackup`) |
| catalog | `src/sql/execution/system.rs` | `toydb/sql/execution/system.py` |
| matviews | `src/sql/types/matview.rs`, `src/sql/execution/matviews.rs` | `toydb/sql/types/matview.py`, `toydb/sql/execution/matviews.py` |
| profile | `src/sql/execution/profile.rs` | `toydb/sql/execution/profile.py` |
| (users, pgwire) MD5 | `src/encoding/md5.rs` | none: Python uses the standard library's `hashlib` |
| (pgwire) client | `src/pgwire/client.rs` | `toydb/pgwire/client.py` |

Unit tests: Rust in the `#[cfg(test)] mod tests` of each new file; Python in the mirrored `tests/.../test_*.py`.
Test code has no doc-text requirements beyond the existing conventions.

---

## 1. `users`

### 1.1 `sql/types/user`

**Module doc:**

> Users, memberships and privileges, as stored in the catalog.
>
> A user has a name, a superuser flag and optionally a password, stored as PostgreSQL stores MD5 passwords: `md5`
> and the hex MD5 digest of the password followed by the user name. The superuser `admin` always exists and is not
> stored. A user can be a member of other users; it acts for itself, for the users it is a member of, and for the
> users those act for. A table or view has an owner, the user that created it (objects of `admin` are owned by
> `admin`, which is not stored), and privileges granted to users: `SELECT`, `INSERT`, `UPDATE`, `DELETE`. A
> statement that creates a table or view, also one added later, makes the current user its owner. A statement that
> changes a table's definition doesn't change its owner or its privileges, and a CREATE TABLE that doesn't create a
> table doesn't change the existing table's owner.

**Items:**

* The user type: `A user, as stored in the catalog.` Its fields: `The user name.`, `Whether the user is a
  superuser.`, `The user's password as an MD5 hash, if it has one.`
* The privilege type: `A privilege on a table or view: SELECT, INSERT, UPDATE or DELETE.`

### 1.2 `sql/execution/privileges`

**Module doc:**

> Statement accesses and privilege checks.
>
> The accesses of a statement are the tables, views and materialized views it uses, each with a kind: `read`
> (reads rows or statistics), `insert`, `update`, `delete` (writes rows), `create`, `drop`, `alter` (changes the
> definition of a table: adds, drops or renames columns or otherwise changes its schema display), `policy` (creates
> or drops a policy of a table), `analyze` (stores statistics), `refresh` (recomputes a materialized view), `grant`
> (grants or revokes privileges on it).
>
> * SELECT reads every table, view and materialized view of its FROM clause; INSERT, UPDATE, DELETE and IMPORT write their table;
>   CREATE and DROP create and drop theirs; CREATE VIEW creates the view and reads the tables and views of its
>   query; ANALYZE analyzes its table, or every table; SHOW STATISTICS and EXPORT read theirs, EXPORT AS SQL every
>   table and view; EXPLAIN, DESCRIBE and ESTIMATE of a statement have that statement's accesses, EXECUTE those of
>   the statement it executes; GRANT and REVOKE on a table grant it; CREATE and DROP POLICY have the `policy`
>   access of their table; CREATE MATERIALIZED VIEW creates it and reads the tables and views of its query, REFRESH
>   has the `refresh` access, DROP the `drop` access. Other statements have none.
> * A statement added later has the accesses that follow from what it does: `read` of every table and view named in
>   a FROM clause of a query it contains and executes, `insert`, `update` or `delete` of a table whose rows it
>   writes, `create` or `drop` of what it creates or drops, `alter` of a table whose definition it changes. A part of
>   an existing statement added later (a clause, an option, a query in place of a `VALUES` list) adds the accesses
>   that follow from what it does in the same way.
> * Accesses are in the order of the statement's canonical text, each name and kind once.
>
> Every statement is checked against the session's current user before it is executed, in the statement's
> transaction: `read` requires the `SELECT` privilege, `insert`, `update` and `delete` the privilege of that name,
> `drop`, `alter`, `policy`, `analyze`, `refresh` and `grant` acting for the owner, `create` nothing. A user has a
> privilege if it is a superuser, acts for the owner, or acts for a user it was granted to. Accesses to names that are
> neither tables, views nor materialized views are not checked. CREATE and DROP USER, GRANT and REVOKE of memberships, SHOW CHANGES, SHOW AUDIT
> and backups require a superuser.

**Items:**

* The access kind type: `The kind of an access: what a statement does with a table or view.`
* The function that returns a statement's accesses: `Returns the accesses of a statement, in the order of its
  canonical text.`
* The function that checks a statement: `Checks that the current user may execute a statement: the privileges its
  accesses require, then its own requirement.`
* The function that tells whether a user has a privilege: `Returns true if a user has a privilege on a table or
  view: it is a superuser, acts for the owner, or acts for a user the privilege was granted to.`

### 1.3 `sql/execution/users`

**Module doc:**

> CREATE USER, DROP USER, GRANT, REVOKE, SET ROLE, RESET ROLE, SHOW USERS and SHOW GRANTS, and login.
>
> * Users are created and dropped by superusers. Dropping a user removes its memberships and the privileges granted
>   to it; a user that owns a table or view, or that a policy names, can't be dropped, nor can `admin` or the
>   session's current or login user.
> * GRANT and REVOKE of privileges on a table or view require acting for its owner; on a view only SELECT can be
>   granted. GRANT and REVOKE of a user make a user a member of another one or end that; they require a superuser,
>   and memberships can't form a cycle.
> * SET ROLE makes another user the session's current user, if the login user is a superuser or acts for it; RESET
>   ROLE returns to the login user. Neither is transactional.
> * SHOW GRANTS lists, for every table and view, its owner as the privilege `OWNER` and the granted privileges.

**Items:**

* The function that executes DROP USER: `Drops a user, its memberships and its privileges. Returns true if the user
  existed.`
* The function that grants privileges: `Grants privileges on a table or view to a user.`
* The function that makes a membership: `Makes a user a member of another user, unless that would create a cycle.`
* The function that checks a login: `Checks a login: the user must exist, and the password must be the user's
  password (none for a user without one).`
* The function that hashes a password: `Returns the stored form of a user's password: md5 and the hex MD5 of the
  password followed by the user name.`
* The function that returns SHOW GRANTS: `Returns the SHOW GRANTS result: the owner and the privileges of every table
  and view, by object, privilege and grantee.`

---

## 2. `audit` — `sql/execution/audit`

**Module doc:**

> The audit log and the session list of a server, for `SHOW AUDIT` and `SHOW SESSIONS`.
>
> Every statement that a session executes, successfully or not, is recorded after it is executed — except
> statements that fail to parse and SHOW AUDIT — with the session's current user, the statement's class, its name
> (its leading keywords in lower case joined by `_`, without optional words and operands, as in JSON results)
> and its accesses as objects (`<name> (<kind>)`), and its error. The class follows from the statement and its accesses, the first that
> applies:
>
> 1. `ROLE`: CREATE USER, DROP USER, GRANT, REVOKE;
> 2. `DDL`: a `create`, `drop`, `alter` or `policy` access;
> 3. `WRITE`: an `insert`, `update`, `delete` or `refresh` access;
> 4. `READ`: a `read` access;
> 5. `MISC`: every other statement.
>
> Statements added later get their class from their accesses by the same rule. Sessions are numbered in the order
> the server creates them. Both lists are kept in memory, are not written to storage and are not replicated.

**Items:**

* The entry type: `An audit log entry: the user, class, statement name, objects and error of an executed statement.`
* The function that classifies a statement: `Returns the audit class of a statement, from its kind and its
  accesses.`
* The log type: `The audit log of a server: the entries in the order the statements were executed.`
* The session list type: `The open sessions of a server, by number.`

---

## 3. `checks`

### 3.1 `sql/types/check`

**Module doc:**

> CHECK constraints, as stored in the catalog: a table's constraints, each with its name and the canonical
> one-line text of its condition.

**Items:**

* The constraint type: `A CHECK constraint: its name and its condition, as canonical text.`

### 3.2 `sql/execution/checks`

**Module doc:**

> CHECK constraints: created with their table, enforced on every written row.
>
> * Constraints are table constraints (`[CONSTRAINT name] CHECK (condition)`) and column constraints (`CHECK
>   (condition)` in a column definition), in the order written. A constraint without a name is named
>   `<table>_check<i>`, its position among the table's constraints counting from 1. Names are unique in the table.
>   The condition is planned like the WHERE condition of `SELECT * FROM <table>`. These checks come after the other
>   checks of CREATE TABLE and before anything is written. A CREATE TABLE that doesn't create a table neither
>   checks nor creates constraints, and the existing table's constraints stay as they are.
> * A statement that adds a column, also one added later, adds the column's constraints as the table's next ones
>   (named by their position among all the table's constraints, and checked as in CREATE TABLE, with the new column
>   in the table) and checks every existing row against them, in primary key order, with its value of the new
>   column, after all of its own checks and before anything is changed. The table's other constraints are not
>   checked again.
> * Every row that a statement inserts or updates — INSERT, UPDATE, IMPORT, and every statement added later that
>   inserts or updates rows — is checked after it is complete and before any of the statement's rows is written
>   (so before the checks of data types, NULLs, keys, unique values and references): FALSE violates a constraint,
>   TRUE and NULL satisfy it, another value is an error.
> * No statement may drop or rename a column that a constraint of its table uses (that its condition references):
>   every statement that drops or renames columns, also one added later, fails with `column <c> is used by check
>   constraint <name>`, naming the first such constraint, after all of its own checks and before anything is
>   changed.
> * DROP TABLE drops the table's constraints.

**Items:**

* The function that creates a table's constraints: `Names, checks and stores the CHECK constraints of a new table.`
* The function that checks rows: `Checks rows against the CHECK constraints of their table, in order.`

---

## 4. `policies`

### 4.1 `sql/types/policy`

**Module doc:**

> Row-level security policies, as stored in the catalog: a policy of a table has a name, optionally the user it
> applies to, and the canonical one-line text of its USING condition and, if it has one, of its WITH CHECK
> condition.

**Items:**

* The policy type: `A row-level security policy: its table, name, user (none for all users), USING condition and
  WITH CHECK condition (if any).`

### 4.2 `sql/execution/policies`

**Module doc:**

> Row-level security policies: CREATE POLICY and DROP POLICY, and the rows a user reads.
>
> * A table with policies is protected. A user that is neither a superuser nor acts for the table's owner reads
>   only the rows of a protected table for which the USING condition of one of its applicable policies — those for a
>   user it acts for, and those without a user — is TRUE, evaluated in order of name before the statement's own
>   conditions. This holds for every read of the table's rows: scans, key and index lookups, in queries, views,
>   UPDATE, DELETE, EXPORT and every statement added later that reads rows. Plans don't change.
> * Every row such a user inserts or updates (INSERT, UPDATE, IMPORT and every statement added later that inserts or
>   updates rows) must satisfy the check condition (WITH CHECK, or else USING) of one applicable policy, checked after
>   the row's CHECK constraints and before any row is written.
> * No statement may drop or rename a column that a policy of its table uses (that its USING or WITH CHECK condition
>   references): every statement that drops or renames columns, also one added later, fails with `column <c> is used
>   by policy <name>`, naming the first such policy by name, after the check-constraint check and before anything is
>   changed.
> * DROP TABLE drops the table's policies.

**Items:**

* The function that returns the applicable policies: `Returns the policies of a table that apply to a user, by
  name, or none if the user reads every row.`
* The function that filters rows: `Returns true if a policy shows the row: one of the policies is TRUE for it.`
* The function that checks written rows: `Checks that each written row satisfies the check condition of one
  applicable policy.`

---

## 5. `pgwire`

### 5.1 `pgwire` (Rust `pgwire/mod.rs`, Python `pgwire/__init__.py`)

**Module doc:**

> A server for a subset of the PostgreSQL frontend/backend protocol, version 3.0, so that PostgreSQL clients can
> use toydb. Each connection has a session of its own, with the startup message's user as its login user; its
> statements are executed exactly as those of a client connection.

**Items:**

* The listener function: `Accepts PostgreSQL connections and serves each in a thread of its own.`

### 5.2 `pgwire/message`

**Module doc:**

> The messages of the PostgreSQL protocol: decoding of frontend messages and encoding of backend messages.
>
> A message is a type byte, a 4-byte big-endian length that counts itself, and the body; the startup-phase messages
> have no type byte. Integers are big-endian and strings are zero-terminated UTF-8.

**Items:**

* The frontend message type: `A message from the client.`
* The backend message type: `A message to the client.`

### 5.3 `pgwire/types`

**Module doc:**

> PostgreSQL types of toydb values, and their text and binary formats.
>
> | toydb data type | PostgreSQL type | OID | size |
> |---|---|---|---|
> | BOOLEAN | `bool` | 16 | 1 |
> | INTEGER | `int8` | 20 | 8 |
> | FLOAT | `float8` | 701 | 8 |
> | STRING | `text` | 25 | -1 |
> | every data type added later, and an unknown type | `text` | 25 | -1 |
>
> A value is sent in its own type's format: BOOLEAN as `t`/`f` (binary: one byte), INTEGER in decimal (binary: 8
> bytes), FLOAT as displayed in query results except `NaN`, `Infinity`, `-Infinity` (binary: 8 bytes IEEE 754),
> STRING as itself (binary: UTF-8), a value of every data type added later as displayed in query results (binary:
> the UTF-8 of that text).
>
> Parameter OIDs map to data types: 16 BOOLEAN; 20, 21, 23 INTEGER; 700, 701 FLOAT; 25, 1043 STRING; other OIDs are
> not supported. Parameter values are read by their declared OID: `t`, `true`, `f`, `false` in any case; integers
> with an optional sign within the range of the type; floats as IMPORT reads them, or `NaN`, `Infinity`,
> `-Infinity`, `inf`, `-inf` in any case; text as itself. Binary parameters have the sizes of their types.

**Items:**

* The function that maps a data type to a PostgreSQL type: `Returns the PostgreSQL type that a data type is sent as,
  text for an unknown type.`
* The function that encodes a value as text: `Encodes a value in the text format.`
* The function that encodes a value as binary: `Encodes a value in the binary format.`
* The function that maps a parameter OID: `Returns the data type of a parameter OID, or an error if it isn't
  supported.`
* The function that decodes a parameter: `Decodes a parameter value of the given OID and format.`

### 5.4 `pgwire/connection`

**Module doc:**

> A PostgreSQL connection: startup, simple and extended query.
>
> * Startup accepts only protocol 3.0, refuses SSL and GSS encryption, asks a user with a password for it with the
>   MD5 method, and then sends ParameterStatus, BackendKeyData and ReadyForQuery.
> * A Query message is split into statements at the SQL lexer's `;` tokens; each is executed as if sent alone, and
>   the first error ends the query.
> * Results: rows as RowDescription, DataRows and `SELECT <n>`; the lines of EXPLAIN (column `QUERY PLAN`),
>   FORMAT and EXPORT as rows of one text column; `INSERT 0 <n>`, `UPDATE <n>`, `DELETE <n>`; every other result,
>   also one added later, as its statement's name in upper case with spaces (`CREATE TABLE`, `SET ROLE`).
> * A result column is named by its header without table qualifier. An unnamed column is named as PostgreSQL names
>   it, from the select-list item: a function or aggregate call by the function's name, an expression atom that
>   begins with a keyword and is not a literal by that keyword in lower case, every other expression `?column?`.
>   Constructs added later are named by what they are. In a simple query, a column's type is that of its first
>   non-NULL value (text if none).
> * Extended query: Parse (one statement, typed parameters `$n`), Bind (parameter values, result formats), Describe
>   (as DESCRIBE describes the statement, its parameters replaced by NULL for a statement, bound for a portal),
>   Execute (with a row limit), Close, Sync. After an error, messages up to Sync are skipped; Sync closes the
>   portals.
> * COPY (a command of the connection, recognized by its first word): `COPY <table> [(<columns>)] TO STDOUT` sends
>   the rows as EXPORT reads them, and `... FROM STDIN` inserts the rows the client sends as IMPORT inserts them, in
>   PostgreSQL's text format (tab-separated, `\N` for NULL, backslash escapes). A field is read by its column's data
>   type as a text parameter of that type is read; a value of a data type added later as IMPORT reads it.
> * Errors carry a SQLSTATE code by kind: invalid input 42000, permission denied 42501, read-only transaction
>   25006, serialization failure 40001, operation aborted 40003, invalid data XX000, io error 58030.

**Items:**

* The connection type: `A PostgreSQL connection and its session, prepared statements and portals.`
* The function that returns a command tag: `Returns the command tag of a statement result.`
* The function that names a column: `Returns the name of a result column: its header, or the name PostgreSQL gives
  an unnamed expression.`
* The function that reads a COPY field: `Reads a field of COPY FROM STDIN as a value of its column's data type.`

---

## 6. `backup`

### 6.1 `backup` (Rust `backup/mod.rs`, Python `backup/__init__.py`)

**Module doc:**

> Backups of a whole database: created in one transaction (a consistent snapshot), restored in one transaction,
> both by a superuser.
>
> A backup holds the users and memberships, the tables with their owners, check constraints and rows, the views,
> the grants and the policies — not statistics, prepared statements, digests or the logs. A backup can be created as
> of an earlier version. A restore first checks the whole file, then applies its records in order with the effects
> and errors of the corresponding statements; the first error ends it and nothing is restored.

**Items:**

* The function that creates a backup: `Creates a backup of the database the transaction sees.`
* The function that restores a backup: `Restores a backup into the database, in the transaction.`

### 6.2 `backup/format`

**Module doc:**

> The backup file format, version 1, and its listing.
>
> The file is `TOYDBBAK`, a u16 version, records (a u8 kind, a u32 length, the payload), an END record with the
> number of records, and the CRC-32 of everything before it. Integers are big-endian; a string is a u32 length and
> UTF-8.
>
> * A data type is written as its type tag: BOOLEAN 1, INTEGER 2, FLOAT 3, STRING 4. A data type added later gets
>   the next unused tag, in the order the types are added.
> * A value is written as NULL (tag 0) or as its type's tag and a payload: BOOLEAN one byte, INTEGER eight bytes,
>   FLOAT its eight IEEE 754 bytes, STRING a string, a value of a data type added later its display in query results
>   as a string.
> * Records: USER, MEMBER, TABLE, CHECK, ROW, VIEW, MATVIEW, GRANT, POLICY, END, written in this order (tables after
>   the tables they reference, each with its checks and rows; views and materialized views after those they depend
>   on). A USER holds the stored password hash.
>
> Reading checks the header, the version and the CRC before the records; an unknown record kind, type tag or value
> tag is an error.

**Items:**

* The record type: `A backup record.`
* The function that writes a data type: `Writes the type tag of a data type.`
* The function that writes a value: `Writes a value: its type tag and payload, or the NULL tag.`
* The function that reads a data type: `Reads a type tag, erroring on an unknown tag.`
* The function that reads a value: `Reads a value, erroring on an unknown tag.`
* The function that lists a backup: `Returns the lines of toybackup list: one per record, values displayed as in
  query results.`
* The function that compares backups: `Returns the lines of toybackup diff: the records of each identity that is in
  one backup only, or differs, by record kind and identity.`

### 6.3 `bin/toybackup`

**Module doc:**

> toybackup: creates, restores, lists, verifies and compares backups. `create` (optionally `--as-of` a version) and
> `restore` connect to a server (with `--user` and `--password` for a superuser); `list`, `verify` and `diff` read
> files.

---

## 7. `catalog` — `sql/execution/system`

**Module doc:**

> System tables: the catalog, the server's sessions and audit log, and the SQL language, as read-only tables that
> queries read. Their names begin with `toydb_`; a table or view of the same name comes first. They are computed when
> a `SystemTable` plan node is executed, in the statement's transaction, and are not exported or backed up.
>
> * `toydb_tables`, `toydb_columns`, `toydb_views`, `toydb_checks`, `toydb_policies`, `toydb_users`,
>   `toydb_members`, `toydb_grants`, `toydb_statistics`, `toydb_matviews`: the catalog. `toydb_sessions`, `toydb_audit` (superusers
>   only), `toydb_prepared` and `toydb_statements` (the reading session's): as their SHOW statements. `pg_type`:
>   the PostgreSQL types that the wire protocol uses, by OID.
> * `toydb_keywords`: every reserved keyword, also those added later, in upper case, with its category, the first
>   that applies: `statement` (a statement begins with it), `type` (it names a data type in a column definition),
>   `literal` (it is a literal on its own), `operator` (it is a word of an operator of the precedence table, levels
>   1 to 10 — `AND`, `OR`, `NOT`, `LIKE`, `IS` — or of an operator added later at one of those levels), `reserved`
>   (every other keyword).
> * `toydb_functions`: every function and aggregate function that expressions can call, also those added later, by
>   name in lower case, with its kind (`scalar` or `aggregate`) and its number of arguments (one row per number).
> * `toydb_types`: every data type, also those added later, with the keywords that name it in a column definition
>   (its spellings), the PostgreSQL type and OID the wire protocol sends it as, and its backup type tag.

**Items:**

* The function that returns a system table's schema: `Returns the schema of the system table of that name, if
  there is one.`
* The function that computes a system table's rows: `Computes the rows of a system table in the transaction.`
* The function that returns a keyword's category: `Returns the category of a keyword, the first that applies:
  statement, type, literal, operator, reserved.`
* The list of functions: `The functions and aggregate functions that expressions can call, with their kind and
  number of arguments.`

---

## 7a. `matviews`

### 7a.1 `sql/types/matview`

**Module doc:**

> Materialized view definitions, as stored in the catalog: a name, the column names, the canonical one-line text of
> the query and its dependencies, as for a view. The rows are stored separately.

**Items:**

* The definition type: `A materialized view definition, as stored in the catalog.`

### 7a.2 `sql/execution/matviews`

**Module doc:**

> CREATE, REFRESH and DROP MATERIALIZED VIEW, and the dependency checks of materialized views.
>
> * CREATE makes the checks of CREATE VIEW, executes the query and stores its rows; REFRESH executes the stored
>   query again and replaces them. A statement that reads a materialized view reads the stored rows.
> * A table, view or materialized view that a materialized view depends on can't be dropped, after the checks of
>   views, and no statement may change the definition of a table that a materialized view depends on: every
>   statement that changes a table's definition, also one added later, fails with `table <t> is used by
>   materialized view <m>` (the first such materialized view by name) after the views check and before the check
>   constraints' check.

**Items:**

* The function that executes CREATE: `Creates a materialized view: checks it like a view, executes its query and
  stores the rows.`
* The function that executes REFRESH: `Executes a materialized view's query again and replaces its rows.`
* The function that checks a table's dependencies: `Errors if a materialized view depends on the given table.`

---

## 7b. `md5` (Rust only) — `encoding/md5`

**Module doc:**

> MD5 digests (RFC 1321), for PostgreSQL's MD5 password method and the stored form of passwords.

---

## 7c. `pgwire/client`

**Module doc:**

> A PostgreSQL client for the messages the server supports: startup with the MD5 password method, simple and
> extended query, COPY. `toysql --pg` and the test runners use it.

---

## 7d. `profile` — `sql/execution/profile`

**Module doc:**

> Query profiles for `EXPLAIN (ANALYZE)`: executes a planned statement, with its effects, and returns its plan with
> the estimated and the actual number of rows of every line of the plan display.
>
> The statement is executed as when it is issued, except that every plan node is run to completion before its
> parent reads its rows, and the rows each node produces are counted. A write's statement line has the count of its
> result; a statement line without one (also of a plan added later) has 0; every node, also one added later, has the
> rows it produced.

**Items:**

* The function that profiles a statement: `Executes a plan, running every node to completion, and returns the
  number of rows of every line of its plan display, in display order.`

---

## 8. Lists in core modules

Where the base adds a list of all keywords (Rust: `Keyword` has no iteration) or of all data types to a core module,
the list carries the one-line doc comment `All keywords.` / `All data types.`; nothing else.

---

## 9. README sections

Added to both `README.md` files, after the tier-4 extensions:

```markdown
Nine more extensions add users, auditing, constraints, row-level security,
a PostgreSQL protocol, backups, system tables, materialized views and query
profiles:

* **Users and privileges.** `CREATE USER name [PASSWORD 'p'] [SUPERUSER]`,
  `DROP USER`, `GRANT`/`REVOKE` of `SELECT`, `INSERT`, `UPDATE`, `DELETE` on a
  table or view, and of a user (membership), `SET ROLE`, `RESET ROLE`,
  `SHOW USERS`, `SHOW GRANTS`. The creator of a table or view owns it. Every
  statement is checked against the session's current user. The superuser
  `admin` is the user of connections that don't log in.

* **Audit log.** `SHOW AUDIT [SINCE n]` lists the statements a server has
  executed, with their user, class (`ROLE`, `DDL`, `WRITE`, `READ`, `MISC`) and
  the objects they accessed; `SHOW SESSIONS` lists the server's sessions.

* **CHECK constraints.** `CREATE TABLE` accepts table constraints
  `[CONSTRAINT name] CHECK (condition)` and column constraints
  `CHECK (condition)`; every inserted or updated row must satisfy them.

* **Row-level security.** `CREATE POLICY name ON table [TO user] USING
  (condition) [WITH CHECK (condition)]` and `DROP POLICY`: users other than the
  owner read, and write, only the rows of a table with policies for which one
  of their policies is true.

* **PostgreSQL protocol.** Every server also speaks a subset of the PostgreSQL
  protocol 3.0 (startup with an MD5 password, simple and extended query, COPY)
  on `listen_pg` (default `localhost:9605`); `toysql --pg` uses it.

* **Backups.** `toybackup create|restore|list|verify|diff` writes, restores
  and compares checksummed binary backups of the whole database.

* **Materialized views.** `CREATE MATERIALIZED VIEW name [(columns)] AS SELECT
  ...` stores the query's rows, `REFRESH MATERIALIZED VIEW name` recomputes
  them, `DROP MATERIALIZED VIEW [IF EXISTS] name` removes it. Queries read the
  stored rows.

* **System tables.** Queries can read `toydb_tables`, `toydb_columns`,
  `toydb_views`, `toydb_matviews`, `toydb_checks`, `toydb_policies`, `toydb_users`,
  `toydb_members`, `toydb_grants`, `toydb_statistics`, `toydb_sessions`,
  `toydb_audit`, `toydb_prepared`, `toydb_statements`, `toydb_keywords`,
  `toydb_functions`, `toydb_types` and `pg_type`.

* **Query profiles.** `EXPLAIN (ANALYZE) statement` executes the statement and
  returns its plan with the estimated and the actual number of rows of every
  node.

`AUDIT`, `CHECK`, `CONSTRAINT`, `GRANT`, `GRANTS`, `PASSWORD`, `POLICY`, `RESET`,
`REVOKE`, `ROLE`, `SESSIONS`, `SUPERUSER`, `TO`, `USER`, `USERS`, `USING`,
`WITH`, `MATERIALIZED` and `REFRESH` are reserved keywords. The golden scripts of these extensions are in the `ext5/`
script directories.
```

In the README's layout table, each workspace lists its `ext5/` script directories next to `ext4/`, and the binaries
list `toybackup`.

---

## 10. Doc comments of additions to existing modules

One line each, as in the surrounding code.

| Addition | Doc comment |
|---|---|
| keywords (lexer) | none (like the existing keywords) |
| AST EXPLAIN (ANALYZE) | `Whether to execute the statement and count the rows of every node (EXPLAIN (ANALYZE)).` |
| AST column CHECK constraints | `The column's CHECK constraints, in the order written.` |
| AST materialized view statements | `CREATE MATERIALIZED VIEW: creates a materialized view.`, `REFRESH MATERIALIZED VIEW: recomputes a materialized view's rows.`, `DROP MATERIALIZED VIEW: drops a materialized view.` |
| plan node MaterializedView | `A materialized view: emits its stored rows. The alias is only used for formatting.` |
| AST statements | `CREATE USER: creates a user.`, `DROP USER: drops a user.`, `GRANT: grants privileges on a table or view, or makes a user a member of another.`, `REVOKE: revokes privileges on a table or view, or ends a membership.`, `SET ROLE: sets the session's current user.`, `RESET ROLE: resets the session's current user to its login user.`, `SHOW USERS: lists the users.`, `SHOW GRANTS: lists the owners and privileges of tables and views.`, `SHOW AUDIT: lists the server's audit log.`, `SHOW SESSIONS: lists the server's sessions.`, `CREATE POLICY: creates a row-level security policy.`, `DROP POLICY: drops a row-level security policy.` |
| AST CREATE TABLE field | `The table's CHECK constraints, in the order written.` (constraint: `A CHECK constraint: its name, if given, and its condition.`) |
| plan node SystemTable | `A system table: emits the rows of a system table, computed when the node is executed. The alias is only used for formatting.` |
| statement results | none (like the existing results) |
| error kind PermissionDenied | `The current user may not do what the statement requires.` |
| catalog methods | `Creates a user.`, `Drops a user, its memberships and privileges.`, `Fetches a user, or None if it doesn't exist.`, `Returns all users.`, `Sets or removes a membership.`, `Returns all memberships.`, `Sets the privileges granted to a user on an object.`, `Returns all grants.`, `Sets the owner of an object.`, `Fetches the owner of an object (admin if none is stored).`, `Sets the CHECK constraints of a table.`, `Fetches the CHECK constraints of a table.`, `Creates a policy.`, `Drops a policy. Returns true if it existed.`, `Returns the policies of a table, by name.`, `Creates a materialized view with its rows.`, `Replaces the rows of a materialized view.`, `Drops a materialized view. Returns true if it existed.`, `Fetches a materialized view definition, or None if it doesn't exist.`, `Returns the stored rows of a materialized view.`, `Returns all materialized view definitions.` |
| local engine keys | `A materialized view definition, by name.`, `A materialized view's stored row, by name and position.`, `A user, by name.`, `A membership, by member and user.`, `The privileges granted on an object to a user.`, `The owner of an object, if it isn't admin.`, `A table's CHECK constraints, by table name.`, `A policy, by table and policy name.` |
| Raft write and read commands | none (like the existing commands) |
| client protocol requests | `Logs in as a user.`, `Creates a backup.`, `Restores a backup.` |
| the session's users | `The login user.`, `The current user.` |
| the engine's audit log and sessions | `The audit log of the server's sessions.`, `The server's open sessions.` |
| server configuration | `listen_pg`: `Address to listen for PostgreSQL connections on.` (in `config/toydb.yaml`, as the other addresses) |
