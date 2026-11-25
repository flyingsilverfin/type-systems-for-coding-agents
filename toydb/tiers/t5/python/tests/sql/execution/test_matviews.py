"""Tests for toydb.sql.execution.matviews.

Materialized views are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these tests cover
the stored definitions and rows and the checks of the matviews module
directly.
"""

import unittest

from toydb.encoding import bincode
from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.engine.local import ROW, Key, KeyPrefix
from toydb.sql.execution.matviews import (
    check_table_dependencies,
    create_matview,
    drop_matview,
    refresh_matview,
)
from toydb.sql.execution.views import create_view, drop_view
from toydb.sql.parser import Parser, ast
from toydb.sql.types import Value
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.user import ADMIN
from toydb.storage.memory import Memory


def query(sql: str) -> ast.Select:
    """Parses a SELECT statement."""
    statement = Parser.parse(sql)
    assert isinstance(statement, ast.Statement.Select)
    return statement


class TestMatViews(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE a (id INT PRIMARY KEY, v STRING)")
        session.execute("CREATE TABLE b (id INT PRIMARY KEY, a_id INT REFERENCES a)")
        session.execute("INSERT INTO a VALUES (1, 'x'), (2, 'y'), (3, 'x')")
        session.close()
        self.txn = self.engine.begin()

    def tearDown(self) -> None:
        self.txn.rollback()

    def create(self, name: str, columns: list[str] | None, sql: str) -> int:
        """Creates a materialized view in the test transaction as admin,
        returning the number of rows stored."""
        return create_matview(self.txn, name, columns, query(sql), ADMIN)

    def rows(self, name: str) -> list[list[Value]]:
        """Returns the stored rows of a materialized view."""
        return self.txn.get_matview_rows(name)

    def test_definition(self) -> None:
        """The stored definition has the column names, the query's one-line
        canonical text and the names of its FROM clause; the rows are stored
        in the order the query returns them."""
        self.assertEqual(self.create("m", None, "select V, count(*) as N from A group by v order by v desc"), 2)
        self.assertEqual(
            self.txn.get_matview("m"),
            MaterializedView(
                name="m",
                columns=["v", "n"],
                query="SELECT v, count(*) AS n FROM a GROUP BY v ORDER BY v DESC",
                dependencies=["a"],
            ),
        )
        self.assertEqual(
            self.rows("m"),
            [[Value.String("y"), Value.Integer(1)], [Value.String("x"), Value.Integer(2)]],
        )
        self.assertEqual(self.create("n", ["p", "q"], "SELECT b.id, a.id FROM a JOIN b ON TRUE"), 0)
        self.assertEqual(self.rows("n"), [])
        self.assertEqual([m.name for m in self.txn.list_matviews()], ["m", "n"])

    def test_storage(self) -> None:
        """The definition is stored under its own key, the rows under keys by
        name and position."""
        self.create("m", None, "SELECT id FROM a WHERE id > 1")
        definitions = self.txn.txn.scan_prefix(KeyPrefix.MaterializedView().encode())
        self.assertEqual([Key.decode(key) for key, _ in definitions], [Key.MaterializedView("m")])
        rows = [
            (Key.decode(key), bincode.deserialize(ROW, value))
            for key, value in self.txn.txn.scan_prefix(KeyPrefix.MaterializedViewRow("m").encode())
        ]
        self.assertEqual(
            rows,
            [
                (Key.MaterializedViewRow("m", 0), [Value.Integer(2)]),
                (Key.MaterializedViewRow("m", 1), [Value.Integer(3)]),
            ],
        )

    def test_create_errors(self) -> None:
        """CREATE makes the checks of CREATE VIEW, then executes the query."""
        create_view(self.txn, "w", None, query("SELECT id FROM a"))
        self.create("m", None, "SELECT id FROM a")
        cases: list[tuple[str, list[str] | None, str, str]] = [
            ("", None, "SELECT * FROM nosuch", "view name can't be empty"),
            ("a", None, "SELECT * FROM nosuch", "table a already exists"),
            ("w", None, "SELECT * FROM nosuch", "view w already exists"),
            ("m", None, "SELECT * FROM nosuch", "materialized view m already exists"),
            ("x", None, "SELECT * FROM nosuch", "table nosuch does not exist"),
            ("x", ["a"], "SELECT id, v FROM a", "wrong number of column names for view x: expected 2, found 1"),
            ("x", None, "SELECT id, id + 1 FROM a", "column 2 of view x has no name"),
            ("x", None, "SELECT a.id, m.id FROM a, m", "duplicate column id in view x"),
            ("x", None, "SELECT 1 / (id - 2) AS q FROM a", "can't divide by zero"),
        ]
        for name, columns, sql, message in cases:
            with self.subTest(sql=sql, name=name):
                with self.assertRaises(Error) as cm:
                    self.create(name, columns, sql)
                self.assertEqual(str(cm.exception), f"invalid input: {message}")
        self.assertIsNone(self.txn.get_matview("x"))

    def test_namespace(self) -> None:
        """Tables, views and materialized views share one namespace."""
        self.create("m", None, "SELECT id FROM a")
        with self.assertRaises(Error) as cm:
            create_view(self.txn, "m", None, query("SELECT 1 AS one"))
        self.assertEqual(str(cm.exception), "invalid input: materialized view m already exists")
        self.txn.commit()
        session = self.engine.session()
        with self.assertRaises(Error) as cm:
            session.execute("CREATE TABLE m (id INT PRIMARY KEY)")
        self.assertEqual(str(cm.exception), "invalid input: materialized view m already exists")
        session.close()
        self.txn = self.engine.begin()

    def test_refresh(self) -> None:
        """REFRESH executes the stored query again and replaces the rows; when
        the query fails, the stored rows stay."""
        self.create("m", None, "SELECT id, 10 / (4 - id) AS q FROM a")
        self.txn.insert("a", [[Value.Integer(5), Value.String("z")]])
        self.assertEqual(refresh_matview(self.txn, "m", ADMIN), 4)
        self.assertEqual([row[0] for row in self.rows("m")], [Value.Integer(i) for i in (1, 2, 3, 5)])

        self.txn.delete("a", [Value.Integer(1), Value.Integer(2), Value.Integer(5)])
        self.assertEqual(refresh_matview(self.txn, "m", ADMIN), 1)
        self.assertEqual(self.rows("m"), [[Value.Integer(3), Value.Integer(10)]])

        self.txn.insert("a", [[Value.Integer(4), Value.String("z")]])
        with self.assertRaises(Error) as cm:
            refresh_matview(self.txn, "m", ADMIN)
        self.assertEqual(str(cm.exception), "invalid input: can't divide by zero")
        self.assertEqual(self.rows("m"), [[Value.Integer(3), Value.Integer(10)]])

        with self.assertRaises(Error) as cm:
            refresh_matview(self.txn, "nosuch", ADMIN)
        self.assertEqual(str(cm.exception), "invalid input: materialized view nosuch does not exist")

    def test_read_only(self) -> None:
        """A read-only transaction can't create, refresh or drop materialized
        views, also when a refresh would store no rows; the other checks come
        first."""
        self.create("m", None, "SELECT id FROM a WHERE FALSE")
        self.txn.commit()
        self.txn = self.engine.begin_read_only()
        with self.assertRaises(Error.ReadOnly):
            self.create("w", None, "SELECT 1 AS one")
        with self.assertRaises(Error) as cm:
            self.create("w", None, "SELECT 1 / 0 AS one")
        self.assertEqual(str(cm.exception), "invalid input: can't divide by zero")
        with self.assertRaises(Error.ReadOnly):
            refresh_matview(self.txn, "m", ADMIN)
        with self.assertRaises(Error.ReadOnly):
            drop_matview(self.txn, "m", False)
        self.assertFalse(drop_matview(self.txn, "nosuch", True))

    def test_drop(self) -> None:
        """DROP drops a materialized view that no view or materialized view
        depends on, with its rows; views are checked first."""
        self.create("m", None, "SELECT id FROM a")
        self.create("mm", None, "SELECT id FROM m")
        create_view(self.txn, "vm", None, query("SELECT id FROM m"))
        with self.assertRaises(Error) as cm:
            drop_matview(self.txn, "m", False)
        self.assertEqual(str(cm.exception), "invalid input: materialized view m is used by view vm")
        drop_view(self.txn, "vm", False)
        with self.assertRaises(Error) as cm:
            drop_matview(self.txn, "m", False)
        self.assertEqual(
            str(cm.exception), "invalid input: materialized view m is used by materialized view mm"
        )
        with self.assertRaises(Error) as cm:
            drop_matview(self.txn, "a", False)
        self.assertEqual(str(cm.exception), "invalid input: materialized view a does not exist")
        self.assertFalse(drop_matview(self.txn, "a", True))

        self.assertTrue(drop_matview(self.txn, "mm", False))
        self.assertTrue(drop_matview(self.txn, "m", True))
        self.assertIsNone(self.txn.get_matview("m"))
        self.assertEqual(self.rows("m"), [])
        self.assertEqual(self.txn.list_matviews(), [])

    def test_dependencies(self) -> None:
        """A table or view that materialized views depend on reports the first
        materialized view by name, after the views; DROP TABLE checks it after
        the views check."""
        check_table_dependencies(self.txn, "b")
        create_view(self.txn, "va", None, query("SELECT id FROM a"))
        self.create("mb", None, "SELECT * FROM b")
        self.create("Mb", None, "SELECT id FROM b")
        self.create("mv", None, "SELECT * FROM va")
        with self.assertRaises(Error) as cm:
            check_table_dependencies(self.txn, "b")
        self.assertEqual(str(cm.exception), "invalid input: table b is used by materialized view Mb")
        with self.assertRaises(Error) as cm:
            self.txn.drop_table("b", True)
        self.assertEqual(str(cm.exception), "invalid input: table b is used by materialized view Mb")
        with self.assertRaises(Error) as cm:
            drop_view(self.txn, "va", False)
        self.assertEqual(str(cm.exception), "invalid input: view va is used by materialized view mv")

        self.create("ma", None, "SELECT * FROM a")
        with self.assertRaises(Error) as cm:
            self.txn.drop_table("a", False)
        self.assertEqual(str(cm.exception), "invalid input: table a is referenced from b.a_id")

    def test_user(self) -> None:
        """The query is executed as the given user, with its policies."""
        session = self.engine.session()
        session.execute("CREATE USER alice")
        session.execute("CREATE POLICY p ON a TO alice USING (v = 'x')")
        session.close()
        self.txn.rollback()
        self.txn = self.engine.begin()
        self.assertEqual(create_matview(self.txn, "m", None, query("SELECT id FROM a"), "alice"), 2)
        self.assertEqual(refresh_matview(self.txn, "m", ADMIN), 3)
        self.assertEqual(refresh_matview(self.txn, "m", "alice"), 2)


if __name__ == "__main__":
    unittest.main()
