"""CHECK constraints: created with their table, enforced on every written row.

* Constraints are table constraints (`[CONSTRAINT name] CHECK (condition)`) and
  column constraints (`CHECK (condition)` in a column definition), in the order
  written. A constraint without a name is named `<table>_check<i>`, its
  position among the table's constraints counting from 1. Names are unique in
  the table. The condition is planned like the WHERE condition of
  `SELECT * FROM <table>`. These checks come after the other checks of CREATE
  TABLE and before anything is written. A CREATE TABLE that doesn't create a
  table neither checks nor creates constraints, and the existing table's
  constraints stay as they are.
* A statement that adds a column, also one added later, adds the column's
  constraints as the table's next ones (named by their position among all the
  table's constraints, and checked as in CREATE TABLE, with the new column in
  the table) and checks every existing row against them, in primary key order,
  with its value of the new column, after all of its own checks and before
  anything is changed. The table's other constraints are not checked again.
* Every row that a statement inserts or updates — INSERT, UPDATE, IMPORT, and
  every statement added later that inserts or updates rows — is checked after
  it is complete and before any of the statement's rows is written (so before
  the checks of data types, NULLs, keys, unique values and references): FALSE
  violates a constraint, TRUE and NULL satisfy it, another value is an error.
* No statement may drop or rename a column that a constraint of its table
  uses (that its condition references): every statement that drops or renames
  columns, also one added later, fails with
  `column <c> is used by check constraint <name>`, naming the first such
  constraint, after all of its own checks and before anything is changed.
* DROP TABLE drops the table's constraints.
"""

from toydb.error import errinput
from toydb.sql.engine import Catalog
from toydb.sql.parser import Parser
from toydb.sql.planner.planner import Planner, Scope
from toydb.sql.types import Expression, Row, Table, Value
from toydb.sql.types.check import Check


def create_checks(catalog: Catalog, table: Table, checks: list[tuple[str | None, str]]) -> None:
    """Names, checks and stores the CHECK constraints of a new table."""
    # The table is created once it and its constraints are checked.
    table.validate_create(catalog)

    constraints: list[Check] = []
    for i, (name, condition) in enumerate(checks, start=1):
        if name is None:
            name = f"{table.name}_check{i}"
        if any(check.name == name for check in constraints):
            raise errinput(f"duplicate check constraint {name} in table {table.name}")
        if not name:
            raise errinput("check constraint name can't be empty")
        check = Check(name=name, condition=condition)
        build_condition(table, condition)
        constraints.append(check)

    catalog.create_table(table)
    catalog.set_checks(table.name, constraints)


def add_check(catalog: Catalog, table: str, check: Check) -> None:
    """Adds a named constraint to a table as its next one, after checking it."""
    schema = catalog.must_get_table(table)
    constraints = catalog.get_checks(schema.name)
    if not check.name:
        raise errinput("check constraint name can't be empty")
    if any(constraint.name == check.name for constraint in constraints):
        raise errinput(f"duplicate check constraint {check.name} in table {schema.name}")
    build_condition(schema, check.condition)
    catalog.set_checks(schema.name, [*constraints, check])


def build_checks(catalog: Catalog, table: Table) -> list[tuple[str, Expression]]:
    """Plans the CHECK constraints of a table, in order."""
    return [
        (check.name, build_condition(table, check.condition))
        for check in catalog.get_checks(table.name)
    ]


def check_rows(table: Table, constraints: list[tuple[str, Expression]], rows: list[Row]) -> None:
    """Checks rows against the CHECK constraints of their table, in order."""
    for row in rows:
        for name, condition in constraints:
            match condition.evaluate(row):
                case Value.Boolean(True) | Value.Null():
                    pass
                case Value.Boolean(False):
                    key = row[table.primary_key]
                    raise errinput(f"row {key} violates check constraint {name}")
                case value:
                    raise errinput(f"check constraint {name} returned {value}, expected boolean")


def build_condition(table: Table, condition: str) -> Expression:
    """Plans a condition of a table, such as that of a CHECK constraint."""
    expression = Planner.desugar_expression(Parser.parse_expr(condition))
    return Planner.build_expression(expression, Scope.from_table(table))
