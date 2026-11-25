# Tier 5 extension layers — specification

Tier 5 is the complete tier-4 toydb (SQL engine, Raft, server, client, binaries, the four tier-3 extensions of
`tiers/t3/EXT_SPEC.md` and the seven tier-4 extensions of `tiers/t4/EXT4_SPEC.md`) plus nine more **extension
layers**. Each is a realistic subsystem in modules of its own, wired into the real system — the SQL session, the
catalog and the Raft SQL engine, the server, the client library, the binaries — and reachable from SQL, the server's
network protocols or a binary, and so from both golden-script runners:

| § | Extension (id) | Reached through | Depends on |
|---|---|---|---|
| 2 | **Users and privileges** (`users`) — users, group membership, login, ownership, table privileges, a privilege check for every statement | `CREATE USER`, `DROP USER`, `GRANT`, `REVOKE`, `SET ROLE`, `RESET ROLE`, `SHOW USERS`, `SHOW GRANTS`; login over the client protocol | – |
| 3 | **Audit log** (`audit`) — every statement a server executes, with its user, class and the objects it accesses; the server's sessions | `SHOW AUDIT`, `SHOW SESSIONS` | 2 |
| 4 | **CHECK constraints** (`checks`) — named table conditions, stored with the catalog and enforced on every write | `CHECK` / `CONSTRAINT ... CHECK` in `CREATE TABLE` | – |
| 5 | **Row-level security policies** (`policies`) — per-user conditions that restrict which rows of a table a user reads | `CREATE POLICY`, `DROP POLICY` | 2, 4 |
| 6 | **PostgreSQL wire protocol** (`pgwire`) — a second network protocol, a subset of PostgreSQL's protocol 3.0: startup and password authentication, simple and extended query, type OIDs, text and binary encodings, command tags, error codes | the server's PostgreSQL port; the runners' `pg` command | 2, 3 |
| 7 | **Backup and restore** (`backup`) — a versioned, checksummed binary format of the whole database, the `toybackup` binary | the client protocol; `toybackup`; the runners' `backup` commands | 2, 4, 5 |
| 8 | **System tables** (`catalog`) — the catalog, the server's sessions and audit log, and the SQL language itself as tables that queries read | `SELECT ... FROM toydb_tables`, `toydb_columns`, `toydb_keywords`, `toydb_types`, ... | 2–7 |
| 9 | **Materialized views** (`matviews`) — stored query results, refreshed on demand, with dependency checks | `CREATE MATERIALIZED VIEW`, `REFRESH MATERIALIZED VIEW`, `DROP MATERIALIZED VIEW`, materialized views in `FROM` | 2, 3, 7, 8 |
| 10 | **Query profiles** (`profile`) — executes a statement and returns its plan with the estimated and the actual number of rows of every node | `EXPLAIN (ANALYZE)` | 2, 3 |

The ids name the script directories `ext5_scripts/<id>/`; the order of the table is the build order (an extension
may use the ones above it).

This document is language-neutral. Sections 1–11 define the user-facing behaviour completely; the extensions'
documentation text (`EXT5_DOCS.md`) is derived from them. Section 12 lists the requirements for implementing the
tier-5 base; it is not user documentation. References of the form *EXT_SPEC 3.2* are to `tiers/t3/EXT_SPEC.md`,
*EXT4 3.3* to `tiers/t4/EXT4_SPEC.md`; both stay in force unchanged except where section 11 extends them.

**Generic rules.** As in tiers 3 and 4, every output is defined by rules stated in terms of SQL syntax, values, data
types, statement results, the plan display and the schema display — not by a closed list of cases. Where a rule lists
the existing cases, it also says how a case added to toydb later (a data type and its values, a kind of literal, an
expression, operator, function or aggregate function, a plan node, a statement, a statement result, a statement that
changes the definition of a table, a keyword) is handled, and the handling of such an addition follows from these
rules together with the addition's own definition. The tier-5 rules are **mechanical**: for an addition, the
correct behaviour of each tier-5 layer is a direct consequence of the layer's rule and the addition's definition
(its data type name, its value display, its syntax and keywords, its statement name, its precedence, what it reads
and writes), never a matter of judgment.

**Conventions.** As in EXT4: examples use the golden-script conventions of the SQL runner (`> statement`, the
output below `---`, `Error: <message>`); *displayed as in query results* means the value display of result rows
(`NULL`, `TRUE`, `1`, `-0.5`, `1e100`, `NaN`, `inf`, `'text'`); the *schema display* is the `CREATE TABLE` text of
the `schema` command, and its *one-line form* is its lines without indentation joined by single spaces, without a
space after the opening `(` and before the closing `)` (as in the change log, EXT4 8.1). Error messages are
complete output lines. "Code points" order compares names character by character by Unicode code point.

---

## 1. Shared definitions

### 1.1 New statements and keywords

```
CREATE USER <name> [PASSWORD '<password>'] [SUPERUSER]
DROP USER [IF EXISTS] <name>
GRANT <privilege> [, <privilege> ...] ON <table or view> TO <user>
REVOKE <privilege> [, <privilege> ...] ON <table or view> FROM <user>
GRANT <user> TO <user>
REVOKE <user> FROM <user>
SET ROLE <user>
RESET ROLE
SHOW USERS
SHOW GRANTS
SHOW AUDIT [SINCE <number>]
SHOW SESSIONS
CREATE TABLE <name> ( <column or table constraint>, ... )      -- table constraints: [CONSTRAINT <name>] CHECK ( <expression> )
CREATE POLICY <name> ON <table> [TO <user>] USING ( <expression> ) [WITH CHECK ( <expression> )]
DROP POLICY [IF EXISTS] <name> ON <table>
CREATE MATERIALIZED VIEW <name> [( <column>, ... )] AS <query>
REFRESH MATERIALIZED VIEW <name>
DROP MATERIALIZED VIEW [IF EXISTS] <name>
EXPLAIN (ANALYZE) <statement>
```

In a column definition, `CHECK ( <expression> )` is a further column constraint. `COPY` (section 6.6) is a command of
the PostgreSQL protocol, not a SQL statement.

`<privilege>` is one of the keywords `SELECT`, `INSERT`, `UPDATE`, `DELETE`. `AUDIT`, `CHECK`, `CONSTRAINT`,
`GRANT`, `GRANTS`, `PASSWORD`, `POLICY`, `RESET`, `REVOKE`, `ROLE`, `SESSIONS`, `SUPERUSER`, `TO`, `USER`, `USERS`,
`USING`, `WITH`, `MATERIALIZED` and `REFRESH` are reserved keywords with the same rules as the existing ones (EXT_SPEC 7.2): case-insensitive, not
usable as unquoted identifiers (`CREATE TABLE user (id INT PRIMARY KEY)` →
`Error: invalid input: expected identifier, got USER`), valid when double-quoted, quoted by the schema display.
Names that merely contain them (`users_old`, `checked`) are unaffected. Names of users and policies are identifiers
with the existing rules (unquoted names are lower case). The parser's messages follow the existing rules (section 11).

### 1.2 Statement names

The **statement name** of a statement is the name EXT_SPEC 2.1 defines: its leading keywords in lower case joined by
`_`, without optional words and operands. For the existing statements and those of this tier:

| Statement | Name | Statement | Name |
|---|---|---|---|
| `SELECT` | `select` | `CREATE VIEW` | `create_view` |
| `INSERT` | `insert` | `DROP VIEW` | `drop_view` |
| `UPDATE` | `update` | `SHOW VIEWS` | `show_views` |
| `DELETE` | `delete` | `DESCRIBE` | `describe` |
| `CREATE TABLE` | `create_table` | `ESTIMATE` | `estimate` |
| `DROP TABLE` | `drop_table` | `PREPARE` | `prepare` |
| `BEGIN`, `COMMIT`, `ROLLBACK` | `begin`, `commit`, `rollback` | `EXECUTE` | `execute` |
| `EXPLAIN`, `EXPLAIN (ANALYZE)` | `explain` | `DEALLOCATE` | `deallocate` |
| `SHOW STATEMENTS` | `show_statements` | `SHOW PREPARED` | `show_prepared` |
| `ANALYZE` | `analyze` | `LINT` | `lint` |
| `SHOW STATISTICS` | `show_statistics` | `SHOW CHANGES` | `show_changes` |
| `EXPORT` | `export` | `CREATE USER`, `DROP USER` | `create_user`, `drop_user` |
| `IMPORT` | `import` | `GRANT`, `REVOKE` (both forms) | `grant`, `revoke` |
| `FORMAT` | `format` | `SET ROLE`, `RESET ROLE` | `set_role`, `reset_role` |
| `SHOW USERS`, `SHOW GRANTS` | `show_users`, `show_grants` | `SHOW AUDIT`, `SHOW SESSIONS` | `show_audit`, `show_sessions` |
| `CREATE POLICY`, `DROP POLICY` | `create_policy`, `drop_policy` | `CREATE`, `REFRESH`, `DROP MATERIALIZED VIEW` | `create_materialized_view`, `refresh_materialized_view`, `drop_materialized_view` |
| a statement `FOO BAR t ...` added later | `foo_bar` | | |

### 1.3 Accesses

Privileges (section 2) and the audit log (section 3) are defined in terms of the **accesses** of a statement: a list
of (name, kind) pairs, naming the tables and views the statement uses and how. The kinds are:

| Kind | The statement ... |
|---|---|
| `read` | reads the rows (or the statistics) of the table or view |
| `insert`, `update`, `delete` | inserts, updates or deletes rows of the table |
| `create` | creates the table or view |
| `drop` | drops the table or view |
| `alter` | changes the definition of the table: adds, drops or renames columns or otherwise changes what its schema display shows |
| `policy` | creates or drops a row-level security policy of the table |
| `analyze` | stores statistics of the table |
| `refresh` | recomputes the rows of the materialized view |
| `grant` | grants or revokes privileges on the table or view |

| Statement | Accesses |
|---|---|
| `SELECT` | `read` of every table, view and materialized view named in its FROM clause (FROM items left to right, joins as written) |
| `INSERT INTO t ...` | `insert` of `t` |
| `UPDATE t ...`, `DELETE FROM t ...` | `update` of `t`, `delete` of `t` |
| `CREATE TABLE t ...` | `create` of `t` |
| `DROP TABLE [IF EXISTS] t` | `drop` of `t` |
| `CREATE VIEW v ... AS <query>` | `create` of `v`, then `read` of every table and view named in the query's FROM clause |
| `DROP VIEW [IF EXISTS] v` | `drop` of `v` |
| `ANALYZE t`; `ANALYZE` | `analyze` of `t`; `analyze` of every table, by name (code points) |
| `SHOW STATISTICS t`, `EXPORT t AS ...` | `read` of `t` |
| `EXPORT AS SQL` | `read` of every table and view, by name (code points) |
| `IMPORT INTO t ...` | `insert` of `t` |
| `EXPLAIN s`, `EXPLAIN (ANALYZE) s`, `DESCRIBE s`, `ESTIMATE s` (a statement `s`) | the accesses of `s` |
| `EXECUTE p (...)` | the accesses of the prepared statement with its arguments bound (EXT4 6.3) |
| `GRANT ... ON t ...`, `REVOKE ... ON t ...` | `grant` of `t` |
| `CREATE POLICY ... ON t ...`, `DROP POLICY ... ON t` | `policy` of `t` |
| `CREATE MATERIALIZED VIEW m ... AS <query>` | `create` of `m`, then `read` of every table, view and materialized view named in the query's FROM clause |
| `REFRESH MATERIALIZED VIEW m`, `DROP MATERIALIZED VIEW [IF EXISTS] m` | `refresh` of `m`, `drop` of `m` |
| every other existing statement (`BEGIN`, `COMMIT`, `ROLLBACK`, `FORMAT`, `LINT`, `PREPARE`, `DEALLOCATE`, `DESCRIBE <name>`, all `SHOW` statements other than `SHOW STATISTICS`, `CREATE USER`, `DROP USER`, `GRANT`/`REVOKE` of a user, `SET ROLE`, `RESET ROLE`) | none |

**Statements added later** have the accesses that follow from what they do: `read` of every table and view named in
a FROM clause of a query they contain and execute (at any depth), `insert`, `update` or `delete` of a table whose rows
they insert, update or delete, `create` or `drop` of a table or view they create or drop, `alter` of a table whose
definition they change. A part of an existing statement added later (a clause, an option, a query in place of a
`VALUES` list) adds the accesses that follow from what it does in the same way.

The accesses are listed in the order in which their names appear in the statement's canonical text (EXT4 1.5), each
(name, kind) pair once; for the same position, in the order of the kinds table above (`CREATE VIEW v AS SELECT * FROM
v2` lists `v` (create), `v2` (read)). Names are as stored (unquoted).

### 1.4 Results, runners, transactions

* `SHOW USERS`, `SHOW GRANTS`, `SHOW AUDIT` and `SHOW SESSIONS` return ordinary query results with the columns given
  in their sections (unqualified labels): the runners print their rows, `[header]` shows the names, the JSON form is a
  `select` result, SHOW STATEMENTS counts their rows.
* The other new statements print nothing without `[json]`; their JSON forms (EXT_SPEC 2.1) are:

  | Statement | JSON |
  |---|---|
  | `CREATE USER u` | `{"result":"create_user","user":"u"}` |
  | `DROP USER [IF EXISTS] u` | `{"result":"drop_user","user":"u","existed":<bool>}` |
  | `GRANT ... ON t TO u`, `REVOKE ... ON t FROM u` | `{"result":"grant","table":"t"}`, `{"result":"revoke","table":"t"}` |
  | `GRANT g TO u`, `REVOKE g FROM u` | `{"result":"grant"}`, `{"result":"revoke"}` |
  | `SET ROLE u`, `RESET ROLE` | `{"result":"set_role","user":<the new current user>}`, `{"result":"reset_role","user":<the new current user>}` |
  | `CREATE POLICY p ON t ...` | `{"result":"create_policy","table":"t","policy":"p"}` |
  | `DROP POLICY [IF EXISTS] p ON t` | `{"result":"drop_policy","table":"t","policy":"p","existed":<bool>}` |
  | `CREATE MATERIALIZED VIEW m ...` | `{"result":"create_materialized_view","view":"m","count":<rows stored>}` |
  | `REFRESH MATERIALIZED VIEW m` | `{"result":"refresh_materialized_view","view":"m","count":<rows stored>}` |
  | `DROP MATERIALIZED VIEW [IF EXISTS] m` | `{"result":"drop_materialized_view","view":"m","existed":<bool>}` |

* Transactions:

  | Statement | Outside an explicit transaction | Inside one |
  |---|---|---|
  | `CREATE USER`, `DROP USER`, `GRANT`, `REVOKE`, `CREATE POLICY`, `DROP POLICY`, `CREATE`, `REFRESH` and `DROP MATERIALIZED VIEW` | an implicit read-write transaction (a version is allocated, also when the statement fails) | the session's |
  | `SHOW USERS`, `SHOW GRANTS` | an implicit read-only transaction | the session's |
  | `SET ROLE`, `RESET ROLE`, `SHOW AUDIT`, `SHOW SESSIONS` | no transaction (the catalog reads of their checks use an implicit read-only one) | no transaction (the checks read the catalog in the session's transaction) |
  | `EXPLAIN (ANALYZE) s` | that of `s`, exactly as if `s` were issued | the session's |

  Users, memberships, grants, ownership, check constraints and policies are part of the catalog and transactional
  exactly like table definitions and views (EXT4 3.3): seen by their own transaction at once, by others after commit
  under snapshot isolation, undone by ROLLBACK, replicated through Raft in a cluster; two transactions that write the
  same object concurrently conflict (`Error: serialization failure, retry transaction`). The session's current user
  (section 2.6) is not transactional.

### 1.5 The tier-3 and tier-4 extensions

* **Digests** (EXT_SPEC 3.2): every new statement has a digest by the existing rules, with its keywords, names and
  operands in the order of its syntax, literals as `?` (the password of `CREATE USER` included):
  `CREATE USER alice PASSWORD ? SUPERUSER`, `GRANT SELECT, INSERT ON t TO alice`, `SHOW AUDIT SINCE ?`,
  `CREATE POLICY p ON t TO alice USING (id > ?) WITH CHECK (id > ?)`, `EXPLAIN (ANALYZE) SELECT * FROM t`.
  `CREATE TABLE` writes its items in the order written, a table constraint as `CHECK (<expr>)` or
  `CONSTRAINT <name> CHECK (<expr>)`, and a column's CHECK constraints last among its constraints, in the order
  written: `CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER DEFAULT ? CHECK (n != ?), CHECK (n > ?), CONSTRAINT
  small CHECK (n < ?))`.
* **FORMAT** (EXT4 2.1): every new statement is one line, written by the same principles (literals by EXT4 1.4,
  names by EXT4 1.3): `CREATE USER alice PASSWORD 'it''s' SUPERUSER`, `REVOKE DELETE ON t FROM "Bob"`,
  `CREATE POLICY p ON t USING (id > 1) WITH CHECK (id > 2)`. In `CREATE TABLE`, a table constraint is an item of the
  column list, on a line of its own like a column: `  CHECK (n > 0)`, `  CONSTRAINT small CHECK (n < 100)`; a column's
  CHECK constraints come last on its line (`  n INTEGER DEFAULT 1 CHECK (n != 0)`). `EXPLAIN (ANALYZE)` is a line of
  its own followed by the lines of the statement, like `EXPLAIN`.
* **JSON** (EXT_SPEC 2): the results of 1.4.
* **"Not supported"** (EXT4 1.2): none of the new statements is a planned statement. `EXPLAIN`, `EXPLAIN (ANALYZE)`,
  `DESCRIBE`, `ESTIMATE` and `PREPARE` of them fail with `... is not supported for <KEYWORD>`, `<KEYWORD>` being `CREATE USER`,
  `DROP USER`, `GRANT`, `REVOKE`, `SET`, `RESET`, `SHOW`, `CREATE POLICY`, `DROP POLICY`, `CREATE MATERIALIZED`,
  `REFRESH` or `DROP MATERIALIZED`.
* **LINT** (EXT4 7): the new statements have no statement-level rules; the conditions of `CHECK` constraints and of
  `CREATE POLICY` (USING and WITH CHECK) are expressions of the statement and are checked by the expression rules, except
  `constant_condition` (which concerns WHERE, HAVING and ON conditions only). Their column references are not
  resolved (they are neither FROM items nor the table of an UPDATE or DELETE), so `type_mismatch` doesn't apply to
  them.
* **Change log** (EXT4 8): changes of users, memberships, grants, ownership, check constraints and policies are not
  recorded. A `CREATE TABLE` with check constraints records its `create_table` change as before (the schema display
  doesn't show check constraints).
* **EXPORT AS SQL** (EXT_SPEC 6.1): the CREATE TABLE lines of a table with check constraints list them after the
  columns, one per line, always named: `  CONSTRAINT <name> CHECK (<condition>)` (names by EXT_SPEC 1.6, the condition
  as stored, 4.2), with `,` after every column and constraint line but the last. Users, grants and policies are not
  exported (they are part of backups, section 7).
* **SHOW CHANGES**, **SHOW STATEMENTS**, **SHOW PREPARED** and the other tier-3/4 statements are otherwise unchanged;
  what each requires of the current user is in 2.5.

---

## 2. Extension: Users and privileges

### 2.1 Users

A **user** has a name, a `superuser` flag and optionally a password, which is stored as PostgreSQL stores an MD5
password: `md5` followed by the 32 lower-case hex digits of the MD5 digest of the password followed by the user name
(the password `pw` of `alice` is stored as `md5` and the MD5 of `pwalice`). The password itself is not stored. The user **`admin`** always exists: it is a
superuser without a password; it can't be created, dropped or given a password, and it is not stored (a database
without other users stores nothing new). Users can be **members** of other users (as of groups): a member *acts for*
the user it is a member of. A user acts for itself, for every user it is a member of, and, transitively, for every
user those act for.

```
CREATE USER <name> [PASSWORD '<password>'] [SUPERUSER]
```

creates a user. `PASSWORD` and `SUPERUSER` may each be given at most once, in this order. Checks, in order:

1. The current user must be a superuser (2.5).
2. `Error: invalid input: user name can't be empty` (`CREATE USER ""`).
3. `Error: invalid input: user <name> already exists` (also for `admin`).
4. In a read-only transaction: `Error: read-only transaction`.

```
DROP USER [IF EXISTS] <name>
```

drops a user, its memberships (both as a member and as a user others are members of) and the privileges granted to
it. Checks, in order: the current user must be a superuser; the user must exist
(`Error: invalid input: user <name> does not exist`; with `IF EXISTS` the statement then does nothing);
`Error: invalid input: user admin can't be dropped`; `Error: invalid input: can't drop the current user <name>` (the
current user or the login user of the executing session); `Error: invalid input: user <name> owns <object>` if it
owns a table, view or materialized view (`<object>` the first by name); `Error: invalid input: user <name> is used by policy <p> on
table <t>` if a policy names it (the first by table name, then policy name); in a read-only transaction
`Error: read-only transaction`.

### 2.2 Ownership

Every table, view and materialized view has an **owner**: the current user of the session that created it. Objects created by `admin`
are owned by `admin`, and ownership by `admin` is not stored. The owner of an object (and every user that acts for
the owner) may do everything with it: read and write its rows, drop it, change its definition, analyze it, create and
drop its policies, grant and revoke privileges on it. Dropping an object removes its ownership and the privileges
granted on it. A statement added later that creates a table or view makes the current user its owner; one that
changes a table's definition doesn't change its owner or the privileges granted on it. A `CREATE TABLE` that doesn't
create a table (for example because of an option added later that makes it do nothing when the table exists)
doesn't change the existing table's owner.

### 2.3 Privileges

```
GRANT <privilege> [, <privilege> ...] ON <name> TO <user>
REVOKE <privilege> [, <privilege> ...] ON <name> FROM <user>
```

grant or revoke privileges on a table, view or materialized view (section 9). The privileges are `SELECT` (read its
rows), `INSERT`, `UPDATE` and `DELETE` (insert, update, delete its rows); on a view or materialized view only `SELECT`
can be granted (`only SELECT can be granted on view <name>`, for both). A privilege may be listed more
than once. Granting a privilege the user already has, or revoking one it doesn't have, does nothing (it writes
nothing, so it also succeeds in a read-only transaction once the checks before the read-only one pass). Checks, in
order: the current user must act for the object's owner (or be a superuser) — the `grant` access of 1.3;
`Error: invalid input: table <name> does not exist` (neither a table nor a view nor a materialized view);
`Error: invalid input: only SELECT can be granted on view <name>` (another privilege on a view);
`Error: invalid input: user <user> does not exist`; in a read-only transaction `Error: read-only transaction`.

```
GRANT <group> TO <user>
REVOKE <group> FROM <user>
```

make `<user>` a member of `<group>`, or remove the membership (doing nothing, and writing nothing, if it is already,
or not, a member).
Checks, in order: the current user must be a superuser; both users must exist (`Error: invalid input: user <name>
does not exist`, `<group>` first); for GRANT, `Error: invalid input: user <name> can't be a member of itself` (the
two names are equal) and `Error: invalid input: granting <group> to <user> would create a cycle` (`<group>` already
acts for `<user>`); in a read-only transaction `Error: read-only transaction`. Superuser status is not passed on
through membership: a member of a superuser is not a superuser.

`GRANT` and `REVOKE` tell the two forms apart by the token after the keyword: one of the four privilege keywords
starts the privilege form, an identifier the membership form.

### 2.4 What a user may do

A user **has a privilege** on a table or view if it is a superuser, if it acts for the object's owner, or if it acts
for a user the privilege was granted to.

### 2.5 Privilege checks

Every statement is checked against the session's current user after it is parsed and after the checks of EXT4 1.2
and 6.1, before anything else (for `EXECUTE`, after its arguments are checked and bound, EXT4 6.3), in the statement's
transaction (the session's, or the statement's implicit one, which is begun first; for statements that use no
transaction, an implicit read-only one). Each requirement is checked in order and the first that fails is reported;
requirements concern the statement's accesses (1.3), in their order, and then the statement's own requirement:

| Access kind | Required |
|---|---|
| `read` | the `SELECT` privilege |
| `insert`, `update`, `delete` | the `INSERT`, `UPDATE`, `DELETE` privilege |
| `create` | nothing (the current user becomes the owner) |
| `drop`, `alter`, `policy`, `analyze`, `grant`, `refresh` | acting for the owner (or being a superuser) |

| Statement | Own requirement |
|---|---|
| `CREATE USER`, `DROP USER`, `GRANT <group> TO <user>`, `REVOKE <group> FROM <user>`, `SHOW CHANGES`, `SHOW AUDIT`, creating and restoring backups (section 7) | the current user is a superuser |
| `SET ROLE <user>` | the login user is a superuser or acts for `<user>` |
| every other statement | none |

An access to a name that is neither a table nor a view nor a materialized view, as the statement's transaction sees
it, is not checked (the
statement then reports its own error, such as `table nosuch does not exist`); neither is the `create` access. The
messages, with `<u>` the current user and `<name>` the object's name as stored:

```
Error: permission denied: user <u> has no <PRIVILEGE> privilege on <name>
Error: permission denied: user <u> is not the owner of <name>
Error: permission denied: user <u> is not a superuser
Error: permission denied: user <login> can't set role <user>
```

(`permission denied` errors are a new kind of error, like `read-only transaction`: they have no `invalid input:`
prefix.) Reading a view requires the `SELECT` privilege on the view only, not on the tables its query reads. An
`EXECUTE` that fails the check has passed the checks of EXT4 6.3: SHOW PREPARED counts it as an execution, but not
as a planning.

```
> CREATE TABLE t (id INT PRIMARY KEY, v STRING)
> CREATE USER alice
> GRANT SELECT ON t TO alice
> SET ROLE alice
> SELECT * FROM t
!> INSERT INTO t VALUES (1, 'a')
!> DROP TABLE t
---
Error: permission denied: user alice has no INSERT privilege on t
Error: permission denied: user alice is not the owner of t
```

### 2.6 The session's user, SET ROLE, login

Every session has a **login user** and a **current user**. Sessions of the SQL runner, and connections of the
client protocol that don't log in, have `admin` as both. Statements are executed as the current user.

* `SET ROLE <user>` makes `<user>` the current user: `Error: invalid input: user <user> does not exist`, then the
  check of 2.5. `RESET ROLE` makes the login user the current user again. Neither is transactional nor undone by
  ROLLBACK, and both work inside transactions.
* **Login** over the client protocol: a connection may authenticate once, before its first statement, with a user
  name and an optional password; it then has that user as login and current user.
  `Error: invalid input: user <u> does not exist`; `Error: permission denied: password authentication failed for
  user <u>` (a user with a password logs in only with that password; a user without one only without a password).
  A failed login can be retried; after a successful login, or after the connection's first statement (also one that
  fails or fails to parse), a login fails
  with `Error: invalid input: session already started`. The client library has a function to log in; `toysql` and
  `toybackup` take `--user` and `--password` options; the cluster runner has the command
  `login <user> [password=<p>]` (for the prefix's client, which it connects first if needed).
* A user that is dropped while it is the current or login user of another session stays that session's user: the
  session then has no privileges beyond those of a user that doesn't act for anyone.

### 2.7 SHOW USERS and SHOW GRANTS

`SHOW USERS` returns one row per user, `admin` included, ordered by name, with the columns `name` (STRING),
`is_superuser` (BOOLEAN), `has_password` (BOOLEAN) and `member_of` (STRING: the users it is a direct member of,
ordered by name, separated by `, `; `''` if none).

`SHOW GRANTS` returns one row per privilege of every table, view and materialized view, with the columns `object`, `grantee` and
`privilege` (STRING): a row `<object>, <owner>, 'OWNER'` for its owner, and a row for every granted privilege
(`'SELECT'`, `'INSERT'`, `'UPDATE'`, `'DELETE'`). The rows are ordered by object name, then by privilege in the order
`OWNER`, `SELECT`, `INSERT`, `UPDATE`, `DELETE`, then by grantee name. Both statements require nothing of the user.

```
> RESET ROLE
> CREATE USER bob PASSWORD 'pw'
> GRANT INSERT, SELECT ON t TO bob
> GRANT alice TO bob
> SHOW USERS
> SHOW GRANTS
---
'admin', TRUE, FALSE, ''
'alice', FALSE, FALSE, ''
'bob', FALSE, TRUE, 'alice'
't', 'admin', 'OWNER'
't', 'alice', 'SELECT'
't', 'bob', 'SELECT'
't', 'bob', 'INSERT'
```

---

## 3. Extension: Audit log

Every server keeps an **audit log** of the statements executed by its sessions, and a list of its open sessions.
(The log doesn't record which session executed a statement: SHOW SESSIONS shows the sessions.)
Both are kept in memory from the time the server (in the SQL runner: the test engine) started, are not written to
storage, are lost on restart and are not replicated: in a cluster, each node logs the statements of the sessions
connected to it.

### 3.1 Sessions

Every session of a server has a **number**: 1 for the first session the server created, 2 for the next, and so on.
The SQL runner creates the session of a prefix when the prefix is first used by any command (also `schema`, `dump`
and the commands of sections 6 and 7); a client connection's session is created when the connection is accepted; a
PostgreSQL connection (section 6) has a session of its own, created when its startup message has been accepted.

`SHOW SESSIONS` returns one row per open session of the server, ordered by number, with the columns `session`
(INTEGER), `username` (STRING: the current user), `login` (STRING: the login user), `txn` (INTEGER: the version of
the session's open transaction, NULL if none) and `executed` (INTEGER: the number of statements the session executed
before this one, as counted by the audit log). It uses no transaction and requires nothing. A session ends, and leaves
the list, when its connection closes (client `close`, a PostgreSQL `Terminate`); sessions of the SQL runner never end.

### 3.2 The log

A statement is recorded when it has been executed, successfully or not, if it was parsed; statements that fail to
parse, and `SHOW AUDIT` itself, are never recorded. Each entry has:

| Column | Type | Value |
|---|---|---|
| `seq` | INTEGER | 1 for the server's first entry, then increasing by one |
| `username` | STRING | the session's current user when the statement began (`SET ROLE alice` is recorded with the user it was executed by) |
| `class` | STRING | `ROLE`, `DDL`, `WRITE`, `READ` or `MISC`, below |
| `statement` | STRING | the statement name (1.2) |
| `objects` | STRING | the statement's accesses (1.3), each written `<name> (<kind>)`, separated by `, `; `''` if it has none |
| `error` | STRING | NULL if the statement succeeded, otherwise its error message as displayed after `Error: ` (`invalid input: table x does not exist`, `permission denied: ...`) |

The **class** of a statement is determined by its accesses and its kind, as the first of these that applies:

1. `ROLE` — `CREATE USER`, `DROP USER`, `GRANT`, `REVOKE` (both forms);
2. `DDL` — it has a `create`, `drop`, `alter` or `policy` access;
3. `WRITE` — it has an `insert`, `update`, `delete` or `refresh` access;
4. `READ` — it has a `read` access;
5. `MISC` — every other statement.

So `CREATE VIEW v AS SELECT * FROM t` is `DDL` with objects `v (create), t (read)`, `EXPLAIN SELECT * FROM t` is
`READ`, `ANALYZE t` is `MISC` with objects `t (analyze)`, an `EXECUTE` has the class and objects of the statement it
executes (its `statement` is `execute`), and an `EXECUTE` that fails before its statement is bound (an unknown
prepared statement, wrong arguments) is `MISC` without objects. A statement added later gets its class from its
accesses by the same rule. The accesses are those of the statement as parsed (and bound), whether or not it
succeeded, also for names that don't exist (`SELECT * FROM nosuch` is `READ`, `nosuch (read)`).

### 3.3 SHOW AUDIT

```
SHOW AUDIT [SINCE <number>]
```

returns the log, oldest first, with the columns of 3.2; with `SINCE <n>` (a non-negative integer literal) only the
entries with a `seq` above `<n>`. It requires a superuser (2.5) and uses no transaction. Parse errors as for
`SHOW CHANGES SINCE` (EXT4 8.1).

```
> CREATE TABLE t (id INT PRIMARY KEY)
> CREATE USER alice
> SET ROLE alice
!> INSERT INTO t VALUES (1)
> RESET ROLE
> SHOW AUDIT
---
Error: permission denied: user alice has no INSERT privilege on t
1, 'admin', 'DDL', 'create_table', 't (create)', NULL
2, 'admin', 'ROLE', 'create_user', '', NULL
3, 'admin', 'MISC', 'set_role', '', NULL
4, 'alice', 'WRITE', 'insert', 't (insert)', 'permission denied: user alice has no INSERT privilege on t'
5, 'alice', 'MISC', 'reset_role', '', NULL
```

---

## 4. Extension: CHECK constraints

### 4.1 Syntax and names

In `CREATE TABLE`, the parenthesized list may contain **table constraints** before, between and after the column
definitions:

```
CHECK ( <condition> )
CONSTRAINT <name> CHECK ( <condition> )
```

and a column definition may have **column constraints** `CHECK ( <condition> )` among its other constraints (any
number of them). A column constraint is a constraint of the table like a table constraint, unnamed, and its condition
may use every column of the table. The condition is an expression, read up to the matching `)`. The constraints of a
table are ordered as written, column and table constraints together. A constraint without a name is named
`<table>_check<i>`, where `<i>` is its position among all the CHECK constraints of the statement, counting from 1
(`CREATE TABLE t (id INT PRIMARY KEY CHECK (id != 5), CHECK (id > 0), CONSTRAINT c CHECK (id < 9))` has the
constraints `t_check1`, `t_check2`, `c`).

### 4.2 Creating a table with constraints

The statement makes all the checks it makes today first (planning, `table <t> already exists`, the column
definitions, references, ...). Then, for each constraint in order (column constraints where they are written):

1. Its name must be unique among the table's constraints: `Error: invalid input: duplicate check constraint <name>
   in table <t>` (also when an explicit name equals a generated one); a name can't be empty:
   `Error: invalid input: check constraint name can't be empty`.
2. The condition is planned as the WHERE condition of `SELECT * FROM <t>` would be, with the table's columns in scope
   (unqualified or qualified by the table name), and with the same errors (`Error: invalid input: unknown column x`,
   `Error: invalid input: unknown function count with 1 arguments` for an aggregate function, ...). It is neither
   optimized nor evaluated.

These checks are made when the statement is executed (DESCRIBE, ESTIMATE and EXPLAIN of the statement don't make
them), before anything is written, and so before the `read-only transaction` error.

A constraint is stored as its name and the canonical one-line text of its condition (EXT4 1.5); the condition is
planned again from the text whenever rows are written. The `schema` display and the cluster runner's `table` command
don't show constraints; EXPORT AS SQL (1.5), backups (section 7) and the system tables (section 8) do. A table
without constraints stores nothing new. A `CREATE TABLE` that doesn't create a table (for example because of an
option added later that makes it do nothing when the table exists) neither checks nor creates constraints (none of
the checks above is made), and the existing table's constraints stay as they are.

### 4.3 Enforcement

Every row that a statement writes into a table — inserted by INSERT, IMPORT or any statement added later that
inserts rows, or updated by UPDATE or any statement added later that updates rows — must satisfy every constraint of
the table. The constraints are evaluated for each row after the row is complete (defaults filled in, the SET
expressions evaluated) and before any of the statement's rows is written, row by row in the order the statement
writes them (INSERT: VALUES order; UPDATE: primary key order of the old keys), and for each row the constraints in
their order. The existing checks of the rows (data types, NULL, primary key, unique and reference checks) come after
all of them, when the rows are written. For each constraint:

* `TRUE` or `NULL`: the row satisfies it (as in SQL, a condition that is NULL is not a violation).
* `FALSE`: `Error: invalid input: row <key> violates check constraint <name>`, `<key>` being the row's primary key
  displayed as in query results.
* any other value: `Error: invalid input: check constraint <name> returned <value>, expected boolean`.
* an evaluation error is the statement's error (`Error: invalid input: can't compare 'x' and 0`).

A failing statement writes nothing, as today (inside an explicit transaction, the rows of earlier statements stay).

```
> CREATE TABLE acct (id INT PRIMARY KEY, bal INT, lim INT DEFAULT 100, CHECK (bal >= 0), CONSTRAINT under CHECK (bal <= lim))
> INSERT INTO acct (id, bal) VALUES (1, 50), (2, NULL)
!> INSERT INTO acct (id, bal) VALUES (3, 10), (4, -1)
!> UPDATE acct SET bal = bal + 60 WHERE id = 1
> EXPORT AS SQL
---
Error: invalid input: row 4 violates check constraint acct_check1
Error: invalid input: row 1 violates check constraint under
CREATE TABLE acct (
  id INTEGER PRIMARY KEY,
  bal INTEGER DEFAULT NULL,
  lim INTEGER DEFAULT 100,
  CONSTRAINT acct_check1 CHECK (bal >= 0),
  CONSTRAINT under CHECK (bal <= lim)
);
INSERT INTO acct VALUES (1, 50, 100);
INSERT INTO acct VALUES (2, NULL, 100);
```

### 4.4 Constraints and table definitions

* `DROP TABLE` drops the table's constraints.
* **No statement may drop or rename a column that a constraint of its table uses**: every statement that drops or
  renames columns — none exists in toydb today; for example a statement added later that changes a table's
  definition — fails with `Error: invalid input: column <c> is used by check constraint <name>` (the first such
  constraint in constraint order) when one of the columns it drops or renames is used by a constraint. A constraint
  uses the columns its condition references. The check comes after all of the statement's own checks, before the
  check of 5.3, before anything is changed and before the `read-only transaction` error.
* **Statements that add columns.** A statement that adds a column — none exists today; for example a statement
  added later that adds a column given in the column syntax of CREATE TABLE — adds the column's CHECK constraints
  as the table's next constraints, named `<table>_check<i>` with `<i>` their position among all the table's
  constraints (so a table with two constraints gets `<table>_check3`; a name that exists already is
  `duplicate check constraint`), and checks them: their conditions are planned with the table's columns including
  the new one (4.2), and every existing row, with the value it gets for the new column, must satisfy them
  (`Error: invalid input: row <key> violates check constraint <name>`, rows in primary key order). These checks come
  after all of the statement's own checks, before anything is changed and before the `read-only transaction`
  error. The table's other constraints don't use the new column and are not checked again.

---

## 5. Extension: Row-level security policies

### 5.1 Statements

```
CREATE POLICY <name> ON <table> [TO <user>] USING ( <condition> ) [WITH CHECK ( <condition> )]
DROP POLICY [IF EXISTS] <name> ON <table>
```

A **policy** of a table lets a user read the rows of the table for which its USING condition is TRUE, and write rows
for which its check condition is TRUE: the WITH CHECK condition, or the USING condition if it has none. A policy with
`TO <user>` applies to the users that act for `<user>` (2.1); one without `TO` applies to every user.

`CREATE POLICY` checks, in order: the current user must act for the table's owner (the `policy` access, 2.5);
`Error: invalid input: table <table> does not exist` (also for a view); `Error: invalid input: policy name can't be
empty`; `Error: invalid input: policy <name> already exists on table <table>`; `Error: invalid input: user <user>
does not exist`; the USING condition, then the WITH CHECK condition, are planned as for a check constraint (4.2, with
the same errors); in a read-only transaction `Error: read-only transaction`. The policy is stored with its name, its
user (if any) and the canonical one-line text of its conditions.

`DROP POLICY` checks: the `policy` access; `Error: invalid input: table <table> does not exist`;
`Error: invalid input: policy <name> does not exist on table <table>` (with `IF EXISTS` the statement then does
nothing); `Error: read-only transaction`.

### 5.2 Which rows a user reads

A table that has at least one policy is **protected**. For a session whose current user is neither a superuser nor
acts for the table's owner, every row that a statement reads from a protected table — through a scan, a key lookup
or an index lookup of the table, directly or in the query of a view, in every statement that reads rows (SELECT,
UPDATE and DELETE, EXPORT, the queries contained in statements, and every statement added later that reads rows) —
is read only if at least one of the policies that apply to the current user is TRUE for it. With no applicable policy,
no row of the table is read. The policies are evaluated in order of name, for each row before the statement's own
conditions, until one is TRUE; FALSE and NULL hide the row, another value fails with
`Error: invalid input: policy <name> returned <value>, expected boolean`, and an evaluation error is the statement's
error. Tables without policies, and superusers and owners, read every row.

* UPDATE and DELETE therefore change only the rows the user reads. The existing checks of written rows (primary
  key, unique, references) see every row.
* **Written rows.** For such a user, every row that a statement inserts or updates in a protected table (INSERT,
  UPDATE, IMPORT, and every statement added later that inserts or updates rows) must satisfy the check condition of
  at least one applicable policy (evaluated in order of name, until one is TRUE; FALSE and NULL don't satisfy it,
  another value fails with `Error: invalid input: policy <name> returned <value>, expected boolean`, and an
  evaluation error is the statement's error, as for reads): otherwise
  `Error: invalid input: new row violates row-level security policy for table <table>`; with no applicable
  policy, no row can be written. Rows are checked as CHECK constraints are (4.3), after them: for each row, its
  constraints, then its policies, before any of the statement's rows is written.
* Plans are not affected: `EXPLAIN`, `ESTIMATE` and `DESCRIBE` show the same plan for every user.
* `ANALYZE` and `SHOW STATISTICS`, which owners and superusers use, are not affected.

```
> CREATE TABLE doc (id INT PRIMARY KEY, owner_name STRING, body STRING)
> INSERT INTO doc VALUES (1, 'alice', 'a1'), (2, 'bob', 'b1'), (3, 'alice', 'a2')
> CREATE USER alice
> GRANT SELECT, DELETE ON doc TO alice
> CREATE POLICY own ON doc TO alice USING (owner_name = 'alice')
> SET ROLE alice
> SELECT id, body FROM doc
> DELETE FROM doc
> RESET ROLE
> SELECT id FROM doc
---
1, 'a1'
3, 'a2'
2
```

### 5.3 Policies and other objects

* `DROP TABLE` drops the table's policies; `DROP USER` of a user that a policy names fails (2.1).
* EXPORT AS SQL doesn't export policies; backups do (section 7).
* The system tables show policies with their conditions (section 8).
* **No statement may drop or rename a column that a policy of its table uses** (as in 4.4):
  `Error: invalid input: column <c> is used by policy <name>` (the first such policy by name), checked after the
  check-constraint check of 4.4, before anything is changed and before the `read-only transaction` error. A policy
  uses the columns its USING and WITH CHECK conditions reference.

---

## 6. Extension: PostgreSQL wire protocol

Every server also accepts connections that speak a subset of the **PostgreSQL frontend/backend protocol, version
3.0**, on the address of its configuration option `listen_pg` (default `localhost:9605`). A PostgreSQL connection
has its own session (3.1), with the startup message's user as login and current user. Everything a session does
(transactions, privileges, prepared statements of EXT4 6, digests, the audit log) works as for a client connection.

### 6.1 Messages

The message formats are PostgreSQL's: a frontend message is a type byte, a 4-byte big-endian length (counting
itself) and the body; the startup-phase messages (SSLRequest, GSSENCRequest, CancelRequest, StartupMessage) have no
type byte. Integers are big-endian; strings are UTF-8 and terminated by a zero byte.

**Startup.**

1. SSLRequest and GSSENCRequest are answered with the single byte `N` (not supported); the client then sends its
   StartupMessage. A CancelRequest closes the connection without an answer (cancellation is not supported).
2. A StartupMessage must have protocol version 3.0 (196608): otherwise `FATAL 0A000 unsupported frontend protocol
   <major>.<minor>` and the connection is closed (the rest of such a message is not read). Its parameter `user` is required and must not be empty (`FATAL 28000 no user name
   given`) and must name a user (`FATAL 28000 user <u> does not exist`). `database` and every other parameter except
   `application_name` are ignored.
3. A user with a password is asked for it with AuthenticationMD5Password and a random 4-byte salt; the
   PasswordMessage must carry `md5` followed by the hex MD5 of (the stored hash without its `md5` prefix, followed by
   the salt) — PostgreSQL's MD5 method, which a client computes from the password — else (also when another message
   comes in its place) `FATAL 28P01 password authentication failed for user <u>`. A user without a password is not
   asked.
4. The server then sends AuthenticationOk, then ParameterStatus for `application_name` (as given, or empty),
   `client_encoding` `UTF8`, `DateStyle` `ISO`, `integer_datetimes` `on`, `is_superuser` (`on`/`off`, of the login
   user), `server_encoding` `UTF8`, `server_version` `14.0`, `session_authorization` (the user) and
   `standard_conforming_strings` `on`, in this order, then BackendKeyData (process ID: the session number; secret
   key: a random number), then ReadyForQuery.

**ReadyForQuery** carries the transaction status: `I` (idle) if the session has no open transaction, `T` if it has
one. (toydb transactions stay usable after a failed statement, so `E` is never sent.)

**Errors** are sent as ErrorResponse with the fields `S` and `V` (severity: `ERROR`, or `FATAL` when the connection is
then closed), `C` (the SQLSTATE code) and `M` (the message). The message of a toydb error is its display (as after
`Error: `); its code is given by its kind: `invalid input` `42000`, `permission denied` `42501`,
`read-only transaction` `25006`, `serialization failure` `40001`, `operation aborted` `40003`, `invalid data`
`XX000`, `io error` `58030`. Protocol errors have the codes and messages given in this section. An unknown message
type is `FATAL 08P01 invalid frontend message type <c>` (the type byte as a character) and closes the connection;
`Terminate` closes it silently. A message that can't be decoded (shorter than its fields, a string without its zero
byte or not UTF-8, bytes after its last field, a Describe or Close target other than `S` and `P`) is `FATAL 08P01
invalid message format`. CopyData, CopyDone, CopyFail and PasswordMessage outside of COPY FROM STDIN and the startup
are ignored. The session (3.1) is created when the startup has succeeded, just before AuthenticationOk; a connection
whose startup fails has none.

### 6.2 Simple query

A Query message carries a string of SQL text. It is split into statements at the `;` tokens of the SQL lexer (a
string or quoted identifier containing `;` is not split); if the text can't be tokenized, the lexer's error is
reported and nothing is executed. Statements that are empty (nothing between two `;`) are skipped; a text without any
statement gets EmptyQueryResponse. The statements are executed one by one, each exactly as if sent alone by a client
(in particular, each outside an explicit transaction commits on its own); for each the server sends its result
(6.3), and after the first that fails, ErrorResponse and no further statement is executed. Finally, ReadyForQuery.

### 6.3 Results

| Statement result | Messages |
|---|---|
| rows (SELECT and every statement that returns rows) | RowDescription, DataRow per row, CommandComplete `SELECT <n>` |
| `EXPLAIN` | RowDescription of one `text` column named `QUERY PLAN`, a DataRow per line of the plan display, CommandComplete `EXPLAIN` |
| `FORMAT`, `EXPORT` (lines) | RowDescription of one `text` column named by the statement name (`format`, `export`), a DataRow per line, CommandComplete `FORMAT` / `EXPORT` |
| `INSERT` (also IMPORT) | CommandComplete `INSERT 0 <n>` |
| `UPDATE`, `DELETE` | CommandComplete `UPDATE <n>`, `DELETE <n>` |
| every other result | CommandComplete with the statement name (1.2) of the statement the result belongs to, in upper case, with spaces for `_`: `BEGIN`, `COMMIT`, `CREATE TABLE`, `DROP VIEW`, `ANALYZE`, `PREPARE`, `CREATE USER`, `SET ROLE`, `CREATE POLICY`, ...; a result added later likewise (`FOO BAR` for a statement `foo_bar`) |

**RowDescription.** Each column is described with its name, table OID 0, column number 0, its type's OID and size
(6.4), type modifier -1, and its format code (0 text, 1 binary).

* **Name**: the column's header (as `[header]` shows it) without a table qualifier (`t.id` → `id`, `n` → `n`). A
  column without a header (an unnamed expression) is named as PostgreSQL names it, from the select-list item of the
  query (also of a prepared query): a column reference by the column's name; a function or aggregate function call by
  the function's name in lower case (`count`, `sqrt`); an expression atom that begins with a keyword and is not a
  literal by that keyword in lower case; every other expression (literals, parameters, operators) `?column?`.
  Constructs added later follow the same rules by what they are (a function call, an operator, an atom beginning
  with a keyword, a literal). Results that aren't query results (`SHOW ...`, `DESCRIBE`, ...) always have headers.
* **Type** in a simple query: the data type of the column's first non-NULL value (in row order); a column without a
  non-NULL value has type `text`. (In the extended protocol, Describe gives the types, 6.5.)

**DataRow** values are NULL (length -1) or the value's encoding in the column's format:

| Value | Text format | Binary format |
|---|---|---|
| BOOLEAN | `t` / `f` | one byte, 1 / 0 |
| INTEGER | decimal digits, `-` if negative | 8 bytes, big-endian two's complement |
| FLOAT | finite: as displayed in query results (`1.5`, `1.0`, `1e100`, `-0.0`); `NaN`, `Infinity`, `-Infinity` | 8 bytes, big-endian IEEE 754 |
| STRING | the string itself | its UTF-8 bytes |
| a value of every other data type (added later) | the value as displayed in query results | the UTF-8 bytes of its text format |

Each value is encoded by its own data type (a column of mixed types, e.g. from a CASE expression, sends each value
in its own encoding).

### 6.4 Types

| toydb data type | PostgreSQL type | OID | size |
|---|---|---|---|
| BOOLEAN | `bool` | 16 | 1 |
| INTEGER | `int8` | 20 | 8 |
| FLOAT | `float8` | 701 | 8 |
| STRING | `text` | 25 | -1 |
| every data type added later, and an unknown type | `text` | 25 | -1 |

Parameter types of the extended protocol are given by OIDs; these map to toydb data types: 16 → BOOLEAN; 20 `int8`,
21 `int2`, 23 `int4` → INTEGER; 700 `float4`, 701 `float8` → FLOAT; 25 `text`, 1043 `varchar` → STRING. 0 means
unspecified; any other OID is `ERROR 0A000 unsupported parameter type <oid>`. A parameter of a toydb data type that
no OID maps to can't be declared.

**Parameter values** (Bind) are NULL or bytes in the parameter's format, decoded by its declared OID:

| OID | Text format | Binary format |
|---|---|---|
| 16 | `t`, `true`, `f`, `false`, in any letter case | 1 byte, 0 or 1 |
| 21, 23, 20 | an optional `+` or `-` and one or more ASCII digits, within the range of the type (16, 32, 64 bits) | 2, 4, 8 bytes, big-endian two's complement |
| 700, 701 | a FLOAT as EXT_SPEC 1.5 reads it (`1`, `-0.5`, `2.5e-3`), or `NaN`, `Infinity`, `-Infinity`, `inf`, `-inf` in any letter case | 4, 8 bytes, big-endian IEEE 754 |
| 25, 1043 | the text itself | the bytes, which must be UTF-8 |

Invalid text is `ERROR 22P02 invalid input syntax for type <type>: "<text>"` (`<type>` the PostgreSQL type name of
the OID); a number out of the type's range `ERROR 22003 value "<text>" is out of range for type <type>`; invalid
binary data (wrong length, not UTF-8) `ERROR 22P03 incorrect binary data format in bind parameter <i>` (`<i>`
counting from 1); a value in text format that is not UTF-8 `ERROR 22021 invalid byte sequence for encoding UTF8`.

### 6.5 Extended query

The extended protocol's messages are processed in order; responses to them are sent when the client sends Sync (or
Flush). After an error, every message up to the next Sync is skipped (also Query and Flush; Terminate still closes
the connection); Sync then sends ReadyForQuery.

* **Parse** (statement name, query, parameter OIDs) parses the query, which must be one statement (`ERROR 42601 cannot
  insert multiple commands into a prepared statement`; a parse error is the toydb error, an empty query is allowed
  and gets EmptyQueryResponse when executed). Its parameters `$1 ... $n` (EXT4 6.1) number as many as the larger of
  the highest parameter number and the number of OIDs given. Each parameter needs a type: the i-th OID, mapped by
  6.4 (`ERROR 42P18 could not determine data type of parameter $<i>` if it is 0 or missing). A statement with
  parameters must be a planned statement (EXT4 1.2): `ERROR 42000 invalid input: PREPARE is not supported for
  <KEYWORD>`. The name `''` is the unnamed statement, which every Parse without name replaces; a named statement
  must not exist (`ERROR 42P05 prepared statement <name> already exists`, checked before the query is parsed). These statements belong to the PostgreSQL
  connection; they are not the prepared statements of `PREPARE` and `SHOW PREPARED`. ParseComplete.
* **Bind** (portal name, statement name, parameter format codes, parameter values, result format codes) creates a
  portal: the statement with every parameter replaced by its value (as EXECUTE binds arguments, EXT4 6.3). Format
  codes: none means all text, one applies to all, otherwise one per parameter (`ERROR 08P01 bind message has <k>
  parameter formats but <m> parameters`); the number of values must be the statement's
  (`ERROR 08P01 bind message supplies <k> parameters, but prepared statement requires <m>`); the statement must exist
  (`ERROR 26000 prepared statement <name> does not exist`, `unnamed prepared statement does not exist`). Result
  format codes likewise apply to the result columns (checked by Describe and Execute: `ERROR 08P01 bind message has
  <k> result formats but query has <m> columns`; a statement without result columns is not checked). A format code
  other than 0 and 1 is `ERROR 22023 unsupported format code: <c>`. Bind checks, in this order: that the statement
  exists, the number of parameter format codes, the number of values, every parameter format code, the values (in
  order, 6.4), every result format code. The unnamed portal `''` is replaced by every Bind without portal
  name; all portals are closed by Sync. BindComplete.
* **Describe** of a statement: ParameterDescription (the parameter OIDs as declared), then the RowDescription of the
  statement's result columns as DESCRIBE gives them (EXT4 4) for the statement with every parameter replaced by
  NULL, with format code 0; or NoData for a statement without result columns. Describe of a portal: the
  RowDescription of its bound statement as DESCRIBE gives it, with the portal's result formats, or NoData. Types by
  6.4 (an unknown type is `text`); names by 6.3. A statement that DESCRIBE doesn't describe (EXT4 4: neither a
  planned statement nor EXECUTE) is DESCRIBE's error (`ERROR 42000 invalid input: DESCRIBE is not supported for
  BEGIN`), also for a statement that returns rows (`SHOW USERS`). `ERROR 26000` / `ERROR 34000 portal <name> does
  not exist` (`unnamed portal does not exist`).
* **Execute** (portal, maximum rows) executes the portal's statement as the session would execute it, the first time
  it is executed, and sends its DataRows (in the portal's result formats, without RowDescription) and CommandComplete
  as in 6.3 (for rows, `SELECT <n>` with `<n>` the total number of rows of the portal's result); with a maximum
  `m > 0` and more rows remaining, only the next `m` rows and PortalSuspended, the rest on the next Execute of the
  portal. An Execute of a portal whose statement has completed executes nothing and sends
  CommandComplete with a count of 0 (`SELECT 0`, `INSERT 0 0`, ...; tags without a count as they are).
* **Close** of a statement or portal removes it (also if it doesn't exist): CloseComplete.
* **Sync**: ReadyForQuery. **Flush**: sends the pending responses.

### 6.6 COPY

A statement of a Query message whose first token is the word `COPY` (in any letter case; `COPY` is not a toydb
keyword, so `copy` stays a valid name) is a **COPY command** of the connection, not toydb SQL (it is accepted only in
a Query message):

```
COPY <table> [( <column>, ... )] TO STDOUT
COPY <table> [( <column>, ... )] FROM STDIN
```

`TO`, `FROM`, `STDOUT` and `STDIN` in any letter case; names are identifiers as in SQL. Anything else is
`ERROR 42601 syntax error in COPY command`. The columns default to all columns of the table in table order; an unknown
one is `ERROR 42000 invalid input: unknown column <c> in table <t>`, one given twice `ERROR 42000 invalid input: column
<c> given multiple times`; a name that is not a table `ERROR 42000 invalid input: table <t> does not exist`. A COPY is
executed by the session like a statement: `COPY ... TO STDOUT` has the accesses, privileges, transaction and rows
(policies included) of `EXPORT <table> AS CSV`, and `COPY ... FROM STDIN` those of `IMPORT INTO <table>`; the audit log
records it with the statement name `copy` and the error message of its ErrorResponse (a COPY command with a syntax
error is not recorded, like a statement that fails to parse); digests don't record it. Its privileges are checked
before CopyInResponse or CopyOutResponse is sent.

The data is in PostgreSQL's text format: one line per row, ending with a newline; fields separated by a tab; NULL as
`\N`; in a field, `\` is written `\\`, a tab `\t`, a newline `\n` and a carriage return `\r`. Reading undoes these four
escapes; a backslash followed by any other character stands for that character, and a backslash at the end of a field
for itself.

* **TO STDOUT**: CopyOutResponse (format 0, the number of columns, each format 0), one CopyData per row (the row's
  line), CopyDone, then CommandComplete `COPY <n>`. Values are written in the text format of 6.3.
* **FROM STDIN**: CopyInResponse (format 0, the number of columns, each format 0); the client then sends CopyData
  messages, whose contents are joined, and CopyDone, or CopyFail (`ERROR 57014 COPY from stdin failed: <message>`,
  nothing is inserted). Flush and Sync are ignored meanwhile; any other message is `ERROR 08P01 unexpected message
  type <c> during COPY from stdin` and nothing is inserted; a connection closed meanwhile inserts nothing (the audit
  log records the COPY with the error `connection closed during COPY`). Data that is not UTF-8 is `ERROR 22021
  invalid byte sequence for encoding UTF8`. The data is split into lines, each ending with a newline (text after the last newline is
  a line too; no data is no rows); each line into fields at tabs, with the escapes undone. A field `\N` is NULL; every other field is
  read by its column's data type: BOOLEAN, INTEGER, FLOAT and STRING as text parameters of the OIDs 16, 20, 701 and 25
  are read (6.4), **a value of a data type added later as IMPORT reads it (EXT_SPEC 1.5)**. A line with another number
  of fields is `ERROR 42000 invalid input: wrong number of fields on line <n>: expected <m>, found <k>`, an invalid
  field `ERROR 42000 invalid input: invalid value for <TYPE> column <c> on line <n>` (lines counted from 1). The rows
  are inserted as IMPORT inserts them: one INSERT of all rows into the columns, with the same defaults, checks and
  errors. Then CommandComplete `COPY <n>`.

### 6.7 The client

The library has a PostgreSQL client for these messages (startup with the MD5 password method, simple and extended
query, COPY). The runners' `pg` command uses it, and `toysql --pg` connects to a server's PostgreSQL port with it
and prints results as `toysql` prints them.

### 6.8 Golden-script commands

Both runners have a command `pg`, which drives a PostgreSQL connection of the command's prefix (each prefix has its
own connection; in the SQL runner, over an in-memory stream to the runner's engine; in the cluster runner, over TCP
to the PostgreSQL port of one of the cluster's nodes). It prints the backend messages it receives, one per line:

| Command | Sends | Prints |
|---|---|---|
| `pg startup [user=<u>] [password=<p>] [application_name=<a>] [protocol=<major>.<minor>] [ssl]` | (with `ssl`: SSLRequest, printing `SSLResponse N`), StartupMessage (user default `admin`), and PasswordMessage when asked for a password (if given; else the command closes the connection and prints `Closed`) | messages until ReadyForQuery or the connection is closed |
| `pg query "<sql>"` | Query | messages until ReadyForQuery |
| `pg parse [name=<s>] "<sql>" [types=<oid>,...]` | queues Parse | nothing |
| `pg bind [portal=<p>] [statement=<s>] [params=<v>,...] [formats=<f>,...] [results=<f>,...]` | queues Bind | nothing |
| `pg describe statement=<s>` / `pg describe portal=<p>` | queues Describe | nothing |
| `pg execute [portal=<p>] [max_rows=<n>]` | queues Execute | nothing |
| `pg close statement=<s>` / `pg close portal=<p>` | queues Close | nothing |
| `pg sync` | the queued messages and Sync | messages until ReadyForQuery |
| `pg terminate` | Terminate | nothing |
| `pg copy "<sql>" [data="<text>"] [fail="<message>"]` | Query (a COPY command); for FROM STDIN, after CopyInResponse, CopyData with the data (if given) and CopyDone, or CopyFail with the message | messages until ReadyForQuery |

In `params`, the item `NULL` is a NULL value (the text `NULL` can't be passed), an item `0x` followed by hex digits
is the bytes they give (in either format: a value in binary format is written so, and so are text bytes that aren't
UTF-8), every other item is its UTF-8 text, and values can't contain `,`. `statement` and `portal` name the unnamed statement
or portal when they are omitted from `bind` and `execute`, or given as `""`. The messages are printed as follows (consecutive ParameterStatus messages on one line):

| Message | Printed |
|---|---|
| AuthenticationOk, AuthenticationMD5Password | `AuthenticationOk`, `AuthenticationMD5Password` (the salt is not printed) |
| ParameterStatus | `ParameterStatus <name>=<value> <name>=<value> ...` |
| BackendKeyData | `BackendKeyData` (its numbers are not printed) |
| ReadyForQuery | `ReadyForQuery idle`, `ReadyForQuery transaction` |
| RowDescription | `RowDescription <column>, ...`, each `<name> <type>(<oid>,<size>)`, followed by ` binary` for format 1 |
| DataRow | `DataRow <value>, ...`: `NULL`; a text value between single quotes, written as STRING values are displayed in query results (`'it\'s'`); a binary value (a column that the Bind of the executed portal asked for in binary format, whether or not a RowDescription was received) as `\x` and lower-case hex |
| CommandComplete | `CommandComplete <tag>` |
| ErrorResponse | `ErrorResponse <severity> <code> <message>` |
| ParameterDescription | `ParameterDescription <type>(<oid>), ...` (just `ParameterDescription` without parameters) |
| ParseComplete, BindComplete, CloseComplete, NoData, PortalSuspended, EmptyQueryResponse, CopyDone | their names |
| CopyInResponse, CopyOutResponse | `CopyInResponse <n> columns`, `CopyOutResponse <n> columns` |
| CopyData | `CopyData <data>`, the data written as a STRING value is displayed in query results (`'1\ta\n'`) |
| the server closed the connection | `Closed` |

A `pg` command other than `startup` on a prefix without an open connection is a runner error. In the cluster
runner the output doesn't depend on the node.

```
> CREATE TABLE t (id INT PRIMARY KEY, v STRING, f FLOAT)
> INSERT INTO t VALUES (1, 'a', 1.5), (2, NULL, NULL)
pg startup
pg query "SELECT id, v, f * 2, count(*) FROM t GROUP BY id, v, f ORDER BY id; SELECT nosuch FROM t"
---
AuthenticationOk
ParameterStatus application_name= client_encoding=UTF8 DateStyle=ISO integer_datetimes=on is_superuser=on server_encoding=UTF8 server_version=14.0 session_authorization=admin standard_conforming_strings=on
BackendKeyData
ReadyForQuery idle
RowDescription id int8(20,8), v text(25,-1), ?column? float8(701,8), count int8(20,8)
DataRow '1', 'a', '3.0', '1'
DataRow '2', NULL, NULL, '1'
CommandComplete SELECT 2
ErrorResponse ERROR 42000 invalid input: unknown column nosuch
ReadyForQuery idle
```

---

## 7. Extension: Backup and restore

A **backup** is a binary file holding the whole database: users, memberships, tables with their owners, check
constraints and rows, views, grants and policies. It is created and restored by a superuser through the client
protocol (a request of its own, outside SQL), by the `toybackup` binary, and by the runners' `backup` commands.
Statistics, prepared statements, digests, the change log and the audit log are not part of it. Creating and
restoring a backup are not statements: they are not recorded in the audit log or in the session's digests, and the
statements that a restore's effects correspond to (7.3) are not recorded either.

### 7.1 Format (version 1)

All integers are big-endian. A **string** is a u32 byte length followed by its UTF-8 bytes; an **optional string** a
u8 `0` (absent) or `1` followed by a string; a **bool** a u8 `0` or `1`.

* **Data types** are written as a u8 **type tag**: BOOLEAN `1`, INTEGER `2`, FLOAT `3`, STRING `4`. A data type
  added to toydb later gets the next unused tag, in the order the types are added (the first `5`).
* **Values** are written as a u8 tag and a payload: NULL is the tag `0` without payload; every other value is its
  data type's tag followed by: BOOLEAN — a u8 `0`/`1`; INTEGER — 8 bytes, two's complement; FLOAT — the 8 bytes of
  its IEEE 754 binary64 representation; STRING — a string; **a value of a data type added later — its display in
  query results (EXT_SPEC 1.5 reads it back), as a string**.

The file is: the 8 ASCII bytes `TOYDBBAK`; the format version, a u16 `1`; the **records**; an END record; and the
CRC-32 (the CRC of zlib and PNG: reflected, polynomial `0xEDB88320`, initial value and final XOR `0xFFFFFFFF`) of
every byte before it, as a u32. A record is a u8 kind, a u32 payload length and the payload:

| Kind | Record | Payload |
|---|---|---|
| 1 | USER | name (string), superuser (bool), password (optional string: the stored MD5 hash, 2.1) — every user but `admin` |
| 2 | MEMBER | member (string), the user it is a member of (string) |
| 3 | TABLE | name (string), owner (string), primary key column index (u32), column count (u32), and per column: name (string), data type (type tag), nullable (bool), has a default (bool) and if so the default (value), unique (bool), index (bool), references (optional string) |
| 4 | CHECK | table (string), constraint name (string), condition (string, as stored) |
| 5 | ROW | table (string), value count (u32), the values (in column order) |
| 6 | VIEW | name (string), owner (string), column count (u32), the column names (strings), query (string, as stored) |
| 7 | GRANT | object (string), grantee (string), privileges (u8: SELECT `1`, INSERT `2`, UPDATE `4`, DELETE `8`, added) |
| 8 | POLICY | table (string), name (string), user (optional string), USING condition (string, as stored), WITH CHECK condition (optional string, as stored) |
| 9 | MATVIEW | name (string), owner (string), column count (u32), the column names (strings), query (string, as stored) — section 9 |
| 255 | END | the number of records before it (u32) |

Records are written in this order: the USERs by name; the MEMBERs by member, then user; for each table, in the order
of EXT_SPEC 6.1 (a table after the tables it references), its TABLE, its CHECKs in constraint order and its ROWs in
primary key order; the VIEWs and MATVIEWs in the order of EXT4 3.5 (each after the views and materialized views it
depends on: repeatedly the first by name among those whose dependencies are written); the GRANTs by object, then
grantee (one record per pair, with all its privileges); the POLICYs by table, then name; END.

### 7.2 Creating a backup

A backup is created in the session's transaction or an implicit read-only one, so it is a consistent snapshot; the
user must be a superuser (`Error: permission denied: user <u> is not a superuser`). Policies don't apply
(superusers read every row). A backup **as of a version** (`--as-of <v>`) is created in an implicit read-only
transaction as of that version, exactly as `BEGIN READ ONLY AS OF SYSTEM TIME <v>` begins one, with its errors
(`Error: invalid input: version 99 does not exist`; in an explicit transaction
`Error: invalid input: already in a transaction`).

### 7.3 Restoring a backup

A backup is restored by a superuser into the database, in the session's transaction or an implicit read-write one.
The whole file is first read and checked, in this order, and nothing is restored if it is invalid
(`Error: invalid input: invalid backup: <reason>`):

1. fewer than 14 bytes, or not starting with `TOYDBBAK`: `not a toydb backup`;
2. a version other than 1: `unsupported version <v>`;
3. a wrong CRC: `checksum mismatch`;
4. then the records in order: a record or value that ends early `truncated`; an unknown record kind
   `unknown record kind <k>`; an unknown type or value tag `unknown type tag <t>` / `unknown value tag <t>`; a string
   that is not UTF-8 `invalid string`; bytes between the END record and the CRC `data after end record`; an END count
   that differs from the number of records `wrong record count`; no END record `truncated`. A record's kind and
   length are read first, then its payload, and only then is its kind checked: a record whose length goes past the
   end is `truncated`, whatever its kind. Within a payload, a bool is true for every byte other than `0`, and bytes
   after the record's fields are ignored.

Then the records are applied in order, with the effects and errors of the corresponding statements, executed as
`admin`: USER as `CREATE USER` (`Error: invalid input: user alice already exists`), MEMBER as `GRANT <user> TO
<member>`, TABLE as `CREATE TABLE` of that definition (`Error: invalid input: table t already exists`) owned by the
recorded owner, CHECK as the table's next constraint, ROW as an `INSERT` of the row (with the checks of INSERT and of
the table's constraints; a row with fewer values than the table has columns gets the defaults of the others, as
`INSERT INTO t VALUES` does), VIEW as `CREATE VIEW` with the column list (planned against the restored tables) owned by
the recorded owner, MATVIEW as `CREATE MATERIALIZED VIEW` with the column list (its query is executed in the restoring session, so its
rows are those of the restored tables, and system tables in it show that session's state) owned by the recorded owner, GRANT as `GRANT` (one without privileges does nothing),
POLICY as `CREATE POLICY`. The recorded owner of a TABLE, VIEW or MATVIEW must exist
(`Error: invalid input: user bob does not exist`, checked before the statement's own checks), and the query of a VIEW
or MATVIEW must be a SELECT (`Error: invalid input: view query is not a SELECT: <query>`). A USER's
password is restored as the stored hash it holds. The first error ends the restore, as a failing
statement does: outside an explicit transaction nothing is restored. Restoring a backup into an empty database
re-creates exactly the database that was backed up (apart from what 7 lists as not included).

### 7.4 toybackup

```
toybackup create [-H <host>] [-p <port>] [--user <u>] [--password <p>] <file>
toybackup restore [-H <host>] [-p <port>] [--user <u>] [--password <p>] <file>
toybackup list <file>
toybackup verify <file>
toybackup diff <file> <file>
```

`create --as-of <version>` creates a backup as of the version (7.2).

`create` writes a backup of the server's database to the file, `restore` restores the file into the server's
database. `list` and `verify` read the file only (no server): `verify` prints `ok: <n> records` (the END count), or
the error of 7.3 (`Error: invalid input: invalid backup: <reason>`); `list` prints one line per record (below).
`list`, `verify` and `diff` print the error line of an invalid file on standard output and exit with status 1; any
other error (reading or writing a file, connecting, logging in, an error of the server) is printed as
`Error: <error>` on standard error, also with status 1. Otherwise the status is 0.

| Record | Line |
|---|---|
| the header | `toydb backup version <v>` |
| USER | `user <name>`, followed by ` superuser` and ` password` where they apply |
| MEMBER | `member <member> of <user>` |
| TABLE | `table <name> owner <owner>: <the one-line form of its schema display>` |
| CHECK | `check <table>.<name>: <condition>` |
| ROW | `row <table>: <the values, displayed as in query results, separated by ", ">` |
| VIEW | `view <name> owner <owner> (<columns, separated by ", ">): <query>` |
| MATVIEW | `materialized view <name> owner <owner> (<columns, separated by ", ">): <query>` |
| GRANT | `grant <object> to <grantee>: <the privileges, in the order SELECT, INSERT, UPDATE, DELETE, separated by ", ">` |
| POLICY | `policy <table>.<name>`, followed by ` to <user>` if it has one, then `: <condition>`, then ` with check <condition>` if it has one |
| END | `end <n> records, checksum <the CRC in 8 lower-case hex digits>` |

`diff <a> <b>` compares two backups record by record. A record's **identity** is: USER its name, MEMBER its member
and user, TABLE its name, CHECK its table and name, ROW its table and primary key, VIEW and MATVIEW its name, GRANT its
object and grantee, POLICY its table and name. `diff` prints, for every identity that is in one backup only, its record's list
line prefixed by `- ` (in `<a>`) or `+ ` (in `<b>`), and for every identity in both whose records differ, `- ` and
its line in `<a>`, then `+ ` and its line in `<b>`. The lines are ordered by record kind in this order: USER, MEMBER,
TABLE, CHECK, ROW, VIEW, MATVIEW, GRANT, POLICY, then by identity (names by code points, primary keys in
ORDER BY order). Identical backups print nothing; an invalid file prints its error.

Names are written as stored. For an invalid file, `list` prints the lines of what it read before the problem (the
header line only if the header is valid), then the error line (`Error: invalid input: invalid backup: <reason>`); a
checksum mismatch is found before any record is read.

### 7.5 Golden-script commands

Both runners have these commands; backups are kept by the runner under a name (a snake_case identifier):

| Command | Effect | Prints |
|---|---|---|
| `backup <name> [as_of=<v>]` | creates a backup through the prefix's session (SQL runner) or client (cluster runner) | nothing (errors as usual) |
| `restore <name>` | restores it through the prefix's session or client | nothing (errors as usual) |
| `backup-list <name>` | lists it as `toybackup list` does (the cluster runner runs the `toybackup` binary) | the lines, including the error line of an invalid backup |
| `backup-verify <name>` | verifies it as `toybackup verify` does | its line |
| `backup-diff <a> <b>` | compares two backups as `toybackup diff` does | the lines |
| `backup-hex <name>` | | its bytes as lower-case hex, 16 bytes per line, separated by spaces |
| `backup-load <name> <hex>` | stores the bytes given in hex (spaces ignored) as a backup | nothing |
| `backup-patch <name> offset=<i> hex=<bytes>` | overwrites bytes at the offset (without fixing the CRC) | nothing |
| `backup-truncate <name> length=<n>` | keeps only the first n bytes | nothing |

```
> CREATE TABLE t (id INT PRIMARY KEY, v STRING)
> INSERT INTO t VALUES (1, 'a')
backup b
backup-list b
backup-hex b
---
toydb backup version 1
table t owner admin: CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING DEFAULT NULL)
row t: 1, 'a'
end 2 records, checksum f4f47c67
54 4f 59 44 42 42 41 4b 00 01 03 00 00 00 2e 00
00 00 01 74 00 00 00 05 61 64 6d 69 6e 00 00 00
00 00 00 00 02 00 00 00 02 69 64 02 00 00 01 00
00 00 00 00 01 76 04 01 01 00 00 00 00 05 00 00
00 18 00 00 00 01 74 00 00 00 02 02 00 00 00 00
00 00 00 01 04 00 00 00 01 61 ff 00 00 00 04 00
00 00 02 f4 f4 7c 67
```

(The TABLE record: `03`, length 46, `t`, owner `admin`, primary key column 0, 2 columns: `id` INTEGER, not
nullable, no default, unique, no index, no reference; `v` STRING, nullable, default NULL, ...; the ROW record: `05`,
length 24, `t`, 2 values: INTEGER 1, STRING `a`; END: `ff`, length 4, 2 records; the CRC.)

---

## 8. Extension: System tables

A query can read **system tables**: read-only tables, computed from the catalog, the server and the SQL language
when they are read. Their names begin with `toydb_`, except `pg_type` (8.4), which PostgreSQL clients read. A name in a FROM clause refers to a table of that name if there
is one, otherwise to a view, otherwise to a materialized view (section 9), otherwise to a system table; if there is none of them, the existing error
`table <name> does not exist` is reported. Every statement other than a query that names a system table where a
table is expected fails as for a name that is not a table (`table toydb_tables does not exist`). System tables can be
used like tables in queries (joins, WHERE, GROUP BY, ORDER BY, ...) and in views; they are not exported or backed up,
not listed by `schema` and `tables`, and LINT doesn't resolve them. The dependencies of a view (EXT4 3.1) are the
names in its query's FROM clause, those of system tables included: `CREATE VIEW w AS SELECT * FROM toydb_tables`
depends on `toydb_tables` (as `toydb_views` shows), so a table of that name created later can't be dropped while `w`
exists (EXT4 3.3). EXPORT AS SQL and backups write each view after the views it depends on; its other dependencies
don't affect the order.

### 8.1 Plans

A system table appears in plans as a node `SystemTable: <name>` or `SystemTable: <name> as <alias>`, which has no
input and emits the table's rows, computed when the node is executed, in the statement's transaction. Its columns are
labelled `<name>.<column>` (or with the alias), like a table's. No optimization moves a filter into a `SystemTable`
node or replaces it by a lookup: conditions stay above it (a `Filter`, or a join), as for a `View` node (EXT4 3.2).
Its JSON form is `{"node":"system_table","table":<name>,"alias":<alias or null>}`; `DESCRIBE` describes its columns
with the types and nullability given below; `ESTIMATE` estimates it, as a node added later without input, at 0 rows
(EXT4 5.3).

```
[plan]> SELECT name FROM toydb_tables WHERE owner = 'admin'
---
Projection: toydb_tables.name
└─ Filter: toydb_tables.owner = 'admin'
   └─ SystemTable: toydb_tables
```

Reading a system table requires nothing of the user (it is not an access, 1.3), except `toydb_audit`, which requires
a superuser like SHOW AUDIT. That check is made when the `SystemTable` node is executed (also when the other input
of a join is empty); EXPLAIN, DESCRIBE, ESTIMATE and PREPARE don't execute it, and neither does a query whose plan
no longer contains the node (`SELECT * FROM toydb_audit WHERE FALSE` plans `Nothing` and returns no rows).

### 8.2 The catalog

Columns are `NOT NULL` unless marked nullable. Rows are ordered as stated (the order of a query without ORDER BY).

| Table | Columns | Rows |
|---|---|---|
| `toydb_tables` | `name` STRING, `owner` STRING, `primary_key` STRING (the primary key column's name), `columns` INTEGER, `checks` INTEGER, `policies` INTEGER | one per table, by name |
| `toydb_columns` | `table_name` STRING, `name` STRING, `position` INTEGER (from 1), `type` STRING (as the schema display names it), `nullable` BOOLEAN, `default_value` STRING nullable (the SQL literal of the default, EXT_SPEC 1.3; NULL if the column has no default), `primary_key` BOOLEAN, `is_unique` BOOLEAN, `has_index` BOOLEAN, `references_table` STRING nullable | one per column of every table, by table name, then position |
| `toydb_views` | `name`, `owner`, `columns` (as SHOW VIEWS), `query` (as SHOW VIEWS), `dependencies` (the dependencies, EXT4 3.1, separated by `, `) — all STRING | one per view, by name |
| `toydb_matviews` | `name`, `owner`, `columns`, `query`, `dependencies` (as for views) STRING, `rows` INTEGER (the number of rows stored) | one per materialized view, by name |
| `toydb_checks` | `table_name`, `name`, `condition` (as stored) — STRING | one per check constraint, by table name, then constraint order |
| `toydb_policies` | `table_name` STRING, `name` STRING, `grantee` STRING nullable (the policy's user; NULL for all users), `condition` STRING (USING), `check_condition` STRING nullable (WITH CHECK; NULL if none) | one per policy, by table name, then name |
| `toydb_users` | `name` STRING, `is_superuser` BOOLEAN, `has_password` BOOLEAN | one per user, `admin` included, by name |
| `toydb_members` | `member` STRING, `member_of` STRING | one per membership, by member, then the other user |
| `toydb_grants` | `object`, `grantee`, `privilege` — STRING | the rows of SHOW GRANTS, in its order |
| `toydb_statistics` | `table_name` STRING, `column_name` STRING, `rows` INTEGER, `nulls` INTEGER, `distinct_values` INTEGER, `min_value` STRING nullable, `max_value` STRING nullable (displayed as in query results) | one per column of every analyzed table that has statistics, by table name, then position |

### 8.3 Sessions and statements

| Table | Columns and rows |
|---|---|
| `toydb_sessions` | the rows and columns of SHOW SESSIONS (3.1); `txn` nullable |
| `toydb_audit` | the rows and columns of SHOW AUDIT (3.3); `error` nullable |
| `toydb_prepared` | the rows and columns of SHOW PREPARED (EXT4 6.5), for the reading session |
| `toydb_statements` | the rows and columns of SHOW STATEMENTS (EXT_SPEC 3.1), for the reading session (the statement reading the table is recorded after it, as always) |

### 8.4 The language

| Table | Columns | Rows |
|---|---|---|
| `toydb_keywords` | `word` STRING (upper case), `category` STRING | one per reserved keyword, by word |
| `toydb_functions` | `name` STRING (lower case), `kind` STRING (`scalar` or `aggregate`), `arguments` INTEGER | one per function and aggregate function that expressions can call, by name, then number of arguments |
| `toydb_types` | `name` STRING (as the schema display names it), `spellings` STRING, `pg_type` STRING, `pg_oid` INTEGER, `backup_tag` INTEGER | one per data type, by backup tag |
| `pg_type` | `oid` INTEGER, `typname` STRING, `typlen` INTEGER | one per PostgreSQL type that the wire protocol uses (6.4: the types of results and of parameters), by OID |

* **Keywords**: every reserved keyword of toydb, those added later included. Its `category` is the first of these
  that applies:
  1. `statement` — a statement begins with it (`SELECT`, `CREATE`, `SHOW`, `SET`, `RESET`, `GRANT`, ...);
  2. `type` — it names a data type in a column definition (`INT`, `BOOLEAN`, `VARCHAR`, ...);
  3. `literal` — it is a literal on its own (`TRUE`, `FALSE`, `NULL`, `NAN`, `INFINITY`);
  4. `operator` — it is a word of an operator of the precedence table (EXT_SPEC 3.2, levels 1–10: `AND`, `OR`, `NOT`,
     `LIKE`, `IS`), or of an operator added later at one of those levels;
  5. `reserved` — every other keyword (`FROM`, `WHERE`, `AS`, `KEY`, `USING`, ...).
* **Functions**: `sqrt` (`scalar`, 1); `avg`, `count`, `max`, `min`, `sum` (`aggregate`, 1); and every function or
  aggregate function added later, with the number of arguments it takes (a function that takes several numbers of
  arguments has one row per number).
* **Types**: every data type, those added later included. `spellings` are the keywords that name it in a column
  definition, in upper case, ordered by code points, separated by `, ` (`BOOL, BOOLEAN`); `pg_type` and `pg_oid` the
  PostgreSQL type that the wire protocol sends it as (6.4); `backup_tag` its type tag in backups (7.1).

```
> SELECT * FROM toydb_types
---
'BOOLEAN', 'BOOL, BOOLEAN', 'bool', 16, 1
'INTEGER', 'INT, INTEGER', 'int8', 20, 2
'FLOAT', 'DOUBLE, FLOAT', 'float8', 701, 3
'STRING', 'STRING, TEXT, VARCHAR', 'text', 25, 4
```

---

## 9. Extension: Materialized views

```
CREATE MATERIALIZED VIEW <name> [( <column>, ... )] AS <query>
REFRESH MATERIALIZED VIEW <name>
DROP MATERIALIZED VIEW [IF EXISTS] <name>
```

A **materialized view** is a named query whose result rows are stored: they are computed when the materialized view
is created and when it is refreshed, and a statement that reads it reads the stored rows. Tables, views and
materialized views share one namespace.

### 9.1 Creating, refreshing, dropping

* `CREATE MATERIALIZED VIEW` makes the checks of CREATE VIEW, with the same messages (EXT4 3.1: the name, the
  namespace — `Error: invalid input: materialized view <m> already exists` for a materialized view, the existing
  messages for a table or view —, the query planned, the column list and names: `wrong number of column names for
  view <m>`, `column <i> of view <m> has no name`, ...), then executes the query in the statement's transaction (with its
  errors) and stores its rows, in the order the query returns them; in a read-only transaction `Error: read-only
  transaction`. It stores the name, the column names, the query as the canonical one-line text (like a view), the
  dependencies (the names in the query's FROM clause, as for a view) and the rows. CREATE TABLE and CREATE VIEW of
  the name of a materialized view fail with `Error: invalid input: materialized view <m> already exists`.
* `REFRESH MATERIALIZED VIEW <m>` plans and executes the stored query again, against the current definitions, and
  replaces the stored rows (`Error: invalid input: materialized view <m> does not exist`; the errors of planning and
  executing the query; in a read-only transaction `Error: read-only transaction`).
* `DROP MATERIALIZED VIEW <m>` removes it (`Error: invalid input: materialized view <m> does not exist`, nothing with
  `IF EXISTS`); a materialized view that a view or another materialized view depends on can't be dropped:
  `Error: invalid input: materialized view <m> is used by view <v>` (resp. `... by materialized view <v>`, the first
  by name, views first).
* Materialized views are part of the catalog and transactional like views (EXT4 3.3), their rows included: a refresh
  is seen by other transactions after commit and undone by ROLLBACK.

### 9.2 Reading

A materialized view can be used in a FROM clause wherever a view can. It appears in plans as a node
`MaterializedView: <m>` or `MaterializedView: <m> as <alias>`, without input, which emits the stored rows in the
order they were stored; its columns are labelled like a view's (`m.c`); conditions stay above it, as for a View node.
Its JSON form is `{"node":"materialized_view","view":<m>,"alias":<alias or null>}`; DESCRIBE describes its columns as
it describes a view's (the stored query, planned again); ESTIMATE estimates the node at the number of rows stored.
Every statement other than a query that names a materialized view where a table is expected fails as for a name that
is not a table. Reading a materialized view requires the SELECT privilege on it; policies of the tables its query
reads applied when it was refreshed, for the refreshing user.

### 9.3 Dependencies

The dependency rules of views (EXT4 3.3) hold for materialized views too, after the checks for views:

* `DROP TABLE <t>` of a table a materialized view depends on fails with `Error: invalid input: table <t> is used by
  materialized view <m>` (the first by name), and `DROP VIEW <v>` with `Error: invalid input: view <v> is used by
  materialized view <m>`.
* **Every other statement that changes the definition of a table** — none exists in toydb today; for example a
  statement added later that adds, drops or renames columns — fails with `Error: invalid input: table <t> is used by
  materialized view <m>` when a materialized view depends on the table; the check comes after the views check of
  EXT4 3.3, before the check constraints' (4.4), before anything is changed and before the `read-only transaction`
  error.

### 9.4 The other extensions

* **EXPORT AS SQL** writes, after the views, merged with them in the order of EXT4 3.5, one line per materialized
  view: `CREATE MATERIALIZED VIEW <name> (<column>, ...) AS <query>;`. Its rows are not exported (the CREATE
  re-computes them).
* **Backups** have a MATVIEW record (7.1), restored by executing the query.
* **System tables**: `toydb_matviews` (8.2). **SHOW VIEWS** lists views only.
* **Change log** (EXT4 8): creating, refreshing and dropping materialized views are not recorded.
* **Digests** and **FORMAT**: `CREATE MATERIALIZED VIEW <name> [(<column>, ...)] AS` then the query (FORMAT: on the
  following lines), `REFRESH MATERIALIZED VIEW <name>`, `DROP MATERIALIZED VIEW [IF EXISTS] <name>`.

```
> CREATE TABLE t (id INT PRIMARY KEY, v INT)
> INSERT INTO t VALUES (1, 10), (2, 20)
> CREATE MATERIALIZED VIEW mv AS SELECT v FROM t WHERE v > 15
> INSERT INTO t VALUES (3, 30)
> SELECT * FROM mv
> REFRESH MATERIALIZED VIEW mv
> SELECT * FROM mv
!> DROP TABLE t
---
20
20
30
Error: invalid input: table t is used by materialized view mv
```

---

## 10. Extension: Query profiles

```
EXPLAIN (ANALYZE) <statement>
```

executes the planned statement (EXT4 1.2; also `EXECUTE p (...)`, which stands for its bound statement, bound as for
`EXPLAIN EXECUTE`: it neither uses nor changes the plan cache and is not counted as an execution, EXT4 6.4) and
returns its plan with the number of rows of every node. For any other statement it fails with
`Error: invalid input: EXPLAIN is not supported for <KEYWORD>` (as EXPLAIN does).

* The statement is planned, optimized and executed exactly as when it is issued — same plan, same transaction (the
  session's, or an implicit one of the statement's own: read-only for a query, read-write otherwise), same privilege
  checks, same effects (its writes are made), same errors — except that **every node of the plan is run to
  completion before its parent uses its rows**: each node produces all its rows, which are counted, and its parent
  then reads them. So a statement whose rows a LIMIT would cut off before an error can fail here (`SELECT 1 / (id - 2)
  FROM t LIMIT 1` fails if a row has `id = 2`), and a node whose parent needs only some of its rows still produces
  all of them.
* The result has one row per line of the plan display, in order, with the columns `node` (STRING: the line, as in
  ESTIMATE), `estimate` (INTEGER: the estimate of EXT4 5, computed before the statement is executed) and `rows`
  (INTEGER: the number of rows the node produced). The statement line of a write is the count of its result (rows
  inserted, updated, deleted); the statement line of a plan without rows (`CreateTable`, `DropTable`, and every plan
  added later whose result has no count) is 0; for every node, also a node added later, the rows it produced. The
  rows the statement returns are not part of the result.
* Its digest is `EXPLAIN (ANALYZE) <digest of the statement>`, its rows count for SHOW STATEMENTS; the audit log
  records it as `explain` with the statement's class and accesses. `EXPLAIN (ANALYZE)` of a statement containing a
  parameter fails as for every statement (EXT4 6.1).
* Parsing: `EXPLAIN (` must be followed by `ANALYZE` and `)`: `EXPLAIN (VERBOSE) SELECT 1` →
  `Error: invalid input: expected token ANALYZE, found verbose`, `EXPLAIN (ANALYZE SELECT 1` →
  `Error: invalid input: expected token ), found SELECT`.

```
> CREATE TABLE t (id INT PRIMARY KEY, v INT)
> INSERT INTO t VALUES (1, 10), (2, 20), (3, 30)
> EXPLAIN (ANALYZE) SELECT v FROM t WHERE v > 15 ORDER BY v DESC LIMIT 1
---
'Limit: 1', 1, 1
'└─ Order: t.v desc', 333, 2
'   └─ Projection: t.v', 333, 2
'      └─ Scan: t (t.v > 15)', 333, 2
```

---

## 11. Syntax summary and changes to the earlier extensions

### 11.1 Parsing

The statements of 1.1, with the existing parser messages for the first token that doesn't fit (tokens displayed as
usual):

| Input | Error |
|---|---|
| `CREATE USER`, `DROP USER`, `GRANT`, `GRANT SELECT ON t TO`, `REVOKE alice FROM`, `SET ROLE`, `CREATE POLICY p ON t USING (`, `CREATE TABLE t (id INT PRIMARY KEY, CHECK (` | `Error: invalid input: unexpected end of input` |
| `CREATE USER 1`, `DROP USER 1`, `GRANT SELECT ON 1 TO u`, `GRANT SELECT ON t TO 1`, `SET ROLE 1`, `CREATE POLICY 1 ON t USING (TRUE)` | `Error: invalid input: expected identifier, got 1` |
| `CREATE USER u PASSWORD x` | `Error: invalid input: expected string, found x` |
| `CREATE USER u SUPERUSER PASSWORD 'p'`, `CREATE USER u PASSWORD 'p' PASSWORD 'q'` | `Error: invalid input: unexpected token PASSWORD` |
| `GRANT SELECT t TO u`, `REVOKE SELECT t FROM u` | `Error: invalid input: expected token ON, found t` |
| `GRANT SELECT ON t u`, `GRANT g u` | `Error: invalid input: expected token TO, found u` |
| `REVOKE SELECT ON t TO u`, `REVOKE g TO u` | `Error: invalid input: expected token FROM, found TO` |
| `GRANT SELECT, ON t TO u`, `GRANT SELECT, x ON t TO u` | `Error: invalid input: unexpected token ON` (resp. `unexpected token x`): after `,` a privilege keyword is required |
| `GRANT ALL ON t TO u` | `Error: invalid input: expected token TO, found ON` (`all` is not a keyword, so this is the membership form with the user name `all`) |
| `SET x`, `RESET x` | `Error: invalid input: expected token ROLE, found x` |
| `SHOW USER`, `SHOW GRANT`, `SHOW SESSION` | `Error: invalid input: unexpected token USER` (resp. `GRANT`, `session`) |
| `SHOW AUDIT SINCE x` | `Error: invalid input: unexpected token x, wanted number` |
| `CREATE TABLE t (id INT PRIMARY KEY, CHECK id > 0)` | `Error: invalid input: expected token (, found id` |
| `CREATE TABLE t (id INT PRIMARY KEY, CONSTRAINT c UNIQUE)` | `Error: invalid input: expected token CHECK, found UNIQUE` |
| `CREATE TABLE t (id INT PRIMARY KEY, CONSTRAINT CHECK (id > 0))`, `CREATE TABLE t (id INT PRIMARY KEY, constraint INT)` | `Error: invalid input: expected identifier, got CHECK` (resp. `got INT`): `CONSTRAINT` begins a table constraint, and its name is required |
| `CREATE POLICY p t USING (TRUE)` | `Error: invalid input: expected token ON, found t` |
| `CREATE POLICY p ON t USING (TRUE) WITH (TRUE)`, `... WITH CHECK TRUE` | `Error: invalid input: expected token CHECK, found (` (resp. `expected token (, found TRUE`) |
| `CREATE POLICY p ON t (TRUE)` | `Error: invalid input: expected token USING, found (` |
| `DROP POLICY p` | `Error: invalid input: unexpected end of input` |
| anything after a complete statement: `SHOW USERS x`, `RESET ROLE x`, `DROP USER u v` | `Error: invalid input: unexpected token x` (resp. `x`, `v`) |

After `CREATE`, the tokens `USER` and `POLICY` start the new statements, and after `DROP` likewise; anything else is
read as today (`CREATE USERS u` → `Error: invalid input: expected token TABLE, found USERS`). After `SET`, only
`ROLE` (`SET` doesn't begin any other statement). A statement that fails to parse has no effect.

### 11.2 Additions to the earlier extensions

Section 1.5 (digests, FORMAT, JSON, "not supported", LINT, change log, EXPORT AS SQL) and:

* **toysql**: short messages for the new results in text format (`Created user u`, `Dropped user u` / `User u does
  not exist`, `Granted`, `Revoked`, `Role set to u`, `Role reset to u`, `Created policy p on t`, `Dropped policy p on
  t` / `Policy p on t does not exist`); `--user` and `--password` (2.6).
* **toydump** and the runners' `dump` show the new catalog keys readably (as for views). The users extension's keys:
  `sql:User(<name>)` → `<name> superuser=<true|false> password=<stored hash or None>`, `sql:Membership(<member>,
  <user>)` → the raw empty value `""`, `sql:Grant(<object>, <grantee>)` → the privileges joined by `,` in the order
  `SELECT,INSERT,UPDATE,DELETE`, `sql:Owner(<object>)` → the owner's name. Its Raft write commands: `CREATE USER
  <user as above>`, `DROP USER <name>`, `MEMBERSHIP <member> OF <user>` / `NO MEMBERSHIP <member> OF <user>`,
  `PRIVILEGES <object> <grantee> (<privileges joined by ,>)`, `OWNER <object> <owner>`. The materialized views'
  keys: `sql:MaterializedView(<name>)` → its `CREATE MATERIALIZED VIEW <name> (<columns>) AS <query>` text (as
  EXPORT AS SQL writes it, without `;`), `sql:MaterializedViewRow(<name>, <position>)` → the row's values as
  `sql:Row` shows them; its Raft write commands: `CREATE MATERIALIZED VIEW <name> (<columns>) AS <query> ROWS
  (<values>) (<values>) ...`, `REFRESH MATERIALIZED VIEW <name> ROWS (<values>) ...` (the name as stored),
  `DROP MATERIALIZED VIEW <name>`. CREATE stores the rows at positions 0, 1, ...; REFRESH deletes every stored row
  and then stores the new ones from position 0.
* **toysql** (materialized views): `Created materialized view m with <n> rows`, `Refreshed materialized view m with
  <n> rows`, `Dropped materialized view m` / `Materialized view m does not exist`.

---

## 12. Implementation requirements for the tier-5 base (not user documentation)

The fairness and documentation-parity rules are in `EXT5_DESIGN_NOTES.md` and `EXT5_DOCS.md`; the code of each
language must be what a strong engineer naturally writes in that language, and every new module carries the doc
comment text of `EXT5_DOCS.md`.

1. **Nothing existing changes.** All tier-4 golden script runs (280) and unit tests produce byte-identical output,
   except unit tests that pin the set or number of keywords (Python `tests/sql/parser/test_lexer.py`: 87 → 106). In
   particular no existing statement writes anything new to storage: ownership by `admin` is not stored; users,
   grants, memberships, ownership, check constraints and policies are written only when they exist; the raw `dump`
   and `[ops]` output and the cluster `status` key counts and sizes stay identical; no existing Raft command changes
   its encoding (new commands are appended). The plan display, schema display, value display, error messages, JSON
   forms and the client protocol of existing results are unchanged.
2. **Keywords**: the nineteen of 1.1.
3. **AST**: statements for CREATE USER, DROP USER, GRANT/REVOKE (privileges, object, user; or group, user), SET ROLE,
   RESET ROLE, SHOW USERS, SHOW GRANTS, SHOW AUDIT (optional number), SHOW SESSIONS, CREATE POLICY (with an optional
   WITH CHECK condition), DROP POLICY; CREATE TABLE gets its table constraints (name if given, condition), a column
   definition its CHECK conditions; CREATE, REFRESH and DROP MATERIALIZED VIEW; EXPLAIN an `analyze` flag (or a
   statement of its own). The tier-3 desugaring and the tier-4 statement traversals (expressions, parameters,
   transform) handle them.
4. **Statement results**, appended after the existing variants: CreateUser, DropUser (existed), Grant/Revoke
   (optional table), SetRole/ResetRole (user), CreatePolicy, DropPolicy (existed), CreateMaterializedView (count),
   RefreshMaterializedView (count), DropMaterializedView (existed). Python: hand-written serde.
5. **Errors**: a new error kind `PermissionDenied(message)`, displayed `permission denied: <message>`,
   deterministic, appended after the existing variants.
6. **Catalog storage**: new local-engine keys, appended after the existing variants: a user (by name), a membership
   (member, user), a grant (object, grantee → privileges), an owner (object → user; only for owners other than
   `admin`), a table's check constraints (table → list), a policy (table, name). Catalog methods to write, read and
   list them; matching Raft write and read commands, appended; `encoding/format` formats the keys and commands.
   `DROP TABLE`/`DROP VIEW` remove the object's owner, grants, check constraints and policies (only the keys that
   exist), in this order, after the definition and the table's rows, index entries and statistics (visible in `[ops]`).
7. **Session**: login and current user (`admin` by default); the privilege check (2.5) for every statement, through
   an access function (1.3) shared with the audit log; SET/RESET ROLE; the audit log and the session list are kept by
   the engine the sessions belong to (the SQL runner's local engine, each server's Raft SQL engine), shared by its
   sessions, in memory.
8. **Check constraints** are enforced in the executor's insert and update paths (4.3), and so are the WITH CHECK
   conditions of policies. **Policies** are applied where the executor (and EXPORT) reads table rows (5.2), with the
   current user's applicable policies planned once per statement. **EXPLAIN (ANALYZE)** runs the executor in a
   profiling mode that runs each node to completion and counts its rows (10). **Materialized views** are stored as a
   catalog key per view (definition) and their rows under a key prefix of their own, written only for materialized
   views; the planner resolves them into a plan node `MaterializedView` handled by every exhaustive node function.
9. **pgwire**: a listener in the server (config `listen_pg`, default `localhost:9605`), a connection per client
   with its own session; COPY parsed by the connection from the lexer's tokens; no new dependencies.
10. **Client protocol**: new requests appended: authenticate (user, optional password), backup (→ bytes), restore
    (bytes). `Client` gets the matching methods.
11. **Binaries**: `toybackup` (Rust `src/bin/toybackup.rs`; Python `toydb/bin/toybackup.py` with the launcher
    `bin/toybackup`); `toysql --user/--password`.
12. **Planner**: FROM resolves system tables after tables and views, into a plan node `SystemTable` (name, alias,
    the system table's schema), handled by every exhaustive node function (columns, labels, transform, display,
    JSON, describe, estimate, execution).
13. **Runners** (test code, both languages): the `pg` command (6.8), the backup commands (7.5), the cluster runner's
    `login` command (2.6); in the cluster runner, the test cluster starts every node with a PostgreSQL port. The
    golden scripts of the tier-5 extensions live in a regression directory of each workspace (`src/sql/testscripts/
    ext5/` + `tests/scripts/ext5/` in Rust; `tests/sql/testscripts/ext5/` + `tests/e2e/scripts/ext5/` in Python,
    registered like `ext4/`). They are the scripts of `ext5_scripts/*/sql/` and `ext5_scripts/*/cluster/`.
14. **Size**: about 8,000–8,500 lines of Rust application code for the nine extensions (see the design notes for
    the split), so that the Rust application code reaches at least 20,000 lines.
