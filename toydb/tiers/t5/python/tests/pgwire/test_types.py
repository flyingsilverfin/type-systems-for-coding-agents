"""Tests for toydb.pgwire.types.

The types are mostly covered by the SQL golden scripts
`tests/sql/testscripts/ext5/pg_types` and `pg_params`.
"""

import math
import struct
import unittest

from toydb.pgwire import types
from toydb.pgwire.message import PgError
from toydb.sql.types import DataType, Value


class TestTypes(unittest.TestCase):
    def test_pg_type(self) -> None:
        """Data types map to PostgreSQL types, unknown types to text."""
        cases: list[tuple[DataType | None, types.PgType]] = [
            (DataType.Boolean, types.BOOL),
            (DataType.Integer, types.INT8),
            (DataType.Float, types.FLOAT8),
            (DataType.String, types.TEXT),
            (None, types.TEXT),
        ]
        for datatype, expect in cases:
            with self.subTest(datatype=datatype):
                self.assertEqual(types.pg_type(datatype), expect)
        self.assertEqual((types.INT8.name, types.INT8.oid, types.INT8.size), ("int8", 20, 8))
        self.assertEqual((types.TEXT.name, types.TEXT.oid, types.TEXT.size), ("text", 25, -1))

    def test_encode(self) -> None:
        """Values are encoded by their own data type, NULL as None."""
        cases: list[tuple[Value, str, bytes]] = [
            (Value.Boolean(True), "t", b"\x01"),
            (Value.Boolean(False), "f", b"\x00"),
            (Value.Integer(-7), "-7", struct.pack(">q", -7)),
            (Value.Integer(2**63 - 1), "9223372036854775807", b"\x7f" + b"\xff" * 7),
            (Value.Float(1.5), "1.5", struct.pack(">d", 1.5)),
            (Value.Float(1.0), "1.0", struct.pack(">d", 1.0)),
            (Value.Float(1e100), "1e100", struct.pack(">d", 1e100)),
            (Value.Float(-0.0), "-0.0", struct.pack(">d", -0.0)),
            (Value.Float(math.nan), "NaN", struct.pack(">d", math.nan)),
            (Value.Float(math.inf), "Infinity", struct.pack(">d", math.inf)),
            (Value.Float(-math.inf), "-Infinity", struct.pack(">d", -math.inf)),
            (Value.String("it's é"), "it's é", "it's é".encode()),
        ]
        for value, text, binary in cases:
            with self.subTest(value=value):
                self.assertEqual(types.encode_text(value), text)
                self.assertEqual(types.encode(value, 0), text.encode())
                self.assertEqual(types.encode(value, 1), binary)
        self.assertIsNone(types.encode(Value.Null(), 0))
        self.assertIsNone(types.encode(Value.Null(), 1))

    def test_parameter_type(self) -> None:
        """Parameter OIDs map to data types; other OIDs aren't supported."""
        cases = [
            (16, DataType.Boolean),
            (20, DataType.Integer),
            (21, DataType.Integer),
            (23, DataType.Integer),
            (700, DataType.Float),
            (701, DataType.Float),
            (25, DataType.String),
            (1043, DataType.String),
        ]
        for oid, expect in cases:
            with self.subTest(oid=oid):
                self.assertEqual(types.parameter_type(oid), expect)
        for oid in (0, 114, 1082):
            with self.subTest(oid=oid):
                with self.assertRaises(PgError) as raised:
                    types.parameter_type(oid)
                self.assertEqual(raised.exception.code, "0A000")
                self.assertEqual(raised.exception.message, f"unsupported parameter type {oid}")

    def test_decode_text(self) -> None:
        """Text parameter values are read by their declared OID."""
        cases: list[tuple[int, str, Value]] = [
            (16, "t", Value.Boolean(True)),
            (16, "TRUE", Value.Boolean(True)),
            (16, "False", Value.Boolean(False)),
            (16, "f", Value.Boolean(False)),
            (21, "-32768", Value.Integer(-32768)),
            (21, "32767", Value.Integer(32767)),
            (23, "+7", Value.Integer(7)),
            (23, "-2147483648", Value.Integer(-(2**31))),
            (20, "-0", Value.Integer(0)),
            (20, "9223372036854775807", Value.Integer(2**63 - 1)),
            (700, "1e400", Value.Float(math.inf)),
            (700, "-0.5", Value.Float(-0.5)),
            (701, "2.5e-3", Value.Float(0.0025)),
            (701, "1", Value.Float(1.0)),
            (701, "nan", Value.Float(math.nan)),
            (701, "INFINITY", Value.Float(math.inf)),
            (701, "-Infinity", Value.Float(-math.inf)),
            (701, "Inf", Value.Float(math.inf)),
            (701, "-inf", Value.Float(-math.inf)),
            (25, "a b", Value.String("a b")),
            (1043, "", Value.String("")),
        ]
        for oid, text, expect in cases:
            with self.subTest(oid=oid, text=text):
                self.assertEqual(types.decode_parameter(oid, 0, text.encode(), 1), expect)
        self.assertEqual(types.decode_parameter(20, 0, None, 1), Value.Null())

    def test_decode_text_errors(self) -> None:
        """Invalid text is a syntax error, an integer out of the type's range
        a range error."""
        cases: list[tuple[int, str, str, str]] = [
            (16, "yes", "22P02", 'invalid input syntax for type bool: "yes"'),
            (16, "", "22P02", 'invalid input syntax for type bool: ""'),
            (20, "1.0", "22P02", 'invalid input syntax for type int8: "1.0"'),
            (20, " 1", "22P02", 'invalid input syntax for type int8: " 1"'),
            (23, "١", "22P02", 'invalid input syntax for type int4: "١"'),
            (700, ".5", "22P02", 'invalid input syntax for type float4: ".5"'),
            (701, "Infinite", "22P02", 'invalid input syntax for type float8: "Infinite"'),
            (701, "+1", "22P02", 'invalid input syntax for type float8: "+1"'),
            (21, "32768", "22003", 'value "32768" is out of range for type int2'),
            (21, "-32769", "22003", 'value "-32769" is out of range for type int2'),
            (23, "2147483648", "22003", 'value "2147483648" is out of range for type int4'),
            (
                20,
                "9223372036854775808",
                "22003",
                'value "9223372036854775808" is out of range for type int8',
            ),
        ]
        for oid, text, code, message in cases:
            with self.subTest(oid=oid, text=text):
                with self.assertRaises(PgError) as raised:
                    types.decode_parameter(oid, 0, text.encode(), 1)
                self.assertEqual((raised.exception.code, raised.exception.message), (code, message))

    def test_decode_binary(self) -> None:
        """Binary parameter values have the sizes of their types."""
        cases: list[tuple[int, bytes, Value]] = [
            (16, b"\x01", Value.Boolean(True)),
            (16, b"\x00", Value.Boolean(False)),
            (21, b"\x01\x02", Value.Integer(258)),
            (23, b"\xff\xff\xff\xfe", Value.Integer(-2)),
            (20, b"\x00" * 7 + b"\x01", Value.Integer(1)),
            (700, b"\x3f\xc0\x00\x00", Value.Float(1.5)),
            (701, struct.pack(">d", -0.25), Value.Float(-0.25)),
            (25, "hé".encode(), Value.String("hé")),
            (1043, b"", Value.String("")),
        ]
        for oid, data, expect in cases:
            with self.subTest(oid=oid, data=data):
                self.assertEqual(types.decode_parameter(oid, 1, data, 1), expect)

    def test_decode_binary_errors(self) -> None:
        """Binary data of the wrong length, a boolean other than 0 or 1, and
        text that isn't UTF-8 are invalid."""
        cases: list[tuple[int, bytes]] = [
            (16, b"\x02"),
            (16, b""),
            (21, b"\x01"),
            (23, b"\x00" * 8),
            (20, b"\x01"),
            (700, b"\x00" * 8),
            (701, b"\x00" * 4),
            (25, b"\xff"),
        ]
        for oid, data in cases:
            with self.subTest(oid=oid, data=data):
                with self.assertRaises(PgError) as raised:
                    types.decode_parameter(oid, 1, data, 3)
                self.assertEqual(raised.exception.code, "22P03")
                self.assertEqual(
                    raised.exception.message, "incorrect binary data format in bind parameter 3"
                )

    def test_decode_unsupported(self) -> None:
        """Unsupported OIDs and format codes are errors."""
        with self.assertRaises(PgError) as raised:
            types.decode_parameter(114, 0, b"{}", 1)
        self.assertEqual(raised.exception.code, "0A000")
        with self.assertRaises(PgError) as raised:
            types.decode_parameter(20, 2, b"1", 1)
        self.assertEqual(raised.exception.code, "22023")


if __name__ == "__main__":
    unittest.main()
