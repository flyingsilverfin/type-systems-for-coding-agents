"""IMPORT: imports rows into a table from CSV or JSON lines.

All lines are read and checked first, in order, and the first problem is
reported. The rows are then inserted by a single INSERT plan, exactly as
`INSERT INTO <table> (<columns>) VALUES (<values>)` would insert them, with
the CSV header or the JSON object's member names as the column list: all rows
are completed with the default values of the other columns, so the usual
INSERT constraints and errors apply.
"""

import math
from typing import Any

from toydb.encoding import csv, json
from toydb.error import Error, errinput
from toydb.sql.engine import Transaction
from toydb.sql.execution.executor import ExecutionResult, Executor
from toydb.sql.parser.ast import ImportFormat
from toydb.sql.planner import Node, Plan
from toydb.sql.types import Column, DataType, Expression, Row, Table, Value
from toydb.sql.types.formats import from_text
from toydb.sql.types.value import I64_MAX, I64_MIN

type SourceRow = tuple[dict[int, int], Row]
"""A row read from a line: the table → value column map, and the values."""


def import_lines(txn: Transaction, table: str, format: ImportFormat, lines: list[str]) -> int:
    """Imports lines into a table, returning the number of inserted rows."""
    schema = txn.must_get_table(table)
    match format:
        case ImportFormat.Csv:
            source = read_csv(schema, lines)
        case ImportFormat.Json:
            source = read_json(schema, lines)

    # Build the complete rows like INSERT does (with defaults) before
    # inserting any of them, and insert them with an INSERT plan.
    rows = [Executor.build_row(schema, column_map, values) for column_map, values in source]
    values = Node.Values(rows=[[Expression.Constant(value) for value in row] for row in rows])
    match Plan.Insert(table=schema, column_map=None, source=values).execute(txn):
        case ExecutionResult.Insert(count):
            return count
        case result:
            raise AssertionError(f"unexpected insert result {result!r}")


def column_index(table: Table, name: str) -> int:
    """Looks up a column index by its exact name."""
    for i, column in enumerate(table.columns):
        if column.name == name:
            return i
    raise errinput(f"unknown column {name} in table {table.name}")


def invalid_value(column: Column, line: int) -> Error:
    """Returns an invalid value error for the given column."""
    return errinput(f"invalid value for {column.datatype} column {column.name} on line {line}")


def read_csv(table: Table, lines: list[str]) -> list[SourceRow]:
    """Reads CSV lines. The first line is a header with column names, and
    each following line is a row with values for these columns. Unquoted
    empty fields are NULL."""
    header: list[int] = []
    rows: list[SourceRow] = []
    for number, line in enumerate(lines, start=1):
        try:
            fields = csv.parse_line(line)
        except csv.CSVError:
            raise errinput(f"invalid CSV on line {number}") from None

        if number == 1:
            for name in fields:
                index = column_index(table, name or "")
                if index in header:
                    raise errinput(f"column {name} given multiple times")
                header.append(index)
            continue

        if len(fields) != len(header):
            raise errinput(
                f"wrong number of fields on line {number}: "
                f"expected {len(header)}, found {len(fields)}"
            )
        row: Row = []
        for index, field in zip(header, fields):
            column = table.columns[index]
            if field is None:
                row.append(Value.Null())  # an unquoted empty field
                continue
            try:
                row.append(from_text(field, column.datatype))
            except ValueError:
                raise invalid_value(column, number) from None
        rows.append(({index: i for i, index in enumerate(header)}, row))
    return rows


def read_json(table: Table, lines: list[str]) -> list[SourceRow]:
    """Reads JSON lines. Each line is an object whose members give column
    values."""
    rows: list[SourceRow] = []
    for number, line in enumerate(lines, start=1):
        try:
            object = json.decode(line)
        except ValueError:
            raise errinput(f"invalid JSON on line {number}") from None
        if not isinstance(object, dict):
            raise errinput(f"line {number} is not a JSON object")

        column_map: dict[int, int] = {}
        row: Row = []
        for name, member in object.items():
            index = column_index(table, name)
            column = table.columns[index]
            value = read_json_value(member, column.datatype)
            if value is None:
                raise invalid_value(column, number)
            column_map[index] = len(row)
            row.append(value)
        rows.append((column_map, row))
    return rows


def read_json_value(member: Any, datatype: DataType) -> Value | None:
    """Converts a JSON value to a value of the given data type, or None if it is
    not a valid value for the type. null is NULL for every type. JSON numbers
    are read from their text: an INTEGER only from an integer without fraction
    or exponent (not 2.0 or 2e0), a FLOAT from any number (infinite beyond the
    float range). Values without a native JSON form, and only they, use the
    typed form {"type":"FLOAT","value":"NaN"}."""
    if member is None:
        return Value.Null()
    # NB: bool is a subclass of int, so check for it explicitly.
    match datatype:
        case DataType.Boolean:
            if isinstance(member, bool):
                return Value.Boolean(member)
        case DataType.Integer:
            if isinstance(member, int) and not isinstance(member, bool):
                if I64_MIN <= member <= I64_MAX:
                    return Value.Integer(member)
        case DataType.Float:
            if isinstance(member, int | float) and not isinstance(member, bool):
                try:
                    return Value.Float(float(member))
                except OverflowError:  # an integer beyond the float range
                    return Value.Float(math.inf if member > 0 else -math.inf)
            if (text := typed_value(member, datatype)) in ("NaN", "inf", "-inf"):
                return Value.Float(float(text))
        case DataType.String:
            if isinstance(member, str):
                return Value.String(member)
        case _:
            if (text := typed_value(member, datatype)) is not None:
                try:
                    return from_text(text, datatype)
                except ValueError:
                    pass
    return None


def typed_value(member: Any, datatype: DataType) -> str | None:
    """Returns the value text of a typed JSON value of the given data type,
    an object with exactly the string members type (the data type) and value
    (the value text), or None if it is not such an object."""
    match member:
        case {"type": str(type), "value": str(value)} if len(member) == 2:
            return value if type == str(datatype) else None
        case _:
            return None
