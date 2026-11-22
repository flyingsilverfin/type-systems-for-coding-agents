"""Tests for toydb.sql.engine.changes, and the change log of the local and
Raft SQL engines.

SHOW CHANGES is mostly covered by the SQL and end-to-end golden scripts (see
`tests/sql/testscripts/ext4/chg_*` and `tests/e2e/scripts/ext4/chg_*`).
"""

import unittest

from toydb import raft
from toydb.encoding import bincode
from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.engine.changes import Change, ChangeKind, ChangeLog, format_row, format_table
from toydb.sql.engine.raft import CHANGES, Read, State, Write
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.statistics import TableStatistics
from toydb.sql.types.view import View
from toydb.storage import mvcc
from toydb.storage.btree import BTreeMap
from toydb.storage.memory import Memory

TABLE = Table(
    "t",
    0,
    [
        Column("id", DataType.Integer, False, None, True, False, None),
        Column("v", DataType.String, True, Value.Null(), False, False, None),
    ],
)

VIEW = View("v", ["id"], "SELECT id FROM t", ["t"])


def insert(version: int, id: int, v: str) -> Change:
    """An insert of a row of TABLE."""
    return Change(version, "t", ChangeKind.Insert, Value.Integer(id), f'{{"id":{id},"v":"{v}"}}')


class TestChange(unittest.TestCase):
    def test_row(self) -> None:
        cases: list[tuple[Change, list[Value]]] = [
            (
                insert(2, 1, "a"),
                [
                    Value.Integer(2),
                    Value.String("t"),
                    Value.String("insert"),
                    Value.Integer(1),
                    Value.String('{"id":1,"v":"a"}'),
                ],
            ),
            (
                Change(3, "t", ChangeKind.Delete, Value.String("k")),
                [
                    Value.Integer(3),
                    Value.String("t"),
                    Value.String("delete"),
                    Value.String("k"),
                    Value.Null(),
                ],
            ),
            (
                Change(4, "v", ChangeKind.DropView),
                [
                    Value.Integer(4),
                    Value.String("v"),
                    Value.String("drop_view"),
                    Value.Null(),
                    Value.Null(),
                ],
            ),
        ]
        for change, expect in cases:
            with self.subTest(change=change):
                self.assertEqual(change.row(), expect)

    def test_kind(self) -> None:
        """Each kind is shown by its op name."""
        ops = [str(kind) for kind in ChangeKind]
        self.assertEqual(
            ops,
            [
                "insert",
                "update",
                "delete",
                "create_table",
                "drop_table",
                "analyze",
                "create_view",
                "drop_view",
            ],
        )

    def test_encoding(self) -> None:
        for change in [
            insert(2, 1, "a"),
            Change(3, "t", ChangeKind.Delete, Value.Float(1.5)),
            Change(4, "t", ChangeKind.CreateTable, None, format_table(TABLE)),
            Change(5, "v", ChangeKind.DropView),
        ]:
            with self.subTest(change=change):
                self.assertEqual(Change.decode(change.encode()), change)

    def test_format_row(self) -> None:
        row = [Value.Integer(1), Value.String('it\'s "x"')]
        self.assertEqual(format_row(TABLE, row), '{"id":1,"v":"it\'s \\"x\\""}')
        row = [Value.Integer(2), Value.Null()]
        self.assertEqual(format_row(TABLE, row), '{"id":2,"v":null}')

    def test_format_table(self) -> None:
        self.assertEqual(
            format_table(TABLE),
            "CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING DEFAULT NULL)",
        )
        table = Table(
            "One (",
            0,
            [Column("a )", DataType.String, False, Value.String("x"), True, False, None)],
        )
        self.assertEqual(
            format_table(table), "CREATE TABLE \"One (\" (\"a )\" STRING PRIMARY KEY DEFAULT 'x')"
        )


class TestChangeLog(unittest.TestCase):
    def test_commit_rollback(self) -> None:
        """Pending changes are appended at commit, in commit order, and
        discarded at rollback."""
        log = ChangeLog()
        log.record(insert(1, 1, "a"))
        log.record(insert(2, 2, "b"))
        log.record(insert(3, 3, "c"))
        log.record(insert(1, 4, "d"))
        self.assertEqual(log.since(0), [])

        log.commit(2)
        log.rollback(3)
        log.commit(1)
        self.assertEqual(log.since(0), [insert(2, 2, "b"), insert(1, 1, "a"), insert(1, 4, "d")])
        self.assertEqual(log.pending, {})

        # A transaction without changes appends nothing.
        log.commit(4)
        log.rollback(5)
        self.assertEqual(len(log.since(0)), 3)

    def test_since(self) -> None:
        log = ChangeLog()
        for version in (3, 1, 2):
            log.record(insert(version, version, "x"))
            log.commit(version)
        self.assertEqual([c.version for c in log.since(0)], [3, 1, 2])
        self.assertEqual([c.version for c in log.since(1)], [3, 2])
        self.assertEqual([c.version for c in log.since(2)], [3])
        self.assertEqual(log.since(3), [])


class TestLocal(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())

    def test_changes(self) -> None:
        """Every kind of change is recorded, in the order it was made."""
        txn = self.engine.begin()
        txn.create_table(TABLE)
        txn.insert("t", [[Value.Integer(2), Value.String("b")], [Value.Integer(1), Value.Null()]])
        txn.commit()

        txn = self.engine.begin()
        rows: BTreeMap[Value, list[Value]] = BTreeMap()
        rows.insert(Value.Integer(1), [Value.Integer(1), Value.String("a")])
        rows.insert(Value.Integer(2), [Value.Integer(3), Value.String("b")])
        txn.update("t", rows)
        txn.delete("t", [Value.Integer(3)])
        txn.set_statistics("t", TableStatistics(rows=1, columns={}))
        txn.create_view(VIEW)
        txn.drop_view("v", False)
        txn.drop_table("t", False)
        txn.commit()

        self.assertEqual(
            [change.row() for change in self.engine.changes(0)],
            [
                Change(1, "t", ChangeKind.CreateTable, None, format_table(TABLE)).row(),
                insert(1, 2, "b").row(),
                Change(1, "t", ChangeKind.Insert, Value.Integer(1), '{"id":1,"v":null}').row(),
                Change(2, "t", ChangeKind.Update, Value.Integer(1), '{"id":1,"v":"a"}').row(),
                Change(2, "t", ChangeKind.Delete, Value.Integer(2)).row(),
                Change(2, "t", ChangeKind.Insert, Value.Integer(3), '{"id":3,"v":"b"}').row(),
                Change(2, "t", ChangeKind.Delete, Value.Integer(3)).row(),
                Change(2, "t", ChangeKind.Analyze).row(),
                Change(2, "v", ChangeKind.CreateView, None, str(VIEW)).row(),
                Change(2, "v", ChangeKind.DropView).row(),
                Change(2, "t", ChangeKind.DropTable).row(),
            ],
        )
        self.assertEqual(len(self.engine.changes(1)), 8)

    def test_committed_only(self) -> None:
        """Changes are visible once their transaction commits, and never if it
        rolls back."""
        txn = self.engine.begin()
        txn.create_table(TABLE)
        self.assertEqual(self.engine.changes(0), [])
        txn.commit()
        self.assertEqual(len(self.engine.changes(0)), 1)

        txn = self.engine.begin()
        txn.insert("t", [[Value.Integer(1), Value.Null()]])
        txn.rollback()
        self.assertEqual(len(self.engine.changes(0)), 1)

    def test_read_only(self) -> None:
        """A read-only transaction has the version of the next read-write
        transaction, but its commit or rollback doesn't affect that
        transaction's changes."""
        read_only = [self.engine.begin_read_only(), self.engine.begin_read_only()]
        txn = self.engine.begin()
        txn.create_table(TABLE)
        self.assertEqual(read_only[0].state().version, txn.state().version)
        read_only[0].commit()
        self.assertEqual(self.engine.changes(0), [])
        read_only[1].rollback()
        txn.commit()
        self.assertEqual(len(self.engine.changes(0)), 1)

    def test_failed_write(self) -> None:
        """A failed write records nothing, but the rows written before it
        are kept."""
        txn = self.engine.begin()
        txn.create_table(TABLE)
        txn.insert("t", [[Value.Integer(1), Value.Null()]])
        txn.commit()

        txn = self.engine.begin()
        with self.assertRaises(Error):
            txn.insert("t", [[Value.Integer(2), Value.Null()], [Value.Integer(1), Value.Null()]])
        with self.assertRaises(Error):
            txn.create_table(TABLE)
        self.assertFalse(txn.drop_view("v", True))
        txn.commit()
        changes = self.engine.changes(1)
        self.assertEqual([change.key for change in changes], [Value.Integer(2)])

    def test_conflict(self) -> None:
        """A write that conflicts with another transaction records nothing."""
        txn = self.engine.begin()
        txn.create_table(TABLE)
        txn.commit()
        a, b = self.engine.begin(), self.engine.begin()
        a.insert("t", [[Value.Integer(1), Value.Null()]])
        with self.assertRaises(Error.Serialization):
            b.insert("t", [[Value.Integer(1), Value.String("b")]])
        b.commit()
        a.commit()
        self.assertEqual([change.version for change in self.engine.changes(1)], [a.state().version])

    def test_not_stored(self) -> None:
        """The change log is kept in memory: an engine on the same storage
        starts with an empty log."""
        storage = Memory()
        engine = Local(storage)
        txn = engine.begin()
        txn.create_table(TABLE)
        txn.commit()
        self.assertEqual(len(engine.changes(0)), 1)
        self.assertEqual(Local(storage).changes(0), [])


class TestRaft(unittest.TestCase):
    def test_encoding(self) -> None:
        """The change log read is appended after the existing read commands."""
        read = Read.Changes(since=7)
        self.assertEqual(read.encode()[0], 10)
        self.assertEqual(Read.decode(read.encode()), read)

    def test_apply(self) -> None:
        """The state machine records the changes as it applies the Raft log,
        and returns the committed ones."""
        state = State(Memory())
        index = 0

        def apply(write: Write) -> bytes:
            nonlocal index
            index += 1
            return state.apply(raft.Entry(index=index, term=1, command=write.encode()))

        def changes(since: int) -> list[Change]:
            return bincode.deserialize(CHANGES, state.read(Read.Changes(since).encode()))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.CreateTable(txn=txn, schema=TABLE))
        apply(Write.Insert(txn=txn, table="t", rows=[[Value.Integer(1), Value.String("a")]]))
        self.assertEqual(changes(0), [])
        apply(Write.Commit(txn))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.Delete(txn=txn, table="t", ids=[Value.Integer(1)]))
        apply(Write.Rollback(txn))

        self.assertEqual(
            changes(0),
            [
                Change(1, "t", ChangeKind.CreateTable, None, format_table(TABLE)),
                insert(1, 1, "a"),
            ],
        )
        self.assertEqual(changes(1), [])


if __name__ == "__main__":
    unittest.main()
