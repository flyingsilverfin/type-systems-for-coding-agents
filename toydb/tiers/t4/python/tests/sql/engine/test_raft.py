"""Tests for the column statistics and view commands of toydb.sql.engine.raft.

The Raft SQL engine is mostly covered by the end-to-end golden scripts (see
`tests/e2e/test_tests.py`).
"""

import unittest

from toydb import raft
from toydb.encoding import bincode, format, serde
from toydb.error import Error
from toydb.sql.engine.raft import VIEWS, Read, State, Write
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.statistics import ColumnStatistics, TableStatistics
from toydb.sql.types.view import View
from toydb.storage import mvcc
from toydb.storage.memory import Memory

TABLE = Table("t", 0, [Column("id", DataType.Integer, False, None, True, False, None)])

STATISTICS = TableStatistics(
    rows=1,
    columns={
        "id": ColumnStatistics(0, 1, Value.Integer(1), Value.Integer(1), (Value.Integer(1), 1), None)
    },
)

VIEW = View("v", ["id"], "SELECT id FROM t", ["t"])


class TestCommands(unittest.TestCase):
    def test_encoding(self) -> None:
        """The statistics commands are appended after the existing ones."""
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        write = Write.SetStatistics(txn=txn, table="t", statistics=STATISTICS)
        self.assertEqual(write.encode()[0], 8)
        self.assertEqual(Write.decode(write.encode()), write)
        read = Read.GetStatistics(txn=txn, table="t")
        self.assertEqual(read.encode()[0], 7)
        self.assertEqual(Read.decode(read.encode()), read)

    def test_view_encoding(self) -> None:
        """The view commands are appended after the statistics commands."""
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        cases: list[tuple[Write | Read, int]] = [
            (Write.CreateView(txn=txn, view=VIEW), 9),
            (Write.DropView(txn=txn, view="v", if_exists=True), 10),
            (Read.GetView(txn=txn, view="v"), 8),
            (Read.ListViews(txn=txn), 9),
        ]
        for command, index in cases:
            with self.subTest(command=command):
                self.assertEqual(command.encode()[0], index)
                self.assertEqual(type(command).decode(command.encode()), command)

    def test_format(self) -> None:
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        write = Write.SetStatistics(txn=txn, table="t", statistics=STATISTICS)
        self.assertEqual(
            format.SQLCommand().value(b"", write.encode()),
            "t2 STATISTICS t rows=1 id(nulls=0 distinct=1 min=1 max=1 top=1×1)",
        )
        write = Write.CreateView(txn=txn, view=VIEW)
        self.assertEqual(
            format.SQLCommand().value(b"", write.encode()),
            "t2 CREATE VIEW v (id) AS SELECT id FROM t",
        )
        write = Write.DropView(txn=txn, view="v", if_exists=False)
        self.assertEqual(format.SQLCommand().value(b"", write.encode()), "t2 DROP VIEW v")

    def test_apply(self) -> None:
        """The state machine stores and reads statistics in transactions."""
        state = State(Memory())
        index = 0

        def apply(write: Write) -> bytes:
            nonlocal index
            index += 1
            return state.apply(raft.Entry(index=index, term=1, command=write.encode()))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.CreateTable(txn=txn, schema=TABLE))
        apply(Write.SetStatistics(txn=txn, table="t", statistics=STATISTICS))
        read = state.read(Read.GetStatistics(txn=txn, table="t").encode())
        self.assertEqual(bincode.deserialize(serde.Option(TableStatistics), read), STATISTICS)
        apply(Write.Commit(txn))

        txn = mvcc.TransactionState.decode(state.read(Read.BeginReadOnly(None).encode()))
        read = state.read(Read.GetStatistics(txn=txn, table="t").encode())
        self.assertEqual(bincode.deserialize(serde.Option(TableStatistics), read), STATISTICS)

    def test_apply_views(self) -> None:
        """The state machine creates, reads and drops views in transactions,
        and checks the dependencies of a dropped table."""
        state = State(Memory())
        index = 0

        def apply(write: Write) -> bytes:
            nonlocal index
            index += 1
            return state.apply(raft.Entry(index=index, term=1, command=write.encode()))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.CreateTable(txn=txn, schema=TABLE))
        apply(Write.CreateView(txn=txn, view=VIEW))
        read = state.read(Read.GetView(txn=txn, view="v").encode())
        self.assertEqual(bincode.deserialize(serde.Option(View), read), VIEW)
        with self.assertRaises(Error) as cm:
            apply(Write.DropTable(txn=txn, table="t", if_exists=False))
        self.assertEqual(str(cm.exception), "invalid input: table t is used by view v")
        apply(Write.Commit(txn))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        self.assertEqual(bincode.deserialize(VIEWS, state.read(Read.ListViews(txn=txn).encode())), [VIEW])
        existed = apply(Write.DropView(txn=txn, view="v", if_exists=True))
        self.assertTrue(bincode.deserialize(serde.Bool, existed))
        existed = apply(Write.DropView(txn=txn, view="v", if_exists=True))
        self.assertFalse(bincode.deserialize(serde.Bool, existed))
        self.assertEqual(bincode.deserialize(VIEWS, state.read(Read.ListViews(txn=txn).encode())), [])


if __name__ == "__main__":
    unittest.main()
