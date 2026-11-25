"""EXPORT: exports a table's rows, or the whole database, as text lines in
CSV, JSON (one object per row) or SQL (CREATE TABLE and INSERT statements).

Exports read what the given transaction sees, in primary key order, with
columns in table order.
"""

from dataclasses import dataclass

from toydb.encoding import csv, json
from toydb.error import errinput
from toydb.sql.engine import Transaction
from toydb.sql.execution.policies import applicable, shows
from toydb.sql.parser.ast import ExportFormat
from toydb.sql.parser.lexer import format_ident
from toydb.sql.types import Row, Table
from toydb.sql.types.check import Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.formats import to_csv_field, to_json, to_sql_literal
from toydb.sql.types.view import View


@dataclass
class Export:
    """The result of an export."""

    lines: list[str]
    """The exported lines."""
    rows: int
    """The number of exported table rows."""


def export(txn: Transaction, table: str | None, format: ExportFormat, user: str) -> Export:
    """Exports the given table, or all tables if None (only as SQL), as the
    given user."""
    if table is None:
        if format != ExportFormat.Sql:
            raise errinput("EXPORT without a table requires AS SQL")
        return export_database(txn, user)

    schema = txn.must_get_table(table)
    rows = read_rows(txn, schema, user)
    match format:
        case ExportFormat.Csv:
            lines = export_csv(schema, rows)
        case ExportFormat.Json:
            lines = export_json(schema, rows)
        case ExportFormat.Sql:
            lines = export_sql(schema, txn.get_checks(schema.name), rows)
    return Export(lines=lines, rows=len(rows))


def export_csv(table: Table, rows: list[Row]) -> list[str]:
    """Exports table rows as CSV: a header with the column names, then one
    line per row."""
    header = csv.format_line([column.name for column in table.columns])
    return [header] + [csv.format_line([to_csv_field(value) for value in row]) for row in rows]


def export_json(table: Table, rows: list[Row]) -> list[str]:
    """Exports table rows as JSON lines: one object per row, with the column
    values as members."""
    return [
        json.encode({column.name: to_json(value) for column, value in zip(table.columns, row)})
        for row in rows
    ]


def export_sql(table: Table, checks: list[Check], rows: list[Row]) -> list[str]:
    """Exports a table as SQL: its CREATE TABLE statement (the schema display,
    with defaults as SQL literals, and its CHECK constraints), then an INSERT
    statement per row. Executing the statements re-creates the table."""
    lines = table.format(to_sql_literal).split("\n")
    closing = lines.pop()
    for check in checks:
        lines[-1] += ","
        lines.append(f"  {check}")
    lines.append(f"{closing};")
    name = format_ident(table.name)
    for row in rows:
        lines.append(f"INSERT INTO {name} VALUES ({', '.join(map(to_sql_literal, row))});")
    return lines


def read_rows(txn: Transaction, table: Table, user: str) -> list[Row]:
    """Returns the rows of a table that the user reads."""
    policies = applicable(txn, table, user)
    rows = txn.scan(table.name, None)
    return [row for row in rows if policies is None or shows(policies, row)]


def export_database(txn: Transaction, user: str) -> Export:
    """Exports every table as SQL, ordered such that tables come after the
    tables they reference (other than themselves), and otherwise by name. A
    cycle of references is broken by taking the first remaining table by
    name. The views and materialized views follow the tables, each after the
    views and materialized views it depends on, and otherwise by name."""
    export = Export(lines=[], rows=0)
    tables = ordered_tables(txn.list_tables())
    for table in tables:
        rows = read_rows(txn, table, user)
        export.lines.extend(export_sql(table, txn.get_checks(table.name), rows))
        export.rows += len(rows)

    # Export the views and materialized views.
    for view in ordered_views([*txn.list_views(), *txn.list_matviews()]):
        export.lines.append(f"{view};")
    return export


def ordered_tables(tables: list[Table]) -> list[Table]:
    """Returns tables in the order of export_database()."""
    remaining = sorted(tables, key=lambda table: table.name)
    ordered: list[Table] = []
    done: set[str] = set()
    while remaining:

        def ready(table: Table) -> bool:
            references = {c.references for c in table.columns if c.references is not None}
            return all(name in done for name in references - {table.name})

        table = next((table for table in remaining if ready(table)), remaining[0])
        remaining.remove(table)
        done.add(table.name)
        ordered.append(table)
    return ordered


def ordered_views(
    views: list[View | MaterializedView],
) -> list[View | MaterializedView]:
    """Returns views and materialized views in the order of
    export_database()."""
    remaining = sorted(views, key=lambda view: view.name)
    ordered: list[View | MaterializedView] = []
    while remaining:
        names = {view.name for view in remaining}
        view = next(view for view in remaining if not names.intersection(view.dependencies))
        remaining.remove(view)
        ordered.append(view)
    return ordered
