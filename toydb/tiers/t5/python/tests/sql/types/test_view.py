"""Tests for toydb.sql.types.view."""

import unittest

from toydb.encoding import bincode
from toydb.sql.types.view import View

VIEW = View(
    name="cheap",
    columns=["id", "Name"],
    query="SELECT id, name AS \"Name\" FROM item WHERE price < 2",
    dependencies=["item"],
)


class TestView(unittest.TestCase):
    def test_encoding(self) -> None:
        """A view is encoded as a struct of its fields, in order."""
        encoded = View("v", ["a"], "SELECT 1 AS a", []).encode()
        self.assertEqual(encoded.hex(), "0176" + "010161" + "0d" + b"SELECT 1 AS a".hex() + "00")
        self.assertEqual(View.decode(encoded), View("v", ["a"], "SELECT 1 AS a", []))
        self.assertEqual(bincode.deserialize(View, bincode.serialize(VIEW)), VIEW)

    def test_display(self) -> None:
        """A view is displayed as the CREATE VIEW statement of its
        definition, with its column list and names that read back."""
        self.assertEqual(
            str(VIEW),
            'CREATE VIEW cheap (id, "Name") AS SELECT id, name AS "Name" FROM item WHERE price < 2',
        )
        self.assertEqual(
            str(View("my view", ["view"], "SELECT 1 AS \"view\"", [])),
            'CREATE VIEW "my view" ("view") AS SELECT 1 AS "view"',
        )


if __name__ == "__main__":
    unittest.main()
