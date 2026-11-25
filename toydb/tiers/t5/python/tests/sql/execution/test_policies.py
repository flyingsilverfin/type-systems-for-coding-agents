"""Tests for toydb.sql.execution.policies.

Row-level security policies are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these tests cover
the checks, the applicable policies and the evaluation of the policies module
directly, and where the executor applies them.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution.checks import build_condition
from toydb.sql.execution.export import export
from toydb.sql.execution.policies import (
    PlannedPolicy,
    applicable,
    check_rows,
    create_policy,
    drop_policy,
    shows,
)
from toydb.sql.parser import Parser, ast
from toydb.sql.parser.ast import ExportFormat
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.policy import Policy
from toydb.sql.types.user import User
from toydb.storage.memory import Memory

TABLE = Table(
    "t",
    0,
    [
        Column("id", DataType.Integer, False, None, True, False, None),
        Column("n", DataType.Integer, True, Value.Null(), False, False, None),
    ],
)


class TestPolicies(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT, s STRING)")
        session.execute("CREATE VIEW v AS SELECT * FROM t")
        session.close()
        self.txn = self.engine.begin()
        for name, superuser in (("alice", False), ("bob", False), ("staff", False), ("root", True)):
            self.txn.create_user(User(name, superuser, None))
        self.txn.set_membership("alice", "staff", True)
        self.table = self.txn.must_get_table("t")

    def tearDown(self) -> None:
        self.txn.rollback()

    def create(self, sql: str) -> None:
        """Creates a policy from a CREATE POLICY statement."""
        statement = Parser.parse(sql)
        assert isinstance(statement, ast.Statement.CreatePolicy)
        create_policy(
            self.txn,
            statement.name,
            statement.table,
            statement.user,
            statement.using,
            statement.check,
        )

    def error(self, f: object) -> str:
        """Runs a closure, returning the message of the error it raises."""
        with self.assertRaises(Error) as cm:
            f()  # type: ignore[operator]
        return str(cm.exception)

    def test_create(self) -> None:
        """A policy is stored with its user and the canonical text of its
        conditions."""
        self.create("CREATE POLICY b ON t TO Alice USING ((n > 1) AND s IS NOT NULL)")
        self.create("CREATE POLICY a ON t USING (TRUE) WITH CHECK (n != -1)")
        self.assertEqual(
            self.txn.list_policies("t"),
            [
                Policy("t", "a", None, "TRUE", "n != -1"),
                Policy("t", "b", "alice", "n > 1 AND s IS NOT NULL", None),
            ],
        )

    def test_create_errors(self) -> None:
        """The checks are made in order: the table, the name, the user, then
        the conditions; nothing is created on an error."""
        self.create("CREATE POLICY p ON t USING (TRUE)")
        cases = [
            ("CREATE POLICY q ON nosuch TO nosuch USING (x)", "table nosuch does not exist"),
            ("CREATE POLICY q ON v USING (TRUE)", "table v does not exist"),
            ('CREATE POLICY "" ON t TO nosuch USING (x)', "policy name can't be empty"),
            ("CREATE POLICY p ON t TO nosuch USING (x)", "policy p already exists on table t"),
            ("CREATE POLICY q ON t TO nosuch USING (x)", "user nosuch does not exist"),
            ("CREATE POLICY q ON t USING (x) WITH CHECK (y)", "unknown column x"),
            ("CREATE POLICY q ON t USING (TRUE) WITH CHECK (y)", "unknown column y"),
            ("CREATE POLICY q ON t USING (sum(n) > 0)", "unknown function sum with 1 arguments"),
            ("CREATE POLICY q ON t USING (other.id = 1)", "unknown table other"),
        ]
        for sql, message in cases:
            with self.subTest(sql=sql):
                self.assertEqual(self.error(lambda: self.create(sql)), f"invalid input: {message}")
        self.assertEqual([policy.name for policy in self.txn.list_policies("t")], ["p"])

        # Names are compared exactly, and conditions are planned but not
        # evaluated.
        self.create('CREATE POLICY "P" ON t USING (1 / 0 = 1)')
        self.assertEqual([policy.name for policy in self.txn.list_policies("t")], ["P", "p"])

    def test_read_only(self) -> None:
        """A read-only transaction fails after the other checks."""
        txn = self.engine.begin_read_only()
        with self.assertRaises(Error) as cm:
            create_policy(txn, "p", "t", None, Parser.parse_expr("nosuch"), None)
        self.assertEqual(str(cm.exception), "invalid input: unknown column nosuch")
        with self.assertRaises(Error.ReadOnly):
            create_policy(txn, "p", "t", None, Parser.parse_expr("TRUE"), None)
        self.assertFalse(drop_policy(txn, "p", "t", True))

    def test_drop(self) -> None:
        self.create("CREATE POLICY p ON t USING (TRUE)")
        self.assertEqual(
            self.error(lambda: drop_policy(self.txn, "p", "nosuch", True)),
            "invalid input: table nosuch does not exist",
        )
        self.assertEqual(
            self.error(lambda: drop_policy(self.txn, "q", "t", False)),
            "invalid input: policy q does not exist on table t",
        )
        self.assertFalse(drop_policy(self.txn, "q", "t", True))
        self.assertTrue(drop_policy(self.txn, "p", "t", False))
        self.assertEqual(self.txn.list_policies("t"), [])

    def test_applicable(self) -> None:
        """The policies of a user it acts for and those without a user apply,
        by name; superusers, owners and users that act for the owner, and
        everyone on a table without policies, read every row."""
        self.assertIsNone(applicable(self.txn, self.table, "alice"))
        self.create("CREATE POLICY d ON t TO alice USING (TRUE)")
        self.create("CREATE POLICY c ON t TO staff USING (TRUE)")
        self.create("CREATE POLICY b ON t USING (TRUE)")
        self.create("CREATE POLICY a ON t TO bob USING (TRUE)")

        def names(user: str) -> list[str] | None:
            policies = applicable(self.txn, self.table, user)
            return None if policies is None else [policy.name for policy in policies]

        self.assertEqual(names("alice"), ["b", "c", "d"])
        self.assertEqual(names("staff"), ["b", "c"])
        self.assertEqual(names("bob"), ["a", "b"])
        self.assertEqual(names("nosuch"), ["b"])
        self.assertIsNone(names("admin"))
        self.assertIsNone(names("root"))
        self.txn.set_owner("t", "staff")
        self.assertIsNone(names("staff"))
        self.assertIsNone(names("alice"))
        self.assertEqual(names("bob"), ["a", "b"])

    def test_conditions(self) -> None:
        """The check condition is WITH CHECK, or else USING."""
        self.create("CREATE POLICY a ON t USING (n > 0) WITH CHECK (n > 10)")
        self.create("CREATE POLICY b ON t USING (s = 'x')")
        policies = applicable(self.txn, self.table, "bob")
        assert policies is not None
        row = [Value.Integer(1), Value.Integer(5), Value.String("y")]
        self.assertTrue(shows(policies, row))
        with self.assertRaises(Error) as cm:
            check_rows(self.table, policies, [row])
        self.assertEqual(
            str(cm.exception), "invalid input: new row violates row-level security policy for table t"
        )
        check_rows(self.table, policies, [[Value.Integer(1), Value.Integer(5), Value.String("x")]])
        check_rows(self.table, policies, [[Value.Integer(1), Value.Integer(50), Value.Null()]])


class TestEvaluation(unittest.TestCase):
    @staticmethod
    def policy(name: str, condition: str) -> PlannedPolicy:
        """Plans a policy of the test table, with the same USING and check
        condition."""
        expression = build_condition(TABLE, condition)
        return PlannedPolicy(name, expression, expression)

    def test_shows(self) -> None:
        """A row is shown if a policy is TRUE for it, in order, until one is;
        FALSE and NULL hide it, other values and evaluation errors fail."""
        policies = [self.policy("a", "n > 0"), self.policy("b", "1 / n = 1")]
        integer, null = Value.Integer, Value.Null()
        self.assertTrue(shows(policies, [integer(1), integer(5)]))
        self.assertTrue(shows(policies, [integer(1), integer(1)]))
        self.assertFalse(shows(policies, [integer(1), integer(-5)]))
        self.assertFalse(shows(policies, [integer(1), null]))
        self.assertFalse(shows([], [integer(1), integer(5)]))
        with self.assertRaises(Error) as cm:
            shows(policies, [integer(1), integer(0)])
        self.assertEqual(str(cm.exception), "invalid input: can't divide by zero")
        with self.assertRaises(Error) as cm:
            shows([self.policy("c", "n")], [integer(1), integer(2)])
        self.assertEqual(str(cm.exception), "invalid input: policy c returned 2, expected boolean")

    def test_check_rows(self) -> None:
        """Each row must satisfy one check condition; with no policy, no row
        can be written."""
        policies = [self.policy("a", "n > 0"), self.policy("b", "n IS NULL")]
        integer = Value.Integer
        check_rows(TABLE, policies, [[integer(1), integer(5)], [integer(2), Value.Null()]])
        cases = [
            (policies, [[integer(1), integer(5)], [integer(2), integer(0)]]),
            ([], [[integer(1), integer(1)]]),
        ]
        for planned, rows in cases:
            with self.subTest(rows=rows), self.assertRaises(Error) as cm:
                check_rows(TABLE, planned, rows)
            self.assertEqual(
                str(cm.exception),
                "invalid input: new row violates row-level security policy for table t",
            )


class TestExecution(unittest.TestCase):
    """The executor and EXPORT apply the policies of the user."""

    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.admin = self.engine.session()
        for sql in (
            "CREATE TABLE t (id INT PRIMARY KEY, n INT INDEX, CHECK (n != 0))",
            "INSERT INTO t VALUES (1, 1), (2, 2), (3, 3)",
            "CREATE USER alice",
            "GRANT SELECT, INSERT, UPDATE ON t TO alice",
            "CREATE POLICY p ON t TO alice USING (n != 2) WITH CHECK (n < 10)",
        ):
            self.admin.execute(sql)
        self.alice = self.engine.session()
        self.alice.execute("SET ROLE alice")

    def tearDown(self) -> None:
        self.admin.close()
        self.alice.close()

    def ids(self, sql: str) -> list[int]:
        """Returns the first column of alice's result rows, as integers."""
        return [row[0].try_into_i64() for row in self.alice.execute(sql).try_into_rows()]

    def error(self, sql: str) -> str:
        """Returns the message of alice's statement's error."""
        with self.assertRaises(Error) as cm:
            self.alice.execute(sql)
        return str(cm.exception)

    def test_reads(self) -> None:
        """Scans, key and index lookups read only the shown rows, and the
        policies come before a scan's filter."""
        self.assertEqual(self.ids("SELECT id FROM t"), [1, 3])
        self.assertEqual(self.ids("SELECT id FROM t WHERE id = 2"), [])
        self.assertEqual(self.ids("SELECT id FROM t WHERE n = 2 OR n = 3"), [3])
        self.assertEqual(self.ids("SELECT id FROM t WHERE 1 / (n - 2) > 0"), [3])

    def test_writes(self) -> None:
        """Written rows are checked row by row: the CHECK constraints, then
        the policies, before the rows are written."""
        cases = [
            (
                "INSERT INTO t VALUES (4, 20), (5, 0)",
                "new row violates row-level security policy for table t",
            ),
            ("INSERT INTO t VALUES (4, 0), (5, 20)", "row 4 violates check constraint t_check1"),
            ("INSERT INTO t VALUES (1, 5)", "primary key 1 already exists"),
            ("UPDATE t SET n = n * 5", "new row violates row-level security policy for table t"),
        ]
        for sql, message in cases:
            with self.subTest(sql=sql):
                self.assertEqual(self.error(sql), f"invalid input: {message}")
        self.alice.execute("UPDATE t SET n = n + 1")
        rows = self.admin.execute("SELECT n FROM t").try_into_rows()
        self.assertEqual([row[0] for row in rows], [Value.Integer(n) for n in (2, 2, 4)])

    def test_export(self) -> None:
        """EXPORT exports the rows the user reads."""
        txn = self.engine.begin_read_only()
        self.assertEqual(export(txn, "t", ExportFormat.Csv, "alice").lines, ["id,n", "1,1", "3,3"])
        self.assertEqual(export(txn, "t", ExportFormat.Csv, "admin").rows, 3)
        self.assertEqual(
            export(txn, None, ExportFormat.Sql, "alice").lines[-2:],
            ["INSERT INTO t VALUES (1, 1);", "INSERT INTO t VALUES (3, 3);"],
        )


if __name__ == "__main__":
    unittest.main()
