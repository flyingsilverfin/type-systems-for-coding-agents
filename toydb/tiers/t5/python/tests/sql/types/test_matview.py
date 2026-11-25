"""Tests for toydb.sql.types.matview."""

import unittest

from toydb.encoding import bincode
from toydb.sql.types.matview import MaterializedView

MATVIEW = MaterializedView(
    name="totals",
    columns=["kind", "Total"],
    query='SELECT kind, sum(price) AS "Total" FROM item GROUP BY kind',
    dependencies=["item"],
)


class TestMaterializedView(unittest.TestCase):
    def test_encoding(self) -> None:
        """A materialized view is encoded as a struct of its fields, in
        order, like a view."""
        matview = MaterializedView("m", ["a"], "SELECT 1 AS a", [])
        encoded = matview.encode()
        self.assertEqual(encoded.hex(), "016d" + "010161" + "0d" + b"SELECT 1 AS a".hex() + "00")
        self.assertEqual(MaterializedView.decode(encoded), matview)
        self.assertEqual(bincode.deserialize(MaterializedView, bincode.serialize(MATVIEW)), MATVIEW)

    def test_display(self) -> None:
        """A materialized view is displayed as the CREATE MATERIALIZED VIEW
        statement of its definition, with its column list and names that read
        back."""
        self.assertEqual(
            str(MATVIEW),
            'CREATE MATERIALIZED VIEW totals (kind, "Total") AS '
            'SELECT kind, sum(price) AS "Total" FROM item GROUP BY kind',
        )
        self.assertEqual(
            str(MaterializedView("my view", ["view"], 'SELECT 1 AS "view"', [])),
            'CREATE MATERIALIZED VIEW "my view" ("view") AS SELECT 1 AS "view"',
        )


if __name__ == "__main__":
    unittest.main()
