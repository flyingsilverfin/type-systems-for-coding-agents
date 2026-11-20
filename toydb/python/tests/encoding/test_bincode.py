"""Tests for toydb.encoding.bincode. The Rust module has no unit tests, since
it wraps the `bincode` crate; these check the reimplemented encoding against
byte sequences produced by the crate with `config::standard()`."""

import io
import math
import unittest
from dataclasses import dataclass
from typing import Any, Self

from toydb import encoding
from toydb.encoding import bincode, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error


@dataclass(frozen=True)
class Entry(encoding.Value):
    """A struct, like raft::Entry."""

    index: int
    term: int
    command: bytes | None

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u64(self.index)
        serializer.serialize_u64(self.term)
        serde.Option(serde.Bytes).serialize(self.command, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> Self:
        deserializer.deserialize_struct()
        return cls(
            index=deserializer.deserialize_u64(),
            term=deserializer.deserialize_u64(),
            command=serde.Option(serde.Bytes).deserialize(deserializer),
        )


class TestBincode(unittest.TestCase):
    def test_serialize_deserialize(self) -> None:
        cases: dict[str, tuple[serde.Serde[Any], Any, str]] = {
            "bool_false": (serde.Bool, False, "00"),
            "bool_true": (serde.Bool, True, "01"),
            "u8": (serde.U8, 255, "ff"),
            "i8": (serde.I8, -1, "ff"),
            "u64_0": (serde.U64, 0, "00"),
            "u64_250": (serde.U64, 250, "fa"),
            "u64_251": (serde.U64, 251, "fbfb00"),
            "u64_u16_max": (serde.U64, 65535, "fbffff"),
            "u64_65536": (serde.U64, 65536, "fc00000100"),
            "u64_u32_max": (serde.U64, 2**32 - 1, "fcffffffff"),
            "u64_u32_max_1": (serde.U64, 2**32, "fd0000000001000000"),
            "u64_max": (serde.U64, 2**64 - 1, "fdffffffffffffffff"),
            "u16_300": (serde.U16, 300, "fb2c01"),
            "u32_70000": (serde.U32, 70000, "fc70110100"),
            "i64_0": (serde.I64, 0, "00"),
            "i64_neg_1": (serde.I64, -1, "01"),
            "i64_1": (serde.I64, 1, "02"),
            "i64_neg_125": (serde.I64, -125, "f9"),
            "i64_125": (serde.I64, 125, "fa"),
            "i64_neg_126": (serde.I64, -126, "fbfb00"),
            "i64_min": (serde.I64, -(2**63), "fdffffffffffffffff"),
            "i64_max": (serde.I64, 2**63 - 1, "fdfeffffffffffffff"),
            "f64_pi": (serde.F64, math.pi, "182d4454fb210940"),
            "f64_neg_zero": (serde.F64, -0.0, "0000000000000080"),
            "string": (serde.String, "foo", "03666f6f"),
            "string_empty": (serde.String, "", "00"),
            "string_utf8": (serde.String, "👋", "04f09f918b"),
            "bytes": (serde.Bytes, b"\x00\x01\xff", "030001ff"),
            "none": (serde.Option(serde.U64), None, "00"),
            "some": (serde.Option(serde.U64), 7, "0107"),
            "unit": (serde.Unit, None, ""),
            "tuple": (serde.Tuple(serde.U64, serde.Option(serde.U8)), (3, 1), "030101"),
            "vec": (serde.Vec(serde.I64), [1, -1, 300], "0302 01 fb5802".replace(" ", "")),
            "vec_empty": (serde.Vec(serde.I64), [], "00"),
            "btree_set": (serde.BTreeSet(serde.U64), {3, 1, 2}, "03010203"),
            "btree_map": (
                serde.BTreeMap(serde.String, serde.U64),
                {"b": 2, "a": 1},
                "02016101016202",
            ),
            "result_ok": (serde.Result(serde.U64), 5, "0005"),
            "result_err": (serde.Result(serde.U64), Error.InvalidInput("foo"), "010203666f6f"),
            "error_abort": (Error, Error.Abort(), "00"),
            "error_data": (Error, Error.InvalidData("a"), "010161"),
            "error_io": (Error, Error.IO("a"), "030161"),
            "error_read_only": (Error, Error.ReadOnly(), "04"),
            "error_serialization": (Error, Error.Serialization(), "05"),
            "struct": (Entry, Entry(1, 2, b"foo"), "01020103666f6f"),
            "struct_none": (Entry, Entry(300, 0, None), "fb2c010000"),
            "vec_struct": (serde.Vec(Entry), [Entry(1, 1, None)], "01010100"),
        }
        for name, (ty, input, expect) in cases.items():
            with self.subTest(name):
                output = bincode.serialize(input, ty)
                self.assertEqual(output.hex(), expect, "encode failed")
                self.assertEqual(bincode.deserialize(ty, output), input, "decode failed")

    def test_value(self) -> None:
        """Tests the encoding.Value methods."""
        entry = Entry(1, 2, b"foo")
        self.assertEqual(entry.encode().hex(), "01020103666f6f")
        self.assertEqual(Entry.decode(entry.encode()), entry)
        self.assertEqual(bincode.serialize(entry), entry.encode())

        buffer = io.BytesIO()
        entry.encode_into(buffer)
        Entry(3, 4, None).encode_into(buffer)
        buffer.seek(0)
        self.assertEqual(Entry.decode_from(buffer), entry)
        self.assertEqual(Entry.maybe_decode_from(buffer), Entry(3, 4, None))
        self.assertIsNone(Entry.maybe_decode_from(buffer))
        with self.assertRaises(Error) as context:
            Entry.decode_from(buffer)
        self.assertEqual(
            str(context.exception),
            'invalid data: Io { inner: Error { kind: UnexpectedEof, message: "failed to fill '
            'whole buffer" }, additional: 1 }',
        )
        with self.assertRaises(Error) as context:
            bincode.deserialize_from(serde.String, io.BytesIO(b"\x03a"))
        self.assertEqual(
            str(context.exception),
            'invalid data: Io { inner: Error { kind: UnexpectedEof, message: "failed to fill '
            'whole buffer" }, additional: 3 }',
        )
        self.assertIsNone(bincode.maybe_deserialize_from(serde.String, io.BytesIO(b"\x03a")))

    def test_deserialize_trailing(self) -> None:
        """Trailing bytes are ignored, like in the Rust wrapper."""
        self.assertEqual(bincode.deserialize(serde.U64, bytes.fromhex("01ff")), 1)

    def test_deserialize_error(self) -> None:
        cases: dict[str, tuple[str, serde.Serde[Any], str]] = {
            "empty": ("", serde.U64, "UnexpectedEnd { additional: 1 }"),
            "varint_partial": ("fb01", serde.U64, "UnexpectedEnd { additional: 1 }"),
            "f64_partial": ("0000", serde.F64, "UnexpectedEnd { additional: 6 }"),
            "string_partial": ("0361", serde.String, "UnexpectedEnd { additional: 2 }"),
            "bool": ("02", serde.Bool, "InvalidBooleanValue(2)"),
            "u16_u32": ("fc00000100", serde.U16, "InvalidIntegerType { expected: U16, found: U32 }"),
            "u64_u128": ("fe", serde.U64, "InvalidIntegerType { expected: U64, found: U128 }"),
            "u64_reserved": (
                "ff",
                serde.U64,
                "InvalidIntegerType { expected: U64, found: Reserved }",
            ),
            "i64_i128": ("fe", serde.I64, "InvalidIntegerType { expected: I64, found: I128 }"),
            "utf8": (
                "01c0",
                serde.String,
                "Utf8 { inner: Utf8Error { valid_up_to: 0, error_len: Some(1) } }",
            ),
            "utf8_incomplete": (
                "02e282",
                serde.String,
                "Utf8 { inner: Utf8Error { valid_up_to: 0, error_len: None } }",
            ),
            "vec_len": ("fdffffffffffffffff01", serde.Vec(serde.U64), "UnexpectedEnd { additional: 1 }"),
            "option": (
                "02",
                serde.Option(serde.Bool),
                'UnexpectedVariant { type_name: "Option<T>", allowed: Range { min: 0, max: 1 }, '
                "found: 2 }",
            ),
            "variant": (
                "06",
                Error,
                'OtherString("invalid value: integer `6`, expected variant index 0 <= i < 6")',
            ),
        }
        for name, (input, ty, expect) in cases.items():
            with self.subTest(name):
                with self.assertRaises(Error) as context:
                    bincode.deserialize(ty, bytes.fromhex(input))
                self.assertEqual(context.exception, Error.InvalidData(expect))

    def test_serialize_error(self) -> None:
        """Values that don't fit their Rust type panic."""
        for ty, value in [(serde.U8, 256), (serde.U64, -1), (serde.U64, 2**64), (serde.I64, 2**63)]:
            with self.subTest(f"{ty}={value}"):
                with self.assertRaises(AssertionError):
                    bincode.serialize(value, ty)


if __name__ == "__main__":
    unittest.main()
