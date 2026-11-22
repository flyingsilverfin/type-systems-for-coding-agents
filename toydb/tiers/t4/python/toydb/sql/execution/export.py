"""EXPORT: exports a table's rows, or the whole database, as text lines in
CSV, JSON (one object per row) or SQL (CREATE TABLE and INSERT statements).

Exports read what the given transaction sees, in primary key order, with
columns in table order.
"""

from dataclasses import dataclass

from toydb.encoding import csv, json
from toydb.error import errinput
from toydb.sql.engine import Transaction
from toydb.sql.parser.ast import ExportFormat
from toydb.sql.parser.lexer import format_ident
from toydb.sql.types import Row, Table
from toydb.sql.types.formats import to_csv_field, to_json, to_sql_literal


@dataclass
class Export:
    """The result of an export."""

    lines: list[str]
    """The exported lines."""
    rows: int
    """The number of exported table rows."""


def export(txn: Transaction, table: str | None, format: ExportFormat) -> Export:
    """Exports the given table, or all tables if None (only as SQL)."""
    if table is None:
        if format != ExportFormat.Sql:
            raise errinput("EXPORT without a table requires AS SQL")
        return export_database(txn)

    schema = txn.must_get_table(table)
    rows = list(txn.scan(schema.name, None))
    match format:
        case ExportFormat.Csv:
            lines = export_csv(schema, rows)
        case ExportFormat.Json:
            lines = export_json(schema, rows)
        case ExportFormat.Sql:
            lines = export_sql(schema, rows)
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


def export_sql(table: Table, rows: list[Row]) -> list[str]:
    """Exports a table as SQL: its CREATE TABLE statement (the schema display,
    with defaults as SQL literals), then an INSERT statement per row.
    Executing the statements re-creates the table."""
    lines = table.format(to_sql_literal).split("\n")
    lines[-1] += ";"
    name = format_ident(table.name)
    for row in rows:
        lines.append(f"INSERT INTO {name} VALUES ({', '.join(map(to_sql_literal, row))});")
    return lines


def export_database(txn: Transaction) -> Export:
    """Exports every table as SQL, ordered such that tables come after the
    tables they reference (other than themselves), and otherwise by name. A
    cycle of references is broken by taking the first remaining table by
    name. The views follow the tables, each after the views it depends on,
    and otherwise by name."""
    remaining = sorted(txn.list_tables(), key=lambda table: table.name)
    exported: set[str] = set()
    export = Export(lines=[], rows=0)
    while remaining:

        def ready(table: Table) -> bool:
            references = {c.references for c in table.columns if c.references is not None}
            return all(name in exported for name in references - {table.name})

        table = next((table for table in remaining if ready(table)), remaining[0])
        remaining.remove(table)
        exported.add(table.name)
        rows = list(txn.scan(table.name, None))
        export.lines.extend(export_sql(table, rows))
        export.rows += len(rows)

    # Export the views.
    views = sorted(txn.list_views(), key=lambda view: view.name)
    while views:
        view = next(view for view in views if all(name in exported for name in view.dependencies))
        views.remove(view)
        exported.add(view.name)
        export.lines.append(f"{view};")
    return export
