"""CREATE VIEW, DROP VIEW and SHOW VIEWS, and the dependency checks of views.

* CREATE VIEW checks the name, plans the query (without optimizing or
  executing it), names the columns from the column list or the query's result
  columns, and stores the definition.
* A table or view that a view depends on can't be dropped, and no statement
  may change the definition of a table that a view depends on.
* SHOW VIEWS lists the views by name, with their columns and query.
"""

from toydb.error import errinput, panic
from toydb.sql.engine import Catalog
from toydb.sql.parser import ast
from toydb.sql.parser.format import format_name, format_one_line
from toydb.sql.planner import Plan
from toydb.sql.types import Label, Row, Value
from toydb.sql.types.view import View

COLUMNS: list[Label] = [Label.Unqualified(name) for name in ("name", "columns", "query")]
"""The SHOW VIEWS result columns."""


def create_view(catalog: Catalog, name: str, columns: list[str] | None, query: ast.Select) -> None:
    """Creates a view, after checking its name, its query and its column
    names."""
    if not name:
        raise errinput("view name can't be empty")
    if catalog.get_table(name) is not None:
        raise errinput(f"table {name} already exists")
    if catalog.get_view(name) is not None:
        raise errinput(f"view {name} already exists")

    # Plan the query, to check it and find its result columns.
    match Plan.build(query, catalog):
        case Plan.Select(root):
            labels = [root.column_label(i) for i in range(root.columns())]
        case plan:
            panic(f"unexpected plan for a view query {plan!r}")
    if columns is not None and len(columns) != len(labels):
        raise errinput(
            f"wrong number of column names for view {name}: "
            f"expected {len(labels)}, found {len(columns)}"
        )

    names: list[str] = []
    for i, label in enumerate(labels):
        if columns is not None:
            column = columns[i]
        else:
            match label:
                case Label.Qualified(_, column) | Label.Unqualified(column):
                    pass
                case _:
                    raise errinput(f"column {i + 1} of view {name} has no name")
        if not column:
            raise errinput("column name can't be empty")
        if column in names:
            raise errinput(f"duplicate column {column} in view {name}")
        names.append(column)

    view = View(
        name=name, columns=names, query=format_one_line(query), dependencies=dependencies(query)
    )
    catalog.create_view(view)


def dependencies(query: ast.Select) -> list[str]:
    """Returns the names of the tables and views in a query's FROM clause."""
    names: list[str] = []

    def collect(item: ast.From) -> None:
        match item:
            case ast.From.Table(name):
                if name not in names:
                    names.append(name)
            case ast.From.Join(left, right):
                collect(left)
                collect(right)
            case _:
                panic(f"unknown from item {item!r}")

    for item in query.from_:
        collect(item)
    return names


def drop_view(catalog: Catalog, name: str, if_exists: bool) -> bool:
    """Drops a view, unless another view depends on it. Returns true if the
    view existed."""
    if catalog.get_view(name) is None:
        if if_exists:
            return False
        raise errinput(f"view {name} does not exist")
    for view in sorted(catalog.list_views(), key=lambda view: view.name):
        if name in view.dependencies:
            raise errinput(f"view {name} is used by view {view.name}")
    return catalog.drop_view(name, if_exists)


def check_table_dependencies(catalog: Catalog, table: str) -> None:
    """Errors if a view depends on the given table."""
    for view in sorted(catalog.list_views(), key=lambda view: view.name):
        if table in view.dependencies:
            raise errinput(f"table {table} is used by view {view.name}")


def show_views(catalog: Catalog) -> list[Row]:
    """Returns the SHOW VIEWS result: one row per view, ordered by name."""
    return [
        [
            Value.String(view.name),
            Value.String(", ".join(format_name(column) for column in view.columns)),
            Value.String(view.query),
        ]
        for view in sorted(catalog.list_views(), key=lambda view: view.name)
    ]
