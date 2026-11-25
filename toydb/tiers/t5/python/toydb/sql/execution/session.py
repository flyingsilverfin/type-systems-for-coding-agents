"""SQL client sessions.

Mirrors `src/sql/execution/session.rs`.
"""

import logging
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Generic, TypeVar

from toydb import backup, encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error, errdata, errinput, errpermission, panic
from toydb.fmt import debug, debug_tuple
from toydb.iter import IntoIter
from toydb.sql.engine import Engine, Transaction
from toydb.sql.engine.changes import COLUMNS as CHANGE_COLUMNS
from toydb.sql.execution import audit, matviews, policies, privileges, users
from toydb.sql.execution.analyze import COLUMNS as STATISTICS_COLUMNS
from toydb.sql.execution.analyze import analyze, show_statistics
from toydb.sql.execution.digest import StatementDigests, digest
from toydb.sql.execution.executor import ExecutionResult
from toydb.sql.execution.export import export
from toydb.sql.execution.importer import import_lines
from toydb.sql.execution.lint import COLUMNS as LINT_COLUMNS
from toydb.sql.execution.lint import lint
from toydb.sql.execution.prepared import COLUMNS as PREPARED_COLUMNS
from toydb.sql.execution.prepared import PreparedStatements
from toydb.sql.execution.privileges import Access
from toydb.sql.execution.profile import COLUMNS as PROFILE_COLUMNS
from toydb.sql.execution.profile import profile_rows
from toydb.sql.execution.views import COLUMNS as VIEW_COLUMNS
from toydb.sql.execution.views import create_view, drop_view, show_views
from toydb.sql.parser import Parser, ast
from toydb.sql.parser.format import format_statement
from toydb.sql.planner import Plan
from toydb.sql.planner.describe import COLUMNS as DESCRIBE_COLUMNS
from toydb.sql.planner.describe import ColumnDescription, describe, describe_table
from toydb.sql.planner.estimate import COLUMNS as ESTIMATE_COLUMNS
from toydb.sql.planner.estimate import estimate_rows
from toydb.sql.types import Label, Row, Rows, Value
from toydb.sql.types.user import ADMIN
from toydb.storage import mvcc

if TYPE_CHECKING:
    from toydb.sql.engine import raft

log = logging.getLogger(__name__)

T = TypeVar("T", bound=Transaction)
R = TypeVar("R")


class Session(Generic[T]):
    """A SQL client session. Parses and executes raw SQL statements and
    handles transaction control."""

    def __init__(self, engine: Engine[T]) -> None:
        """Creates a new session using the given SQL engine."""
        self.engine = engine
        """The SQL engine."""
        self.txn: T | None = None
        """The current transaction, if any."""
        self.digests = StatementDigests()
        """Statistics of the statements executed by the session."""
        self.prepared = PreparedStatements()
        """Prepared statements, by name."""
        self.login = ADMIN
        """The login user."""
        self.user = ADMIN
        """The current user."""
        self.started = False
        """Whether the session has logged in or executed a statement."""
        self.executed = 0
        """The number of statements the session executed."""
        with engine.sessions.lock() as sessions:
            number = sessions.add(self)
        self.number = number
        """The session's number in the engine's session list."""

    def authenticate(self, user: str, password: str | None) -> None:
        """Logs in as a user."""
        if self.started:
            raise errinput("session already started")
        self.with_txn(True, lambda txn: users.check_login(txn, user, password))
        self.set_login(user)

    def set_login(self, user: str) -> None:
        """Logs in as a user that the caller has authenticated."""
        self.login = self.user = user
        self.started = True

    def execute(self, statement: str) -> "StatementResult":
        """Executes a client statement, and records it in the session's
        statement statistics and in the engine's audit log (see `audit`)."""
        self.started = True
        return self.execute_parsed(Parser.parse(statement))

    def execute_parsed(self, statement: ast.Statement) -> "StatementResult":
        """Executes a parsed client statement, like execute()."""
        self.started = True
        user = self.user
        try:
            result = self.execute_digested(statement)
        except Error as error:
            self.record_audit(user, statement, error)
            raise
        self.record_audit(user, statement, None)
        return result

    def execute_digested(self, statement: ast.Statement) -> "StatementResult":
        """Executes a parsed statement, and records it in the session's
        statement statistics by its digest. SHOW STATEMENTS is not
        recorded."""
        if isinstance(statement, ast.Statement.ShowStatements):
            self.check(statement)
            return StatementResult.Select(StatementDigests.COLUMNS, self.digests.rows())
        statement_digest = digest(statement)
        try:
            result, rows = self.execute_statement(statement)
        except Error:
            self.digests.record_error(statement_digest)
            raise
        self.digests.record(statement_digest, rows)
        return result

    def record_audit(self, user: str, statement: ast.Statement, error: Error | None) -> None:
        """Records an executed statement in the audit log, with the user that
        executed it."""
        if isinstance(statement, ast.Statement.ShowAudit):
            return  # SHOW AUDIT isn't recorded
        accesses = self.accesses(statement)
        with self.engine.audit.lock() as log:
            log.record(user, statement, accesses, str(error) if error is not None else None)
        self.executed += 1

    def record_copy(self, user: str, statement: ast.Statement, error: str | None) -> None:
        """Records a COPY command of the PostgreSQL protocol in the audit
        log."""
        accesses = self.accesses(statement)
        with self.engine.audit.lock() as log:
            log.record(user, statement, accesses, error, name="copy")
        self.executed += 1

    def accesses(self, statement: ast.Statement) -> list[Access]:
        """Returns the accesses of a statement, in the session's transaction
        or an implicit read-only one."""
        try:
            return self.with_txn(
                True, lambda txn: privileges.accesses(txn, statement, self.prepared)
            )
        except Error:
            return []  # an EXECUTE that can't be bound

    def execute_statement(self, statement: ast.Statement) -> tuple["StatementResult", int]:
        """Executes a parsed statement. Returns the result and the number of
        rows it returned or wrote, for the statement statistics."""
        # Reject parameters outside of PREPARE, FORMAT and LINT.
        if not isinstance(
            statement, (ast.Statement.Format, ast.Statement.Lint, ast.Statement.Prepare)
        ):
            if parameters := statement.parameters():
                raise errinput(
                    f"parameter ${parameters[0]} can only be used in a prepared statement"
                )

        # Transaction control and the extension statements are handled here,
        # other statements are handled by the SQL executor.
        match statement:
            # BEGIN: starts a new transaction and returns its state.
            case ast.Statement.Begin(read_only, as_of):
                self.check(statement)
                if self.txn is not None:
                    raise errinput("already in a transaction")
                match (read_only, as_of):
                    case (False, None):
                        txn = self.engine.begin()
                    case (True, None):
                        txn = self.engine.begin_read_only()
                    case (True, version):
                        txn = self.engine.begin_as_of(version)
                    case _:
                        raise errinput("can't start read-write transaction in a given version")
                state = txn.state().clone()
                self.txn = txn
                return StatementResult.Begin(state), 0

            # COMMIT: commits the currently open transaction, if any.
            case ast.Statement.Commit():
                self.check(statement)
                if (txn := self.txn) is None:
                    raise errinput("not in a transaction")
                self.txn = None
                version = txn.state().version
                txn.commit()
                return StatementResult.Commit(version), 0

            # ROLLBACK: rolls back the currently open transaction, if any.
            case ast.Statement.Rollback():
                self.check(statement)
                if (txn := self.txn) is None:
                    raise errinput("not in a transaction")
                self.txn = None
                version = txn.state().version
                txn.rollback()
                return StatementResult.Rollback(version), 0

            # EXPLAIN (ANALYZE): executes a planned statement, in the
            # transaction that the statement would use, and returns its plan
            # with the estimated and the actual rows of every line.
            case ast.Statement.Explain(inner, analyze=True):
                inner = self.planned_statement("EXPLAIN", inner)
                read_only = isinstance(inner, ast.Statement.Select)

                def execute_profiled(txn: T) -> list[Row]:
                    plan = Plan.build(inner, txn).optimize()
                    result, rows = profile_rows(txn, plan, self.user, self)
                    if isinstance(result, ExecutionResult.CreateTable):
                        self.set_owner(txn, result.name)
                    return rows

                rows = self.with_checked_txn(read_only, statement, execute_profiled)
                return StatementResult.Select(PROFILE_COLUMNS, rows), len(rows)

            # EXPLAIN: returns the given SQL query's plan. Only planned
            # statements can be explained.
            case ast.Statement.Explain(inner):
                match inner:
                    # Transaction statements are passed on to the planner, as
                    # they always have been.
                    case ast.Statement.Begin() | ast.Statement.Commit() | ast.Statement.Rollback():
                        pass
                    case _:
                        inner = self.planned_statement("EXPLAIN", inner)
                plan = self.with_checked_txn(
                    True, statement, lambda txn: Plan.build(inner, txn).optimize()
                )
                return StatementResult.Explain(plan), 0

            # SHOW STATEMENTS is handled by execute(), since it isn't recorded.
            case ast.Statement.ShowStatements():
                panic("SHOW STATEMENTS must be handled by execute()")

            # ANALYZE: computes and stores column statistics.
            case ast.Statement.Analyze(table):
                tables = self.with_checked_txn(False, statement, lambda txn: analyze(txn, table))
                return StatementResult.Analyze(tables), 0

            # SHOW STATISTICS: returns a table's column statistics.
            case ast.Statement.ShowStatistics(table):
                rows = self.with_checked_txn(
                    True, statement, lambda txn: show_statistics(txn, table)
                )
                return StatementResult.Select(STATISTICS_COLUMNS, rows), len(rows)

            # EXPORT: exports a table or the database as text lines.
            case ast.Statement.Export(table, format):
                exported = self.with_checked_txn(
                    True, statement, lambda txn: export(txn, table, format, self.user)
                )
                return StatementResult.Export(format, exported.lines), exported.rows

            # IMPORT: imports text lines into a table, like an INSERT.
            case ast.Statement.Import(table, format, lines):
                count = self.with_checked_txn(
                    False,
                    statement,
                    lambda txn: import_lines(txn, table, format, lines, self.user),
                )
                return StatementResult.Insert(count), count

            # FORMAT: returns the canonical SQL text of a statement.
            case ast.Statement.Format(inner):
                self.check(statement)
                return StatementResult.Format(format_statement(inner)), 0

            # CREATE VIEW: creates a view.
            case ast.Statement.CreateView(name, columns, query):

                def create(txn: T) -> None:
                    create_view(txn, name, columns, query)
                    self.set_owner(txn, name)

                self.with_checked_txn(False, statement, create)
                return StatementResult.CreateView(name), 0

            # DROP VIEW: drops a view.
            case ast.Statement.DropView(name, if_exists):
                existed = self.with_checked_txn(
                    False, statement, lambda txn: drop_view(txn, name, if_exists)
                )
                return StatementResult.DropView(name, existed), 0

            # SHOW VIEWS: lists the views.
            case ast.Statement.ShowViews():
                rows = self.with_checked_txn(True, statement, show_views)
                return StatementResult.Select(VIEW_COLUMNS, rows), len(rows)

            # DESCRIBE: describes a table or view, or the result of a
            # planned statement, which is planned but not optimized.
            case ast.Statement.Describe(str() as name):
                descriptions = self.with_checked_txn(
                    True, statement, lambda txn: describe_table(txn, name)
                )
                rows = [description.row() for description in descriptions]
                return StatementResult.Select(DESCRIBE_COLUMNS, rows), len(rows)
            case ast.Statement.Describe(ast.Statement() as inner):
                inner = self.planned_statement("DESCRIBE", inner)
                descriptions = self.with_checked_txn(
                    True, statement, lambda txn: describe(Plan.build(inner, txn))
                )
                rows = [description.row() for description in descriptions]
                return StatementResult.Select(DESCRIBE_COLUMNS, rows), len(rows)

            # ESTIMATE: estimates the rows of each line of a planned
            # statement's plan, which is planned and optimized as by EXPLAIN.
            case ast.Statement.Estimate(inner):
                inner = self.planned_statement("ESTIMATE", inner)
                rows = self.with_checked_txn(
                    True,
                    statement,
                    lambda txn: estimate_rows(txn, Plan.build(inner, txn).optimize()),
                )
                return StatementResult.Select(ESTIMATE_COLUMNS, rows), len(rows)

            # PREPARE: prepares a statement for the session. It doesn't use a
            # transaction.
            case ast.Statement.Prepare(name, types, inner):
                self.check(statement)
                self.prepared.prepare(name, types, inner)
                return StatementResult.Prepare(name), 0

            # EXECUTE: executes a prepared statement with the given arguments,
            # in the transaction that the statement would use.
            case ast.Statement.Execute(name, args):
                prepared = self.prepared.get(name)
                bound, arguments = prepared.bind(args)
                prepared.executions += 1
                read_only = isinstance(bound, ast.Statement.Select)

                def execute(txn: T) -> StatementResult:
                    result = StatementResult.try_from(
                        prepared.plan(txn, bound, arguments).execute(txn, self.user, self)
                    )
                    if isinstance(result, StatementResult.CreateTable):
                        self.set_owner(txn, result.name)
                    return result

                result = self.with_checked_txn(read_only, statement, execute)
                return result, result.row_count()

            # DEALLOCATE: removes a prepared statement.
            case ast.Statement.Deallocate(name):
                self.check(statement)
                self.prepared.deallocate(name)
                return StatementResult.Deallocate(name), 0

            # SHOW PREPARED: lists the session's prepared statements. It
            # doesn't use a transaction.
            case ast.Statement.ShowPrepared():
                self.check(statement)
                rows = self.prepared.rows()
                return StatementResult.Select(PREPARED_COLUMNS, rows), len(rows)

            # LINT: returns warnings about the statement and the statements it
            # contains, without planning or executing them.
            case ast.Statement.Lint(inner):
                warnings = self.with_checked_txn(True, statement, lambda txn: lint(txn, inner))
                rows = [warning.row() for warning in warnings]
                return StatementResult.Select(LINT_COLUMNS, rows), len(rows)

            # SHOW CHANGES: lists the committed changes from the engine's
            # change log. It doesn't use a transaction.
            case ast.Statement.ShowChanges(since):
                self.check(statement)
                changes = self.engine.changes(0 if since is None else since)
                rows = [change.row() for change in changes]
                return StatementResult.Select(CHANGE_COLUMNS, rows), len(rows)

            # SHOW AUDIT: lists the server's audit log. It doesn't use a
            # transaction.
            case ast.Statement.ShowAudit(since):
                self.check(statement)
                with self.engine.audit.lock() as log:
                    rows = [entry.row() for entry in log.since(0 if since is None else since)]
                return StatementResult.Select(audit.AUDIT_COLUMNS, rows), len(rows)

            # SHOW SESSIONS: lists the server's sessions. It doesn't use a
            # transaction.
            case ast.Statement.ShowSessions():
                self.check(statement)
                with self.engine.sessions.lock() as sessions:
                    rows = sessions.rows()
                return StatementResult.Select(audit.SESSIONS_COLUMNS, rows), len(rows)

            # CREATE USER: creates a user.
            case ast.Statement.CreateUser(name, password, superuser):
                self.with_checked_txn(
                    False,
                    statement,
                    lambda txn: users.create_user(txn, name, password, superuser),
                )
                return StatementResult.CreateUser(name), 0

            # DROP USER: drops a user.
            case ast.Statement.DropUser(name, if_exists):
                existed = self.with_checked_txn(
                    False,
                    statement,
                    lambda txn: users.drop_user(txn, name, if_exists, self.user, self.login),
                )
                return StatementResult.DropUser(name, existed), 0

            # GRANT: grants privileges on a table or view, or makes a user a
            # member of another.
            case ast.Statement.Grant(ast.Privileges(privileges, table), user):
                self.with_checked_txn(
                    False, statement, lambda txn: users.grant(txn, privileges, table, user)
                )
                return StatementResult.Grant(table), 0
            case ast.Statement.Grant(str() as group, user):
                self.with_checked_txn(
                    False, statement, lambda txn: users.grant_membership(txn, group, user)
                )
                return StatementResult.Grant(None), 0

            # REVOKE: revokes privileges on a table or view, or ends a
            # membership.
            case ast.Statement.Revoke(ast.Privileges(privileges, table), user):
                self.with_checked_txn(
                    False, statement, lambda txn: users.revoke(txn, privileges, table, user)
                )
                return StatementResult.Revoke(table), 0
            case ast.Statement.Revoke(str() as group, user):
                self.with_checked_txn(
                    False, statement, lambda txn: users.revoke_membership(txn, group, user)
                )
                return StatementResult.Revoke(None), 0

            # SET ROLE: sets the session's current user.
            case ast.Statement.SetRole(user):

                def check_role(txn: T) -> None:
                    if txn.get_user(user) is None:
                        raise errinput(f"user {user} does not exist")
                    self.check_in(txn, statement)

                self.with_txn(True, check_role)
                self.user = user
                return StatementResult.SetRole(self.user), 0

            # RESET ROLE: resets the session's current user to its login user.
            case ast.Statement.ResetRole():
                self.check(statement)
                self.user = self.login
                return StatementResult.ResetRole(self.user), 0

            # SHOW USERS: lists the users.
            case ast.Statement.ShowUsers():
                rows = self.with_checked_txn(True, statement, users.show_users)
                return StatementResult.Select(users.USERS_COLUMNS, rows), len(rows)

            # SHOW GRANTS: lists the owners and privileges of tables and views.
            case ast.Statement.ShowGrants():
                rows = self.with_checked_txn(True, statement, users.show_grants)
                return StatementResult.Select(users.GRANTS_COLUMNS, rows), len(rows)

            # CREATE POLICY: creates a row-level security policy.
            case ast.Statement.CreatePolicy(name, table, user, using, check):
                self.with_checked_txn(
                    False,
                    statement,
                    lambda txn: policies.create_policy(txn, name, table, user, using, check),
                )
                return StatementResult.CreatePolicy(table, name), 0

            # DROP POLICY: drops a row-level security policy.
            case ast.Statement.DropPolicy(name, table, if_exists):
                existed = self.with_checked_txn(
                    False,
                    statement,
                    lambda txn: policies.drop_policy(txn, name, table, if_exists),
                )
                return StatementResult.DropPolicy(table, name, existed), 0

            # CREATE MATERIALIZED VIEW: creates a materialized view.
            case ast.Statement.CreateMaterializedView(name, columns, query):

                def create_matview(txn: T) -> int:
                    count = matviews.create_matview(txn, name, columns, query, self.user, self)
                    self.set_owner(txn, name)
                    return count

                count = self.with_checked_txn(False, statement, create_matview)
                return StatementResult.CreateMaterializedView(name, count), 0

            # REFRESH MATERIALIZED VIEW: recomputes a materialized view's rows.
            case ast.Statement.RefreshMaterializedView(name):
                count = self.with_checked_txn(
                    False,
                    statement,
                    lambda txn: matviews.refresh_matview(txn, name, self.user, self),
                )
                return StatementResult.RefreshMaterializedView(name, count), 0

            # DROP MATERIALIZED VIEW: drops a materialized view.
            case ast.Statement.DropMaterializedView(name, if_exists):
                existed = self.with_checked_txn(
                    False, statement, lambda txn: matviews.drop_matview(txn, name, if_exists)
                )
                return StatementResult.DropMaterializedView(name, existed), 0

            # Other statements (SELECT etc.) are handled by the SQL executor.
            case other:
                read_only = isinstance(other, ast.Statement.Select)

                def execute(txn: T) -> StatementResult:
                    result = StatementResult.try_from(
                        Plan.build(other, txn).optimize().execute(txn, self.user, self)
                    )
                    if isinstance(result, StatementResult.CreateTable):
                        self.set_owner(txn, result.name)
                    return result

                result = self.with_checked_txn(read_only, other, execute)
                return result, result.row_count()

    def check(self, statement: ast.Statement) -> None:
        """Checks that the current user may execute a statement that doesn't
        use a transaction."""
        self.with_txn(True, lambda txn: self.check_in(txn, statement))

    def check_in(self, txn: T, statement: ast.Statement) -> None:
        """Checks that the current user may execute a statement, in the given
        transaction."""
        privileges.check(txn, statement, self.user, self.login, self.prepared)

    def with_checked_txn(
        self, read_only: bool, statement: ast.Statement, f: Callable[[T], R]
    ) -> R:
        """Runs a closure in the statement's transaction, like with_txn(),
        after checking that the current user may execute the statement."""

        def run(txn: T) -> R:
            self.check_in(txn, statement)
            return f(txn)

        return self.with_txn(read_only, run)

    def set_owner(self, txn: T, name: str) -> None:
        """Makes the current user the owner of a table or view it created.
        Ownership by admin is not stored."""
        if self.user != ADMIN:
            txn.set_owner(name, self.user)

    def backup(self, as_of: mvcc.Version | None = None) -> bytes:
        """Creates a backup of the database (see `toydb.backup`), as of a
        version if given."""

        def create(txn: T) -> bytes:
            self.check_superuser(txn)
            return backup.create(txn)

        if as_of is None:
            return self.with_txn(True, create)
        if self.txn is not None:
            raise errinput("already in a transaction")
        txn = self.engine.begin_as_of(as_of)
        try:
            result = create(txn)
        except Error:
            txn.rollback()
            raise
        txn.commit()
        return result

    def restore(self, data: bytes) -> None:
        """Restores a backup into the database (see `toydb.backup`)."""

        def restore(txn: T) -> None:
            self.check_superuser(txn)
            backup.restore(txn, data, self)

        self.with_txn(False, restore)

    def check_superuser(self, txn: T) -> None:
        """Checks that the current user is a superuser."""
        if not privileges.is_superuser(txn, self.user):
            raise errpermission(f"user {self.user} is not a superuser")

    def describe_columns(self, statement: ast.Statement) -> list[tuple[Label, ColumnDescription]]:
        """Describes the result columns of a statement as DESCRIBE does, with
        their labels, without executing or recording it."""
        inner = self.planned_statement("DESCRIBE", statement)

        def run(txn: T) -> list[tuple[Label, ColumnDescription]]:
            plan = Plan.build(inner, txn)
            match plan:
                case Plan.Select(root):
                    labels = [root.column_label(index) for index in range(root.columns())]
                case _:
                    labels = []
            return list(zip(labels, describe(plan)))

        return self.with_checked_txn(True, ast.Statement.Describe(statement), run)

    def planned_statement(self, word: str, statement: ast.Statement) -> ast.Statement:
        """Returns the planned statement that EXPLAIN, DESCRIBE or ESTIMATE
        (the given word) work on: the statement itself, or for EXECUTE the
        prepared statement with its arguments bound. Errors for other
        statements."""
        if isinstance(statement, ast.Statement.Execute):
            bound, _ = self.prepared.get(statement.name).bind(statement.args)
            return bound
        if (keyword := self.unplanned_keyword(statement)) is not None:
            raise errinput(f"{word} is not supported for {keyword}")
        return statement

    @staticmethod
    def unplanned_keyword(statement: ast.Statement) -> str | None:
        """Returns the leading keywords of a statement that has no plan of its
        own, for errors about statements that require a planned statement, or
        None if the statement has a plan."""
        match statement:
            case (
                ast.Statement.CreateTable()
                | ast.Statement.DropTable()
                | ast.Statement.Delete()
                | ast.Statement.Insert()
                | ast.Statement.Update()
                | ast.Statement.Select()
            ):
                return None
            case ast.Statement.Begin():
                return "BEGIN"
            case ast.Statement.Commit():
                return "COMMIT"
            case ast.Statement.Rollback():
                return "ROLLBACK"
            case ast.Statement.Explain():
                return "EXPLAIN"
            case (
                ast.Statement.ShowStatements()
                | ast.Statement.ShowStatistics()
                | ast.Statement.ShowViews()
                | ast.Statement.ShowPrepared()
                | ast.Statement.ShowChanges()
                | ast.Statement.ShowUsers()
                | ast.Statement.ShowGrants()
                | ast.Statement.ShowAudit()
                | ast.Statement.ShowSessions()
            ):
                return "SHOW"
            case ast.Statement.Analyze():
                return "ANALYZE"
            case ast.Statement.Export():
                return "EXPORT"
            case ast.Statement.Import():
                return "IMPORT"
            case ast.Statement.Format():
                return "FORMAT"
            case ast.Statement.CreateView():
                return "CREATE VIEW"
            case ast.Statement.DropView():
                return "DROP VIEW"
            case ast.Statement.Describe():
                return "DESCRIBE"
            case ast.Statement.Estimate():
                return "ESTIMATE"
            case ast.Statement.Prepare():
                return "PREPARE"
            case ast.Statement.Execute():
                return "EXECUTE"
            case ast.Statement.Deallocate():
                return "DEALLOCATE"
            case ast.Statement.Lint():
                return "LINT"
            case ast.Statement.CreateUser():
                return "CREATE USER"
            case ast.Statement.DropUser():
                return "DROP USER"
            case ast.Statement.Grant():
                return "GRANT"
            case ast.Statement.Revoke():
                return "REVOKE"
            case ast.Statement.SetRole():
                return "SET"
            case ast.Statement.ResetRole():
                return "RESET"
            case ast.Statement.CreatePolicy():
                return "CREATE POLICY"
            case ast.Statement.DropPolicy():
                return "DROP POLICY"
            case ast.Statement.CreateMaterializedView():
                return "CREATE MATERIALIZED"
            case ast.Statement.RefreshMaterializedView():
                return "REFRESH"
            case ast.Statement.DropMaterializedView():
                return "DROP MATERIALIZED"
            case _:
                panic(f"unknown statement {statement!r}")

    def with_txn(self, read_only: bool, f: Callable[[T], R]) -> R:
        """Runs a closure in the session's transaction, if there is one,
        otherwise a temporary implicit transaction. If read_only is true, uses
        a read-only implicit transaction. Does not automatically retry
        errors."""
        # Use the current explicit transaction, if there is one.
        if self.txn is not None:
            return f(self.txn)
        # Otherwise, use an implicit transaction. Doing this session-side
        # results in additional Raft roundtrips to begin and complete the
        # transaction -- we could avoid this if the Raft SQL state machine
        # supported implicit transactions, but we keep it simple.
        txn = self.engine.begin_read_only() if read_only else self.engine.begin()
        try:
            result = f(txn)
        except Error:
            txn.rollback()
            raise
        txn.commit()
        return result

    def status(self: "Session[raft.Transaction]") -> "raft.Status":
        """Returns the Raft SQL engine status. Only available for sessions
        using the `Raft` engine (`impl Session<'_, Raft>` in Rust)."""
        from toydb.sql.engine.raft import Raft

        assert isinstance(self.engine, Raft), "status requires a Raft engine"
        return self.engine.status()

    def close(self) -> None:
        """Removes the session from the engine's session list, and if the
        session has an open transaction when closed, rolls it back. This is
        the Rust `Drop` implementation, and must be called when the session is
        no longer used."""
        with self.engine.sessions.lock() as sessions:
            sessions.remove(self.number)
        if (txn := self.txn) is None:
            return
        self.txn = None
        try:
            txn.rollback()
        except Error as error:
            log.error("implicit transaction rollback failed: %s", error)


class StatementResult(encoding.Value, ABC):
    """A session statement result, returned over the network to SQL clients."""

    Begin: ClassVar[type["Begin"]]
    Commit: ClassVar[type["Commit"]]
    Rollback: ClassVar[type["Rollback"]]
    Explain: ClassVar[type["Explain"]]
    CreateTable: ClassVar[type["CreateTable"]]
    DropTable: ClassVar[type["DropTable"]]
    Delete: ClassVar[type["Delete"]]
    Insert: ClassVar[type["Insert"]]
    Update: ClassVar[type["Update"]]
    Select: ClassVar[type["Select"]]
    Analyze: ClassVar[type["Analyze"]]
    Export: ClassVar[type["Export"]]
    Format: ClassVar[type["Format"]]
    CreateView: ClassVar[type["CreateView"]]
    DropView: ClassVar[type["DropView"]]
    Prepare: ClassVar[type["Prepare"]]
    Deallocate: ClassVar[type["Deallocate"]]
    CreateUser: ClassVar[type["CreateUser"]]
    DropUser: ClassVar[type["DropUser"]]
    Grant: ClassVar[type["Grant"]]
    Revoke: ClassVar[type["Revoke"]]
    SetRole: ClassVar[type["SetRole"]]
    ResetRole: ClassVar[type["ResetRole"]]
    CreatePolicy: ClassVar[type["CreatePolicy"]]
    DropPolicy: ClassVar[type["DropPolicy"]]
    CreateMaterializedView: ClassVar[type["CreateMaterializedView"]]
    RefreshMaterializedView: ClassVar[type["RefreshMaterializedView"]]
    DropMaterializedView: ClassVar[type["DropMaterializedView"]]

    @staticmethod
    def try_from(result: ExecutionResult) -> "StatementResult":
        """Converts an execution result into a statement result."""
        match result:
            case ExecutionResult.CreateTable(name):
                return CreateTable(name)
            case ExecutionResult.DropTable(name, existed):
                return DropTable(name, existed)
            case ExecutionResult.Delete(count):
                return Delete(count)
            case ExecutionResult.Insert(count):
                return Insert(count)
            case ExecutionResult.Update(count):
                return Update(count)
            case ExecutionResult.Select(columns, rows):
                # We buffer the entire set of rows, for simplicity.
                return Select(columns, list(rows))
            case _:
                panic(f"unknown execution result {result!r}")

    def row_count(self) -> int:
        """Returns the number of rows the statement returned (SELECT) or
        wrote (INSERT, UPDATE and DELETE), or 0 for other statements."""
        match self:
            case Select(rows=rows):
                return len(rows)
            case Insert(count) | Update(count) | Delete(count):
                return count
            case _:
                return 0

    def try_into_rows(self) -> Rows:
        """Attempts to convert a SELECT result into a row iterator."""
        match self:
            case Select(rows=rows):
                return IntoIter(rows)
            case _:
                raise errdata(f"expected select result, found {debug(self)}")

    def try_into_row(self) -> Row:
        """Extracts the first row from a SELECT result."""
        if (row := next(self.try_into_rows(), None)) is None:
            raise errdata("no rows returned")
        return row

    def try_into_value(self) -> Value:
        """Extracts the value of the first column in the first row."""
        row = self.try_into_row()
        if not row:
            raise errdata("no columns returned")
        return row[0]

    def try_into_bool(self) -> bool:
        """Extracts the first boolean value of the first column in the first
        row."""
        return self.try_into_value().try_into_bool()

    def try_into_f64(self) -> float:
        """Extracts the first f64 value of the first column in the first row."""
        return self.try_into_value().try_into_f64()

    def try_into_i64(self) -> int:
        """Extracts the first i64 value of the first column in the first row."""
        return self.try_into_value().try_into_i64()

    def try_into_string(self) -> str:
        """Extracts the first string value of the first column in the first
        row."""
        return self.try_into_value().try_into_string()

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case Begin(state):
                serializer.serialize_newtype_variant(0)
                state.serialize(serializer)
            case Commit(version):
                serializer.serialize_struct_variant(1)
                serializer.serialize_u64(version)
            case Rollback(version):
                serializer.serialize_struct_variant(2)
                serializer.serialize_u64(version)
            case Explain(plan):
                serializer.serialize_newtype_variant(3)
                plan.serialize(serializer)
            case CreateTable(name):
                serializer.serialize_struct_variant(4)
                serializer.serialize_str(name)
            case DropTable(name, existed):
                serializer.serialize_struct_variant(5)
                serializer.serialize_str(name)
                serializer.serialize_bool(existed)
            case Delete(count):
                serializer.serialize_struct_variant(6)
                serializer.serialize_u64(count)
            case Insert(count):
                serializer.serialize_struct_variant(7)
                serializer.serialize_u64(count)
            case Update(count):
                serializer.serialize_struct_variant(8)
                serializer.serialize_u64(count)
            case Select(columns, rows):
                serializer.serialize_struct_variant(9)
                COLUMNS.serialize(columns, serializer)
                ROWS.serialize(rows, serializer)
            case Analyze(tables):
                serializer.serialize_struct_variant(10)
                TABLES.serialize(tables, serializer)
            case Export(format, lines):
                serializer.serialize_struct_variant(11)
                format.serialize(serializer)
                LINES.serialize(lines, serializer)
            case Format(lines):
                serializer.serialize_struct_variant(12)
                LINES.serialize(lines, serializer)
            case CreateView(name):
                serializer.serialize_struct_variant(13)
                serializer.serialize_str(name)
            case DropView(name, existed):
                serializer.serialize_struct_variant(14)
                serializer.serialize_str(name)
                serializer.serialize_bool(existed)
            case Prepare(name):
                serializer.serialize_struct_variant(15)
                serializer.serialize_str(name)
            case Deallocate(name):
                serializer.serialize_struct_variant(16)
                serializer.serialize_str(name)
            case CreateUser(name):
                serializer.serialize_struct_variant(17)
                serializer.serialize_str(name)
            case DropUser(name, existed):
                serializer.serialize_struct_variant(18)
                serializer.serialize_str(name)
                serializer.serialize_bool(existed)
            case Grant(table):
                serializer.serialize_struct_variant(19)
                serde.Option(serde.String).serialize(table, serializer)
            case Revoke(table):
                serializer.serialize_struct_variant(20)
                serde.Option(serde.String).serialize(table, serializer)
            case SetRole(user):
                serializer.serialize_struct_variant(21)
                serializer.serialize_str(user)
            case ResetRole(user):
                serializer.serialize_struct_variant(22)
                serializer.serialize_str(user)
            case CreatePolicy(table, name):
                serializer.serialize_struct_variant(23)
                serializer.serialize_str(table)
                serializer.serialize_str(name)
            case DropPolicy(table, name, existed):
                serializer.serialize_struct_variant(24)
                serializer.serialize_str(table)
                serializer.serialize_str(name)
                serializer.serialize_bool(existed)
            case CreateMaterializedView(name, count):
                serializer.serialize_struct_variant(25)
                serializer.serialize_str(name)
                serializer.serialize_u64(count)
            case RefreshMaterializedView(name, count):
                serializer.serialize_struct_variant(26)
                serializer.serialize_str(name)
                serializer.serialize_u64(count)
            case DropMaterializedView(name, existed):
                serializer.serialize_struct_variant(27)
                serializer.serialize_str(name)
                serializer.serialize_bool(existed)
            case _:
                panic(f"unknown statement result {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "StatementResult":
        match deserializer.deserialize_enum():
            case 0:
                return Begin(mvcc.TransactionState.deserialize(deserializer))
            case 1:
                return Commit(deserializer.deserialize_u64())
            case 2:
                return Rollback(deserializer.deserialize_u64())
            case 3:
                return Explain(Plan.deserialize(deserializer))
            case 4:
                return CreateTable(deserializer.deserialize_str())
            case 5:
                return DropTable(deserializer.deserialize_str(), deserializer.deserialize_bool())
            case 6:
                return Delete(deserializer.deserialize_u64())
            case 7:
                return Insert(deserializer.deserialize_u64())
            case 8:
                return Update(deserializer.deserialize_u64())
            case 9:
                return Select(COLUMNS.deserialize(deserializer), ROWS.deserialize(deserializer))
            case 10:
                return Analyze(TABLES.deserialize(deserializer))
            case 11:
                format = ast.ExportFormat.deserialize(deserializer)
                return Export(format, LINES.deserialize(deserializer))
            case 12:
                return Format(LINES.deserialize(deserializer))
            case 13:
                return CreateView(deserializer.deserialize_str())
            case 14:
                return DropView(deserializer.deserialize_str(), deserializer.deserialize_bool())
            case 15:
                return Prepare(deserializer.deserialize_str())
            case 16:
                return Deallocate(deserializer.deserialize_str())
            case 17:
                return CreateUser(deserializer.deserialize_str())
            case 18:
                return DropUser(deserializer.deserialize_str(), deserializer.deserialize_bool())
            case 19:
                return Grant(serde.Option(serde.String).deserialize(deserializer))
            case 20:
                return Revoke(serde.Option(serde.String).deserialize(deserializer))
            case 21:
                return SetRole(deserializer.deserialize_str())
            case 22:
                return ResetRole(deserializer.deserialize_str())
            case 23:
                return CreatePolicy(deserializer.deserialize_str(), deserializer.deserialize_str())
            case 24:
                table, name = deserializer.deserialize_str(), deserializer.deserialize_str()
                return DropPolicy(table, name, deserializer.deserialize_bool())
            case 25:
                name = deserializer.deserialize_str()
                return CreateMaterializedView(name, deserializer.deserialize_u64())
            case 26:
                name = deserializer.deserialize_str()
                return RefreshMaterializedView(name, deserializer.deserialize_u64())
            case 27:
                name = deserializer.deserialize_str()
                return DropMaterializedView(name, deserializer.deserialize_bool())
            case index:
                raise deserializer.invalid_variant(index, 28)


@dataclass
class Begin(StatementResult):
    state: mvcc.TransactionState

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Begin", [self.state], pretty)


@dataclass
class Commit(StatementResult):
    version: mvcc.Version


@dataclass
class Rollback(StatementResult):
    version: mvcc.Version


@dataclass
class Explain(StatementResult):
    plan: Plan

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Explain", [self.plan], pretty)


@dataclass
class CreateTable(StatementResult):
    name: str


@dataclass
class DropTable(StatementResult):
    name: str
    existed: bool


@dataclass
class Delete(StatementResult):
    count: int


@dataclass
class Insert(StatementResult):
    count: int


@dataclass
class Update(StatementResult):
    count: int


@dataclass
class Select(StatementResult):
    """For simplicity, we buffer and send the entire set of rows as a list
    instead of streaming them to the client. Streaming reads haven't been
    implemented from Raft either, so they're buffered all the way through."""

    columns: list[Label]
    rows: list[Row]


@dataclass
class Analyze(StatementResult):
    tables: list[str]
    """The analyzed tables, in the order they were analyzed."""


@dataclass
class Export(StatementResult):
    format: ast.ExportFormat
    lines: list[str]


@dataclass
class Format(StatementResult):
    """The lines of FORMAT."""

    lines: list[str]


@dataclass
class CreateView(StatementResult):
    name: str


@dataclass
class DropView(StatementResult):
    name: str
    existed: bool


@dataclass
class Prepare(StatementResult):
    name: str


@dataclass
class Deallocate(StatementResult):
    name: str


@dataclass
class CreateUser(StatementResult):
    name: str


@dataclass
class DropUser(StatementResult):
    name: str
    existed: bool


@dataclass
class Grant(StatementResult):
    table: str | None


@dataclass
class Revoke(StatementResult):
    table: str | None


@dataclass
class SetRole(StatementResult):
    user: str


@dataclass
class ResetRole(StatementResult):
    user: str


@dataclass
class CreatePolicy(StatementResult):
    table: str
    name: str


@dataclass
class DropPolicy(StatementResult):
    table: str
    name: str
    existed: bool


@dataclass
class CreateMaterializedView(StatementResult):
    name: str
    count: int


@dataclass
class RefreshMaterializedView(StatementResult):
    name: str
    count: int


@dataclass
class DropMaterializedView(StatementResult):
    name: str
    existed: bool


StatementResult.Begin = Begin
StatementResult.Commit = Commit
StatementResult.Rollback = Rollback
StatementResult.Explain = Explain
StatementResult.CreateTable = CreateTable
StatementResult.DropTable = DropTable
StatementResult.Delete = Delete
StatementResult.Insert = Insert
StatementResult.Update = Update
StatementResult.Select = Select
StatementResult.Analyze = Analyze
StatementResult.Export = Export
StatementResult.Format = Format
StatementResult.CreateView = CreateView
StatementResult.DropView = DropView
StatementResult.Prepare = Prepare
StatementResult.Deallocate = Deallocate
StatementResult.CreateUser = CreateUser
StatementResult.DropUser = DropUser
StatementResult.Grant = Grant
StatementResult.Revoke = Revoke
StatementResult.SetRole = SetRole
StatementResult.ResetRole = ResetRole
StatementResult.CreatePolicy = CreatePolicy
StatementResult.DropPolicy = DropPolicy
StatementResult.CreateMaterializedView = CreateMaterializedView
StatementResult.RefreshMaterializedView = RefreshMaterializedView
StatementResult.DropMaterializedView = DropMaterializedView

COLUMNS: serde.Serde[list[Label]] = serde.Vec(Label)
"""The encoding of the result columns, a `Vec<Label>`."""

ROWS: serde.Serde[list[Row]] = serde.Vec(serde.Vec(Value))
"""The encoding of the result rows, a `Vec<Row>`."""

TABLES: serde.Serde[list[str]] = serde.Vec(serde.String)
"""The encoding of the analyzed tables, a `Vec<String>`."""

LINES: serde.Serde[list[str]] = serde.Vec(serde.String)
"""The encoding of the exported and formatted lines, a `Vec<String>`."""
