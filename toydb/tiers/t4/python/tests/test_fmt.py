"""Tests for toydb.fmt, the Rust Display/Debug formatting helpers. Expected
values are the output of the corresponding Rust format strings."""

import math
import unittest
from dataclasses import dataclass
from enum import Enum

from toydb import fmt
from toydb.error import Error
from toydb.fmt import debug


class Color(Enum):
    Red = 1


@dataclass
class Unit:
    pass


@dataclass
class Status:
    name: str
    size: int
    vote: int | None
    keys: list[bytes]


class TestFmt(unittest.TestCase):
    def test_display_f64(self) -> None:
        cases = [
            (0.0, "0"),
            (-0.0, "-0"),
            (1.0, "1"),
            (-1.5, "-1.5"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1e15, "1000000000000000"),
            (1e21, "1000000000000000000000"),
            (1.5e-7, "0.00000015"),
            (123456.789, "123456.789"),
            (math.inf, "inf"),
            (-math.inf, "-inf"),
            (math.nan, "NaN"),
            (1.7976931348623157e308, "17976931348623157" + "0" * 292),
            (5e-324, "0." + "0" * 323 + "5"),
        ]
        for value, expect in cases:
            with self.subTest(expect):
                self.assertEqual(fmt.display_f64(value), expect)

    def test_debug_f64(self) -> None:
        cases = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-1.5, "-1.5"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e16"),
            (1.2345e20, "1.2345e20"),
            (0.0001, "0.0001"),
            (0.00001, "1e-5"),
            (1.5e-7, "1.5e-7"),
            (-1e100, "-1e100"),
            (math.inf, "inf"),
            (-math.inf, "-inf"),
            (math.nan, "NaN"),
        ]
        for value, expect in cases:
            with self.subTest(expect):
                self.assertEqual(fmt.debug_f64(value), expect)

    def test_debug_str(self) -> None:
        cases = {
            "": '""',
            "foo": '"foo"',
            "a\"b'c\\d": '"a\\"b\'c\\\\d"',
            "\0\t\r\n": '"\\0\\t\\r\\n"',
            "\x01\x7f": '"\\u{1}\\u{7f}"',
            "👋 → é": '"👋 → é"',
            "e\u0301": '"e\\u{301}"',
            "\u200b\u00ad": '"\\u{200b}\\u{ad}"',
            "➡\ufe0f": '"➡\\u{fe0f}"',
            "\u00a0\u3000\ue000\ufffd\u2028": '"\\u{a0}\\u{3000}\\u{e000}\ufffd\\u{2028}"',
            "\u200c\u200d\ufeff\U0001f3fb\U000e0020\u093e\u09be": (
                '"\\u{200c}\\u{200d}\\u{feff}\U0001f3fb\\u{e0020}\u093e\\u{9be}"'
            ),
        }
        for value, expect in cases.items():
            with self.subTest(expect):
                self.assertEqual(fmt.debug_str(value), expect)
        self.assertEqual(fmt.debug_char("'"), "'\\''")
        self.assertEqual(fmt.debug_char('"'), "'\"'")

    def test_escape_debug(self) -> None:
        self.assertEqual(fmt.escape_debug("a\"b'c\n"), "a\\\"b\\'c\\n")
        self.assertEqual(fmt.escape_debug("\u0301e\u0301"), "\\u{301}e\\u{301}")

    def test_escape_default(self) -> None:
        self.assertEqual(
            fmt.escape_default(b"a\x00\t\r\n'\"\\ ~\x7f\xff"), "a\\x00\\t\\r\\n\\'\\\"\\\\ ~\\x7f\\xff"
        )

    def test_debug(self) -> None:
        cases: list[tuple[object, str]] = [
            (None, "None"),
            (True, "true"),
            (-3, "-3"),
            (1.0, "1.0"),
            ("a\n", '"a\\n"'),
            (b"\x01\xff", "[1, 255]"),
            ([1, "a"], '[1, "a"]'),
            ([], "[]"),
            ((1, "a"), '(1, "a")'),
            ((1,), "(1,)"),
            ((), "()"),
            ({3, 1, 2}, "{1, 2, 3}"),
            (set(), "{}"),
            ({"b": 1, "a": None}, '{"b": 1, "a": None}'),
            (Color.Red, "Red"),
            (Unit(), "Unit"),
            (fmt.Some(1), "Some(1)"),
            (fmt.Raw("raw"), "raw"),
            (
                Status("a", 1, 2, [b"k"]),
                'Status { name: "a", size: 1, vote: Some(2), keys: [[107]] }',
            ),
            (Status("a", 1, None, []), 'Status { name: "a", size: 1, vote: None, keys: [] }'),
            (Error.Abort(), "Abort"),
            (Error.InvalidInput("foo"), 'InvalidInput("foo")'),
        ]
        for value, expect in cases:
            with self.subTest(expect):
                self.assertEqual(debug(value), expect)
        self.assertEqual(fmt.debug_option(None), "None")
        self.assertEqual(fmt.debug_option("a"), 'Some("a")')
        self.assertEqual(fmt.debug_bytes_hex(b"\x00\x0a\xff"), "[0, a, ff]")
        self.assertEqual(fmt.debug_bytes(b"\x00\x0a\xff"), "[0, 10, 255]")

    def test_debug_pretty(self) -> None:
        self.assertEqual(
            debug(Status("a", 1, 2, [b"k", b""]), pretty=True),
            "Status {\n"
            '    name: "a",\n'
            "    size: 1,\n"
            "    vote: Some(\n"
            "        2,\n"
            "    ),\n"
            "    keys: [\n"
            "        [\n"
            "            107,\n"
            "        ],\n"
            "        [],\n"
            "    ],\n"
            "}",
        )
        self.assertEqual(debug((1, {"a": Unit()}), pretty=True), '(\n    1,\n    {\n        "a": Unit,\n    },\n)')
        self.assertEqual(debug(Error.IO("x"), pretty=True), 'IO(\n    "x",\n)')
        self.assertEqual(debug(Unit(), pretty=True), "Unit")


class TestError(unittest.TestCase):
    def test_display(self) -> None:
        self.assertEqual(str(Error.Abort()), "operation aborted")
        self.assertEqual(str(Error.InvalidData("foo")), "invalid data: foo")
        self.assertEqual(str(Error.InvalidInput("foo")), "invalid input: foo")
        self.assertEqual(str(Error.IO("foo")), "io error: foo")
        self.assertEqual(str(Error.ReadOnly()), "read-only transaction")
        self.assertEqual(str(Error.Serialization()), "serialization failure, retry transaction")

    def test_eq(self) -> None:
        self.assertEqual(Error.InvalidData("foo"), Error.InvalidData("foo"))
        self.assertNotEqual(Error.InvalidData("foo"), Error.InvalidData("bar"))
        self.assertNotEqual(Error.InvalidData("foo"), Error.InvalidInput("foo"))
        self.assertEqual(Error.Abort(), Error.Abort())

    def test_is_deterministic(self) -> None:
        self.assertFalse(Error.Abort().is_deterministic())
        self.assertFalse(Error.InvalidData("").is_deterministic())
        self.assertTrue(Error.InvalidInput("").is_deterministic())
        self.assertFalse(Error.IO("").is_deterministic())
        self.assertTrue(Error.ReadOnly().is_deterministic())
        self.assertTrue(Error.Serialization().is_deterministic())

    def test_from_exception(self) -> None:
        try:
            open("/nonexistent/file")
        except OSError as error:
            self.assertEqual(
                Error.from_exception(error), Error.IO("No such file or directory (os error 2)")
            )
        try:
            b"\xc0".decode()
        except UnicodeDecodeError as error:
            self.assertEqual(
                Error.from_exception(error),
                Error.InvalidData("invalid utf-8 sequence of 1 bytes from index 0"),
            )


if __name__ == "__main__":
    unittest.main()
