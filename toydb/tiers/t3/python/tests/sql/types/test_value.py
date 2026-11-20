"""Tests for toydb.sql.types.value.

The Rust module has no unit tests of its own (it is covered by the SQL golden
scripts). The expected formatting, encoding and ordering here was generated
by running the Rust implementation on the same values.
"""

import math
import unittest

from toydb.encoding import bincode, keycode
from toydb.error import Error, Panic
from toydb.fmt import debug
from toydb.sql.parser import ast
from toydb.sql.types import DataType, Label, Value
from toydb.sql.types.value import I64_MAX, I64_MIN

VALUES: list[Value] = [
    Value.Null(),
    Value.Boolean(False),
    Value.Boolean(True),
    Value.Integer(I64_MIN),
    Value.Integer(-1),
    Value.Integer(0),
    Value.Integer(1),
    Value.Integer(9007199254740992),
    Value.Integer(9007199254740993),
    Value.Integer(I64_MAX),
    Value.Float(-math.nan),
    Value.Float(-math.inf),
    Value.Float(-1.5),
    Value.Float(-0.0),
    Value.Float(0.0),
    Value.Float(1.0),
    Value.Float(9007199254740992.0),
    Value.Float(math.inf),
    Value.Float(math.nan),
    Value.String(""),
    Value.String("a"),
    Value.String("ab"),
    Value.String("b"),
    Value.String("é"),
    Value.String("日本"),
]

# For each of VALUES: Display, Debug, Bincode (hex) and Keycode (hex).
FORMATS: list[tuple[str, str, str, str]] = [
    ('NULL', 'Null', '00', '00'),
    ('FALSE', 'Boolean(false)', '0100', '0100'),
    ('TRUE', 'Boolean(true)', '0101', '0101'),
    ('-9223372036854775808', 'Integer(-9223372036854775808)', '02fdffffffffffffffff', '020000000000000000'),
    ('-1', 'Integer(-1)', '0201', '027fffffffffffffff'),
    ('0', 'Integer(0)', '0200', '028000000000000000'),
    ('1', 'Integer(1)', '0202', '028000000000000001'),
    ('9007199254740992', 'Integer(9007199254740992)', '02fd0000000000004000', '028020000000000000'),
    ('9007199254740993', 'Integer(9007199254740993)', '02fd0200000000004000', '028020000000000001'),
    ('9223372036854775807', 'Integer(9223372036854775807)', '02fdfeffffffffffffff', '02ffffffffffffffff'),
    ('NaN', 'Float(NaN)', '03000000000000f87f', '03fff8000000000000'),
    ('-inf', 'Float(-inf)', '03000000000000f0ff', '03000fffffffffffff'),
    ('-1.5', 'Float(-1.5)', '03000000000000f8bf', '034007ffffffffffff'),
    ('-0.0', 'Float(-0.0)', '030000000000000000', '038000000000000000'),
    ('0.0', 'Float(0.0)', '030000000000000000', '038000000000000000'),
    ('1.0', 'Float(1.0)', '03000000000000f03f', '03bff0000000000000'),
    ('9007199254740992.0', 'Float(9007199254740992.0)', '030000000000004043', '03c340000000000000'),
    ('inf', 'Float(inf)', '03000000000000f07f', '03fff0000000000000'),
    ('NaN', 'Float(NaN)', '03000000000000f87f', '03fff8000000000000'),
    ("''", 'String("")', '0400', '040000'),
    ("'a'", 'String("a")', '040161', '04610000'),
    ("'ab'", 'String("ab")', '04026162', '0461620000'),
    ("'b'", 'String("b")', '040162', '04620000'),
    ("'é'", 'String("é")', '0402c3a9', '04c3a90000'),
    ("'日本'", 'String("日本")', '0406e697a5e69cac', '04e697a5e69cac0000'),
]

# For each of VALUES: the cmp() ordering and equality with each of VALUES.
COMPARISONS: list[tuple[str, str]] = [
    ('=<<<<<<<<<<<<<<<<<<<<<<<<', '1000000000000000000000000'),
    ('>=<<<<<<<<<<<<<<<<<<<<<<<', '0100000000000000000000000'),
    ('>>=<<<<<<<<<<<<<<<<<<<<<<', '0010000000000000000000000'),
    ('>>>=<<<<<<>><<<<<<<<<<<<<', '0001000000000000000000000'),
    ('>>>>=<<<<<>>><<<<<<<<<<<<', '0000100000000000000000000'),
    ('>>>>>=<<<<>>>>=<<<<<<<<<<', '0000010000000110000000000'),
    ('>>>>>>=<<<>>>>>=<<<<<<<<<', '0000001000000001000000000'),
    ('>>>>>>>=<<>>>>>>=<<<<<<<<', '0000000100000000100000000'),
    ('>>>>>>>>=<>>>>>>=<<<<<<<<', '0000000010000000100000000'),
    ('>>>>>>>>>=>>>>>>><<<<<<<<', '0000000001000000000000000'),
    ('>>><<<<<<<=<<<<<<<<<<<<<<', '0000000000100000001000000'),
    ('>>><<<<<<<>=<<<<<<<<<<<<<', '0000000000010000000000000'),
    ('>>>><<<<<<>>=<<<<<<<<<<<<', '0000000000001000000000000'),
    ('>>>>><<<<<>>>=<<<<<<<<<<<', '0000010000000110000000000'),
    ('>>>>>=<<<<>>>>=<<<<<<<<<<', '0000010000000110000000000'),
    ('>>>>>>=<<<>>>>>=<<<<<<<<<', '0000001000000001000000000'),
    ('>>>>>>>==<>>>>>>=<<<<<<<<', '0000000110000000100000000'),
    ('>>>>>>>>>>>>>>>>>=<<<<<<<', '0000000000000000010000000'),
    ('>>>>>>>>>>>>>>>>>>=<<<<<<', '0000000000100000001000000'),
    ('>>>>>>>>>>>>>>>>>>>=<<<<<', '0000000000000000000100000'),
    ('>>>>>>>>>>>>>>>>>>>>=<<<<', '0000000000000000000010000'),
    ('>>>>>>>>>>>>>>>>>>>>>=<<<', '0000000000000000000001000'),
    ('>>>>>>>>>>>>>>>>>>>>>>=<<', '0000000000000000000000100'),
    ('>>>>>>>>>>>>>>>>>>>>>>>=<', '0000000000000000000000010'),
    ('>>>>>>>>>>>>>>>>>>>>>>>>=', '0000000000000000000000001'),
]


def error(f: object, *args: object) -> str:
    """Calls the function, returning the error message."""
    assert callable(f)
    try:
        f(*args)
    except Error as error:
        return str(error)
    raise AssertionError("expected error")


class TestValue(unittest.TestCase):
    def test_format(self) -> None:
        for value, (display, debugged, _, _) in zip(VALUES, FORMATS, strict=True):
            with self.subTest(value=value):
                self.assertEqual(str(value), display)
                self.assertEqual(debug(value), debugged)
                self.assertEqual(repr(value), debugged)
        self.assertEqual(str(Value.String("a'b\"c\n\\")), "'a\\'b\\\"c\\n\\\\'")
        self.assertEqual(debug(Value.String("a'b\"c\n")), 'String("a\'b\\"c\\n")')
        self.assertEqual(debug(Value.Integer(1), pretty=True), "Integer(\n    1,\n)")
        self.assertEqual(str(Value.Float(1e16)), "1e16")
        self.assertEqual(str(Value.Float(123456.789)), "123456.789")
        self.assertEqual(str(Value.Float(1e-5)), "1e-5")

    def test_encoding(self) -> None:
        for value, (_, _, bincoded, keycoded) in zip(VALUES, FORMATS, strict=True):
            with self.subTest(value=value):
                self.assertEqual(value.encode().hex(), bincoded)
                self.assertEqual(bincode.serialize(value).hex(), bincoded)
                self.assertEqual(keycode.serialize(value).hex(), keycoded)
                # -0.0 and -NaN are normalized when encoded, but still equal.
                self.assertEqual(Value.decode(value.encode()), value)
                self.assertEqual(keycode.deserialize(Value, keycode.serialize(value)), value)
        with self.assertRaises(Error):
            Value.decode(b"\x05")

    def test_cmp(self) -> None:
        signs = {-1: "<", 0: "=", 1: ">"}
        for a, (ordering, equality) in zip(VALUES, COMPARISONS, strict=True):
            for b, order, equal in zip(VALUES, ordering, equality, strict=True):
                with self.subTest(a=a, b=b):
                    self.assertEqual(signs[a.cmp(b)], order)
                    self.assertEqual(a == b, equal == "1")
                    self.assertEqual(a != b, equal == "0")
                    self.assertEqual(a < b, order == "<")
                    self.assertEqual(a <= b, order in "<=")
                    self.assertEqual(a > b, order == ">")
                    self.assertEqual(a >= b, order in ">=")

    def test_sort(self) -> None:
        """Values sort by cmp(): Null, booleans, numbers, then strings."""
        # Leave out integers that equal a float, whose order is ambiguous.
        values = [v for v in VALUES if not (isinstance(v, Value.Integer) and abs(v.value) < 2**60)]
        values.append(Value.Integer(2))
        expect = (
            "NULL, FALSE, TRUE, NaN, -inf, -9223372036854775808, -1.5, -0.0, 0.0, 1.0, 2, "
            "9007199254740992.0, 9223372036854775807, inf, NaN, '', 'a', 'ab', 'b', 'é', '日本'"
        )
        self.assertEqual(", ".join(str(v) for v in sorted(reversed(values))), expect)
        self.assertEqual(str(min(values)), "NULL")
        self.assertEqual(str(max(values)), "'日本'")

    def test_hash(self) -> None:
        """Equal values of the same type have the same hash, including NULL,
        NaN and -NaN, and 0.0 and -0.0."""
        for a in VALUES:
            for b in VALUES:
                if type(a) is type(b) and a == b:
                    self.assertEqual(hash(a), hash(b))
        self.assertEqual(len({Value.Float(math.nan), Value.Float(-math.nan)}), 1)
        self.assertEqual(len({Value.Float(0.0), Value.Float(-0.0)}), 1)
        self.assertEqual(len({Value.Null(), Value.Null()}), 1)
        self.assertEqual(len(set(VALUES)), 23)
        self.assertIn(Value.String("a"), {Value.String("a"): 1})
        self.assertNotEqual(Value.Integer(1), 1)
        self.assertNotEqual(Value.Null(), None)

    def test_datatype(self) -> None:
        self.assertIsNone(Value.Null().datatype())
        self.assertEqual(Value.Boolean(True).datatype(), DataType.Boolean)
        self.assertEqual(Value.Integer(1).datatype(), DataType.Integer)
        self.assertEqual(Value.Float(1.0).datatype(), DataType.Float)
        self.assertEqual(Value.String("").datatype(), DataType.String)

    def test_is_undefined(self) -> None:
        undefined = [str(value) for value in VALUES if value.is_undefined()]
        self.assertEqual(undefined, ["NULL", "NaN", "NaN"])

    def test_checked_arithmetic(self) -> None:
        integer, float, null, string = Value.Integer, Value.Float, Value.Null(), Value.String
        self.assertEqual(integer(1).checked_add(integer(2)), integer(3))
        self.assertEqual(integer(1).checked_add(float(0.5)), float(1.5))
        self.assertEqual(float(0.5).checked_add(null), null)
        self.assertEqual(integer(5).checked_sub(integer(7)), integer(-2))
        self.assertEqual(integer(3).checked_mul(float(0.5)), float(1.5))
        self.assertEqual(integer(-7).checked_div(integer(2)), integer(-3))
        self.assertEqual(integer(7).checked_div(integer(-2)), integer(-3))
        self.assertEqual(integer(-7).checked_rem(integer(3)), integer(-1))
        self.assertEqual(integer(7).checked_rem(integer(-3)), integer(1))
        self.assertEqual(float(-7.5).checked_rem(integer(2)), float(-1.5))
        self.assertEqual(integer(2).checked_pow(integer(62)), integer(2**62))
        self.assertEqual(integer(2).checked_pow(integer(-1)), float(0.5))
        self.assertEqual(float(2.0).checked_pow(integer(-3)), float(0.125))
        self.assertEqual(integer(1).checked_div(float(0.0)), float(math.inf))
        self.assertEqual(integer(-1).checked_div(float(0.0)), float(-math.inf))
        self.assertEqual(str(integer(0).checked_div(float(0.0))), "NaN")
        self.assertEqual(str(integer(1).checked_rem(float(0.0))), "NaN")
        self.assertEqual(float(0.0).checked_pow(integer(-1)), float(math.inf))
        self.assertEqual(float(10.0).checked_pow(float(400.0)), float(math.inf))
        self.assertEqual(str(float(-8.0).checked_pow(float(0.5))), "NaN")
        # A result that is an integer type remains an integer, not a bool.
        self.assertIs(type(integer(1).checked_add(integer(1)).try_into_i64()), int)

        overflow = "invalid input: integer overflow"
        self.assertEqual(error(integer(I64_MAX).checked_add, integer(1)), overflow)
        self.assertEqual(error(integer(I64_MIN).checked_sub, integer(1)), overflow)
        self.assertEqual(error(integer(I64_MAX).checked_mul, integer(2)), overflow)
        self.assertEqual(error(integer(2).checked_pow, integer(63)), overflow)
        self.assertEqual(error(integer(1).checked_pow, integer(2**32)), overflow)
        self.assertEqual(integer(-2).checked_pow(integer(63)), integer(I64_MIN))
        self.assertEqual(integer(-1).checked_pow(integer(2**32 - 1)), integer(-1))
        zero = "invalid input: can't divide by zero"
        self.assertEqual(error(integer(1).checked_div, integer(0)), zero)
        self.assertEqual(error(integer(1).checked_rem, integer(0)), zero)
        self.assertEqual(
            error(integer(1).checked_add, string("a")), "invalid input: can't add 1 and 'a'"
        )
        self.assertEqual(
            error(null.checked_sub, string("a")), "invalid input: can't subtract NULL and 'a'"
        )
        self.assertEqual(
            error(Value.Boolean(True).checked_mul, integer(2)),
            "invalid input: can't multiply TRUE and 2",
        )
        self.assertEqual(
            error(string("a").checked_div, float(1.5)), "invalid input: can't divide 'a' and 1.5"
        )
        self.assertEqual(
            error(string("a").checked_pow, null), "invalid input: can't exponentiate 'a' and NULL"
        )
        self.assertEqual(
            error(null.checked_rem, string("a")),
            "invalid input: can't take remainder of NULL and 'a'",
        )
        # Integer division overflow panics in Rust.
        with self.assertRaises(Panic):
            integer(I64_MIN).checked_div(integer(-1))
        with self.assertRaises(Panic):
            integer(I64_MIN).checked_rem(integer(-1))

    def test_conversions(self) -> None:
        self.assertEqual(Value.from_native(True), Value.Boolean(True))
        self.assertEqual(Value.from_native(1), Value.Integer(1))
        self.assertEqual(Value.from_native(1.5), Value.Float(1.5))
        self.assertEqual(Value.from_native("a"), Value.String("a"))
        self.assertIs(Value.Boolean(True).try_into_bool(), True)
        self.assertEqual(Value.Integer(1).try_into_i64(), 1)
        self.assertEqual(Value.Float(1.5).try_into_f64(), 1.5)
        self.assertEqual(Value.String("a").try_into_string(), "a")
        self.assertEqual(error(Value.Integer(1).try_into_bool), "invalid data: not a boolean: 1")
        self.assertEqual(error(Value.Null().try_into_f64), "invalid data: not a float: NULL")
        self.assertEqual(
            error(Value.String("a").try_into_i64), "invalid data: not an integer: 'a'"
        )
        self.assertEqual(
            error(Value.Boolean(True).try_into_string), "invalid data: not a string: TRUE"
        )


class TestDataType(unittest.TestCase):
    def test_datatype(self) -> None:
        expect = [
            (DataType.Boolean, "BOOLEAN", "Boolean", "00"),
            (DataType.Integer, "INTEGER", "Integer", "01"),
            (DataType.Float, "FLOAT", "Float", "02"),
            (DataType.String, "STRING", "String", "03"),
        ]
        self.assertEqual(len(DataType), len(expect))
        for datatype, display, debugged, encoded in expect:
            with self.subTest(datatype=datatype):
                self.assertEqual(str(datatype), display)
                self.assertEqual(debug(datatype), debugged)
                self.assertEqual(bincode.serialize(datatype).hex(), encoded)
                self.assertEqual(bincode.deserialize(DataType, bytes.fromhex(encoded)), datatype)
        with self.assertRaises(Error):
            bincode.deserialize(DataType, b"\x04")


class TestLabel(unittest.TestCase):
    def test_label(self) -> None:
        expect = [
            (Label.None_(), "", "None", "?", "00"),
            (Label.Unqualified("a"), "a", 'Unqualified("a")', "a", "010161"),
            (Label.Qualified("t", "a"), "t.a", 'Qualified("t", "a")', "a", "0201740161"),
        ]
        for label, display, debugged, header, encoded in expect:
            with self.subTest(label=label):
                self.assertEqual(str(label), display)
                self.assertEqual(debug(label), debugged)
                self.assertEqual(label.as_header(), header)
                self.assertEqual(bincode.serialize(label).hex(), encoded)
                self.assertEqual(bincode.deserialize(Label, bytes.fromhex(encoded)), label)

    def test_conversions(self) -> None:
        self.assertEqual(Label.from_name(None), Label.None_())
        self.assertEqual(Label.from_name("a"), Label.Unqualified("a"))
        self.assertEqual(
            Label.Unqualified("a").into_ast_expression(), ast.Expression.Column(None, "a")
        )
        self.assertEqual(
            Label.Qualified("t", "a").into_ast_expression(), ast.Expression.Column("t", "a")
        )
        with self.assertRaises(Panic):
            Label.None_().into_ast_expression()


if __name__ == "__main__":
    unittest.main()
