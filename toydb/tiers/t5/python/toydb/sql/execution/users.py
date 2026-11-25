"""CREATE USER, DROP USER, GRANT, REVOKE, SET ROLE, RESET ROLE, SHOW USERS and
SHOW GRANTS, and login.

* Users are created and dropped by superusers. Dropping a user removes its
  memberships and the privileges granted to it; a user that owns a table or
  view, or that a policy names, can't be dropped, nor can `admin` or the
  session's current or login user.
* GRANT and REVOKE of privileges on a table or view require acting for its
  owner; on a view only SELECT can be granted. GRANT and REVOKE of a user make
  a user a member of another one or end that; they require a superuser, and
  memberships can't form a cycle.
* SET ROLE makes another user the session's current user, if the login user
  is a superuser or acts for it; RESET ROLE returns to the login user. Neither
  is transactional.
* SHOW GRANTS lists, for every table and view, its owner as the privilege
  `OWNER` and the granted privileges.
"""

import hashlib

from toydb.error import errinput, errpermission
from toydb.sql.engine import Catalog
from toydb.sql.execution.privileges import acts_for
from toydb.sql.types import Label, Row, Value
from toydb.sql.types.user import ADMIN, Privilege, User

USERS_COLUMNS: list[Label] = [
    Label.Unqualified(name) for name in ("name", "is_superuser", "has_password", "member_of")
]
"""The SHOW USERS result columns."""

GRANTS_COLUMNS: list[Label] = [
    Label.Unqualified(name) for name in ("object", "grantee", "privilege")
]
"""The SHOW GRANTS result columns."""


def create_user(catalog: Catalog, name: str, password: str | None, superuser: bool) -> None:
    """Creates a user."""
    if not name:
        raise errinput("user name can't be empty")
    hashed = hash_password(name, password) if password is not None else None
    catalog.create_user(User(name=name, superuser=superuser, password=hashed))


def drop_user(catalog: Catalog, name: str, if_exists: bool, current: str, login: str) -> bool:
    """Drops a user, its memberships and its privileges. Returns true if the
    user existed."""
    if catalog.get_user(name) is None:
        if if_exists:
            return False
        raise errinput(f"user {name} does not exist")
    if name == ADMIN:
        raise errinput(f"user {name} can't be dropped")
    if name in (current, login):
        raise errinput(f"can't drop the current user {name}")
    for object in objects(catalog):
        if catalog.get_owner(object) == name:
            raise errinput(f"user {name} owns {object}")
    for table in catalog.list_tables():
        for policy in catalog.list_policies(table.name):
            if policy.user == name:
                raise errinput(f"user {name} is used by policy {policy.name} on table {table.name}")
    catalog.drop_user(name)
    return True


def grant(catalog: Catalog, privileges: list[Privilege], object: str, user: str) -> None:
    """Grants privileges on a table or view to a user."""
    held = check_grant(catalog, privileges, object, user)
    if not set(privileges) <= held:
        catalog.set_grant(object, user, sorted(held | set(privileges)))


def revoke(catalog: Catalog, privileges: list[Privilege], object: str, user: str) -> None:
    """Revokes privileges on a table or view from a user."""
    held = check_grant(catalog, privileges, object, user)
    if held & set(privileges):
        catalog.set_grant(object, user, sorted(held - set(privileges)))


def check_grant(
    catalog: Catalog, privileges: list[Privilege], object: str, user: str
) -> set[Privilege]:
    """Checks a GRANT or REVOKE of privileges on a table or view, and returns
    the privileges the user holds on it."""
    if catalog.get_table(object) is None:
        if catalog.get_view(object) is None and catalog.get_matview(object) is None:
            raise errinput(f"table {object} does not exist")
        if any(privilege != Privilege.Select for privilege in privileges):
            raise errinput(f"only SELECT can be granted on view {object}")
    if catalog.get_user(user) is None:
        raise errinput(f"user {user} does not exist")
    for grant in catalog.list_grants():
        if grant.object == object and grant.grantee == user:
            return set(grant.privileges)
    return set()


def grant_membership(catalog: Catalog, group: str, user: str) -> None:
    """Makes a user a member of another user, unless that would create a
    cycle."""
    check_membership(catalog, group, user)
    if group == user:
        raise errinput(f"user {user} can't be a member of itself")
    if user in acts_for(catalog, group):
        raise errinput(f"granting {group} to {user} would create a cycle")
    if (user, group) not in catalog.list_memberships():
        catalog.set_membership(user, group, True)


def revoke_membership(catalog: Catalog, group: str, user: str) -> None:
    """Ends a user's membership of another user, if it is a member."""
    check_membership(catalog, group, user)
    if (user, group) in catalog.list_memberships():
        catalog.set_membership(user, group, False)


def check_membership(catalog: Catalog, group: str, user: str) -> None:
    """Checks that both users of a membership exist."""
    for name in (group, user):
        if catalog.get_user(name) is None:
            raise errinput(f"user {name} does not exist")


def check_login(catalog: Catalog, user: str, password: str | None) -> None:
    """Checks a login: the user must exist, and the password must be the
    user's password (none for a user without one)."""
    if (found := catalog.get_user(user)) is None:
        raise errinput(f"user {user} does not exist")
    hashed = hash_password(user, password) if password is not None else None
    if hashed != found.password:
        raise errpermission(f"password authentication failed for user {user}")


def hash_password(user: str, password: str) -> str:
    """Returns the stored form of a user's password: md5 and the hex MD5 of the
    password followed by the user name."""
    return "md5" + hashlib.md5((password + user).encode()).hexdigest()


def show_users(catalog: Catalog) -> list[Row]:
    """Returns the SHOW USERS result."""
    memberships = catalog.list_memberships()
    rows: list[Row] = []
    for user in catalog.list_users():
        member_of = sorted(group for member, group in memberships if member == user.name)
        rows.append(
            [
                Value.String(user.name),
                Value.Boolean(user.superuser),
                Value.Boolean(user.password is not None),
                Value.String(", ".join(member_of)),
            ]
        )
    return rows


def show_grants(catalog: Catalog) -> list[Row]:
    """Returns the SHOW GRANTS result: the owner and the privileges of every
    table and view, by object, privilege and grantee."""
    grants = catalog.list_grants()
    rows: list[Row] = []
    for object in objects(catalog):
        owner = catalog.get_owner(object)
        rows.append([Value.String(object), Value.String(owner), Value.String("OWNER")])
        for privilege in Privilege:
            grantees = sorted(
                grant.grantee
                for grant in grants
                if grant.object == object and privilege in grant.privileges
            )
            for grantee in grantees:
                row = [Value.String(object), Value.String(grantee), Value.String(str(privilege))]
                rows.append(row)
    return rows


def objects(catalog: Catalog) -> list[str]:
    """Returns the names of all tables, views and materialized views, sorted."""
    tables = [table.name for table in catalog.list_tables()]
    views = [view.name for view in catalog.list_views()]
    matviews = [matview.name for matview in catalog.list_matviews()]
    return sorted(tables + views + matviews)
