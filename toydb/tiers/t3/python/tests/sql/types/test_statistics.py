"""Tests for toydb.sql.types.statistics."""

import math
import unittest

from toydb.encoding import bincode
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.statistics import ColumnStatistics, TableStatistics

TABLE = Table(
    name="item",
    primary_key=0,
    columns=[
        Column("id", DataType.Integer, False, None, True, False, None),
        Column("name", DataType.String, True, Value.Null(), False, True, None),
        Column("price", DataType.Float, True, Value.Null(), False, False, None),
        Column("ok", DataType.Boolean, True, Value.Null(), False, False, None),
    ],
)


def row(id: int, name: str | None, price: float | None, ok: bool | None) -> list[Value]:
    def value(v: str | float | bool | None) -> Value:
        return Value.Null() if v is None else Value.from_native(v)

    return [Value.Integer(id), value(name), value(price), value(ok)]


ROWS = [
    row(1, "apple", 1.5, True),
    row(2, "pear", 2.25, False),
    row(3, "fig", None, True),
    row(4, "apple", 0.5, None),
    row(5, "kiwi", 2.25, True),
]


class TestColumnStatistics(unittest.TestCase):
    def test_compute(self) -> None:
        stats = TableStatistics.compute(TABLE, ROWS)
        self.assertEqual(stats.rows, 5)
        self.assertEqual(list(stats.columns), ["id", "name", "price", "ok"])
        self.assertEqual(
            stats.columns["id"],
            ColumnStatistics(0, 5, Value.Integer(1), Value.Integer(5), (Value.Integer(1), 1), None),
        )
        self.assertEqual(
            stats.columns["name"],
            ColumnStatistics(
                0, 4, Value.String("apple"), Value.String("pear"), (Value.String("apple"), 2), 4.2
            ),
        )
        self.assertEqual(
            stats.columns["price"],
            ColumnStatistics(
                1, 3, Value.Float(0.5), Value.Float(2.25), (Value.Float(2.25), 2), None
            ),
        )
        self.assertEqual(
            stats.columns["ok"],
            ColumnStatistics(
                1, 2, Value.Boolean(False), Value.Boolean(True), (Value.Boolean(True), 3), None
            ),
        )

    def test_floats(self) -> None:
        """NaN is a single value that sorts last, and ties go to the smallest
        value."""
        values = [Value.Float(f) for f in (math.nan, math.inf, -math.inf, 0.0, math.nan, 0.0)]
        stats = ColumnStatistics.compute(DataType.Float, values)
        self.assertEqual(stats.distinct, 4)
        self.assertEqual(str(stats.min), "-inf")
        self.assertEqual(str(stats.max), "NaN")
        self.assertEqual(stats.top, (Value.Float(0.0), 2))

    def test_strings(self) -> None:
        """Lengths count characters, and strings compare by code points."""
        values = [Value.String(s) for s in ("b", "a", "B", "👋👋", "é")]
        stats = ColumnStatistics.compute(DataType.String, values)
        self.assertEqual(stats.min, Value.String("B"))
        self.assertEqual(stats.max, Value.String("👋👋"))
        self.assertEqual(stats.top, (Value.String("B"), 1))
        self.assertEqual(stats.avg_length, 6 / 5)

    def test_empty(self) -> None:
        """Without non-NULL values there is no min, max, top or avg_length."""
        for values in ([], [Value.Null(), Value.Null()]):
            with self.subTest(values=values):
                stats = ColumnStatistics.compute(DataType.String, values)
                self.assertEqual(
                    stats, ColumnStatistics(len(values), 0, None, None, None, None)
                )


class TestTableStatistics(unittest.TestCase):
    def test_encoding(self) -> None:
        stats = TableStatistics.compute(TABLE, ROWS)
        self.assertEqual(TableStatistics.decode(stats.encode()), stats)
        empty = TableStatistics.compute(TABLE, [])
        self.assertEqual(bincode.deserialize(TableStatistics, empty.encode()), empty)

    def test_display(self) -> None:
        stats = TableStatistics.compute(TABLE, ROWS[:2])
        self.assertEqual(
            str(stats),
            "rows=2 id(nulls=0 distinct=2 min=1 max=2 top=1×1) "
            "name(nulls=0 distinct=2 min='apple' max='pear' top='apple'×1 avg_length=4.5) "
            "ok(nulls=0 distinct=2 min=FALSE max=TRUE top=FALSE×1) "
            "price(nulls=0 distinct=2 min=1.5 max=2.25 top=1.5×1)",
        )
        self.assertEqual(
            str(TableStatistics.compute(TABLE, [])),
            "rows=0 id(nulls=0 distinct=0 min=None max=None top=None) "
            "name(nulls=0 distinct=0 min=None max=None top=None) "
            "ok(nulls=0 distinct=0 min=None max=None top=None) "
            "price(nulls=0 distinct=0 min=None max=None top=None)",
        )


if __name__ == "__main__":
    unittest.main()
