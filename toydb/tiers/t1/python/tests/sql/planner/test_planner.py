"""Tests for toydb.sql.planner.

The Rust planner has no unit tests of its own: it is covered by the SQL
goldenscripts in `tests/sql/testscripts` (see `tests/sql/test_mod.py`), which
need the execution engine. These tests cover planning, optimization, plan
formatting and serialization without it, using an in-memory catalog. The
expected plans follow the formats in the Rust goldenscript output.
"""

import unittest

from toydb.encoding import bincode
from toydb.error import Error, errinput
from toydb.sql.parser import Parser
from toydb.sql.planner import OPTIMIZERS, Node, Plan, Planner, Scope
from toydb.sql.planner.plan import invert_remap
from toydb.sql.types import Label, Table


class Catalog:
    """An in-memory catalog, populated by CREATE TABLE statements."""

    def __init__(self, *statements: str) -> None:
        self.tables: dict[str, Table] = {}
        for statement in statements:
            plan = Planner(self).build(Parser.parse(statement))
            assert isinstance(plan, Plan.CreateTable)
            self.tables[plan.schema.name] = plan.schema

    def get_table(self, table: str) -> Table | None:
        return self.tables.get(table)

    def list_tables(self) -> list[Table]:
        return list(self.tables.values())

    def must_get_table(self, table: str) -> Table:
        if (schema := self.get_table(table)) is None:
            raise errinput(f"table {table} does not exist")
        return schema


CATALOG = Catalog(
    "CREATE TABLE test (id INT PRIMARY KEY, value STRING)",
    "CREATE TABLE other (id INT PRIMARY KEY, test_id INT INDEX)",
)


def build(statement: str) -> Plan:
    return Plan.build(Parser.parse(statement), CATALOG)  # type: ignore[arg-type]


def optimizations(statement: str) -> str:
    """Formats the plan after each optimizer that changes it, like the [opt]
    tag of the SQL goldenscript runner."""
    plan = build(statement)
    assert isinstance(plan, Plan.Select)
    root = plan.root
    output = f"Initial:\n{root}\n"
    for optimizer in OPTIMIZERS:
        prev = root
        root = optimizer.optimize(root)
        if root != prev:
            output += f"{optimizer!r}:\n{root}\n"
    return output


class TestPlan(unittest.TestCase):
    def test_display(self) -> None:
        cases = {
            "CREATE TABLE new (id INT PRIMARY KEY)": "CreateTable: new",
            "DROP TABLE test": "DropTable: test",
            "DELETE FROM test WHERE id = 1": "Delete: test\n└─ KeyLookup: test (1)",
            "INSERT INTO test VALUES (1, 'a'), (2, 'b')": "Insert: test\n└─ Values: 2 rows",
            "UPDATE test SET value = 'x' WHERE value = 'a'": (
                "Update: test (value='x')\n└─ Scan: test (test.value = 'a')"
            ),
            "SELECT 1 + 2, 'a' AS s": "Projection: 3, 'a' as s\n└─ Values: blank row",
            "SELECT value FROM test ORDER BY id DESC LIMIT 3 OFFSET 1": (
                "Remap: test.value (dropped: test.id)\n"
                "└─ Limit: 3\n"
                "   └─ Offset: 1\n"
                "      └─ Order: test.id desc\n"
                "         └─ Projection: test.value, test.id\n"
                "            └─ Scan: test"
            ),
            "SELECT value, COUNT(*) FROM test GROUP BY value": (
                "Aggregate: test.value, count(TRUE)\n└─ Scan: test"
            ),
            "SELECT * FROM test a JOIN test b ON a.id = b.id": (
                "HashJoin: inner on a.id = b.id\n├─ Scan: test as a\n└─ Scan: test as b"
            ),
            "SELECT * FROM test RIGHT JOIN other ON other.test_id = test.id": (
                "Remap: test.id, test.value, other.id, other.test_id\n"
                "└─ HashJoin: outer on other.test_id = test.id\n"
                "   ├─ Scan: other\n"
                "   └─ Scan: test"
            ),
            "SELECT * FROM other WHERE test_id = 1 OR test_id = 2": (
                "IndexLookup: other.test_id (1, 2)"
            ),
            "SELECT * FROM test WHERE FALSE": "Nothing",
        }
        for statement, expect in cases.items():
            with self.subTest(statement=statement):
                self.assertEqual(str(build(statement).optimize()), expect)

    def test_optimizers(self) -> None:
        self.assertEqual(
            optimizations("SELECT * FROM test a JOIN test b ON a.id = b.id"),
            "Initial:\n"
            "NestedLoopJoin: inner on a.id = b.id\n"
            "├─ Scan: test as a\n"
            "└─ Scan: test as b\n"
            "HashJoin:\n"
            "HashJoin: inner on a.id = b.id\n"
            "├─ Scan: test as a\n"
            "└─ Scan: test as b\n",
        )
        self.assertEqual(
            optimizations("SELECT * FROM test WHERE id = 1 + 1 AND TRUE"),
            "Initial:\n"
            "Filter: test.id = 1 + 1 AND TRUE\n"
            "└─ Scan: test\n"
            "ConstantFolding:\n"
            "Filter: test.id = 2\n"
            "└─ Scan: test\n"
            "FilterPushdown:\n"
            "Scan: test (test.id = 2)\n"
            "IndexLookup:\n"
            "KeyLookup: test (2)\n",
        )

    def test_optimize_immutable(self) -> None:
        """Optimizing a plan doesn't modify the original plan."""
        plan = build("SELECT * FROM test a JOIN test b ON a.id = b.id WHERE a.id = 1")
        before = str(plan)
        self.assertNotEqual(str(plan.optimize()), before)
        self.assertEqual(str(plan), before)

    def test_errors(self) -> None:
        cases = {
            "SELECT * FROM missing": "table missing does not exist",
            "SELECT *": "SELECT * requires a FROM clause",
            "SELECT missing FROM test": "unknown column missing",
            "SELECT id FROM test, other": "ambiguous column id",
            "SELECT x.id FROM test": "unknown table x",
            "SELECT * FROM test, test": "duplicate table name test",
            "SELECT id FROM test GROUP BY value": (
                "column id must be used in an aggregate or GROUP BY expression"
            ),
            "SELECT * FROM test HAVING id > 1": "HAVING requires GROUP BY or aggregate function",
            "SELECT * FROM test LIMIT -1": "invalid limit -1",
            "SELECT * FROM test OFFSET 'a'": "invalid offset 'a'",
            "SELECT SUM(COUNT(id)) FROM test": "aggregate functions can't be nested",
            "SELECT foo(id) FROM test": "unknown function foo with 1 arguments",
            "INSERT INTO test (id, id) VALUES (1, 2)": "column id given multiple times",
            "INSERT INTO test (x) VALUES (1)": "unknown column x in table test",
            "INSERT INTO test VALUES (id)": "expression must be constant, found column id",
            "CREATE TABLE t (id INT)": "no primary key for table t",
            "CREATE TABLE t (a INT PRIMARY KEY, b INT PRIMARY KEY)": (
                "multiple primary keys for table t"
            ),
        }
        for statement, expect in cases.items():
            with self.subTest(statement=statement):
                with self.assertRaises(Error) as cm:
                    build(statement)
                self.assertEqual(str(cm.exception), f"invalid input: {expect}")

    def test_serialize(self) -> None:
        """Plans survive a Bincode roundtrip."""
        statements = [
            "CREATE TABLE new (id INT PRIMARY KEY, value STRING DEFAULT 'a')",
            "DROP TABLE IF EXISTS test",
            "DELETE FROM test WHERE id = 1",
            "INSERT INTO test (value, id) VALUES ('a', 1)",
            "UPDATE test SET value = 'x' WHERE value = 'a'",
            "SELECT value, MAX(id), MIN(id), SUM(id), AVG(id), COUNT(*) FROM test "
            "WHERE id > 1 GROUP BY value HAVING MAX(id) > 3 ORDER BY value DESC LIMIT 3 OFFSET 1",
            "SELECT * FROM test RIGHT JOIN other ON other.test_id = test.id AND other.id < 3",
            "SELECT * FROM other WHERE test_id = 1",
            "SELECT * FROM test WHERE FALSE",
        ]
        for statement in statements:
            for plan in (build(statement), build(statement).optimize()):
                with self.subTest(statement=statement, plan=str(plan)):
                    encoded = bincode.serialize(plan)
                    decoded = bincode.deserialize(Plan, encoded)
                    self.assertEqual(decoded, plan)
                    self.assertEqual(str(decoded), str(plan))

    def test_serialize_encoding(self) -> None:
        """Enum variants are encoded by their Rust declaration index."""
        self.assertEqual(
            bincode.serialize(Plan.DropTable(name="a", if_exists=True)), b"\x01\x01a\x01"
        )
        node = Node.Limit(source=Node.Nothing([Label.Unqualified("a")]), limit=3)
        self.assertEqual(bincode.serialize(Plan.Select(node)), b"\x05\x05\x07\x01\x01\x01a\x03")


class TestNode(unittest.TestCase):
    def test_columns(self) -> None:
        plan = build("SELECT value FROM test ORDER BY id")
        assert isinstance(plan, Plan.Select)
        root = plan.root
        self.assertEqual(root.columns(), 1)
        self.assertEqual(root.column_label(0), Label.Qualified("test", "value"))
        self.assertEqual(root.column_label(1), Label.None_())
        assert isinstance(root, Node.Remap)
        self.assertEqual(root.source.columns(), 2)
        self.assertEqual(root.source.column_label(1), Label.Qualified("test", "id"))

    def test_invert_remap(self) -> None:
        self.assertEqual(invert_remap([]), [])
        self.assertEqual(invert_remap([None, None]), [])
        self.assertEqual(invert_remap([2, None, 0]), [2, None, 0])
        self.assertEqual(invert_remap([None, 3, 1]), [None, 2, None, 1])


class TestScope(unittest.TestCase):
    def test_lookup_column(self) -> None:
        scope = Scope.from_table(CATALOG.must_get_table("test"))
        self.assertEqual(scope.lookup_column(None, "value"), 1)
        self.assertEqual(scope.lookup_column("test", "id"), 0)
        with self.assertRaises(Error):
            scope.lookup_column("other", "id")

    def test_remap_hidden(self) -> None:
        scope = Scope.from_table(CATALOG.must_get_table("test"))
        self.assertIsNone(scope.remap_hidden())
        child = scope.spawn()
        child.add_passthrough(scope, 1, False)
        child.add_passthrough(scope, 0, True)
        child.add_passthrough(scope, 1, False)
        self.assertEqual(child.remap_hidden(), [0, None, 1])
        self.assertEqual(child.columns, [Label.Qualified("test", "value")] * 2)
        with self.assertRaises(Error):
            child.lookup_column(None, "id")


if __name__ == "__main__":
    unittest.main()
