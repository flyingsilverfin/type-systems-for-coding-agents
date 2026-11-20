"""Tests for the column statistics commands of toydb.sql.engine.raft.

The Raft SQL engine is mostly covered by the end-to-end golden scripts (see
`tests/e2e/test_tests.py`).
"""

import unittest

from toydb import raft
from toydb.encoding import bincode, format, serde
from toydb.sql.engine.raft import Read, State, Write
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.statistics import ColumnStatistics, TableStatistics
from toydb.storage import mvcc
from toydb.storage.memory import Memory

TABLE = Table("t", 0, [Column("id", DataType.Integer, False, None, True, False, None)])

STATISTICS = TableStatistics(
    rows=1,
    columns={
        "id": ColumnStatistics(0, 1, Value.Integer(1), Value.Integer(1), (Value.Integer(1), 1), None)
    },
)


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

    def test_format(self) -> None:
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        write = Write.SetStatistics(txn=txn, table="t", statistics=STATISTICS)
        self.assertEqual(
            format.SQLCommand().value(b"", write.encode()),
            "t2 STATISTICS t rows=1 id(nulls=0 distinct=1 min=1 max=1 top=1×1)",
        )

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


if __name__ == "__main__":
    unittest.main()
