"""Tests for toydb.sql.planner.describe.

DESCRIBE is mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext4` (see `tests/sql/test_mod.py`); these tests cover
the descriptions of plans, nodes and expressions directly.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.parser import Parser
from toydb.sql.planner import Node, Plan
from toydb.sql.planner.describe import (
    ColumnDescription,
    describe,
    describe_column,
    describe_table,
    expression_type,
    is_nullable,
)
from toydb.sql.types import DataType, Expression, Value
from toydb.storage.memory import Memory

BOOLEAN, INTEGER, FLOAT, STRING = DataType.Boolean, DataType.Integer, DataType.Float, DataType.String


class TestDescribe(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute(
            "CREATE TABLE t (id INT PRIMARY KEY, i INT, f FLOAT, s STRING NOT NULL, b BOOLEAN)"
        )
        session.execute("CREATE TABLE u (id INT PRIMARY KEY, t_id INT NOT NULL, n FLOAT NOT NULL)")
        session.execute("CREATE VIEW v AS SELECT id, NULL AS x, i ^ 2 AS y, s AS name FROM t")
        session.execute("CREATE MATERIALIZED VIEW m (k, total) AS SELECT s, sum(i) FROM t GROUP BY s")
        session.close()
        self.txn = self.engine.begin_read_only()

    def tearDown(self) -> None:
        self.txn.rollback()

    def describe(self, sql: str) -> list[ColumnDescription]:
        """Describes a statement, planned without optimizing it."""
        return describe(Plan.build(Parser.parse(sql), self.txn))

    def types(self, sql: str) -> list[tuple[DataType | None, bool]]:
        """Returns the types and nullability of a statement's result columns."""
        return [(d.datatype, d.nullable) for d in self.describe(sql)]

    def test_table(self) -> None:
        """Table columns are described from the schema."""
        self.assertEqual(
            describe_table(self.txn, "u"),
            [
                ColumnDescription("id", INTEGER, False),
                ColumnDescription("t_id", INTEGER, False),
                ColumnDescription("n", FLOAT, False),
            ],
        )

    def test_view(self) -> None:
        """View columns are described from the view's query."""
        self.assertEqual(
            describe_table(self.txn, "v"),
            [
                ColumnDescription("id", INTEGER, False),
                ColumnDescription("x", None, True),
                ColumnDescription("y", None, True),
                ColumnDescription("name", STRING, False),
            ],
        )

    def test_matview(self) -> None:
        """Materialized view columns are described from the stored query,
        planned again, also in queries."""
        self.assertEqual(
            describe_table(self.txn, "m"),
            [ColumnDescription("k", STRING, False), ColumnDescription("total", INTEGER, True)],
        )
        self.assertEqual(
            self.types("SELECT k, total + 1.5 FROM m AS x WHERE total > 1"),
            [(STRING, False), (FLOAT, True)],
        )

    def test_system_table(self) -> None:
        """System table columns are described from their schema, also in
        queries."""
        self.assertEqual(
            describe_table(self.txn, "toydb_members"),
            [
                ColumnDescription("member", STRING, False),
                ColumnDescription("member_of", STRING, False),
            ],
        )
        self.assertEqual(
            self.types("SELECT txn, session + 1 FROM toydb_sessions"),
            [(INTEGER, True), (INTEGER, False)],
        )

    def test_unknown_table(self) -> None:
        with self.assertRaises(Error) as context:
            describe_table(self.txn, "nosuch")
        self.assertEqual(str(context.exception), "invalid input: table nosuch does not exist")

    def test_names(self) -> None:
        """Result columns are named by their labels, or None."""
        names = [d.name for d in self.describe("SELECT id, t.i, s AS name, id + 1 FROM t")]
        self.assertEqual(names, ["t.id", "t.i", "name", None])
        names = [d.name for d in self.describe("SELECT * FROM t x JOIN v ON v.id = x.id")]
        self.assertEqual(names, ["x.id", "x.i", "x.f", "x.s", "x.b", "v.id", "v.x", "v.y", "v.name"])

    def test_statements(self) -> None:
        """Statements other than queries have no result columns."""
        for sql in [
            "INSERT INTO t VALUES (1, 2, 3.0, 'a', TRUE)",
            "UPDATE t SET i = 1",
            "DELETE FROM t WHERE id = 1",
            "CREATE TABLE w (id INT PRIMARY KEY)",
            "DROP TABLE t",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(self.describe(sql), [])

    def test_expression_types(self) -> None:
        """Expressions have the data type of the values they return, or are
        unknown."""
        cases: list[tuple[str, DataType | None]] = [
            # Constants and columns.
            ("1", INTEGER),
            ("1.5", FLOAT),
            ("'a'", STRING),
            ("FALSE", BOOLEAN),
            ("NULL", None),
            ("i", INTEGER),
            ("v.x", None),
            # Logical operators.
            ("b AND TRUE", BOOLEAN),
            ("b OR NULL", BOOLEAN),
            ("NULL OR NULL", None),
            ("b AND i", None),
            ("NOT b", BOOLEAN),
            ("NOT s", None),
            ("NOT v.x", BOOLEAN),
            # Comparisons.
            ("i = f", BOOLEAN),
            ("s < 'a'", BOOLEAN),
            ("s > 1", None),
            ("b = NULL", None),
            ("v.x = 1", BOOLEAN),
            ("i != 1", BOOLEAN),
            ("s != 1", None),
            ("i >= NULL", None),
            ("s <= 'a'", BOOLEAN),
            ("s LIKE 'a%'", BOOLEAN),
            ("i LIKE 'a'", None),
            ("s IS NULL", BOOLEAN),
            ("NULL IS NOT NULL", BOOLEAN),
            ("f IS NAN", BOOLEAN),
            ("s IS NOT NAN", None),
            ("f IS NOT NAN", BOOLEAN),
            ("NULL IS NOT NAN", None),
            # The forms in which !=, >= and <= are planned, written out.
            ("NOT (s = 1)", None),
            ("s < 1 OR s = 1", None),
            ("s = 1 OR s < 1", BOOLEAN),
            # Only the NULL constant itself is the NULL constant.
            ("(NULL + 1) = 1", BOOLEAN),
            ("NOT -NULL", BOOLEAN),
            # Mathematical operations.
            ("i + 1", INTEGER),
            ("i * f", FLOAT),
            ("v.y - 1.5", FLOAT),
            ("v.y % 2", None),
            ("i / NULL", None),
            ("s + 1", None),
            ("(s + 1) * 2.0", FLOAT),
            ("i ^ i", None),
            ("i ^ 2.0", FLOAT),
            ("-f", FLOAT),
            ("+s", None),
            ("-v.x", None),
            ("i!", INTEGER),
            ("v.x!", INTEGER),
            ("f!", None),
            ("sqrt(i)", FLOAT),
            ("sqrt(v.x)", FLOAT),
            ("sqrt(NULL)", None),
        ]
        for expr, datatype in cases:
            with self.subTest(expr=expr):
                [description] = self.describe(f"SELECT {expr} FROM t JOIN v ON v.id = t.id")
                self.assertEqual(description.datatype, datatype)

    def test_expression_nullability(self) -> None:
        """Expressions are nullable if an operand is, except IS NULL."""
        cases: list[tuple[str, bool]] = [
            ("1", False),
            ("NULL", True),
            ("s", False),
            ("i", True),
            ("s = 'a'", False),
            ("s = i", True),
            ("NOT b", True),
            ("i IS NULL", False),
            ("i IS NOT NULL", False),
            ("i IS NOT NAN", True),
            ("f IS NAN", True),
            ("-s", False),
            ("sqrt(id)", False),
            ("id + NULL", True),
            ("(i IS NULL) AND (s = 'a')", False),
        ]
        for expr, nullable in cases:
            with self.subTest(expr=expr):
                [description] = self.describe(f"SELECT {expr} FROM t")
                self.assertEqual(description.nullable, nullable)

    def test_aggregates(self) -> None:
        """count() is an INTEGER that is never NULL; the other aggregate
        functions are always nullable. GROUP BY columns are described like
        their expressions."""
        self.assertEqual(
            self.types(
                "SELECT s, COUNT(*), COUNT(i), SUM(i), SUM(f), AVG(s), MIN(s), MAX(b), "
                "COUNT(*) + 1, SUM(i) * 2.0 FROM t GROUP BY s"
            ),
            [
                (STRING, False),
                (INTEGER, False),
                (INTEGER, False),
                (INTEGER, True),
                (FLOAT, True),
                (None, True),
                (STRING, True),
                (BOOLEAN, True),
                (INTEGER, False),
                (FLOAT, True),
            ],
        )
        self.assertEqual(
            self.types("SELECT id % 2, NULL = 1, MIN(NULL) = 1 FROM t GROUP BY id % 2, NULL"),
            [(INTEGER, False), (BOOLEAN, True), (BOOLEAN, True)],
        )

    def test_outer_joins(self) -> None:
        """The columns of the inner side of an outer join are nullable."""
        self.assertEqual(
            self.types("SELECT t.s, u.n FROM t LEFT JOIN u ON u.t_id = t.id"),
            [(STRING, False), (FLOAT, True)],
        )
        self.assertEqual(
            self.types("SELECT t.s, u.n FROM t RIGHT JOIN u ON u.t_id = t.id"),
            [(STRING, True), (FLOAT, False)],
        )
        self.assertEqual(
            self.types("SELECT t.s, u.n FROM t JOIN u ON u.t_id = t.id, u AS w"),
            [(STRING, False), (FLOAT, False)],
        )
        self.assertEqual(
            self.types(
                "SELECT t.s, u.n, w.n FROM t JOIN u ON u.t_id = t.id RIGHT JOIN u AS w ON w.id = u.id"
            ),
            [(STRING, True), (FLOAT, True), (FLOAT, False)],
        )

    def test_nodes(self) -> None:
        """Columns are traced through every kind of node, including those
        that only optimization creates."""
        table = self.txn.must_get_table("u")
        scan = Node.Scan(table=table, filter=None, alias=None)
        values = Node.Values(
            rows=[
                [Expression.Constant(Value.Integer(1)), Expression.Constant(Value.Null())],
                [Expression.Constant(Value.Null()), Expression.Constant(Value.String("a"))],
                [Expression.Constant(Value.Integer(2)), Expression.Constant(Value.Float(1.0))],
            ]
        )
        self.assertEqual(describe_column(values, 0), (INTEGER, True))
        self.assertEqual(describe_column(values, 1), (None, True))
        lookup = Node.KeyLookup(table=table, keys=[Value.Integer(1)], alias=None)
        self.assertEqual(describe_column(lookup, 2), (FLOAT, False))
        join = Node.HashJoin(left=lookup, left_column=0, right=scan, right_column=1, outer=True)
        self.assertEqual(describe_column(join, 2), (FLOAT, False))
        self.assertEqual(describe_column(join, 5), (FLOAT, True))
        remap = Node.Remap(source=scan, targets=[None, 0])
        self.assertEqual(describe_column(remap, 0), (INTEGER, False))
        self.assertEqual(describe_column(Node.Remap(source=scan, targets=[1]), 0), (None, True))
        self.assertEqual(describe_column(Node.Limit(source=scan, limit=1), 1), (INTEGER, False))

    def test_expression_functions(self) -> None:
        """expression_type and is_nullable look up columns in the given
        node."""
        scan = Node.Scan(table=self.txn.must_get_table("t"), filter=None, alias=None)
        expr = Expression.Add(Expression.Column(1), Expression.Column(2))
        self.assertEqual(expression_type(expr, scan), FLOAT)
        self.assertTrue(is_nullable(expr, scan))
        expr = Expression.Like(Expression.Column(3), Expression.Constant(Value.String("a")))
        self.assertEqual(expression_type(expr, scan), BOOLEAN)
        self.assertFalse(is_nullable(expr, scan))

    def test_row(self) -> None:
        """Descriptions are DESCRIBE result rows."""
        self.assertEqual(
            ColumnDescription("a", INTEGER, False).row(),
            [Value.String("a"), Value.String("INTEGER"), Value.Boolean(False)],
        )
        self.assertEqual(
            ColumnDescription(None, None, True).row(),
            [Value.Null(), Value.Null(), Value.Boolean(True)],
        )


if __name__ == "__main__":
    unittest.main()
