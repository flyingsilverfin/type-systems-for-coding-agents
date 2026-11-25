"""Tests for the column statistics and view commands of toydb.sql.engine.raft.

The Raft SQL engine is mostly covered by the end-to-end golden scripts (see
`tests/e2e/test_tests.py`).
"""

import unittest
from typing import TypeVar

from toydb import raft
from toydb.encoding import bincode, format, serde
from toydb.error import Error
from toydb.sql.engine.raft import (
    GRANTS,
    MATVIEWS,
    MEMBERSHIPS,
    ROWS,
    USERS,
    VIEWS,
    Read,
    State,
    Write,
)
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.check import CHECKS, Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.policy import POLICIES, Policy
from toydb.sql.types.statistics import ColumnStatistics, TableStatistics
from toydb.sql.types.user import Grant, Privilege, User
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

MATVIEW = MaterializedView("m", ["id"], "SELECT id FROM t", ["t"])

T = TypeVar("T")


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

    def test_user_encoding(self) -> None:
        """The user commands are appended after the view commands."""
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        cases: list[tuple[Write | Read, int]] = [
            (Write.CreateUser(txn=txn, user=User("u", True, "md5x")), 11),
            (Write.DropUser(txn=txn, user="u"), 12),
            (Write.SetMembership(txn=txn, member="u", user="g", is_member=True), 13),
            (Write.SetGrant(txn=txn, object="t", grantee="u", privileges=[Privilege.Update]), 14),
            (Write.SetOwner(txn=txn, object="t", owner="u"), 15),
            (Read.GetUser(txn=txn, user="u"), 11),
            (Read.ListUsers(txn=txn), 12),
            (Read.ListMemberships(txn=txn), 13),
            (Read.ListGrants(txn=txn), 14),
            (Read.GetOwner(txn=txn, object="t"), 15),
        ]
        for command, index in cases:
            with self.subTest(command=command):
                self.assertEqual(command.encode()[0], index)
                self.assertEqual(type(command).decode(command.encode()), command)

    def test_user_format(self) -> None:
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        cases: list[tuple[Write, str]] = [
            (Write.CreateUser(txn=txn, user=User("u", False, None)), "t2 CREATE USER u superuser=false password=None"),
            (Write.DropUser(txn=txn, user="u"), "t2 DROP USER u"),
            (
                Write.SetMembership(txn=txn, member="u", user="g", is_member=False),
                "t2 NO MEMBERSHIP u OF g",
            ),
            (
                Write.SetGrant(txn=txn, object="t", grantee="u", privileges=[Privilege.Select]),
                "t2 PRIVILEGES t u (SELECT)",
            ),
            (Write.SetOwner(txn=txn, object="t", owner="u"), "t2 OWNER t u"),
        ]
        for write, expect in cases:
            with self.subTest(write=write):
                self.assertEqual(format.SQLCommand().value(b"", write.encode()), expect)

    def test_checks_encoding(self) -> None:
        """The CHECK constraint commands are appended after the user commands."""
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        cases: list[tuple[Write | Read, int]] = [
            (Write.SetChecks(txn=txn, table="t", checks=[Check("c", "id > 0")]), 16),
            (Read.GetChecks(txn=txn, table="t"), 16),
        ]
        for command, index in cases:
            with self.subTest(command=command):
                self.assertEqual(command.encode()[0], index)
                self.assertEqual(type(command).decode(command.encode()), command)

    def test_checks_format(self) -> None:
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        checks = [Check("t_check1", "id > 0"), Check("c", "TRUE")]
        write = Write.SetChecks(txn=txn, table="t", checks=checks)
        self.assertEqual(
            format.SQLCommand().value(b"", write.encode()),
            "t2 CHECKS t (CONSTRAINT t_check1 CHECK (id > 0), CONSTRAINT c CHECK (TRUE))",
        )

    def test_apply_checks(self) -> None:
        """The state machine stores and reads CHECK constraints in
        transactions, and drops them with their table."""
        state = State(Memory())
        index = 0

        def apply(write: Write) -> bytes:
            nonlocal index
            index += 1
            return state.apply(raft.Entry(index=index, term=1, command=write.encode()))

        def get_checks(txn: mvcc.TransactionState) -> list[Check]:
            return bincode.deserialize(CHECKS, state.read(Read.GetChecks(txn=txn, table="t").encode()))

        checks = [Check("t_check1", "id > 0")]
        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.CreateTable(txn=txn, schema=TABLE))
        apply(Write.SetChecks(txn=txn, table="t", checks=checks))
        self.assertEqual(get_checks(txn), checks)
        apply(Write.Commit(txn))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        self.assertEqual(get_checks(txn), checks)
        apply(Write.DropTable(txn=txn, table="t", if_exists=False))
        self.assertEqual(get_checks(txn), [])

    def test_policies_encoding(self) -> None:
        """The policy commands are appended after the CHECK constraint
        commands."""
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        policy = Policy("t", "p", "u", "id > 0", "id > 1")
        cases: list[tuple[Write | Read, int]] = [
            (Write.CreatePolicy(txn=txn, policy=policy), 17),
            (Write.DropPolicy(txn=txn, table="t", name="p"), 18),
            (Read.ListPolicies(txn=txn, table="t"), 17),
        ]
        for command, index in cases:
            with self.subTest(command=command):
                self.assertEqual(command.encode()[0], index)
                self.assertEqual(type(command).decode(command.encode()), command)

    def test_policies_format(self) -> None:
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        cases: list[tuple[Write, str]] = [
            (
                Write.CreatePolicy(txn=txn, policy=Policy("t", "p", None, "TRUE", "id > 1")),
                "t2 CREATE POLICY p ON t USING (TRUE) WITH CHECK (id > 1)",
            ),
            (Write.DropPolicy(txn=txn, table="t", name="p"), "t2 DROP POLICY p ON t"),
        ]
        for write, expect in cases:
            with self.subTest(write=write):
                self.assertEqual(format.SQLCommand().value(b"", write.encode()), expect)

    def test_apply_policies(self) -> None:
        """The state machine creates, lists and drops policies in
        transactions."""
        state = State(Memory())
        index = 0

        def apply(write: Write) -> bytes:
            nonlocal index
            index += 1
            return state.apply(raft.Entry(index=index, term=1, command=write.encode()))

        def list_policies(txn: mvcc.TransactionState) -> list[Policy]:
            read = Read.ListPolicies(txn=txn, table="t")
            return bincode.deserialize(POLICIES, state.read(read.encode()))

        policy = Policy("t", "p", None, "id > 0", None)
        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.CreateTable(txn=txn, schema=TABLE))
        apply(Write.CreatePolicy(txn=txn, policy=policy))
        self.assertEqual(list_policies(txn), [policy])
        apply(Write.Commit(txn))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        drop = Write.DropPolicy(txn=txn, table="t", name="p")
        self.assertTrue(bincode.deserialize(serde.Bool, apply(drop)))
        self.assertFalse(bincode.deserialize(serde.Bool, apply(drop)))
        self.assertEqual(list_policies(txn), [])

    def test_matviews_encoding(self) -> None:
        """The materialized view commands are appended after the policy
        commands."""
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        rows = [[Value.Integer(1)], [Value.Integer(2)]]
        cases: list[tuple[Write | Read, int]] = [
            (Write.CreateMatView(txn=txn, matview=MATVIEW, rows=rows), 19),
            (Write.RefreshMatView(txn=txn, name="m", rows=[]), 20),
            (Write.DropMatView(txn=txn, name="m"), 21),
            (Read.GetMatView(txn=txn, name="m"), 18),
            (Read.GetMatViewRows(txn=txn, name="m"), 19),
            (Read.ListMatViews(txn=txn), 20),
        ]
        for command, index in cases:
            with self.subTest(command=command):
                self.assertEqual(command.encode()[0], index)
                self.assertEqual(type(command).decode(command.encode()), command)

    def test_matviews_format(self) -> None:
        txn = mvcc.TransactionState(version=2, read_only=False, active=set())
        rows = [[Value.Integer(1)], [Value.Integer(2)]]
        cases: list[tuple[Write, str]] = [
            (
                Write.CreateMatView(txn=txn, matview=MATVIEW, rows=rows),
                "t2 CREATE MATERIALIZED VIEW m (id) AS SELECT id FROM t ROWS (1) (2)",
            ),
            (
                Write.RefreshMatView(txn=txn, name="m", rows=rows[:1]),
                "t2 REFRESH MATERIALIZED VIEW m ROWS (1)",
            ),
            (Write.DropMatView(txn=txn, name="m"), "t2 DROP MATERIALIZED VIEW m"),
        ]
        for write, expect in cases:
            with self.subTest(write=write):
                self.assertEqual(format.SQLCommand().value(b"", write.encode()), expect)

    def test_apply_matviews(self) -> None:
        """The state machine creates, refreshes, reads and drops materialized
        views and their rows in transactions."""
        state = State(Memory())
        index = 0

        def apply(write: Write) -> bytes:
            nonlocal index
            index += 1
            return state.apply(raft.Entry(index=index, term=1, command=write.encode()))

        def read(read: Read, serde: serde.Serde[T]) -> T:
            return bincode.deserialize(serde, state.read(read.encode()))

        rows = [[Value.Integer(2)], [Value.Integer(1)]]
        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.CreateMatView(txn=txn, matview=MATVIEW, rows=rows))
        apply(Write.Commit(txn))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        self.assertEqual(read(Read.GetMatView(txn=txn, name="m"), serde.Option(MaterializedView)), MATVIEW)
        self.assertEqual(read(Read.ListMatViews(txn=txn), MATVIEWS), [MATVIEW])
        self.assertEqual(read(Read.GetMatViewRows(txn=txn, name="m"), ROWS), rows)
        apply(Write.RefreshMatView(txn=txn, name="m", rows=rows[1:]))
        self.assertEqual(read(Read.GetMatViewRows(txn=txn, name="m"), ROWS), rows[1:])
        drop = Write.DropMatView(txn=txn, name="m")
        self.assertTrue(bincode.deserialize(serde.Bool, apply(drop)))
        self.assertFalse(bincode.deserialize(serde.Bool, apply(drop)))
        self.assertEqual(read(Read.GetMatViewRows(txn=txn, name="m"), ROWS), [])
        self.assertIsNone(read(Read.GetMatView(txn=txn, name="m"), serde.Option(MaterializedView)))

    def test_apply_users(self) -> None:
        """The state machine stores and reads users, memberships, grants and
        owners in transactions."""
        state = State(Memory())
        index = 0

        def apply(write: Write) -> bytes:
            nonlocal index
            index += 1
            return state.apply(raft.Entry(index=index, term=1, command=write.encode()))

        def read(read: Read, serde: serde.Serde[T]) -> T:
            return bincode.deserialize(serde, state.read(read.encode()))

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.CreateTable(txn=txn, schema=TABLE))
        apply(Write.CreateUser(txn=txn, user=User("u", False, None)))
        apply(Write.CreateUser(txn=txn, user=User("g", False, None)))
        apply(Write.SetMembership(txn=txn, member="u", user="g", is_member=True))
        apply(Write.SetGrant(txn=txn, object="t", grantee="g", privileges=[Privilege.Select]))
        apply(Write.SetOwner(txn=txn, object="t", owner="u"))
        with self.assertRaises(Error) as cm:
            apply(Write.CreateUser(txn=txn, user=User("u", False, None)))
        self.assertEqual(str(cm.exception), "invalid input: user u already exists")
        apply(Write.Commit(txn))

        txn = mvcc.TransactionState.decode(state.read(Read.BeginReadOnly(None).encode()))
        user = read(Read.GetUser(txn=txn, user="u"), serde.Option(User))
        self.assertEqual(user, User("u", False, None))
        users = read(Read.ListUsers(txn=txn), USERS)
        self.assertEqual([user.name for user in users], ["admin", "g", "u"])
        self.assertEqual(read(Read.ListMemberships(txn=txn), MEMBERSHIPS), [("u", "g")])
        grants = read(Read.ListGrants(txn=txn), GRANTS)
        self.assertEqual(grants, [Grant("t", "g", [Privilege.Select])])
        self.assertEqual(read(Read.GetOwner(txn=txn, object="t"), serde.String), "u")

        txn = mvcc.TransactionState.decode(apply(Write.Begin()))
        apply(Write.DropUser(txn=txn, user="g"))
        self.assertEqual(read(Read.ListMemberships(txn=txn), MEMBERSHIPS), [])
        self.assertEqual(read(Read.ListGrants(txn=txn), GRANTS), [])

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
