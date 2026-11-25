"""Prepared statements: `PREPARE`, `EXECUTE`, `DEALLOCATE` and `SHOW PREPARED`.

A prepared statement is a planned statement with typed parameters `$1`, `$2`,
..., kept by the session under a name. EXECUTE checks the arguments (their
number, and that each is a constant of the parameter's type or NULL) and runs
the statement with every parameter replaced by the value of its argument.

Each prepared statement caches the plan of its last successful planning, with
the argument values and the definitions of the tables and views that planning
used. The cached plan is used again for identical arguments while those
definitions are unchanged; otherwise the statement is planned anew.
"""

import math
from dataclasses import dataclass, field

from toydb.error import errinput, panic
from toydb.sql.engine import Catalog
from toydb.sql.parser import ast
from toydb.sql.parser.format import format_one_line
from toydb.sql.planner import Plan
from toydb.sql.planner.planner import Planner
from toydb.sql.types import DataType, Label, Row, Table, Value
from toydb.sql.types.check import Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.policy import Policy
from toydb.sql.types.statistics import TableStatistics
from toydb.sql.types.user import Grant, Membership, Privilege, User
from toydb.sql.types.view import View

COLUMNS: list[Label] = [
    Label.Unqualified(name) for name in ("name", "parameters", "statement", "executions", "plans")
]
"""The SHOW PREPARED result columns."""


@dataclass
class CachedPlan:
    """A cached plan, with the argument values it was planned for and the
    table and view definitions that planning looked up."""

    plan: Plan
    arguments: list[Value]
    tables: dict[str, Table | None]
    views: dict[str, View | None]
    matviews: dict[str, MaterializedView | None]

    def is_valid(self, arguments: list[Value], catalog: Catalog) -> bool:
        """Returns true if the plan can be used for the arguments in the
        catalog."""
        return (
            len(arguments) == len(self.arguments)
            and all(identical(a, b) for a, b in zip(arguments, self.arguments))
            and all(catalog.get_table(name) == table for name, table in self.tables.items())
            and all(catalog.get_view(name) == view for name, view in self.views.items())
            and all(
                catalog.get_matview(name) == matview for name, matview in self.matviews.items()
            )
        )


@dataclass
class PreparedStatement:
    """A prepared statement: the parsed statement, its parameter types, its
    cached plan and its execution counts."""

    name: str
    types: list[DataType]
    statement: ast.Statement
    cached: CachedPlan | None = None
    executions: int = 0
    plans: int = 0

    def bind(self, args: list[ast.Expression]) -> tuple[ast.Statement, list[Value]]:
        """Checks the arguments of an EXECUTE and returns the statement with
        every parameter replaced by its value."""
        if len(args) != len(self.types):
            raise errinput(
                f"wrong number of arguments for prepared statement {self.name}: "
                f"expected {len(self.types)}, found {len(args)}"
            )
        values: list[Value] = []
        for i, (arg, datatype) in enumerate(zip(args, self.types), start=1):
            parameters: list[ast.Expression] = []
            arg.collect(lambda expr: isinstance(expr, ast.Expression.Parameter), parameters)
            match parameters:
                case [ast.Expression.Parameter(number), *_]:
                    raise errinput(f"parameter ${number} can only be used in a prepared statement")
            value = Planner.build_constant_value(Planner.desugar_expression(arg))
            if (vtype := value.datatype()) is not None and vtype != datatype:
                raise errinput(f"invalid datatype {vtype} for {datatype} parameter ${i}")
            values.append(value)
        return substitute(self.statement, values), values

    def plan(self, catalog: Catalog, statement: ast.Statement, arguments: list[Value]) -> Plan:
        """Returns the cached plan if it is valid for the arguments and the
        current definitions, otherwise plans the statement and caches the plan
        if planning succeeds."""
        if self.cached is not None and self.cached.is_valid(arguments, catalog):
            return self.cached.plan
        self.plans += 1
        recorder = RecordingCatalog(catalog)
        plan = Plan.build(statement, recorder).optimize()
        self.cached = CachedPlan(
            plan, arguments, recorder.tables, recorder.views, recorder.matviews
        )
        return plan


@dataclass
class PreparedStatements:
    """The prepared statements of a session, by name."""

    statements: dict[str, PreparedStatement] = field(default_factory=dict)

    def prepare(self, name: str, types: list[DataType], statement: ast.Statement) -> None:
        """Prepares a statement under a name, after checking it."""
        from toydb.sql.execution.session import Session

        if name in self.statements:
            raise errinput(f"prepared statement {name} already exists")
        if (keyword := Session.unplanned_keyword(statement)) is not None:
            raise errinput(f"PREPARE is not supported for {keyword}")
        for number in statement.parameters():
            if number > len(types):
                raise errinput(f"no type given for parameter ${number}")
        self.statements[name] = PreparedStatement(name, list(types), statement)

    def get(self, name: str) -> PreparedStatement:
        """Fetches a prepared statement, or errors if it does not exist."""
        if (prepared := self.statements.get(name)) is None:
            raise errinput(f"prepared statement {name} does not exist")
        return prepared

    def deallocate(self, name: str) -> None:
        """Removes a prepared statement, or errors if it does not exist."""
        self.get(name)
        del self.statements[name]

    def rows(self) -> list[Row]:
        """Returns the SHOW PREPARED result: one row per prepared statement,
        ordered by name."""
        return [
            [
                Value.String(name),
                Value.String(", ".join(str(datatype) for datatype in prepared.types)),
                Value.String(format_one_line(prepared.statement)),
                Value.Integer(prepared.executions),
                Value.Integer(prepared.plans),
            ]
            for name, prepared in sorted(self.statements.items())
        ]


class RecordingCatalog(Catalog):
    """A catalog that records the table, view and materialized view
    definitions looked up in it."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.tables: dict[str, Table | None] = {}
        self.views: dict[str, View | None] = {}
        self.matviews: dict[str, MaterializedView | None] = {}

    def create_table(self, table: Table) -> None:
        self.catalog.create_table(table)

    def drop_table(self, table: str, if_exists: bool) -> bool:
        return self.catalog.drop_table(table, if_exists)

    def get_table(self, table: str) -> Table | None:
        schema = self.catalog.get_table(table)
        self.tables[table] = schema
        return schema

    def list_tables(self) -> list[Table]:
        return self.catalog.list_tables()

    def get_statistics(self, table: str) -> TableStatistics | None:
        return self.catalog.get_statistics(table)

    def set_statistics(self, table: str, statistics: TableStatistics) -> None:
        self.catalog.set_statistics(table, statistics)

    def create_view(self, view: View) -> None:
        self.catalog.create_view(view)

    def drop_view(self, view: str, if_exists: bool) -> bool:
        return self.catalog.drop_view(view, if_exists)

    def get_view(self, view: str) -> View | None:
        definition = self.catalog.get_view(view)
        self.views[view] = definition
        return definition

    def list_views(self) -> list[View]:
        return self.catalog.list_views()

    def create_user(self, user: User) -> None:
        self.catalog.create_user(user)

    def drop_user(self, user: str) -> None:
        self.catalog.drop_user(user)

    def get_user(self, user: str) -> User | None:
        return self.catalog.get_user(user)

    def list_users(self) -> list[User]:
        return self.catalog.list_users()

    def set_membership(self, member: str, user: str, is_member: bool) -> None:
        self.catalog.set_membership(member, user, is_member)

    def list_memberships(self) -> list[Membership]:
        return self.catalog.list_memberships()

    def set_grant(self, object: str, grantee: str, privileges: list[Privilege]) -> None:
        self.catalog.set_grant(object, grantee, privileges)

    def list_grants(self) -> list[Grant]:
        return self.catalog.list_grants()

    def set_owner(self, object: str, owner: str) -> None:
        self.catalog.set_owner(object, owner)

    def get_owner(self, object: str) -> str:
        return self.catalog.get_owner(object)

    def set_checks(self, table: str, checks: list[Check]) -> None:
        self.catalog.set_checks(table, checks)

    def get_checks(self, table: str) -> list[Check]:
        return self.catalog.get_checks(table)

    def create_policy(self, policy: Policy) -> None:
        self.catalog.create_policy(policy)

    def drop_policy(self, table: str, name: str) -> bool:
        return self.catalog.drop_policy(table, name)

    def list_policies(self, table: str) -> list[Policy]:
        return self.catalog.list_policies(table)

    def create_matview(self, matview: MaterializedView, rows: list[Row]) -> None:
        self.catalog.create_matview(matview, rows)

    def refresh_matview(self, name: str, rows: list[Row]) -> None:
        self.catalog.refresh_matview(name, rows)

    def drop_matview(self, name: str) -> bool:
        return self.catalog.drop_matview(name)

    def get_matview(self, name: str) -> MaterializedView | None:
        matview = self.catalog.get_matview(name)
        self.matviews[name] = matview
        return matview

    def get_matview_rows(self, name: str) -> list[Row]:
        return self.catalog.get_matview_rows(name)

    def list_matviews(self) -> list[MaterializedView]:
        return self.catalog.list_matviews()


def substitute(statement: ast.Statement, values: list[Value]) -> ast.Statement:
    """Returns the statement with every parameter $n replaced by the n-th
    value."""

    def replace(expr: ast.Expression) -> ast.Expression:
        match expr:
            case ast.Expression.Parameter(number):
                return ast.Expression.from_literal(literal(values[number - 1]))
            case _:
                return expr

    return Planner.transform_expressions(statement, lambda expr: expr.transform(replace))


def identical(a: Value, b: Value) -> bool:
    """Returns true if two argument values are identical."""
    match (a, b):
        # 0.0 and -0.0 are equal, but not identical.
        case (Value.Float(x), Value.Float(y)):
            if math.isnan(x) or math.isnan(y):
                return math.isnan(x) and math.isnan(y)
            return x == y and math.copysign(1.0, x) == math.copysign(1.0, y)
        case _:
            return a.datatype() == b.datatype() and a == b


def literal(value: Value) -> ast.Literal:
    """Returns the AST literal of a value."""
    match value:
        case Value.Null():
            return ast.Literal.Null()
        case Value.Boolean(b):
            return ast.Literal.Boolean(b)
        case Value.Integer(i):
            return ast.Literal.Integer(i)
        case Value.Float(f):
            return ast.Literal.Float(f)
        case Value.String(s):
            return ast.Literal.String(s)
        case _:
            panic(f"unknown value {value!r}")
