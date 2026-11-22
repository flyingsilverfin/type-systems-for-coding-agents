"""Tests for toydb.sql.planner.estimate.

ESTIMATE is mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext4` (see `tests/sql/test_mod.py`); these tests cover
the estimates of plans, nodes and conditions directly.
"""

import unittest

from toydb.sql.engine import Local
from toydb.sql.parser import Parser
from toydb.sql.planner import Node, Plan
from toydb.sql.planner.estimate import Estimator, estimate_rows, round_half_up
from toydb.sql.types import Expression, Value
from toydb.storage.memory import Memory


class TestEstimate(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute(
            "CREATE TABLE t "
            "(id INT PRIMARY KEY, i INT, f FLOAT, s STRING INDEX, c INT, x FLOAT)"
        )
        session.execute(
            "INSERT INTO t VALUES (1, 10, 1.0, 'a', 7, 1.0), (2, 20, 2.0, 'b', 7, 2.0), "
            "(3, 20, 3.0, 'c', 7, INFINITY), (4, NULL, 4.0, NULL, 7, 3.0), (5, 50, 5.0, 'a', 7, 4.0)"
        )
        session.execute("CREATE TABLE u (id INT PRIMARY KEY, a INT, b INT)")
        session.execute("CREATE TABLE e (id INT PRIMARY KEY, a INT)")
        session.execute("ANALYZE t")
        session.execute("ANALYZE e")
        session.execute("CREATE VIEW v AS SELECT id, i, i + 1 AS j FROM t")
        session.close()
        self.txn = self.engine.begin_read_only()
        self.estimator = Estimator(self.txn)

    def tearDown(self) -> None:
        self.txn.rollback()

    def plan(self, sql: str) -> Plan:
        """Plans and optimizes a statement."""
        return Plan.build(Parser.parse(sql), self.txn).optimize()

    def estimate(self, sql: str) -> list[float]:
        """Estimates the plan of a statement."""
        return self.estimator.estimate(self.plan(sql))

    def selectivity(self, table: str, condition: str) -> float:
        """Returns the selectivity of a WHERE condition on a table, planned
        without optimizing it."""
        plan = Plan.build(Parser.parse(f"SELECT * FROM {table} WHERE {condition}"), self.txn)
        assert isinstance(plan, Plan.Select)
        assert isinstance(plan.root, Node.Filter)
        return self.estimator.selectivity(plan.root.predicate, plan.root.source)

    def test_round_half_up(self) -> None:
        for value, expect in [
            (0.0, 0),
            (0.49999999999999994, 0),
            (0.5, 1),
            (1.49, 1),
            (2.5, 3),
            (12.5, 13),
            (1000.0, 1000),
        ]:
            with self.subTest(value=value):
                self.assertEqual(round_half_up(value), expect)

    def test_selectivity_with_statistics(self) -> None:
        """Conditions on columns with statistics use them."""
        cases: list[tuple[str, float]] = [
            # Constants.
            ("TRUE", 1.0),
            ("FALSE", 0.0),
            ("NULL", 0.0),
            # Equality: the fraction of non-NULL rows per distinct value.
            ("i = 20", 0.8 / 3),
            ("20 = i", 0.8 / 3),
            ("s = 'zzz'", 0.8 / 3),
            ("f = 2", 0.2),
            # Comparisons with the NULL constant.
            ("i = NULL", 0.0),
            ("NULL < i", 0.0),
            ("i > NULL", 0.0),
            # Two columns: 1 / the larger number of distinct values.
            ("i = f", 0.2),
            ("s = i", 1 / 3),
            # IS NULL: the fraction of NULLs.
            ("i IS NULL", 0.2),
            ("f IS NULL", 0.0),
            # Ranges: interpolation between the minimum and maximum.
            ("i < 30", 0.5 * 0.8),
            ("30 > i", 0.5 * 0.8),
            ("i > 40", 0.25 * 0.8),
            ("40 < i", 0.25 * 0.8),
            ("f < 2.5", 0.375),
            ("i < 5", 0.0),
            ("i > 100", 0.0),
            ("i < 100", 0.8),
            # A column with a single value.
            ("c < 7", 0.0),
            ("c < 8", 1.0),
            ("c > 7", 0.0),
            ("c > 6", 1.0),
            # Ranges without a statistics-based estimate.
            ("s < 'b'", 1 / 3),
            ("i < NAN", 1 / 3),
            ("i > INFINITY", 1 / 3),
            ("x < 2.0", 1 / 3),
            # Logical operators.
            ("i = 20 AND f = 2", 0.8 / 3 * 0.2),
            ("i = 20 OR f = 2", 0.8 / 3 + 0.2 - 0.8 / 3 * 0.2),
            ("NOT i = 20", 1 - 0.8 / 3),
            ("i >= 50", 0.0 + 0.8 / 3),
            # Other conditions.
            ("s LIKE 'a%'", 0.25),
            ("i * 2 = 20", 0.25),
            ("i < f", 0.25),
            ("f IS NAN", 0.25),
            ("1 = 1", 0.25),
        ]
        for condition, expect in cases:
            with self.subTest(condition=condition):
                self.assertAlmostEqual(self.selectivity("t", condition), expect)

    def test_selectivity_without_statistics(self) -> None:
        """Conditions on columns without statistics use fixed selectivities."""
        cases: list[tuple[str, float]] = [
            ("a = 1", 0.1),
            ("a = b", 0.1),
            ("a IS NULL", 0.1),
            ("a < 1", 1 / 3),
            ("1 < a", 1 / 3),
            ("a = NULL", 0.0),
            ("a LIKE 'x'", 0.25),
            ("a = 1 AND b > 1", 0.1 / 3),
        ]
        for condition, expect in cases:
            with self.subTest(condition=condition):
                self.assertAlmostEqual(self.selectivity("u", condition), expect)

        # A column with statistics compared to one without.
        plan = self.plan("SELECT * FROM t, u WHERE t.i = u.a")
        assert isinstance(plan, Plan.Select)
        self.assertEqual(self.estimator.estimate(plan)[0], 5 * 1000 * 0.1)

    def test_selectivity_empty_table(self) -> None:
        """An analyzed table without rows has no rows for any condition."""
        self.assertEqual(self.selectivity("e", "a = 1"), 0.0)
        self.assertEqual(self.selectivity("e", "a IS NULL"), 0.0)
        self.assertEqual(self.selectivity("e", "a = id"), 0.0)

    def test_column_statistics(self) -> None:
        """Columns keep their statistics through nodes that pass them on
        unchanged."""
        table = self.txn.must_get_table("t")
        scan = Node.Scan(table=table, filter=None, alias=None)
        statistics = self.estimator.column_statistics(scan, 1)
        assert statistics is not None
        self.assertEqual(statistics[0], 5)
        self.assertEqual((statistics[1].nulls, statistics[1].distinct), (1, 3))

        def traced(node: Node, index: int) -> int | None:
            statistics = self.estimator.column_statistics(node, index)
            return statistics[1].distinct if statistics is not None else None

        projection = Node.Projection(
            source=scan,
            expressions=[
                Expression.Column(2),
                Expression.Add(Expression.Column(1), Expression.Constant(Value.Integer(1))),
            ],
            aliases=[],
        )
        self.assertEqual(traced(projection, 0), 5)
        self.assertIsNone(traced(projection, 1))
        self.assertEqual(traced(Node.Remap(source=projection, targets=[1, None]), 1), 5)
        self.assertIsNone(traced(Node.Remap(source=projection, targets=[None, 0]), 0))
        self.assertEqual(traced(Node.Limit(source=projection, limit=1), 0), 5)
        self.assertEqual(traced(Node.Filter(source=scan, predicate=Expression.Column(0)), 3), 3)

        lookup = Node.KeyLookup(table=self.txn.must_get_table("u"), keys=[], alias=None)
        join = Node.NestedLoopJoin(left=lookup, right=scan, predicate=None, outer=True)
        self.assertIsNone(traced(join, 1))
        self.assertEqual(traced(join, 4), 3)

        values = Node.Values(rows=[[Expression.Column(0)]])
        self.assertIsNone(traced(values, 0))

        # Aggregates pass on plain GROUP BY columns.
        plan = self.plan("SELECT i, i + 1, COUNT(*) FROM t GROUP BY i, i + 1")
        assert isinstance(plan, Plan.Select)
        aggregate = plan.root
        assert isinstance(aggregate, Node.Aggregate)
        self.assertEqual(traced(aggregate, 0), 3)
        self.assertIsNone(traced(aggregate, 1))
        self.assertIsNone(traced(aggregate, 2))

    def test_range_overflow(self) -> None:
        """A range whose width max - min overflows has no interpolation."""
        session = self.engine.session()
        session.execute("CREATE TABLE big (id INT PRIMARY KEY, f FLOAT)")
        session.execute("INSERT INTO big VALUES (1, -1e308), (2, 0.0), (3, 1e308)")
        session.execute("CREATE TABLE wide (id INT PRIMARY KEY, f FLOAT)")
        session.execute("INSERT INTO wide VALUES (1, 1e308), (2, 1.5e308)")
        session.execute("ANALYZE")
        session.close()
        self.txn.rollback()
        self.txn = self.engine.begin_read_only()
        self.estimator = Estimator(self.txn)
        self.assertEqual(self.estimate("SELECT * FROM big WHERE f < 0"), [1])
        self.assertEqual(self.estimate("SELECT * FROM big WHERE f < 1e308"), [1])
        self.assertEqual(self.estimate("SELECT * FROM big WHERE f > -1e308"), [1])
        # A finite width interpolates, also where v - min overflows.
        self.assertEqual(self.estimate("SELECT * FROM wide WHERE f > -1e308"), [2])
        self.assertEqual(self.estimate("SELECT * FROM wide WHERE f < -1e308"), [0])

    def test_scans_and_lookups(self) -> None:
        self.assertEqual(self.estimate("SELECT * FROM t"), [5])
        self.assertEqual(self.estimate("SELECT * FROM u"), [1000])
        self.assertAlmostEqual(self.estimate("SELECT * FROM t WHERE i = 20")[0], 5 * 0.8 / 3)
        self.assertAlmostEqual(self.estimate("SELECT * FROM t WHERE i > -100")[0], 4)
        self.assertEqual(self.estimate("SELECT * FROM t WHERE id = 1 OR id = 2"), [2])
        self.assertEqual(self.estimate("SELECT * FROM e WHERE id = 1 OR id = 2"), [0])
        self.assertEqual(self.estimate("SELECT * FROM u WHERE id = 1 OR id = 2"), [2])
        self.assertAlmostEqual(
            self.estimate("SELECT * FROM t WHERE s = 'a' OR s IS NULL")[0], 5 * (0.8 / 3 + 0.2)
        )
        self.assertAlmostEqual(self.estimate("SELECT * FROM u WHERE a = 1 OR a = 2")[0], 1000 * 0.19)

    def test_nodes(self) -> None:
        self.assertEqual(self.estimate("SELECT id FROM t ORDER BY f LIMIT 3"), [3, 3, 5, 5, 5])
        self.assertEqual(self.estimate("SELECT id FROM t OFFSET 3"), [2, 5, 5])
        self.assertEqual(self.estimate("SELECT id FROM t OFFSET 30"), [0, 5, 5])
        self.assertEqual(self.estimate("SELECT 1"), [1, 1])
        self.assertEqual(self.estimate("SELECT * FROM t WHERE NULL"), [0])
        self.assertEqual(self.estimate("SELECT * FROM v"), [5, 5, 5])
        lines: list[float] = []
        self.assertEqual(self.estimator.estimate_node(Node.Values(rows=[[], [], []]), lines), 3)
        self.assertEqual(lines, [3])

    def test_aggregates(self) -> None:
        """Aggregates have one row, or at most one per combination of distinct
        values of the GROUP BY expressions."""
        self.assertEqual(self.estimate("SELECT COUNT(*) FROM t"), [1, 5])
        self.assertEqual(self.estimate("SELECT i, COUNT(*) FROM t GROUP BY i"), [4, 5])
        self.assertEqual(self.estimate("SELECT f, COUNT(*) FROM t GROUP BY f"), [5, 5])
        self.assertEqual(self.estimate("SELECT c, COUNT(*) FROM t GROUP BY c"), [1, 5])
        self.assertEqual(self.estimate("SELECT a, COUNT(*) FROM u GROUP BY a"), [10, 1000])
        self.assertEqual(self.estimate("SELECT a, b, COUNT(*) FROM u GROUP BY a, b"), [100, 1000])
        self.assertEqual(self.estimate("SELECT i * 2, COUNT(*) FROM t GROUP BY i * 2"), [5, 5])

    def test_joins(self) -> None:
        # A hash join: 1 / the larger number of distinct values of the columns.
        self.assertEqual(self.estimate("SELECT * FROM t JOIN t AS t2 ON t.i = t2.f"), [5, 5, 5])
        self.assertEqual(self.estimate("SELECT * FROM t JOIN u ON t.i = u.a"), [500, 5, 1000])
        self.assertEqual(self.estimate("SELECT * FROM e LEFT JOIN t ON e.a = t.i"), [0, 0, 5])
        # Nested loop joins.
        self.assertEqual(self.estimate("SELECT * FROM t, u"), [5000, 5, 1000])
        self.assertEqual(self.estimate("SELECT * FROM t JOIN u ON t.i < u.a"), [1250, 5, 1000])
        self.assertEqual(self.estimate("SELECT * FROM t LEFT JOIN e ON t.i < e.a"), [5, 5, 0])

    def test_statements(self) -> None:
        """Statement lines estimate the rows written."""
        self.assertEqual(self.estimate("INSERT INTO u VALUES (1, 2, 3), (2, 3, 4)"), [2, 2])
        self.assertEqual(self.estimate("UPDATE t SET i = 1 WHERE f > 3"), [2.5, 2.5])
        self.assertEqual(self.estimate("DELETE FROM t WHERE id = 1"), [1, 1])
        self.assertEqual(self.estimate("CREATE TABLE w (id INT PRIMARY KEY)"), [0])
        self.assertEqual(self.estimate("DROP TABLE u"), [0])

    def test_estimate_rows(self) -> None:
        """The result rows pair the plan display lines with the rounded
        estimates."""
        rows = estimate_rows(self.txn, self.plan("SELECT id FROM t WHERE i = 20"))
        self.assertEqual(
            rows,
            [
                [Value.String("Projection: t.id"), Value.Integer(1)],
                [Value.String("└─ Scan: t (t.i = 20)"), Value.Integer(1)],
            ],
        )


if __name__ == "__main__":
    unittest.main()
