"""CREATE, REFRESH and DROP MATERIALIZED VIEW, and the dependency checks of
materialized views.

* CREATE makes the checks of CREATE VIEW, executes the query and stores its
  rows; REFRESH executes the stored query again and replaces them. A statement
  that reads a materialized view reads the stored rows.
* A table, view or materialized view that a materialized view depends on can't
  be dropped, after the checks of views, and no statement may change the
  definition of a table that a materialized view depends on: every statement
  that changes a table's definition, also one added later, fails with
  `table <t> is used by materialized view <m>` (the first such materialized view
  by name) after the views check and before the check constraints' check.
"""

from typing import TYPE_CHECKING

from toydb.error import Error, errdata, errinput, panic
from toydb.sql.engine import Transaction
from toydb.sql.execution.executor import ExecutionResult
from toydb.sql.execution.views import check_view, dependencies
from toydb.sql.parser import Parser, ast
from toydb.sql.parser.format import format_one_line
from toydb.sql.planner import Plan
from toydb.sql.types import Row
from toydb.sql.types.matview import MaterializedView

if TYPE_CHECKING:
    from toydb.sql.engine import Catalog
    from toydb.sql.execution.session import Session


def create_matview(
    txn: Transaction,
    name: str,
    columns: list[str] | None,
    query: ast.Select,
    user: str,
    session: "Session | None" = None,
) -> int:
    """Creates a materialized view: checks it like a view, executes its query
    and stores the rows."""
    names = check_view(txn, name, columns, query)
    rows = execute(txn, query, user, session)
    matview = MaterializedView(
        name=name, columns=names, query=format_one_line(query), dependencies=dependencies(query)
    )
    txn.create_matview(matview, rows)
    return len(rows)


def refresh_matview(
    txn: Transaction, name: str, user: str, session: "Session | None" = None
) -> int:
    """Executes a materialized view's query again and replaces its rows."""
    if (matview := txn.get_matview(name)) is None:
        raise errinput(f"materialized view {name} does not exist")
    match Parser.parse(matview.query):
        case ast.Statement.Select() as query:
            rows = execute(txn, query, user, session)
        case _:
            raise errdata(f"materialized view {name} has no SELECT query")
    # Replacing no rows with no rows writes nothing, so check explicitly.
    if txn.state().read_only:
        raise Error.ReadOnly()
    txn.refresh_matview(name, rows)
    return len(rows)


def drop_matview(catalog: "Catalog", name: str, if_exists: bool) -> bool:
    """Drops a materialized view. Returns true if it existed."""
    if catalog.get_matview(name) is None:
        if if_exists:
            return False
        raise errinput(f"materialized view {name} does not exist")
    for view in sorted(catalog.list_views(), key=lambda view: view.name):
        if name in view.dependencies:
            raise errinput(f"materialized view {name} is used by view {view.name}")
    check_dependencies(catalog, "materialized view", name)
    return catalog.drop_matview(name)


def check_table_dependencies(catalog: "Catalog", table: str) -> None:
    """Errors if a materialized view depends on the given table."""
    check_dependencies(catalog, "table", table)


def check_dependencies(catalog: "Catalog", kind: str, name: str) -> None:
    """Errors if a materialized view depends on the given table, view or
    materialized view (the kind)."""
    for matview in sorted(catalog.list_matviews(), key=lambda matview: matview.name):
        if name in matview.dependencies:
            raise errinput(f"{kind} {name} is used by materialized view {matview.name}")


def execute(txn: Transaction, query: ast.Select, user: str, session: "Session | None") -> list[Row]:
    """Plans and executes a materialized view's stored query, returning its rows."""
    match Plan.build(query, txn).optimize().execute(txn, user, session):
        case ExecutionResult.Select(rows=rows):
            return list(rows)
        case result:
            panic(f"unexpected result for a materialized view query {result!r}")
