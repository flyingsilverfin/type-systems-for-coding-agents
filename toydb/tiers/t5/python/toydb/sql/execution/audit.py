"""The audit log and the session list of a server, for `SHOW AUDIT` and
`SHOW SESSIONS`.

Every statement that a session executes, successfully or not, is recorded
after it is executed — except statements that fail to parse and SHOW AUDIT —
with the session's current user, the statement's class, its name (its leading
keywords in lower case joined by `_`, without optional words and operands, as
in JSON results) and its accesses as objects (`<name> (<kind>)`), and its
error. The class follows from the statement and its accesses, the first that
applies:

1. `ROLE`: CREATE USER, DROP USER, GRANT, REVOKE;
2. `DDL`: a `create`, `drop`, `alter` or `policy` access;
3. `WRITE`: an `insert`, `update`, `delete` or `refresh` access;
4. `READ`: a `read` access;
5. `MISC`: every other statement.

Statements added later get their class from their accesses by the same rule.
Sessions are numbered in the order the server creates them. Both lists are
kept in memory, are not written to storage and are not replicated.
"""

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toydb.error import panic
from toydb.sql.execution.privileges import Access, AccessKind
from toydb.sql.parser import ast
from toydb.sql.types import Label, Row, Value

if TYPE_CHECKING:
    from toydb.sql.execution.session import Session

AUDIT_COLUMNS: list[Label] = [
    Label.Unqualified(name)
    for name in ("seq", "username", "class", "statement", "objects", "error")
]
"""The SHOW AUDIT result columns."""

SESSIONS_COLUMNS: list[Label] = [
    Label.Unqualified(name) for name in ("session", "username", "login", "txn", "executed")
]
"""The SHOW SESSIONS result columns."""


class AuditClass(enum.Enum):
    """The audit class of a statement."""

    Role = "ROLE"
    Ddl = "DDL"
    Write = "WRITE"
    Read = "READ"
    Misc = "MISC"

    def __str__(self) -> str:
        return self.value


def classify(statement: ast.Statement, accesses: list[Access]) -> AuditClass:
    """Returns the audit class of a statement, from its kind and its
    accesses."""
    if isinstance(
        statement,
        (
            ast.Statement.CreateUser,
            ast.Statement.DropUser,
            ast.Statement.Grant,
            ast.Statement.Revoke,
        ),
    ):
        return AuditClass.Role
    kinds = {access.kind for access in accesses}
    if kinds & {AccessKind.Create, AccessKind.Drop, AccessKind.Alter, AccessKind.Policy}:
        return AuditClass.Ddl
    if kinds & {AccessKind.Insert, AccessKind.Update, AccessKind.Delete, AccessKind.Refresh}:
        return AuditClass.Write
    if AccessKind.Read in kinds:
        return AuditClass.Read
    return AuditClass.Misc


def statement_name(statement: ast.Statement) -> str:
    """Returns the name of a statement."""
    match statement:
        case ast.Statement.Begin():
            return "begin"
        case ast.Statement.Commit():
            return "commit"
        case ast.Statement.Rollback():
            return "rollback"
        case ast.Statement.Explain():
            return "explain"
        case ast.Statement.CreateTable():
            return "create_table"
        case ast.Statement.DropTable():
            return "drop_table"
        case ast.Statement.Delete():
            return "delete"
        case ast.Statement.Insert():
            return "insert"
        case ast.Statement.Update():
            return "update"
        case ast.Statement.Select():
            return "select"
        case ast.Statement.ShowStatements():
            return "show_statements"
        case ast.Statement.Analyze():
            return "analyze"
        case ast.Statement.ShowStatistics():
            return "show_statistics"
        case ast.Statement.Export():
            return "export"
        case ast.Statement.Import():
            return "import"
        case ast.Statement.Format():
            return "format"
        case ast.Statement.CreateView():
            return "create_view"
        case ast.Statement.DropView():
            return "drop_view"
        case ast.Statement.ShowViews():
            return "show_views"
        case ast.Statement.Describe():
            return "describe"
        case ast.Statement.Estimate():
            return "estimate"
        case ast.Statement.Prepare():
            return "prepare"
        case ast.Statement.Execute():
            return "execute"
        case ast.Statement.Deallocate():
            return "deallocate"
        case ast.Statement.ShowPrepared():
            return "show_prepared"
        case ast.Statement.Lint():
            return "lint"
        case ast.Statement.ShowChanges():
            return "show_changes"
        case ast.Statement.CreateUser():
            return "create_user"
        case ast.Statement.DropUser():
            return "drop_user"
        case ast.Statement.Grant():
            return "grant"
        case ast.Statement.Revoke():
            return "revoke"
        case ast.Statement.SetRole():
            return "set_role"
        case ast.Statement.ResetRole():
            return "reset_role"
        case ast.Statement.ShowUsers():
            return "show_users"
        case ast.Statement.ShowGrants():
            return "show_grants"
        case ast.Statement.ShowAudit():
            return "show_audit"
        case ast.Statement.ShowSessions():
            return "show_sessions"
        case ast.Statement.CreatePolicy():
            return "create_policy"
        case ast.Statement.DropPolicy():
            return "drop_policy"
        case ast.Statement.CreateMaterializedView():
            return "create_materialized_view"
        case ast.Statement.RefreshMaterializedView():
            return "refresh_materialized_view"
        case ast.Statement.DropMaterializedView():
            return "drop_materialized_view"
        case _:
            panic(f"unknown statement {statement!r}")


@dataclass(frozen=True)
class Entry:
    """An audit log entry: the user, class, statement name, objects and error
    of an executed statement."""

    seq: int
    user: str
    class_: AuditClass
    statement: str
    objects: list[Access]
    error: str | None

    def row(self) -> Row:
        """Returns the entry as a SHOW AUDIT row."""
        return [
            Value.Integer(self.seq),
            Value.String(self.user),
            Value.String(str(self.class_)),
            Value.String(self.statement),
            Value.String(", ".join(str(access) for access in self.objects)),
            Value.String(self.error) if self.error is not None else Value.Null(),
        ]


class AuditLog:
    """The audit log of a server: the entries in the order the statements
    were executed."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []

    def record(
        self,
        user: str,
        statement: ast.Statement,
        accesses: list[Access],
        error: str | None,
        name: str | None = None,
    ) -> None:
        """Records an executed statement, by its statement name or the given
        name."""
        self.entries.append(
            Entry(
                seq=len(self.entries) + 1,
                user=user,
                class_=classify(statement, accesses),
                statement=statement_name(statement) if name is None else name,
                objects=accesses,
                error=error,
            )
        )

    def since(self, seq: int) -> list[Entry]:
        """Returns the entries with a sequence number above the given one."""
        return self.entries[seq:]


class SessionList:
    """The open sessions of a server, by number."""

    def __init__(self) -> None:
        self.sessions: dict[int, "Session"] = {}
        self.next = 1

    def add(self, session: "Session") -> int:
        """Adds a session, returning its number."""
        number = self.next
        self.next += 1
        self.sessions[number] = session
        return number

    def remove(self, number: int) -> None:
        """Removes a session, if it is open."""
        self.sessions.pop(number, None)

    def rows(self) -> list[Row]:
        """Returns the SHOW SESSIONS rows."""
        rows = []
        for number, session in sorted(self.sessions.items()):
            txn = session.txn
            rows.append(
                [
                    Value.Integer(number),
                    Value.String(session.user),
                    Value.String(session.login),
                    Value.Integer(txn.state().version) if txn is not None else Value.Null(),
                    Value.Integer(session.executed),
                ]
            )
        return rows
