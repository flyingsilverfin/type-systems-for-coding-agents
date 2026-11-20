"""Tests for toydb.sql.types.formats."""

import math
import unittest

from toydb.encoding import json
from toydb.sql.parser import Parser
from toydb.sql.planner import Planner, Scope
from toydb.sql.types import DataType, Value
from toydb.sql.types.formats import from_text, to_csv_field, to_json, to_sql_literal
from toydb.sql.types.value import I64_MAX, I64_MIN

# Values with their JSON, SQL literal and CSV field.
CASES: list[tuple[Value, str, str, str | None]] = [
    (Value.Null(), "null", "NULL", None),
    (Value.Boolean(True), "true", "TRUE", "TRUE"),
    (Value.Boolean(False), "false", "FALSE", "FALSE"),
    (Value.Integer(42), "42", "42", "42"),
    (Value.Integer(-7), "-7", "-7", "-7"),
    (Value.Integer(I64_MAX), "9223372036854775807", "9223372036854775807", "9223372036854775807"),
    (
        Value.Integer(I64_MIN),
        "-9223372036854775808",
        "-9223372036854775807 - 1",
        "-9223372036854775808",
    ),
    (Value.Float(1.0), "1.0", "1.0", "1.0"),
    (Value.Float(-0.0), "-0.0", "-0.0", "-0.0"),
    (Value.Float(1e100), "1e100", "1e100", "1e100"),
    (Value.Float(1.5e-7), "1.5e-7", "1.5e-7", "1.5e-7"),
    (Value.Float(math.nan), '{"type":"FLOAT","value":"NaN"}', "NAN", "NaN"),
    (Value.Float(math.inf), '{"type":"FLOAT","value":"inf"}', "INFINITY", "inf"),
    (Value.Float(-math.inf), '{"type":"FLOAT","value":"-inf"}', "-INFINITY", "-inf"),
    (Value.String(""), '""', "''", ""),
    (Value.String("it's"), '"it\'s"', "'it''s'", "it's"),
    (Value.String('a"b\\c'), '"a\\"b\\\\c"', "'a\"b\\c'", 'a"b\\c'),
    (Value.String("é ☃"), '"é ☃"', "'é ☃'", "é ☃"),
]


class TestFormats(unittest.TestCase):
    def test_to_json(self) -> None:
        for value, expect, _, _ in CASES:
            with self.subTest(value=value):
                self.assertEqual(json.encode(to_json(value)), expect)

    def test_to_sql_literal(self) -> None:
        """SQL literals evaluate to the same value."""
        for value, _, expect, _ in CASES:
            with self.subTest(value=value):
                literal = to_sql_literal(value)
                self.assertEqual(literal, expect)
                expr = Planner.build_expression(Parser.parse_expr(literal), Scope())
                evaluated = expr.evaluate(None)
                self.assertEqual(evaluated, value)
                self.assertEqual(evaluated.datatype(), value.datatype())

    def test_to_csv_field(self) -> None:
        for value, _, _, expect in CASES:
            with self.subTest(value=value):
                self.assertEqual(to_csv_field(value), expect)

    def test_from_text_roundtrip(self) -> None:
        """Every non-NULL CSV field is read back as the same value."""
        for value, _, _, field in CASES:
            datatype = value.datatype()
            if field is None or datatype is None:
                continue
            with self.subTest(value=value):
                read = from_text(field, datatype)
                self.assertEqual(read, value)
                self.assertEqual(str(read), str(value))

    def test_from_text(self) -> None:
        cases: list[tuple[str, DataType, Value]] = [
            ("TRUE", DataType.Boolean, Value.Boolean(True)),
            ("fAlSe", DataType.Boolean, Value.Boolean(False)),
            ("007", DataType.Integer, Value.Integer(7)),
            ("-0", DataType.Integer, Value.Integer(0)),
            ("1", DataType.Float, Value.Float(1.0)),
            ("2.5e-3", DataType.Float, Value.Float(0.0025)),
            ("1E10", DataType.Float, Value.Float(1e10)),
            ("-0", DataType.Float, Value.Float(-0.0)),
            (" x ", DataType.String, Value.String(" x ")),
            ("", DataType.String, Value.String("")),
        ]
        for text, datatype, expect in cases:
            with self.subTest(text=text, datatype=datatype):
                self.assertEqual(from_text(text, datatype), expect)

    def test_from_text_invalid(self) -> None:
        cases: list[tuple[str, DataType]] = [
            ("", DataType.Boolean),
            ("1", DataType.Boolean),
            ("yes", DataType.Boolean),
            ("", DataType.Integer),
            ("+1", DataType.Integer),
            ("1.0", DataType.Integer),
            ("1e3", DataType.Integer),
            (" 1", DataType.Integer),
            ("9223372036854775808", DataType.Integer),
            ("٣", DataType.Integer),  # a non-ASCII digit
            ("", DataType.Float),
            (".5", DataType.Float),
            ("5.", DataType.Float),
            ("+1", DataType.Float),
            ("1.5.2", DataType.Float),
            ("Infinity", DataType.Float),
            ("nan", DataType.Float),
            ("INF", DataType.Float),
        ]
        for text, datatype in cases:
            with self.subTest(text=text, datatype=datatype), self.assertRaises(ValueError):
                from_text(text, datatype)


if __name__ == "__main__":
    unittest.main()
