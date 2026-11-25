"""Statement accesses and privilege checks.

The accesses of a statement are the tables, views and materialized views it
uses, each with a kind: `read` (reads rows or statistics), `insert`, `update`,
`delete` (writes rows), `create`, `drop`, `alter` (changes the definition of a
table: adds, drops or renames columns or otherwise changes its schema display),
`policy` (creates or drops a policy of a table), `analyze` (stores statistics),
`refresh` (recomputes a materialized view), `grant` (grants or revokes
privileges on it).

* SELECT reads every table, view and materialized view of its FROM clause;
  INSERT, UPDATE, DELETE and IMPORT write their table; CREATE and DROP create
  and drop theirs; CREATE VIEW creates the view and reads the tables and views
  of its query; ANALYZE analyzes its table, or every table; SHOW STATISTICS and
  EXPORT read theirs, EXPORT AS SQL every table and view; EXPLAIN, DESCRIBE and
  ESTIMATE of a statement have that statement's accesses, EXECUTE those of the
  statement it executes; GRANT and REVOKE on a table grant it; CREATE and DROP
  POLICY have the `policy` access of their table; CREATE MATERIALIZED VIEW
  creates it and reads the tables and views of its query, REFRESH has the
  `refresh` access, DROP the `drop` access. Other statements have none.
* A statement added later has the accesses that follow from what it does:
  `read` of every table and view named in a FROM clause of a query it contains
  and executes, `insert`, `update` or `delete` of a table whose rows it writes,
  `create` or `drop` of what it creates or drops, `alter` of a table whose
  definition it changes. A part of an existing statement added later (a clause,
  an option, a query in place of a `VALUES` list) adds the accesses that follow
  from what it does in the same way.
* Accesses are in the order of the statement's canonical text, each name and
  kind once.

Every statement is checked against the session's current user before it is
executed, in the statement's transaction: `read` requires the `SELECT`
privilege, `insert`, `update` and `delete` the privilege of that name, `drop`,
`alter`, `policy`, `analyze`, `refresh` and `grant` acting for the owner,
`create` nothing. A user has a privilege if it is a superuser, acts for the
owner, or acts for a user it was granted to. Accesses to names that are neither
tables, views nor materialized views are not checked. CREATE and DROP USER,
GRANT and REVOKE of memberships, SHOW CHANGES, SHOW AUDIT and backups require a
superuser.
"""

import enum
from dataclasses import dataclass

from toydb.error import errpermission, panic
from toydb.sql.engine import Catalog
from toydb.sql.execution.prepared import PreparedStatements
from toydb.sql.execution.system import is_system_table
from toydb.sql.execution.views import dependencies
from toydb.sql.parser import ast
from toydb.sql.types.user import ADMIN, Privilege


class AccessKind(enum.Enum):
    """The kind of an access: what a statement does with a table or view."""

    Read = "read"
    Insert = "insert"
    Update = "update"
    Delete = "delete"
    Create = "create"
    Drop = "drop"
    Alter = "alter"
    Policy = "policy"
    Analyze = "analyze"
    Refresh = "refresh"
    Grant = "grant"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Access:
    """An access of a statement: a table or view, by name, and its kind."""

    name: str
    kind: AccessKind

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"


def accesses(
    catalog: Catalog, statement: ast.Statement, prepared: PreparedStatements
) -> list[Access]:
    """Returns the accesses of a statement, in the order of its canonical
    text."""
    found: list[Access] = []

    def add(name: str, kind: AccessKind) -> None:
        if (access := Access(name, kind)) not in found:
            found.append(access)

    match statement:
        case ast.Statement.Select() as select:
            for name in dependencies(select):
                if not is_system_table(catalog, name):
                    add(name, AccessKind.Read)
        case ast.Statement.Insert(table=table) | ast.Statement.Import(table=table):
            add(table, AccessKind.Insert)
        case ast.Statement.Update(table=table):
            add(table, AccessKind.Update)
        case ast.Statement.Delete(table=table):
            add(table, AccessKind.Delete)
        case ast.Statement.CreateTable(name=name):
            add(name, AccessKind.Create)
        case (
            ast.Statement.DropTable(name=name)
            | ast.Statement.DropView(name=name)
            | ast.Statement.DropMaterializedView(name=name)
        ):
            add(name, AccessKind.Drop)
        case ast.Statement.CreateView(name=name, query=query) | ast.Statement.CreateMaterializedView(
            name=name, query=query
        ):
            add(name, AccessKind.Create)
            for dependency in dependencies(query):
                if not is_system_table(catalog, dependency):
                    add(dependency, AccessKind.Read)
        case ast.Statement.Analyze(table):
            if table is not None:
                add(table, AccessKind.Analyze)
            else:
                for name in sorted(table.name for table in catalog.list_tables()):
                    add(name, AccessKind.Analyze)
        case ast.Statement.ShowStatistics(table):
            add(table, AccessKind.Read)
        case ast.Statement.Export(table):
            if table is not None:
                add(table, AccessKind.Read)
            else:
                tables = [table.name for table in catalog.list_tables()]
                views = [view.name for view in catalog.list_views()]
                matviews = [matview.name for matview in catalog.list_matviews()]
                for name in sorted(tables + views + matviews):
                    add(name, AccessKind.Read)
        case (
            ast.Statement.Explain(inner)
            | ast.Statement.Describe(ast.Statement() as inner)
            | ast.Statement.Estimate(inner)
        ):
            return accesses(catalog, inner, prepared)
        case ast.Statement.Execute(name, args):
            bound, _ = prepared.get(name).bind(args)
            return accesses(catalog, bound, prepared)
        case ast.Statement.Grant(ast.Privileges(table=table)) | ast.Statement.Revoke(
            ast.Privileges(table=table)
        ):
            add(table, AccessKind.Grant)
        case ast.Statement.CreatePolicy(table=table) | ast.Statement.DropPolicy(table=table):
            add(table, AccessKind.Policy)
        case ast.Statement.RefreshMaterializedView(name):
            add(name, AccessKind.Refresh)

        case (
            ast.Statement.Begin()
            | ast.Statement.Commit()
            | ast.Statement.Rollback()
            | ast.Statement.ShowStatements()
            | ast.Statement.Format()
            | ast.Statement.ShowViews()
            | ast.Statement.Describe()
            | ast.Statement.Prepare()
            | ast.Statement.Deallocate()
            | ast.Statement.ShowPrepared()
            | ast.Statement.Lint()
            | ast.Statement.ShowChanges()
            | ast.Statement.CreateUser()
            | ast.Statement.DropUser()
            | ast.Statement.Grant()
            | ast.Statement.Revoke()
            | ast.Statement.SetRole()
            | ast.Statement.ResetRole()
            | ast.Statement.ShowUsers()
            | ast.Statement.ShowGrants()
            | ast.Statement.ShowAudit()
            | ast.Statement.ShowSessions()
        ):
            pass

        case _:
            panic(f"unknown statement {statement!r}")
    return found


def check(
    catalog: Catalog,
    statement: ast.Statement,
    user: str,
    login: str,
    prepared: PreparedStatements,
) -> None:
    """Checks that the current user may execute a statement: the privileges
    its accesses require, then its own requirement."""
    superuser = is_superuser(catalog, user)
    if not superuser:
        for access in accesses(catalog, statement, prepared):
            check_access(catalog, user, access)

    match statement:
        case (
            ast.Statement.CreateUser()
            | ast.Statement.DropUser()
            | ast.Statement.Grant(str())
            | ast.Statement.Revoke(str())
            | ast.Statement.ShowChanges()
            | ast.Statement.ShowAudit()
        ):
            if not superuser:
                raise errpermission(f"user {user} is not a superuser")
        case ast.Statement.SetRole(role):
            if not is_superuser(catalog, login) and role not in acts_for(catalog, login):
                raise errpermission(f"user {login} can't set role {role}")
        case (
            ast.Statement.Begin()
            | ast.Statement.Commit()
            | ast.Statement.Rollback()
            | ast.Statement.Explain()
            | ast.Statement.CreateTable()
            | ast.Statement.DropTable()
            | ast.Statement.Delete()
            | ast.Statement.Insert()
            | ast.Statement.Update()
            | ast.Statement.Select()
            | ast.Statement.ShowStatements()
            | ast.Statement.Analyze()
            | ast.Statement.ShowStatistics()
            | ast.Statement.Export()
            | ast.Statement.Import()
            | ast.Statement.Format()
            | ast.Statement.CreateView()
            | ast.Statement.DropView()
            | ast.Statement.ShowViews()
            | ast.Statement.Describe()
            | ast.Statement.Estimate()
            | ast.Statement.Prepare()
            | ast.Statement.Execute()
            | ast.Statement.Deallocate()
            | ast.Statement.ShowPrepared()
            | ast.Statement.Lint()
            | ast.Statement.Grant()
            | ast.Statement.Revoke()
            | ast.Statement.ResetRole()
            | ast.Statement.ShowUsers()
            | ast.Statement.ShowGrants()
            | ast.Statement.ShowSessions()
            | ast.Statement.CreatePolicy()
            | ast.Statement.DropPolicy()
            | ast.Statement.CreateMaterializedView()
            | ast.Statement.RefreshMaterializedView()
            | ast.Statement.DropMaterializedView()
        ):
            pass
        case _:
            panic(f"unknown statement {statement!r}")


def check_access(catalog: Catalog, user: str, access: Access) -> None:
    """Checks that a user may make an access."""
    name, kind = access.name, access.kind
    if kind == AccessKind.Create:
        return
    if (
        catalog.get_table(name) is None
        and catalog.get_view(name) is None
        and catalog.get_matview(name) is None
    ):
        return
    match kind:
        case AccessKind.Read:
            privilege = Privilege.Select
        case AccessKind.Insert:
            privilege = Privilege.Insert
        case AccessKind.Update:
            privilege = Privilege.Update
        case AccessKind.Delete:
            privilege = Privilege.Delete
        case (
            AccessKind.Drop
            | AccessKind.Alter
            | AccessKind.Policy
            | AccessKind.Analyze
            | AccessKind.Refresh
            | AccessKind.Grant
        ):
            if not is_superuser(catalog, user) and not acts_for_owner(catalog, user, name):
                raise errpermission(f"user {user} is not the owner of {name}")
            return
        case _:
            panic(f"unknown access kind {kind!r}")
    if not has_privilege(catalog, user, privilege, name):
        raise errpermission(f"user {user} has no {privilege} privilege on {name}")


def has_privilege(catalog: Catalog, user: str, privilege: Privilege, object: str) -> bool:
    """Returns true if a user has a privilege on a table or view: it is a
    superuser, acts for the owner, or acts for a user the privilege was granted
    to."""
    if is_superuser(catalog, user):
        return True
    acting = acts_for(catalog, user)
    if catalog.get_owner(object) in acting:
        return True
    return any(
        grant.object == object and grant.grantee in acting and privilege in grant.privileges
        for grant in catalog.list_grants()
    )


def acts_for_owner(catalog: Catalog, user: str, object: str) -> bool:
    """Returns true if a user acts for the owner of a table or view."""
    return catalog.get_owner(object) in acts_for(catalog, user)


def is_superuser(catalog: Catalog, user: str) -> bool:
    """Returns true if a user exists and is a superuser."""
    if user == ADMIN:
        return True  # admin isn't stored, so don't look it up
    found = catalog.get_user(user)
    return found is not None and found.superuser


def acts_for(catalog: Catalog, user: str) -> set[str]:
    """Returns the users that a user acts for: itself, the users it is a
    member of, and the users those act for."""
    memberships = catalog.list_memberships()
    acting = {user}
    pending = [user]
    while pending:
        current = pending.pop()
        for member, group in memberships:
            if member == current and group not in acting:
                acting.add(group)
                pending.append(group)
    return acting
