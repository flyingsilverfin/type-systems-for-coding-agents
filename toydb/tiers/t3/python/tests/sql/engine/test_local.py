"""Tests for the column statistics storage of toydb.sql.engine.local.

The local engine is mostly covered by the SQL golden scripts (see
`tests/sql/test_mod.py`).
"""

import queue
import unittest

from tests.storage.test_engine import Emit, Operation
from toydb.encoding import format
from toydb.error import Error
from toydb.sql.engine import Key, Local
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.statistics import ColumnStatistics, TableStatistics
from toydb.storage.memory import Memory

TABLE = Table("t", 0, [Column("id", DataType.Integer, False, None, True, False, None)])

STATISTICS = TableStatistics(
    rows=1,
    columns={
        "id": ColumnStatistics(0, 1, Value.Integer(1), Value.Integer(1), (Value.Integer(1), 1), None)
    },
)


class TestKey(unittest.TestCase):
    def test_encoding(self) -> None:
        """The statistics key is appended after the existing keys, which
        encode as before."""
        cases: list[tuple[Key, str]] = [
            (Key.Table("t"), "00740000"),
            (Key.Index("t", "c", Value.Null()), "0174000063000000"),
            (Key.Row("t", Value.Integer(1)), "027400000280000000000000" + "01"),
            (Key.Statistics("t"), "03740000"),
        ]
        for key, expect in cases:
            with self.subTest(key=key):
                self.assertEqual(key.encode().hex(), expect)
                self.assertEqual(Key.decode(key.encode()), key)

    def test_format(self) -> None:
        key = Key.Statistics("t").encode()
        self.assertEqual(
            format.SQL().key_value(key, STATISTICS.encode()),
            "sql:Statistics(t) → rows=1 id(nulls=0 distinct=1 min=1 max=1 top=1×1)",
        )


class TestStatistics(unittest.TestCase):
    def setUp(self) -> None:
        self.ops: queue.Queue[Operation] = queue.Queue()
        self.engine = Local(Emit(Memory(), self.ops))
        txn = self.engine.begin()
        txn.create_table(TABLE)
        txn.commit()

    def written_keys(self) -> list[str]:
        """Drains the emitted storage operations, returning the formatted
        keys that were set or deleted."""
        keys: list[str] = []
        while not self.ops.empty():
            match self.ops.get_nowait():
                case Operation.Set(key, _) | Operation.Delete(key):
                    keys.append(format.MVCC(format.SQL()).key(key))
        return keys

    def test_set_get(self) -> None:
        """Statistics are transactional, with snapshot isolation."""
        txn = self.engine.begin()
        self.assertIsNone(txn.get_statistics("t"))
        txn.set_statistics("t", STATISTICS)
        self.assertEqual(txn.get_statistics("t"), STATISTICS)
        other = self.engine.begin_read_only()
        self.assertIsNone(other.get_statistics("t"))
        txn.commit()
        self.assertIsNone(other.get_statistics("t"))
        self.assertEqual(self.engine.begin_read_only().get_statistics("t"), STATISTICS)

    def test_unknown_table(self) -> None:
        txn = self.engine.begin()
        with self.assertRaises(Error) as cm:
            txn.set_statistics("nosuch", STATISTICS)
        self.assertEqual(str(cm.exception), "invalid input: table nosuch does not exist")

    def test_drop_table(self) -> None:
        """DROP TABLE deletes the statistics, but writes nothing for them if
        the table wasn't analyzed."""
        txn = self.engine.begin()
        self.written_keys()
        txn.drop_table("t", False)
        self.assertFalse(any("Statistics" in key for key in self.written_keys()))
        txn.rollback()

        txn = self.engine.begin()
        txn.set_statistics("t", STATISTICS)
        txn.commit()
        txn = self.engine.begin()
        self.written_keys()
        txn.drop_table("t", False)
        self.assertIn(f"mvcc:Version(sql:Statistics(t), {txn.state().version})", self.written_keys())
        self.assertIsNone(txn.get_statistics("t"))
        txn.create_table(TABLE)
        self.assertIsNone(txn.get_statistics("t"))
        txn.commit()


if __name__ == "__main__":
    unittest.main()
