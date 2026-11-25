"""Row-level security policies: CREATE POLICY and DROP POLICY, and the rows a
user reads.

* A table with policies is protected. A user that is neither a superuser nor
  acts for the table's owner reads only the rows of a protected table for
  which the USING condition of one of its applicable policies — those for a
  user it acts for, and those without a user — is TRUE, evaluated in order of
  name before the statement's own conditions. This holds for every read of the
  table's rows: scans, key and index lookups, in queries, views, UPDATE,
  DELETE, EXPORT and every statement added later that reads rows. Plans don't
  change.
* Every row such a user inserts or updates (INSERT, UPDATE, IMPORT and every
  statement added later that inserts or updates rows) must satisfy the check
  condition (WITH CHECK, or else USING) of one applicable policy, checked after
  the row's CHECK constraints and before any row is written.
* No statement may drop or rename a column that a policy of its table uses
  (that its USING or WITH CHECK condition references): every statement that
  drops or renames columns, also one added later, fails with
  `column <c> is used by policy <name>`, naming the first such policy by name,
  after the check-constraint check and before anything is changed.
* DROP TABLE drops the table's policies.
"""

from dataclasses import dataclass

from toydb.error import errinput
from toydb.sql.engine import Catalog
from toydb.sql.execution.checks import build_condition
from toydb.sql.execution.privileges import acts_for, is_superuser
from toydb.sql.parser import ast
from toydb.sql.parser.format import format_expr
from toydb.sql.types import Expression, Row, Table, Value
from toydb.sql.types.policy import Policy


@dataclass(frozen=True)
class PlannedPolicy:
    """A policy that applies to a user, with its conditions planned."""

    name: str
    """The policy name."""
    condition: Expression
    """The USING condition."""
    check: Expression
    """The check condition: WITH CHECK, or else USING."""


def create_policy(
    catalog: Catalog,
    name: str,
    table: str,
    user: str | None,
    using: ast.Expression,
    check: ast.Expression | None,
) -> None:
    """Creates a policy of a table, after checking it."""
    schema = catalog.must_get_table(table)
    if not name:
        raise errinput("policy name can't be empty")
    if any(policy.name == name for policy in catalog.list_policies(schema.name)):
        raise errinput(f"policy {name} already exists on table {schema.name}")
    if user is not None and catalog.get_user(user) is None:
        raise errinput(f"user {user} does not exist")
    policy = Policy(
        table=schema.name,
        name=name,
        user=user,
        condition=format_expr(using),
        check=format_expr(check) if check is not None else None,
    )
    build_condition(schema, policy.condition)
    if policy.check is not None:
        build_condition(schema, policy.check)
    catalog.create_policy(policy)


def drop_policy(catalog: Catalog, name: str, table: str, if_exists: bool) -> bool:
    """Drops a policy of a table. Returns true if it existed."""
    schema = catalog.must_get_table(table)
    if catalog.drop_policy(schema.name, name):
        return True
    if if_exists:
        return False
    raise errinput(f"policy {name} does not exist on table {schema.name}")


def applicable(catalog: Catalog, table: Table, user: str) -> list[PlannedPolicy] | None:
    """Returns the policies of a table that apply to a user, by name, or none
    if the user reads every row."""
    if is_superuser(catalog, user):
        return None
    policies = catalog.list_policies(table.name)
    if not policies:
        return None
    acting = acts_for(catalog, user)
    if catalog.get_owner(table.name) in acting:
        return None
    planned: list[PlannedPolicy] = []
    for policy in policies:
        if policy.user is not None and policy.user not in acting:
            continue
        condition = build_condition(table, policy.condition)
        check = build_condition(table, policy.check) if policy.check is not None else condition
        planned.append(PlannedPolicy(policy.name, condition, check))
    return planned


def shows(policies: list[PlannedPolicy], row: Row) -> bool:
    """Returns true if a policy shows the row: one of the policies is TRUE for
    it."""
    return any(is_true(policy.name, policy.condition, row) for policy in policies)


def check_rows(table: Table, policies: list[PlannedPolicy], rows: list[Row]) -> None:
    """Checks that each written row satisfies the check condition of one
    applicable policy."""
    for row in rows:
        if not any(is_true(policy.name, policy.check, row) for policy in policies):
            raise errinput(f"new row violates row-level security policy for table {table.name}")


def is_true(name: str, condition: Expression, row: Row) -> bool:
    """Evaluates a condition of a policy for a row."""
    match condition.evaluate(row):
        case Value.Boolean(True):
            return True
        case Value.Boolean(False) | Value.Null():
            return False
        case value:
            raise errinput(f"policy {name} returned {value}, expected boolean")
