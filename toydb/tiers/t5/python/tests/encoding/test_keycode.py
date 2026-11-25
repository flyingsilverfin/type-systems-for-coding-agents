"""Tests for toydb.encoding.keycode, ported from the unit tests in
`src/encoding/keycode.rs`."""

import math
import unittest
from abc import ABC
from dataclasses import dataclass
from typing import Any

from toydb.encoding import keycode, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error
from toydb.ops import Bound

PI = math.pi
U64_MAX = 2**64 - 1
I64_MIN, I64_MAX = -(2**63), 2**63 - 1
F64_MIN, F64_MAX = -1.7976931348623157e308, 1.7976931348623157e308


class Key(ABC):
    """A test key enum. The Rust `Cow` variant is identical to `Tuple` in
    Python apart from the field order, since there are no borrowed values."""

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case Unit():
                serializer.serialize_unit_variant(0)
            case NewType(value):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_str(value)
            case Tuple(a, b, c):
                serializer.serialize_tuple_variant(2)
                serializer.serialize_bool(a)
                serializer.serialize_bytes(b)
                serializer.serialize_u64(c)
            case Cow(a, b, c):
                serializer.serialize_tuple_variant(3)
                serializer.serialize_bytes(a)
                serializer.serialize_bool(b)
                serializer.serialize_str(c)
            case _:
                raise AssertionError("unknown variant")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Key":
        match deserializer.deserialize_enum():
            case 0:
                return Unit()
            case 1:
                return NewType(deserializer.deserialize_str())
            case 2:
                return Tuple(
                    deserializer.deserialize_bool(),
                    deserializer.deserialize_bytes(),
                    deserializer.deserialize_u64(),
                )
            case 3:
                return Cow(
                    deserializer.deserialize_bytes(),
                    deserializer.deserialize_bool(),
                    deserializer.deserialize_str(),
                )
            case index:
                raise deserializer.invalid_variant(index, 4)


@dataclass(frozen=True)
class Unit(Key):
    pass


@dataclass(frozen=True)
class NewType(Key):
    _0: str


@dataclass(frozen=True)
class Tuple(Key):
    _0: bool
    _1: bytes
    _2: int


@dataclass(frozen=True)
class Cow(Key):
    _0: bytes
    _1: bool
    _2: str


def sql_value() -> Any:
    """Returns the sql::types::Value class."""
    from toydb.sql.types import Value

    return Value


class TestKeycode(unittest.TestCase):
    def assert_serialize_deserialize(self, ty: serde.Serde[Any], input: Any, expect: str) -> None:
        """Assert that serializing a value yields the expected byte sequence
        (as a hex-encoded string), and that deserializing it yields the
        original value."""
        output = keycode.serialize(input, ty)
        self.assertEqual(output.hex(), expect, "encode failed")
        decoded = keycode.deserialize(ty, output)
        self.assertEqual(decoded, input, "decode failed")
        if isinstance(input, float):  # distinguish 0.0 and -0.0
            self.assertEqual(math.copysign(1.0, decoded), math.copysign(1.0, input))

    def test_serialize_deserialize(self) -> None:
        cases: dict[str, tuple[serde.Serde[Any], Any, str]] = {
            "bool_false": (serde.Bool, False, "00"),
            "bool_true": (serde.Bool, True, "01"),
            "f64_min": (serde.F64, F64_MIN, "0010000000000000"),
            "f64_neg_inf": (serde.F64, -math.inf, "000fffffffffffff"),
            "f64_neg_pi": (serde.F64, -PI, "3ff6de04abbbd2e7"),
            "f64_neg_zero": (serde.F64, -0.0, "7fffffffffffffff"),
            "f64_zero": (serde.F64, 0.0, "8000000000000000"),
            "f64_pi": (serde.F64, PI, "c00921fb54442d18"),
            "f64_max": (serde.F64, F64_MAX, "ffefffffffffffff"),
            "f64_inf": (serde.F64, math.inf, "fff0000000000000"),
            # We don't test NAN here, since NAN != NAN.
            "i64_min": (serde.I64, I64_MIN, "0000000000000000"),
            "i64_neg_65535": (serde.I64, -65535, "7fffffffffff0001"),
            "i64_neg_1": (serde.I64, -1, "7fffffffffffffff"),
            "i64_0": (serde.I64, 0, "8000000000000000"),
            "i64_1": (serde.I64, 1, "8000000000000001"),
            "i64_65535": (serde.I64, 65535, "800000000000ffff"),
            "i64_max": (serde.I64, I64_MAX, "ffffffffffffffff"),
            "u64_min": (serde.U64, 0, "0000000000000000"),
            "u64_1": (serde.U64, 1, "0000000000000001"),
            "u64_65535": (serde.U64, 65535, "000000000000ffff"),
            "u64_max": (serde.U64, U64_MAX, "ffffffffffffffff"),
            "bytes": (serde.Bytes, b"\x01\xff", "01ff0000"),
            "bytes_empty": (serde.Bytes, b"", "0000"),
            "bytes_escape": (serde.Bytes, b"\x00\x01\x02", "00ff01020000"),
            "string": (serde.String, "foo", "666f6f0000"),
            "string_empty": (serde.String, "", "0000"),
            "string_escape": (serde.String, "foo\x00bar", "666f6f00ff6261720000"),
            "string_utf8": (serde.String, "👋", "f09f918b0000"),
            "tuple": (
                serde.Tuple(serde.Bool, serde.U64, serde.Bytes),
                (True, U64_MAX, b"\x00\x01"),
                "01ffffffffffffffff00ff010000",
            ),
            "array_bool": (
                serde.Tuple(serde.Bool, serde.Bool, serde.Bool),
                (False, True, False),
                "000100",
            ),
            "vec_bool": (serde.Vec(serde.Bool), [False, True, False], "000100"),
            "vec_u64": (
                serde.Vec(serde.U64),
                [0, U64_MAX, 65535],
                "0000000000000000ffffffffffffffff000000000000ffff",
            ),
            "enum_unit": (Key, Unit(), "00"),
            "enum_newtype": (Key, NewType("foo"), "01666f6f0000"),
            "enum_tuple": (
                Key,
                Tuple(False, b"\x00\x01", U64_MAX),
                "020000ff010000ffffffffffffffff",
            ),
            "enum_cow": (Key, Cow(b"\x00\x01", False, "foo"), "0300ff01000000666f6f0000"),
        }
        for name, (ty, input, expect) in cases.items():
            with self.subTest(name):
                self.assert_serialize_deserialize(ty, input, expect)

    def test_serialize_default_serde(self) -> None:
        """Classes implementing serialize() don't need an explicit serde."""
        self.assertEqual(keycode.serialize(NewType("foo")).hex(), "01666f6f0000")

    def test_serialize_deserialize_value(self) -> None:
        try:
            Value = sql_value()
        except ImportError:
            self.skipTest("toydb.sql.types is not available")
        cases = {
            "value_null": (Value.Null(), "00"),
            "value_bool": (Value.Boolean(True), "0101"),
            "value_int": (Value.Integer(-1), "027fffffffffffffff"),
            "value_float": (Value.Float(PI), "03c00921fb54442d18"),
            "value_string": (Value.String("foo"), "04666f6f0000"),
        }
        for name, (input, expect) in cases.items():
            with self.subTest(name):
                self.assert_serialize_deserialize(Value, input, expect)

    def test_serialize_error(self) -> None:
        """Assert that serializing unsupported values panics. The Rust char,
        f32, i128 and u128 cases have no Python equivalent."""
        cases: dict[str, tuple[serde.Serde[Any], Any]] = {
            "i8": (serde.I8, 0),
            "i16": (serde.I16, 0),
            "i32": (serde.I32, 0),
            "u8": (serde.U8, 0),
            "u16": (serde.U16, 0),
            "u32": (serde.U32, 0),
            "some": (serde.Option(serde.Bool), True),
            "none": (serde.Option(serde.Bool), None),
            "vec_u8": (serde.Vec(serde.U8), [0]),
        }
        for name, (ty, input) in cases.items():
            with self.subTest(name):
                with self.assertRaises((NotImplementedError, AssertionError)):
                    keycode.serialize(input, ty)

    def test_deserialize_error(self) -> None:
        """Assert that deserializing invalid inputs results in errors. Takes
        byte slices (as hex-encoded strings) and the type to deserialize into."""
        cases: dict[str, tuple[str, serde.Serde[Any]]] = {
            "bool_empty": ("", serde.Bool),
            "bool_2": ("02", serde.Bool),
            "i8": ("00", serde.I8),
            "i16": ("0000", serde.I16),
            "i32": ("00000000", serde.I32),
            "u16": ("0000", serde.U16),
            "u32": ("00000000", serde.U32),
            "u64_partial": ("0000", serde.U64),
            "option": ("00", serde.Option(serde.Bool)),
            "string_utf8_invalid": ("c0", serde.String),
            "tuple_partial": ("0001", serde.Tuple(serde.Bool, serde.Bool, serde.Bool)),
            "vec_u8": ("0000", serde.Vec(serde.U8)),
        }
        for name, (input, ty) in cases.items():
            with self.subTest(name):
                with self.assertRaises((Error, NotImplementedError)):
                    keycode.deserialize(ty, bytes.fromhex(input))

    def test_deserialize_error_messages(self) -> None:
        cases: dict[str, tuple[str, serde.Serde[Any], str]] = {
            "trailing": (
                "0100ff",
                serde.Bool,
                "invalid data: unexpected trailing bytes [0, ff] at end of key [1, 0, ff]",
            ),
            "insufficient": (
                "0a0b",
                serde.U64,
                "invalid data: insufficient bytes, expected 8 bytes for [a, b]",
            ),
            "bool": ("02", serde.Bool, "invalid data: invalid boolean value 2"),
            "escape": ("610001", serde.Bytes, "invalid data: invalid escape sequence"),
            "unterminated": ("6100", serde.Bytes, "invalid data: invalid escape sequence"),
            "end": ("61", serde.Bytes, "invalid data: unexpected end of input"),
            "utf8": (
                "c00000",
                serde.String,
                "invalid data: invalid utf-8 sequence of 1 bytes from index 0",
            ),
            "variant": (
                "07",
                Key,
                "invalid data: invalid value: integer `7`, expected variant index 0 <= i < 4",
            ),
        }
        for name, (input, ty, expect) in cases.items():
            with self.subTest(name):
                with self.assertRaises(Error) as context:
                    keycode.deserialize(ty, bytes.fromhex(input))
                self.assertEqual(str(context.exception), expect)

    def test_ordering(self) -> None:
        """The encoding preserves the ordering of values."""
        cases: list[tuple[serde.Serde[Any], list[Any]]] = [
            (serde.I64, [I64_MIN, -65535, -1, 0, 1, 65535, I64_MAX]),
            (serde.U64, [0, 1, 255, 256, 65535, U64_MAX]),
            (serde.F64, [-math.inf, F64_MIN, -PI, -0.0, 0.0, 1e-300, PI, F64_MAX, math.inf]),
            (serde.String, ["", "\x00", "\x00\x00", "\x00a", "a", "a\x00", "ab", "b"]),
            (serde.Bool, [False, True]),
        ]
        for ty, values in cases:
            encoded = [keycode.serialize(value, ty) for value in values]
            self.assertEqual(encoded, sorted(encoded))
        self.assertGreater(keycode.serialize(math.nan, serde.F64), encoded[-1])

    def test_prefix_range(self) -> None:
        self.assertEqual(
            keycode.prefix_range(b"a"), (Bound.Included(b"a"), Bound.Excluded(b"b"))
        )
        self.assertEqual(
            keycode.prefix_range(b"a\xff\xff"), (Bound.Included(b"a\xff\xff"), Bound.Excluded(b"b"))
        )
        self.assertEqual(
            keycode.prefix_range(b"\x00\xff\x01"),
            (Bound.Included(b"\x00\xff\x01"), Bound.Excluded(b"\x00\xff\x02")),
        )
        self.assertEqual(
            keycode.prefix_range(b"\xff\xff"), (Bound.Included(b"\xff\xff"), Bound.Unbounded())
        )
        self.assertEqual(keycode.prefix_range(b""), (Bound.Included(b""), Bound.Unbounded()))


if __name__ == "__main__":
    unittest.main()
