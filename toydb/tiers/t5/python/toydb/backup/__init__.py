"""Backups of a whole database: created in one transaction (a consistent
snapshot), restored in one transaction, both by a superuser.

A backup holds the users and memberships, the tables with their owners, check
constraints and rows, the views, the grants and the policies — not
statistics, prepared statements, digests or the logs. A backup can be created
as of an earlier version. A restore first checks the whole file, then applies
its records in order with the effects and errors of the corresponding
statements; the first error ends it and nothing is restored.
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING

from toydb.backup.format import Record, decode, encode
from toydb.error import errinput, panic
from toydb.sql.engine import Transaction
from toydb.sql.execution import checks, matviews, policies, users
from toydb.sql.execution.export import ordered_tables, ordered_views
from toydb.sql.execution.importer import insert_rows
from toydb.sql.execution.views import create_view
from toydb.sql.parser import Parser, ast
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.user import ADMIN
from toydb.sql.types.view import View

if TYPE_CHECKING:
    from toydb.sql.execution.session import Session


def create(txn: Transaction) -> bytes:
    """Creates a backup of the database the transaction sees."""
    return encode(list(records(txn)))


def records(txn: Transaction) -> Iterator[Record]:
    """Returns the records of the database, in the order of the file."""
    for user in sorted(txn.list_users(), key=lambda user: user.name):
        if user.name != ADMIN:
            yield Record.User(user)
    for member, user in sorted(txn.list_memberships()):
        yield Record.Member(member, user)

    tables = ordered_tables(txn.list_tables())
    for table in tables:
        yield Record.Table(table, txn.get_owner(table.name))
        for check in txn.get_checks(table.name):
            yield Record.Check(table.name, check)
        for row in txn.scan(table.name, None):
            yield Record.Row(table.name, row)
    for view in ordered_views([*txn.list_views(), *txn.list_matviews()]):
        match view:
            case View(name, columns, query):
                yield Record.View(name, txn.get_owner(name), columns, query)
            case MaterializedView(name, columns, query):
                yield Record.MatView(name, txn.get_owner(name), columns, query)

    for grant in sorted(txn.list_grants(), key=lambda grant: (grant.object, grant.grantee)):
        yield Record.Grant(grant)
    for table in sorted(tables, key=lambda table: table.name):
        for policy in txn.list_policies(table.name):
            yield Record.Policy(policy)


def restore(txn: Transaction, data: bytes, session: "Session | None" = None) -> None:
    """Restores a backup into the database, in the transaction."""
    for record in decode(data):
        apply(txn, record, session)


def apply(txn: Transaction, record: Record, session: "Session | None" = None) -> None:
    """Applies a record as the statement it corresponds to."""
    match record:
        case Record.User(user):
            if not user.name:
                raise errinput("user name can't be empty")
            txn.create_user(user)
        case Record.Member(member, user):
            users.grant_membership(txn, user, member)
        case Record.Table(table, owner):
            check_user(txn, owner)
            txn.create_table(table)
            set_owner(txn, table.name, owner)
        case Record.Check(table, check):
            checks.add_check(txn, table, check)
        case Record.Row(table, row):
            schema = txn.must_get_table(table)
            insert_rows(txn, schema, [({i: i for i in range(len(row))}, row)], ADMIN)
        case Record.View(name, owner, columns, query):
            check_user(txn, owner)
            match Parser.parse(query):
                case ast.Statement.Select() as select:
                    create_view(txn, name, columns, select)
                case _:
                    raise errinput(f"view query is not a SELECT: {query}")
            set_owner(txn, name, owner)
        case Record.MatView(name, owner, columns, query):
            check_user(txn, owner)
            match Parser.parse(query):
                case ast.Statement.Select() as select:
                    matviews.create_matview(txn, name, columns, select, ADMIN, session)
                case _:
                    raise errinput(f"view query is not a SELECT: {query}")
            set_owner(txn, name, owner)
        case Record.Grant(grant):
            users.grant(txn, grant.privileges, grant.object, grant.grantee)
        case Record.Policy(policy):
            using = Parser.parse_expr(policy.condition)
            check = Parser.parse_expr(policy.check) if policy.check is not None else None
            policies.create_policy(txn, policy.name, policy.table, policy.user, using, check)
        case _:
            panic(f"unknown record {record!r}")


def check_user(txn: Transaction, name: str) -> None:
    """Checks that a user exists."""
    if txn.get_user(name) is None:
        raise errinput(f"user {name} does not exist")


def set_owner(txn: Transaction, name: str, owner: str) -> None:
    """Sets the owner of a restored table or view. Ownership by admin is not
    stored."""
    if owner != ADMIN:
        txn.set_owner(name, owner)
