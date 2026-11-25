"""Tests for toydb.sql.execution.checks.

CHECK constraints are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these tests cover
the naming, the checks and the evaluation of the checks module directly.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution.checks import build_checks, build_condition, check_rows, create_checks
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.check import Check
from toydb.storage.memory import Memory


def column(name: str, datatype: DataType, primary_key: bool = False) -> Column:
    """Returns a column without a default, nullable unless a primary key."""
    return Column(
        name=name,
        datatype=datatype,
        nullable=not primary_key,
        default=None if primary_key else Value.Null(),
        unique=primary_key,
        index=False,
        references=None,
    )


TABLE = Table(
    "t",
    0,
    [column("id", DataType.Integer, True), column("n", DataType.Integer), column("s", DataType.String)],
)


class TestCreate(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.txn = self.engine.begin()

    def tearDown(self) -> None:
        self.txn.rollback()

    def test_names(self) -> None:
        """Constraints without a name are named by their position among all
        of the table's constraints; the conditions are stored as given."""
        create_checks(self.txn, TABLE, [(None, "id > 0"), ("c", "n < 9"), (None, "s IS NOT NULL")])
        self.assertIsNotNone(self.txn.get_table("t"))
        self.assertEqual(
            self.txn.get_checks("t"),
            [Check("t_check1", "id > 0"), Check("c", "n < 9"), Check("t_check3", "s IS NOT NULL")],
        )

    def test_errors(self) -> None:
        """The table's own checks come first, then for each constraint its
        name and its condition; nothing is created on an error."""
        other = Table("other", 0, [column("id", DataType.Integer, True)])
        create_checks(self.txn, other, [(None, "TRUE")])
        cases: list[tuple[Table, list[tuple[str | None, str]], str]] = [
            (other, [(None, "nosuch")], "table other already exists"),
            (Table("t", 5, TABLE.columns), [(None, "nosuch")], "invalid primary key index"),
            (TABLE, [(None, "nosuch > 0")], "unknown column nosuch"),
            (TABLE, [("t_check2", "TRUE"), (None, "TRUE")], "duplicate check constraint t_check2 in table t"),
            (TABLE, [("x", "TRUE"), ("x", "nosuch")], "duplicate check constraint x in table t"),
            (TABLE, [("", "TRUE")], "check constraint name can't be empty"),
            (TABLE, [(None, "other.id > 0")], "unknown table other"),
            (TABLE, [(None, "count(id) > 0")], "unknown function count with 1 arguments"),
        ]
        for table, checks, message in cases:
            with self.subTest(checks=checks):
                with self.assertRaises(Error) as cm:
                    create_checks(self.txn, table, checks)
                self.assertEqual(str(cm.exception), f"invalid input: {message}")
                self.assertIsNone(self.txn.get_table("t"))
                self.assertEqual(self.txn.get_checks("t"), [])

        # Names are compared exactly.
        create_checks(self.txn, TABLE, [("x", "TRUE"), ("X", "TRUE")])
        self.assertEqual([check.name for check in self.txn.get_checks("t")], ["x", "X"])

    def test_read_only(self) -> None:
        """A read-only transaction fails after the constraints' checks."""
        txn = self.engine.begin_read_only()
        with self.assertRaises(Error) as cm:
            create_checks(txn, TABLE, [(None, "nosuch")])
        self.assertEqual(str(cm.exception), "invalid input: unknown column nosuch")
        with self.assertRaises(Error.ReadOnly):
            create_checks(txn, TABLE, [(None, "id > 0")])

    def test_not_evaluated(self) -> None:
        """Conditions are planned, but neither optimized nor evaluated."""
        create_checks(self.txn, TABLE, [(None, "1 / 0 = 1"), (None, "FALSE")])
        self.assertEqual(len(self.txn.get_checks("t")), 2)


class TestCheckRows(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.txn = self.engine.begin()
        create_checks(
            self.txn, TABLE, [(None, "n > 0"), ("small", "n < 100"), (None, "s > 'a' OR n > 50")]
        )

    def tearDown(self) -> None:
        self.txn.rollback()

    def check(self, *rows: tuple[Value, Value, Value]) -> None:
        """Checks rows of the test table."""
        check_rows(TABLE, build_checks(self.txn, TABLE), [list(row) for row in rows])

    def test_satisfied(self) -> None:
        """TRUE and NULL satisfy a constraint."""
        integer, string, null = Value.Integer, Value.String, Value.Null()
        self.check((integer(1), integer(5), string("b")))
        self.check((integer(1), null, null))
        self.check((integer(1), integer(60), null))

    def test_violations(self) -> None:
        """The rows are checked in order, each against the constraints in
        order; the first violation is the error, with the row's key."""
        integer, string = Value.Integer, Value.String
        cases: list[tuple[list[tuple[Value, Value, Value]], str]] = [
            ([(integer(1), integer(0), string("b"))], "row 1 violates check constraint t_check1"),
            ([(integer(1), integer(-1), string("a"))], "row 1 violates check constraint t_check1"),
            (
                [(integer(1), integer(5), string("b")), (integer(2), integer(100), string("b"))],
                "row 2 violates check constraint small",
            ),
            (
                [(integer(3), integer(5), string("a")), (integer(2), integer(0), string("b"))],
                "row 3 violates check constraint t_check3",
            ),
        ]
        for rows, message in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(Error) as cm:
                    self.check(*rows)
                self.assertEqual(str(cm.exception), f"invalid input: {message}")

    def test_errors(self) -> None:
        """A condition that returns another value is an error, and so is an
        error while evaluating it."""
        txn = self.engine.begin()
        table = Table("u", 0, [column("k", DataType.String, True), column("n", DataType.Integer)])
        create_checks(txn, table, [(None, "n + 1"), (None, "k > 0")])
        with self.assertRaises(Error) as cm:
            check_rows(table, build_checks(txn, table), [[Value.String("a"), Value.Integer(5)]])
        self.assertEqual(
            str(cm.exception), "invalid input: check constraint u_check1 returned 6, expected boolean"
        )
        with self.assertRaises(Error) as cm:
            check_rows(table, build_checks(txn, table), [[Value.String("a"), Value.Null()]])
        self.assertEqual(str(cm.exception), "invalid input: can't compare 'a' and 0")
        txn.rollback()

    def test_no_constraints(self) -> None:
        """Rows of a table without constraints are not checked."""
        table = Table("w", 0, [column("id", DataType.Integer, True)])
        self.txn.create_table(table)
        self.assertEqual(build_checks(self.txn, table), [])
        check_rows(table, [], [[Value.String("not even an integer")]])

    def test_build_condition(self) -> None:
        """Conditions are planned with the table's columns, unqualified or
        qualified by the table name, and desugared."""
        condition = build_condition(TABLE, "t.n IS NOT NULL AND id > n")
        row = [Value.Integer(2), Value.Integer(1), Value.Null()]
        self.assertEqual(condition.evaluate(row), Value.Boolean(True))
        row = [Value.Integer(2), Value.Null(), Value.Null()]
        self.assertEqual(condition.evaluate(row), Value.Boolean(False))


class TestSession(unittest.TestCase):
    def test_rows_before_writes(self) -> None:
        """Constraints are checked for all of a statement's rows before any
        is written, and before the existing checks of the rows."""
        engine = Local(Memory())
        session = engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT UNIQUE, CHECK (n > 0))")
        session.execute("INSERT INTO t VALUES (1, 1)")
        cases = [
            ("INSERT INTO t VALUES (1, 1), (2, 0)", "row 2 violates check constraint t_check1"),
            ("INSERT INTO t VALUES (2, 1), (3, 3)", "value 1 already in unique column n"),
            ("UPDATE t SET id = id + 10, n = n - 1", "row 11 violates check constraint t_check1"),
        ]
        for sql, message in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(Error) as cm:
                    session.execute(sql)
                self.assertEqual(str(cm.exception), f"invalid input: {message}")
        txn = engine.begin_read_only()
        self.assertEqual(list(txn.scan("t", None)), [[Value.Integer(1), Value.Integer(1)]])
        txn.rollback()
        session.close()


if __name__ == "__main__":
    unittest.main()
