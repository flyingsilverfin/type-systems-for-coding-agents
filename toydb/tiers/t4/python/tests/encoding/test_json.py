"""Tests for toydb.encoding.json."""

import math
import unittest

from toydb.encoding import json


class TestEncode(unittest.TestCase):
    def test_scalars(self) -> None:
        cases: list[tuple[json.JSON, str]] = [
            (None, "null"),
            (True, "true"),
            (False, "false"),
            (0, "0"),
            (-9223372036854775808, "-9223372036854775808"),
            (1.0, "1.0"),
            (-0.0, "-0.0"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1e100, "1e100"),
            (1.5e-7, "1.5e-7"),
            (1.2345678901234568e20, "1.2345678901234568e20"),
        ]
        for value, expect in cases:
            with self.subTest(value=value):
                self.assertEqual(json.encode(value), expect)

    def test_strings(self) -> None:
        """Only ", \\ and control characters are escaped."""
        cases = [
            ("", '""'),
            ('a"b\\c', '"a\\"b\\\\c"'),
            ("\b\f\n\r\t", '"\\b\\f\\n\\r\\t"'),
            ("\x01\x1b\x1f", '"\\u0001\\u001b\\u001f"'),
            ("/\x7f é ☃ 👋", '"/\x7f é ☃ 👋"'),
        ]
        for value, expect in cases:
            with self.subTest(value=value):
                self.assertEqual(json.encode(value), expect)

    def test_compound(self) -> None:
        """Arrays and objects are compact, with members in insertion order."""
        value: json.JSON = {"b": [1, None, {"x": "y"}], "a": {}, "c": []}
        self.assertEqual(json.encode(value), '{"b":[1,null,{"x":"y"}],"a":{},"c":[]}')

    def test_non_finite(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                json.encode(value)


class TestDecode(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(json.decode(' {"a": [1, 2.5, true, null], "b": "\\u00e9"} '),
                         {"a": [1, 2.5, True, None], "b": "é"})
        self.assertEqual(json.decode("-0"), 0)
        self.assertIsInstance(json.decode("2.0"), float)
        self.assertIsInstance(json.decode("2e0"), float)
        self.assertIsInstance(json.decode("2"), int)
        self.assertEqual(json.decode('"\\ud83d\\udc4b"'), "👋")  # a surrogate pair

    def test_member_order(self) -> None:
        """A repeated member keeps its first position and its last value."""
        decoded = json.decode('{"a":1,"b":2,"a":3}')
        self.assertEqual(list(decoded.items()), [("a", 3), ("b", 2)])

    def test_invalid(self) -> None:
        for text in ("", "nope", "NaN", "[Infinity]", '{"a":-Infinity}', "{} {}", "{", "'a'",
                     '"\\ud800"', '{"\\udc00x":1}', '[["\\ud83d"]]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                json.decode(text)


if __name__ == "__main__":
    unittest.main()
