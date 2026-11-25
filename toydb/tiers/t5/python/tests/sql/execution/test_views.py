"""Tests for toydb.sql.execution.views.

Views are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext4` (see `tests/sql/test_mod.py`); these tests cover
the stored definitions and the checks of the views module directly.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution.views import (
    check_table_dependencies,
    create_view,
    dependencies,
    drop_view,
    show_views,
)
from toydb.sql.parser import Parser, ast
from toydb.sql.types import Value
from toydb.sql.types.view import View
from toydb.storage.memory import Memory


def query(sql: str) -> ast.Select:
    """Parses a SELECT statement."""
    statement = Parser.parse(sql)
    assert isinstance(statement, ast.Statement.Select)
    return statement


class TestViews(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE a (id INT PRIMARY KEY, v STRING)")
        session.execute("CREATE TABLE b (id INT PRIMARY KEY, a_id INT REFERENCES a)")
        session.close()
        self.txn = self.engine.begin()

    def tearDown(self) -> None:
        self.txn.rollback()

    def create(self, name: str, columns: list[str] | None, sql: str) -> View:
        """Creates a view in the test transaction, returning its definition."""
        create_view(self.txn, name, columns, query(sql))
        view = self.txn.get_view(name)
        assert view is not None
        return view

    def test_definition(self) -> None:
        """The stored definition has the column names, the query's one-line
        canonical text and the names that its FROM clause reads."""
        view = self.create("v", None, "select B.id, x.v as Name from a join B on b.a_id = a.id, a x")
        self.assertEqual(
            view,
            View(
                name="v",
                columns=["id", "name"],
                query="SELECT b.id, x.v AS name FROM a JOIN b ON b.a_id = a.id, a AS x",
                dependencies=["a", "b"],
            ),
        )

    def test_columns(self) -> None:
        """Columns are named by the column list, or by the result columns."""
        self.assertEqual(self.create("v1", None, "SELECT a.id, v AS x FROM a").columns, ["id", "x"])
        self.assertEqual(self.create("v2", ["p", "q"], "SELECT id, id + 1 FROM a").columns, ["p", "q"])
        self.assertEqual(self.create("v3", None, "SELECT * FROM a").columns, ["id", "v"])
        self.assertEqual(self.create("v4", None, "SELECT v, COUNT(*) AS n FROM a GROUP BY v").columns, ["v", "n"])
        self.assertEqual(self.create("v5", None, "SELECT id FROM v1").columns, ["id"])
        self.assertEqual(self.create("v6", None, "SELECT id FROM a ORDER BY v").columns, ["id"])

    def test_dependencies(self) -> None:
        """Dependencies are the distinct FROM names, in the order in which
        they first occur, including those of nested joins; a query without
        FROM has none."""
        self.assertEqual(
            dependencies(query("SELECT * FROM b JOIN a ON a.id = b.a_id LEFT JOIN b c ON TRUE, z")),
            ["b", "a", "z"],
        )
        self.assertEqual(dependencies(query("SELECT 1")), [])

    def test_system_table_dependencies(self) -> None:
        """The names of system tables are dependencies like any other name."""
        view = self.create("v", None, "SELECT a.id, t.name FROM a JOIN toydb_tables AS t ON TRUE")
        self.assertEqual(view.dependencies, ["a", "toydb_tables"])

    def test_create_errors(self) -> None:
        """CREATE VIEW reports the first failing check, in order."""
        self.create("w", None, "SELECT id FROM a")
        cases: list[tuple[str, list[str] | None, str, str]] = [
            ("", None, "SELECT * FROM nosuch", "view name can't be empty"),
            ("a", None, "SELECT * FROM nosuch", "table a already exists"),
            ("w", None, "SELECT * FROM nosuch", "view w already exists"),
            ("x", ["a"], "SELECT * FROM nosuch", "table nosuch does not exist"),
            ("x", ["a"], "SELECT id, v FROM a", "wrong number of column names for view x: expected 2, found 1"),
            ("x", None, "SELECT id, 1, v + 1 FROM a", "column 2 of view x has no name"),
            ("x", ["a", ""], "SELECT id, v FROM a", "column name can't be empty"),
            ("x", None, 'SELECT id AS "" FROM a', "column name can't be empty"),
            ("x", None, "SELECT a.id, b.id FROM a, b", "duplicate column id in view x"),
            ("x", ["p", "q", "p"], "SELECT id, v, 1 FROM a", "duplicate column p in view x"),
        ]
        for name, columns, sql, message in cases:
            with self.subTest(sql=sql, name=name):
                with self.assertRaises(Error) as cm:
                    create_view(self.txn, name, columns, query(sql))
                self.assertEqual(str(cm.exception), f"invalid input: {message}")

        # Names are compared exactly.
        self.assertEqual(self.create("x", ["n", "N"], "SELECT id, v FROM a").columns, ["n", "N"])

    def test_create_not_optimized(self) -> None:
        """The query is planned but not optimized or executed."""
        self.assertEqual(self.create("v", None, "SELECT 1 / 0 AS x").columns, ["x"])

    def test_read_only(self) -> None:
        """A read-only transaction can't create or drop views, but the other
        checks come first."""
        self.create("v", None, "SELECT id FROM a")
        self.txn.commit()
        self.txn = self.engine.begin_read_only()
        with self.assertRaises(Error.ReadOnly):
            create_view(self.txn, "w", None, query("SELECT 1 AS one"))
        with self.assertRaises(Error) as cm:
            create_view(self.txn, "v", None, query("SELECT 1 AS one"))
        self.assertEqual(str(cm.exception), "invalid input: view v already exists")
        with self.assertRaises(Error.ReadOnly):
            drop_view(self.txn, "v", False)
        self.assertFalse(drop_view(self.txn, "nosuch", True))

    def test_drop(self) -> None:
        """DROP VIEW drops an existing view that no other view depends on."""
        self.create("v", None, "SELECT id FROM a")
        self.create("vv", None, "SELECT id FROM v")
        self.create("vw", None, "SELECT id FROM v")
        with self.assertRaises(Error) as cm:
            drop_view(self.txn, "v", False)
        self.assertEqual(str(cm.exception), "invalid input: view v is used by view vv")
        with self.assertRaises(Error) as cm:
            drop_view(self.txn, "a", False)
        self.assertEqual(str(cm.exception), "invalid input: view a does not exist")
        self.assertFalse(drop_view(self.txn, "a", True))

        self.assertTrue(drop_view(self.txn, "vv", False))
        self.assertTrue(drop_view(self.txn, "vw", True))
        self.assertTrue(drop_view(self.txn, "v", False))
        self.assertIsNone(self.txn.get_view("v"))
        self.assertEqual(self.txn.list_views(), [])

    def test_table_dependencies(self) -> None:
        """A table that views depend on reports the first view by name, and
        DROP TABLE checks it after the foreign key references."""
        check_table_dependencies(self.txn, "a")
        self.create("vb", None, "SELECT * FROM b")
        self.create("Vb", None, "SELECT id FROM b")
        self.create("va", None, "SELECT * FROM a")
        with self.assertRaises(Error) as cm:
            check_table_dependencies(self.txn, "b")
        self.assertEqual(str(cm.exception), "invalid input: table b is used by view Vb")
        with self.assertRaises(Error) as cm:
            self.txn.drop_table("a", False)
        self.assertEqual(str(cm.exception), "invalid input: table a is referenced from b.a_id")
        with self.assertRaises(Error) as cm:
            self.txn.drop_table("b", True)
        self.assertEqual(str(cm.exception), "invalid input: table b is used by view Vb")
        self.assertIsNotNone(self.txn.get_table("b"))

    def test_show(self) -> None:
        """SHOW VIEWS lists the views by name (code points), with the column
        names written to read back, and the stored query."""
        self.create("b_v", ["x", "Y"], "SELECT id, v FROM a")
        self.create("B", None, 'SELECT v AS "view" FROM a')
        self.assertEqual(
            show_views(self.txn),
            [
                [Value.String("B"), Value.String('"view"'), Value.String('SELECT v AS "view" FROM a')],
                [Value.String("b_v"), Value.String('x, "Y"'), Value.String("SELECT id, v FROM a")],
            ],
        )


if __name__ == "__main__":
    unittest.main()
