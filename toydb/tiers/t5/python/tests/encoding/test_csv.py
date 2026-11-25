"""Tests for toydb.encoding.csv."""

import unittest

from toydb.encoding import csv


class TestCSV(unittest.TestCase):
    def test_format_field(self) -> None:
        """Fields are quoted if empty or if they contain , " or line breaks.
        None is an empty unquoted field."""
        cases: list[tuple[str | None, str]] = [
            (None, ""),
            ("", '""'),
            ("plain", "plain"),
            ("x y", "x y"),
            ("it's", "it's"),
            ("a,b", '"a,b"'),
            ('say "hi"', '"say ""hi"""'),
            ("a\nb", '"a\nb"'),
            ("a\rb", '"a\rb"'),
        ]
        for field, expect in cases:
            with self.subTest(field=field):
                self.assertEqual(csv.format_field(field), expect)

    def test_format_line(self) -> None:
        self.assertEqual(csv.format_line(["1", None, "", "a,b"]), '1,,"","a,b"')
        self.assertEqual(csv.format_line([None]), "")

    def test_parse_line(self) -> None:
        cases: list[tuple[str, list[str | None]]] = [
            ("", [None]),
            ("a", ["a"]),
            ("a,", ["a", None]),
            (",", [None, None]),
            ('""', [""]),
            ('1,"a,b",,""', ["1", "a,b", None, ""]),
            ('"say ""hi"""', ['say "hi"']),
            ('"x\ny",z', ["x\ny", "z"]),
            (" a , b ", [" a ", " b "]),
        ]
        for line, expect in cases:
            with self.subTest(line=line):
                self.assertEqual(csv.parse_line(line), expect)

    def test_parse_line_invalid(self) -> None:
        for line in ('"a', 'a,"b', 'b"c', '"b"c', '"a""', 'x,"y" '):
            with self.subTest(line=line), self.assertRaises(csv.CSVError):
                csv.parse_line(line)

    def test_roundtrip(self) -> None:
        fields: list[str | None] = [None, "", "a", '"', ",", "\n", 'a "b", c', "é"]
        self.assertEqual(csv.parse_line(csv.format_line(fields)), fields)


if __name__ == "__main__":
    unittest.main()
