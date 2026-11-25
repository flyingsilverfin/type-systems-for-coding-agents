"""Tests for the column statistics and view storage of toydb.sql.engine.local.

The local engine is mostly covered by the SQL golden scripts (see
`tests/sql/test_mod.py`).
"""

import queue
import unittest

from tests.storage.test_engine import Emit, Operation
from toydb.encoding import bincode, format, serde
from toydb.error import Error
from toydb.sql.engine import Key, Local
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.check import CHECKS, Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.policy import Policy
from toydb.sql.types.statistics import ColumnStatistics, TableStatistics
from toydb.sql.types.user import PRIVILEGES, Grant, Privilege, User
from toydb.sql.types.view import View
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


class TestKey(unittest.TestCase):
    def test_encoding(self) -> None:
        """The statistics and view keys are appended after the existing keys,
        which encode as before."""
        cases: list[tuple[Key, str]] = [
            (Key.Table("t"), "00740000"),
            (Key.Index("t", "c", Value.Null()), "0174000063000000"),
            (Key.Row("t", Value.Integer(1)), "027400000280000000000000" + "01"),
            (Key.Statistics("t"), "03740000"),
            (Key.View("v"), "04760000"),
            (Key.User("u"), "05750000"),
            (Key.Membership("u", "g"), "06750000670000"),
            (Key.Grant("t", "u"), "07740000750000"),
            (Key.Owner("t"), "08740000"),
            (Key.Checks("t"), "09740000"),
            (Key.Policy("t", "p"), "0a740000700000"),
            (Key.MaterializedView("m"), "0b6d0000"),
            (Key.MaterializedViewRow("m", 1), "0c6d00000000000000000001"),
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
        key = Key.View("v").encode()
        self.assertEqual(
            format.SQL().key_value(key, VIEW.encode()),
            "sql:View(v) → CREATE VIEW v (id) AS SELECT id FROM t",
        )
        key = Key.User("u").encode()
        self.assertEqual(
            format.SQL().key_value(key, User("u", True, "md5x").encode()),
            "sql:User(u) → u superuser=true password=md5x",
        )
        key = Key.Grant("t", "u").encode()
        value = bincode.serialize([Privilege.Select, Privilege.Delete], PRIVILEGES)
        self.assertEqual(format.SQL().key_value(key, value), "sql:Grant(t, u) → SELECT,DELETE")
        key = Key.Owner("t").encode()
        value = bincode.serialize("u", serde.String)
        self.assertEqual(format.SQL().key_value(key, value), "sql:Owner(t) → u")
        key = Key.Membership("u", "g").encode()
        self.assertEqual(format.SQL().key_value(key, b""), 'sql:Membership(u, g) → ""')
        key = Key.Checks("t").encode()
        value = bincode.serialize([Check("t_check1", "id > 0"), Check("c", "TRUE")], CHECKS)
        self.assertEqual(
            format.SQL().key_value(key, value),
            "sql:Checks(t) → CONSTRAINT t_check1 CHECK (id > 0), CONSTRAINT c CHECK (TRUE)",
        )
        key = Key.Policy("t", "p").encode()
        value = Policy("t", "p", "u", "id > 0", None).encode()
        self.assertEqual(
            format.SQL().key_value(key, value), "sql:Policy(t, p) → CREATE POLICY p ON t TO u USING (id > 0)"
        )
        key = Key.MaterializedView("m").encode()
        self.assertEqual(
            format.SQL().key_value(key, MATVIEW.encode()),
            "sql:MaterializedView(m) → CREATE MATERIALIZED VIEW m (id) AS SELECT id FROM t",
        )
        key = Key.MaterializedViewRow("m", 0).encode()
        value = bincode.serialize([Value.Integer(1), Value.String("a")], serde.Vec(Value))
        self.assertEqual(
            format.SQL().key_value(key, value), "sql:MaterializedViewRow(m, 0) → 1,'a'"
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


class TestViews(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        txn = self.engine.begin()
        txn.create_table(TABLE)
        txn.commit()

    def test_create_get_list(self) -> None:
        """Views are transactional, with snapshot isolation."""
        txn = self.engine.begin()
        self.assertIsNone(txn.get_view("v"))
        txn.create_view(VIEW)
        txn.create_view(View("a", ["one"], "SELECT 1 AS one", []))
        self.assertEqual(txn.get_view("v"), VIEW)
        self.assertEqual([view.name for view in txn.list_views()], ["a", "v"])
        other = self.engine.begin_read_only()
        self.assertIsNone(other.get_view("v"))
        txn.commit()
        self.assertEqual(other.list_views(), [])
        self.assertEqual(self.engine.begin_read_only().get_view("v"), VIEW)

    def test_namespace(self) -> None:
        """Views and tables share one namespace."""
        txn = self.engine.begin()
        txn.create_view(VIEW)
        cases: list[tuple[str, object]] = [
            ("table t already exists", lambda: txn.create_view(View("t", [], "SELECT 1", []))),
            ("view v already exists", lambda: txn.create_view(VIEW)),
            ("view v already exists", lambda: txn.create_table(Table("v", 0, TABLE.columns))),
        ]
        for message, f in cases:
            with self.subTest(message=message), self.assertRaises(Error) as cm:
                f()  # type: ignore[operator]
            self.assertEqual(str(cm.exception), f"invalid input: {message}")
        self.assertIsNone(txn.get_table("v"))

    def test_drop(self) -> None:
        txn = self.engine.begin()
        txn.create_view(VIEW)
        with self.assertRaises(Error) as cm:
            txn.drop_view("t", False)
        self.assertEqual(str(cm.exception), "invalid input: view t does not exist")
        self.assertFalse(txn.drop_view("nosuch", True))
        self.assertTrue(txn.drop_view("v", False))
        self.assertIsNone(txn.get_view("v"))
        self.assertFalse(txn.drop_view("v", True))

    def test_drop_table_dependents(self) -> None:
        """DROP TABLE fails, without changing anything, if a view depends on
        the table."""
        txn = self.engine.begin()
        txn.create_view(VIEW)
        with self.assertRaises(Error) as cm:
            txn.drop_table("t", False)
        self.assertEqual(str(cm.exception), "invalid input: table t is used by view v")
        self.assertEqual(txn.get_table("t"), TABLE)
        txn.drop_view("v", False)
        self.assertTrue(txn.drop_table("t", False))

    def test_conflict(self) -> None:
        """Concurrent writes of the same view conflict."""
        a, b = self.engine.begin(), self.engine.begin()
        a.create_view(VIEW)
        with self.assertRaises(Error.Serialization):
            b.create_view(VIEW)



class TestMatViews(unittest.TestCase):
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

    def test_create_refresh_drop(self) -> None:
        """Materialized views and their rows are transactional, with snapshot
        isolation; the rows are kept in the order they were stored."""
        rows = [[Value.Integer(2)], [Value.Integer(1)]]
        txn = self.engine.begin()
        self.assertIsNone(txn.get_matview("m"))
        txn.create_matview(MATVIEW, rows)
        self.assertEqual(txn.get_matview("m"), MATVIEW)
        self.assertEqual(txn.get_matview_rows("m"), rows)
        self.assertEqual(txn.list_matviews(), [MATVIEW])
        other = self.engine.begin_read_only()
        txn.commit()
        self.assertIsNone(other.get_matview("m"))
        self.assertEqual(other.get_matview_rows("m"), [])

        txn = self.engine.begin()
        txn.refresh_matview("m", rows[1:])
        self.assertEqual(txn.get_matview_rows("m"), rows[1:])
        self.assertEqual(self.engine.begin_read_only().get_matview_rows("m"), rows)
        txn.rollback()
        self.assertEqual(self.engine.begin_read_only().get_matview_rows("m"), rows)

        txn = self.engine.begin()
        self.assertTrue(txn.drop_matview("m"))
        self.assertFalse(txn.drop_matview("m"))
        self.assertIsNone(txn.get_matview("m"))
        self.assertEqual(txn.get_matview_rows("m"), [])
        self.assertEqual(txn.list_matviews(), [])

    def test_keys(self) -> None:
        """The definition and every row are written under keys of their own;
        dropping removes them and the owner."""
        txn = self.engine.begin()
        self.written_keys()
        txn.create_matview(MATVIEW, [[Value.Integer(1)]])
        txn.set_owner("m", "u")
        version = txn.state().version
        keys = [key for key in self.written_keys() if key.startswith("mvcc:Version")]
        self.assertEqual(
            keys,
            [
                f"mvcc:Version(sql:MaterializedView(m), {version})",
                f"mvcc:Version(sql:MaterializedViewRow(m, 0), {version})",
                f"mvcc:Version(sql:Owner(m), {version})",
            ],
        )
        txn.drop_matview("m")
        keys = [key for key in self.written_keys() if key.startswith("mvcc:Version")]
        self.assertEqual(
            keys,
            [
                f"mvcc:Version(sql:MaterializedView(m), {version})",
                f"mvcc:Version(sql:MaterializedViewRow(m, 0), {version})",
                f"mvcc:Version(sql:Owner(m), {version})",
            ],
        )

    def test_namespace(self) -> None:
        """Materialized views share the namespace of tables and views."""
        txn = self.engine.begin()
        txn.create_matview(MATVIEW, [])
        cases: list[tuple[str, object]] = [
            ("materialized view m already exists", lambda: txn.create_view(View("m", [], "SELECT 1", []))),
            ("materialized view m already exists", lambda: txn.create_table(Table("m", 0, TABLE.columns))),
        ]
        for message, f in cases:
            with self.subTest(message=message), self.assertRaises(Error) as cm:
                f()  # type: ignore[operator]
            self.assertEqual(str(cm.exception), f"invalid input: {message}")

    def test_drop_table_dependents(self) -> None:
        """DROP TABLE fails if a materialized view depends on the table."""
        txn = self.engine.begin()
        txn.create_matview(MATVIEW, [])
        with self.assertRaises(Error) as cm:
            txn.drop_table("t", False)
        self.assertEqual(str(cm.exception), "invalid input: table t is used by materialized view m")
        self.assertEqual(txn.get_table("t"), TABLE)

    def test_conflict(self) -> None:
        """Concurrent refreshes of the same materialized view conflict."""
        txn = self.engine.begin()
        txn.create_matview(MATVIEW, [[Value.Integer(1)]])
        txn.commit()
        a, b = self.engine.begin(), self.engine.begin()
        a.refresh_matview("m", [[Value.Integer(2)]])
        with self.assertRaises(Error.Serialization):
            b.refresh_matview("m", [[Value.Integer(3)]])


class TestUsers(unittest.TestCase):
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

    def test_users(self) -> None:
        """Users are transactional; admin always exists and isn't stored."""
        txn = self.engine.begin()
        self.assertEqual(txn.list_users(), [User.admin()])
        self.assertEqual(txn.get_user("admin"), User.admin())
        txn.create_user(User("b", False, None))
        txn.create_user(User("B", True, None))
        with self.assertRaises(Error) as cm:
            txn.create_user(User("admin", False, None))
        self.assertEqual(str(cm.exception), "invalid input: user admin already exists")
        self.assertEqual([user.name for user in txn.list_users()], ["B", "admin", "b"])
        self.assertIsNone(self.engine.begin_read_only().get_user("b"))
        txn.commit()
        self.assertEqual(self.engine.begin_read_only().get_user("b"), User("b", False, None))

    def test_owner(self) -> None:
        """Ownership by admin is not stored."""
        txn = self.engine.begin()
        self.written_keys()
        self.assertEqual(txn.get_owner("t"), "admin")
        txn.set_owner("t", "u")
        self.assertEqual(txn.get_owner("t"), "u")
        txn.set_owner("t", "admin")
        self.assertEqual(txn.get_owner("t"), "admin")
        version = txn.state().version
        self.assertEqual(
            self.written_keys(),
            [f"mvcc:TxnWrite({version}, sql:Owner(t))", f"mvcc:Version(sql:Owner(t), {version})"]
            * 2,
        )

    def test_drop_table(self) -> None:
        """DROP TABLE deletes the owner and grants of the table, but writes
        nothing for them if there are none."""
        txn = self.engine.begin()
        self.written_keys()
        txn.drop_table("t", False)
        written = self.written_keys()
        self.assertFalse(any("Owner" in key or "Grant" in key for key in written))
        txn.rollback()

        txn = self.engine.begin()
        txn.create_user(User("u", False, None))
        txn.set_owner("t", "u")
        txn.set_grant("t", "u", [Privilege.Select])
        txn.set_grant("x", "u", [Privilege.Select])
        txn.drop_table("t", False)
        self.assertEqual(txn.get_owner("t"), "admin")
        self.assertEqual(txn.list_grants(), [Grant("x", "u", [Privilege.Select])])

    def test_drop_user(self) -> None:
        txn = self.engine.begin()
        for name in ("a", "b", "c"):
            txn.create_user(User(name, False, None))
        txn.set_membership("a", "b", True)
        txn.set_membership("b", "c", True)
        txn.set_membership("c", "a", True)
        txn.set_grant("t", "b", [Privilege.Insert])
        txn.set_grant("t", "c", [Privilege.Insert])
        txn.drop_user("b")
        self.assertEqual([user.name for user in txn.list_users()], ["a", "admin", "c"])
        self.assertEqual(txn.list_memberships(), [("c", "a")])
        self.assertEqual(txn.list_grants(), [Grant("t", "c", [Privilege.Insert])])

    def test_conflict(self) -> None:
        """Concurrent writes of the same user conflict."""
        a, b = self.engine.begin(), self.engine.begin()
        a.create_user(User("u", False, None))
        with self.assertRaises(Error.Serialization):
            b.create_user(User("u", False, None))



class TestChecks(unittest.TestCase):
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
        """CHECK constraints are transactional; a table without constraints
        has none."""
        checks = [Check("t_check1", "id > 0"), Check("c", "id < 9")]
        txn = self.engine.begin()
        self.assertEqual(txn.get_checks("t"), [])
        txn.set_checks("t", checks)
        self.assertEqual(txn.get_checks("t"), checks)
        self.assertEqual(self.engine.begin_read_only().get_checks("t"), [])
        txn.commit()
        self.assertEqual(self.engine.begin_read_only().get_checks("t"), checks)

        txn = self.engine.begin()
        txn.set_checks("t", [])
        self.assertEqual(txn.get_checks("t"), [])

    def test_drop_table(self) -> None:
        """DROP TABLE deletes the table's constraints, but writes nothing for
        them if there are none."""
        txn = self.engine.begin()
        self.written_keys()
        txn.drop_table("t", False)
        self.assertFalse(any("Checks" in key for key in self.written_keys()))
        txn.rollback()

        txn = self.engine.begin()
        txn.set_checks("t", [Check("t_check1", "id > 0")])
        self.written_keys()
        txn.drop_table("t", False)
        version = txn.state().version
        self.assertIn(f"mvcc:Version(sql:Checks(t), {version})", self.written_keys())
        self.assertEqual(txn.get_checks("t"), [])

    def test_conflict(self) -> None:
        """Concurrent writes of the same table's constraints conflict."""
        a, b = self.engine.begin(), self.engine.begin()
        a.set_checks("t", [Check("a", "TRUE")])
        with self.assertRaises(Error.Serialization):
            b.set_checks("t", [Check("b", "TRUE")])


class TestPolicies(unittest.TestCase):
    def setUp(self) -> None:
        self.ops: queue.Queue[Operation] = queue.Queue()
        self.engine = Local(Emit(Memory(), self.ops))
        txn = self.engine.begin()
        txn.create_table(TABLE)
        txn.create_table(Table("tt", 0, TABLE.columns))
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

    def test_create_list_drop(self) -> None:
        """Policies are transactional and listed by name, per table."""
        b = Policy("t", "b", None, "TRUE", None)
        a = Policy("t", "a", "u", "id > 0", "id > 1")
        txn = self.engine.begin()
        self.assertEqual(txn.list_policies("t"), [])
        txn.create_policy(b)
        txn.create_policy(a)
        txn.create_policy(Policy("tt", "c", None, "FALSE", None))
        self.assertEqual(txn.list_policies("t"), [a, b])
        self.assertEqual(self.engine.begin_read_only().list_policies("t"), [])
        txn.commit()
        self.assertEqual(self.engine.begin_read_only().list_policies("t"), [a, b])

        txn = self.engine.begin()
        self.written_keys()
        self.assertFalse(txn.drop_policy("t", "nosuch"))
        self.assertEqual(self.written_keys(), [])
        self.assertTrue(txn.drop_policy("t", "a"))
        self.assertEqual(txn.list_policies("t"), [b])
        self.assertEqual([policy.name for policy in txn.list_policies("tt")], ["c"])

    def test_drop_table(self) -> None:
        """DROP TABLE deletes the table's policies, and only those."""
        txn = self.engine.begin()
        txn.create_policy(Policy("t", "p", None, "TRUE", None))
        txn.create_policy(Policy("tt", "p", None, "TRUE", None))
        self.written_keys()
        txn.drop_table("t", False)
        version = txn.state().version
        self.assertIn(f"mvcc:Version(sql:Policy(t, p), {version})", self.written_keys())
        self.assertEqual(txn.list_policies("t"), [])
        self.assertEqual(len(txn.list_policies("tt")), 1)

    def test_conflict(self) -> None:
        """Concurrent writes of the same policy conflict."""
        a, b = self.engine.begin(), self.engine.begin()
        a.create_policy(Policy("t", "p", None, "TRUE", None))
        with self.assertRaises(Error.Serialization):
            b.create_policy(Policy("t", "p", None, "FALSE", None))


if __name__ == "__main__":
    unittest.main()
