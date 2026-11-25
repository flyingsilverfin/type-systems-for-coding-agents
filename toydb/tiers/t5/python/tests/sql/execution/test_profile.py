"""Tests for toydb.sql.execution.profile.

EXPLAIN (ANALYZE) is mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these tests cover
the row counts of plans and nodes and the profiling executor directly.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution import Executor
from toydb.sql.execution.executor import ExecutionResult
from toydb.sql.execution.profile import COLUMNS, profile, profile_rows
from toydb.sql.parser import Parser
from toydb.sql.planner import Plan
from toydb.sql.types import Label, Value
from toydb.sql.types.user import ADMIN
from toydb.storage.memory import Memory


class TestProfile(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT, s STRING INDEX)")
        session.execute("INSERT INTO t VALUES (1, 10, 'a'), (2, 20, 'b'), (3, 30, 'a')")
        session.execute("CREATE TABLE u (id INT PRIMARY KEY, t_id INT)")
        session.execute("INSERT INTO u VALUES (1, 1), (2, 1), (3, 9)")
        session.close()
        self.txn = self.engine.begin()

    def tearDown(self) -> None:
        self.txn.rollback()

    def plan(self, sql: str) -> Plan:
        """Plans and optimizes a statement."""
        return Plan.build(Parser.parse(sql), self.txn).optimize()

    def profile(self, sql: str) -> list[int]:
        """Profiles the plan of a statement as admin."""
        _, counts = profile(self.txn, self.plan(sql), ADMIN)
        return counts

    def test_columns(self) -> None:
        self.assertEqual(COLUMNS, [Label.Unqualified(n) for n in ("node", "estimate", "rows")])

    def test_select(self) -> None:
        """A query has a line per node, starting with the root."""
        self.assertEqual(self.profile("SELECT v FROM t WHERE v > 15"), [2, 2])
        self.assertEqual(self.profile("SELECT * FROM t WHERE id = 2 OR id = 9"), [1])
        self.assertEqual(self.profile("SELECT id FROM t WHERE s = 'a'"), [2, 2])
        self.assertEqual(self.profile("SELECT count(*) FROM t GROUP BY s"), [2, 2, 3])
        self.assertEqual(self.profile("SELECT * FROM t WHERE FALSE"), [0])
        self.assertEqual(self.profile("SELECT 1"), [1, 1])

    def test_run_to_completion(self) -> None:
        """Every node produces all its rows, also below a LIMIT."""
        self.assertEqual(self.profile("SELECT * FROM t LIMIT 1"), [1, 3])
        self.assertEqual(self.profile("SELECT * FROM t LIMIT 1 OFFSET 1"), [1, 2, 3])
        with self.assertRaises(Error) as cm:
            self.profile("SELECT 1 / (id - 2) FROM t LIMIT 1")
        self.assertEqual(str(cm.exception), "invalid input: can't divide by zero")

    def test_joins(self) -> None:
        """Both inputs of a join are counted once, in display order."""
        self.assertEqual(self.profile("SELECT * FROM t JOIN u ON t.id = u.t_id"), [2, 3, 3])
        self.assertEqual(self.profile("SELECT * FROM t JOIN u ON t.id < u.t_id"), [3, 3, 3])
        self.assertEqual(self.profile("SELECT * FROM u LEFT JOIN t ON t.id = u.t_id"), [3, 3, 3])

    def test_writes(self) -> None:
        """A write's statement line has its count, and its writes are made."""
        self.assertEqual(self.profile("INSERT INTO u VALUES (4, 2), (5, 3)"), [2, 2])
        self.assertEqual(self.profile("UPDATE u SET t_id = 2 WHERE t_id = 1"), [2, 2])
        self.assertEqual(self.profile("DELETE FROM u WHERE id = 1 OR id = 7"), [1, 1])
        rows = list(self.txn.scan("u", None))
        self.assertEqual(
            [[value.value for value in row] for row in rows],
            [[2, 2], [3, 9], [4, 2], [5, 3]],
        )

    def test_definitions(self) -> None:
        """Plans without a count have a statement line of 0."""
        self.assertEqual(self.profile("CREATE TABLE w (id INT PRIMARY KEY)"), [0])
        self.assertIsNotNone(self.txn.get_table("w"))
        self.assertEqual(self.profile("DROP TABLE w"), [0])
        self.assertIsNone(self.txn.get_table("w"))

    def test_profile_rows(self) -> None:
        """The result rows have the plan display lines, the estimates computed
        before the execution, and the counts."""
        result, rows = profile_rows(self.txn, self.plan("DELETE FROM t WHERE v > 15"), ADMIN)
        self.assertEqual(result, ExecutionResult.Delete(2))
        self.assertEqual(
            rows,
            [
                [Value.String("Delete: t"), Value.Integer(333), Value.Integer(2)],
                [Value.String("└─ Scan: t (t.v > 15)"), Value.Integer(333), Value.Integer(2)],
            ],
        )

    def test_executor_not_profiling(self) -> None:
        """An executor only counts rows when profiling."""
        executor = Executor(self.txn, ADMIN)
        executor.execute(self.plan("SELECT * FROM t"))
        self.assertIsNone(executor.profile)
        executor = Executor(self.txn, ADMIN, profiling=True)
        executor.execute(self.plan("SELECT * FROM t WHERE v > 15"))
        self.assertEqual(executor.profile, [2])


class TestSessionProfile(unittest.TestCase):
    """EXPLAIN (ANALYZE) in a session."""

    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.session = self.engine.session()
        self.session.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        self.session.execute("INSERT INTO t VALUES (1, 10), (2, 20)")

    def tearDown(self) -> None:
        self.session.close()

    def rows(self, sql: str) -> list[list[object]]:
        """Executes a statement and returns its result rows as Python
        values."""
        result = self.session.execute(sql)
        return [[value.value for value in row] for row in result.rows]  # type: ignore[attr-defined]

    def test_result(self) -> None:
        self.assertEqual(
            self.rows("EXPLAIN (ANALYZE) SELECT v FROM t WHERE id = 2"),
            [["Projection: t.v", 1, 1], ["└─ KeyLookup: t (2)", 1, 1]],
        )

    def test_owner(self) -> None:
        """A table created through EXPLAIN (ANALYZE) is owned by its creator."""
        self.session.execute("CREATE USER bob")
        self.session.execute("SET ROLE bob")
        self.assertEqual(
            self.rows("EXPLAIN (ANALYZE) CREATE TABLE b (id INT PRIMARY KEY)"),
            [["CreateTable: b", 0, 0]],
        )
        txn = self.engine.begin_read_only()
        self.assertEqual(txn.get_owner("b"), "bob")
        txn.rollback()

    def test_execute(self) -> None:
        """EXPLAIN (ANALYZE) EXECUTE executes the bound statement without
        counting an execution."""
        self.session.execute("PREPARE d (INT) AS DELETE FROM t WHERE id = $1")
        self.assertEqual(
            self.rows("EXPLAIN (ANALYZE) EXECUTE d (1)"),
            [["Delete: t", 1, 1], ["└─ KeyLookup: t (1)", 1, 1]],
        )
        self.assertEqual(self.session.prepared.get("d").executions, 0)
        self.assertEqual(self.rows("SELECT id FROM t"), [[2]])

    def test_not_supported(self) -> None:
        for sql, keyword in [
            ("EXPLAIN (ANALYZE) BEGIN", "BEGIN"),
            ("EXPLAIN (ANALYZE) SHOW VIEWS", "SHOW"),
            ("EXPLAIN (ANALYZE) REFRESH MATERIALIZED VIEW m", "REFRESH"),
        ]:
            with self.subTest(sql=sql), self.assertRaises(Error) as cm:
                self.session.execute(sql)
            self.assertEqual(
                str(cm.exception), f"invalid input: EXPLAIN is not supported for {keyword}"
            )


if __name__ == "__main__":
    unittest.main()
