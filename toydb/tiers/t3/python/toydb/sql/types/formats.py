"""Value formats used by the SQL extensions: values as JSON, as SQL literals
and as CSV fields, and values read back from text.

These are the shared definitions of the JSON output, statement digest, and
export/import extensions. Each format is defined in terms of the value display
(`str(value)`), so that a data type which has no special form in a format uses
its display.
"""

import math
import re

from toydb.encoding.json import JSON
from toydb.error import panic
from toydb.sql.types.expression import Expression
from toydb.sql.types.value import I64_MAX, I64_MIN, DataType, Value

_INTEGER = re.compile(r"-?[0-9]+")
"""An optionally negative decimal integer, ASCII digits only."""

_FLOAT = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
"""An optionally negative decimal number with optional fraction and exponent."""


def to_json(value: Value) -> JSON:
    """Converts a value to JSON. Values without a native JSON form (NaN and
    infinite floats, and values of other data types) use the typed form
    {"type": <datatype>, "value": <display>}."""
    match value:
        case Value.Null():
            return None
        case Value.Boolean(b):
            return b
        case Value.Integer(i):
            return i
        case Value.Float(f) if math.isfinite(f):
            return f
        case Value.String(s):
            return s
        case _:
            return typed_json(value)


def typed_json(value: Value) -> dict[str, JSON]:
    """Returns the typed JSON form of a non-NULL value."""
    return {"type": str(value.datatype()), "value": str(value)}


def to_sql_literal(value: Value) -> str:
    """Formats a value as a SQL literal, i.e. SQL text that evaluates to
    exactly this value."""
    match value:
        case Value.Integer(i) if i == I64_MIN:
            # There is no literal for the smallest integer, since literals
            # are unsigned and negated.
            return f"-{I64_MAX} - 1"
        case Value.Float(f) if math.isnan(f):
            return "NAN"
        case Value.Float(f) if math.isinf(f):
            return "INFINITY" if f > 0 else "-INFINITY"
        case Value.String(s):
            return "'{}'".format(s.replace("'", "''"))
        case _:
            # NULL, booleans, integers and finite floats: their plan display as
            # constants, which is their value display. The plan display shows
            # constants of other data types as the SQL text that produces them.
            return str(Expression.Constant(value))


def to_csv_field(value: Value) -> str | None:
    """Formats a value as a CSV field: None (an empty unquoted field) for
    NULL, the string itself for strings, otherwise the value display."""
    match value:
        case Value.Null():
            return None
        case Value.String(s):
            return s
        case _:
            return str(value)


def from_text(text: str, datatype: DataType) -> Value:
    """Reads a value of the given data type from text, as written by
    `to_csv_field` (other than NULL). The text is not trimmed. Raises
    ValueError if the text is not a valid value of the type."""
    match datatype:
        case DataType.Boolean:
            match text.lower():
                case "true":
                    return Value.Boolean(True)
                case "false":
                    return Value.Boolean(False)
        case DataType.Integer:
            if _INTEGER.fullmatch(text):
                integer = int(text)
                if I64_MIN <= integer <= I64_MAX:
                    return Value.Integer(integer)
        case DataType.Float:
            if text in ("NaN", "inf", "-inf") or _FLOAT.fullmatch(text):
                return Value.Float(float(text))
        case DataType.String:
            return Value.String(text)
        case _:
            panic(f"unknown datatype {datatype}")
    raise ValueError(f"invalid {datatype} value {text!r}")
