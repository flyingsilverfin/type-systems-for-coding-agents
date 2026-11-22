"""Tests for toydb.sql.execution.prepared.

Prepared statements are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext4` (see `tests/sql/test_mod.py`); these tests cover
the checks, the binding of arguments and the plan cache directly.
"""

import math
import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution.prepared import (
    PreparedStatement,
    PreparedStatements,
    RecordingCatalog,
    identical,
)
from toydb.sql.parser import Parser, ast
from toydb.sql.planner import Plan
from toydb.sql.types import DataType, Value
from toydb.storage.memory import Memory


def expressions(sql: str) -> list[ast.Expression]:
    """Parses the arguments of an EXECUTE statement."""
    statement = Parser.parse(f"EXECUTE p ({sql})")
    assert isinstance(statement, ast.Statement.Execute)
    return statement.args


class TestPreparedStatements(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING)")
        session.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        session.execute("CREATE VIEW tv AS SELECT v FROM t WHERE id = 1")
        session.close()
        self.prepared = PreparedStatements()

    def prepare(self, name: str, types: list[DataType], sql: str) -> PreparedStatement:
        """Prepares a statement, returning it."""
        self.prepared.prepare(name, types, Parser.parse(sql))
        return self.prepared.get(name)

    def execute(self, prepared: PreparedStatement, args: str) -> Plan:
        """Binds the arguments and plans the statement as EXECUTE does,
        returning the plan."""
        statement, arguments = prepared.bind(expressions(args) if args else [])
        txn = self.engine.begin()
        try:
            return prepared.plan(txn, statement, arguments)
        finally:
            txn.rollback()

    def test_prepare(self) -> None:
        """PREPARE checks the name, the statement and the parameter types, in
        that order."""
        self.prepare("p", [DataType.Integer], "SELECT $1")
        cases = [
            ("p", [], "BEGIN", "prepared statement p already exists"),
            ("q", [], "BEGIN", "PREPARE is not supported for BEGIN"),
            ("q", [], "SHOW PREPARED", "PREPARE is not supported for SHOW"),
            ("q", [], "CREATE VIEW v AS SELECT 1 AS a", "PREPARE is not supported for CREATE VIEW"),
            ("q", [DataType.Integer], "SELECT $1, $3, $2", "no type given for parameter $2"),
            ("q", [], "SELECT * FROM t WHERE id = $1", "no type given for parameter $1"),
        ]
        for name, types, sql, message in cases:
            with self.subTest(sql=sql), self.assertRaises(Error) as cm:
                self.prepared.prepare(name, types, Parser.parse(sql))
            self.assertEqual(str(cm.exception), f"invalid input: {message}")

        # Types without a parameter are allowed, and the statement isn't
        # checked against the database.
        self.prepare("q", [DataType.Integer, DataType.String], "SELECT $1")
        self.prepare("bad", [], "SELECT nosuch FROM nowhere")
        self.assertEqual(sorted(self.prepared.statements), ["bad", "p", "q"])

    def test_deallocate(self) -> None:
        """DEALLOCATE removes a prepared statement, which can then be
        prepared again."""
        self.prepare("p", [], "SELECT 1")
        self.prepared.deallocate("p")
        for f in (self.prepared.get, self.prepared.deallocate):
            with self.assertRaises(Error) as cm:
                f("p")
            self.assertEqual(str(cm.exception), "invalid input: prepared statement p does not exist")
        self.prepare("p", [DataType.Integer], "SELECT $1")

    def test_bind(self) -> None:
        """Binding evaluates the arguments and replaces every parameter by
        the literal of its value, also in join predicates and DEFAULTs."""
        prepared = self.prepare(
            "p",
            [DataType.Integer, DataType.Float, DataType.String],
            "SELECT $1 FROM t JOIN t AS u ON u.id = $1 WHERE v = $3 OR $2 IS NOT NAN LIMIT $1",
        )
        statement, values = prepared.bind(expressions("1 + 1, NAN, 'x'"))
        self.assertEqual(values, [Value.Integer(2), Value.Float(math.nan), Value.String("x")])
        self.assertEqual(
            statement,
            Parser.parse(
                "SELECT 2 FROM t JOIN t AS u ON u.id = 2 WHERE v = 'x' OR NAN IS NOT NAN LIMIT 2"
            ),
        )

        # A negative value is bound as the literal of the value.
        statement, _ = self.prepare("n", [DataType.Integer], "SELECT $1").bind(
            expressions("-9223372036854775807 - 1")
        )
        assert isinstance(statement, ast.Statement.Select)
        literal = ast.Expression.Literal(ast.Literal.Integer(-9223372036854775808))
        self.assertEqual(statement.select, [(literal, None)])

        create = self.prepare(
            "mk", [DataType.String], "CREATE TABLE x (id INT PRIMARY KEY, s STRING DEFAULT $1)"
        )
        statement, _ = create.bind(expressions("'it''s'"))
        self.assertEqual(
            statement, Parser.parse("CREATE TABLE x (id INT PRIMARY KEY, s STRING DEFAULT 'it''s')")
        )

    def test_bind_errors(self) -> None:
        """Arguments are counted, then evaluated and type-checked in order.
        NULL is accepted for every type; there are no conversions."""
        prepared = self.prepare("p", [DataType.Integer, DataType.Float], "SELECT $1, $2")
        cases = [
            ("1", "wrong number of arguments for prepared statement p: expected 2, found 1"),
            ("1, 2, 3", "wrong number of arguments for prepared statement p: expected 2, found 3"),
            ("1.0, 1.0", "invalid datatype FLOAT for INTEGER parameter $1"),
            ("1, 1", "invalid datatype INTEGER for FLOAT parameter $2"),
            ("'x', 1 / 0", "invalid datatype STRING for INTEGER parameter $1"),
            ("1 / 0, 'x'", "can't divide by zero"),
            ("x, 1.0", "expression must be constant, found column x"),
            ("NULL IS NOT NULL, 1.0", "invalid datatype BOOLEAN for INTEGER parameter $1"),
        ]
        for args, message in cases:
            with self.subTest(args=args), self.assertRaises(Error) as cm:
                prepared.bind(expressions(args))
            self.assertEqual(str(cm.exception), f"invalid input: {message}")
        _, values = prepared.bind(expressions("NULL, NULL"))
        self.assertEqual(values, [Value.Null(), Value.Null()])
        with self.assertRaises(Error) as cm:
            self.prepare("q", [], "SELECT 1").bind(expressions("1"))
        self.assertEqual(
            str(cm.exception),
            "invalid input: wrong number of arguments for prepared statement q: expected 0, found 1",
        )

    def test_cache(self) -> None:
        """The cached plan is used for identical arguments while the tables it
        used are unchanged; a failed planning keeps it."""
        prepared = self.prepare("get", [DataType.Integer], "SELECT v FROM t WHERE id = $1")
        first = self.execute(prepared, "1")
        self.assertIs(self.execute(prepared, "1"), first)
        self.assertIsNot(self.execute(prepared, "2"), first)
        self.assertEqual(prepared.plans, 2)
        assert prepared.cached is not None
        self.assertEqual(prepared.cached.arguments, [Value.Integer(2)])
        self.assertEqual(list(prepared.cached.tables), ["t"])

        # Another definition of the table, or none, plans anew; the failed
        # planning keeps the cached plan.
        cached = prepared.cached
        session = self.engine.session()
        session.execute("DROP VIEW tv")
        session.execute("DROP TABLE t")
        with self.assertRaises(Error):
            self.execute(prepared, "2")
        self.assertIs(prepared.cached, cached)
        self.assertEqual(prepared.plans, 3)
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING NOT NULL)")
        self.execute(prepared, "2")
        self.assertEqual(prepared.plans, 4)
        session.execute("DROP TABLE t")
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING NOT NULL)")
        self.execute(prepared, "2")
        self.assertEqual(prepared.plans, 4)
        session.close()

    def test_cache_views(self) -> None:
        """A plan that uses a view depends on the view's definition and on
        the tables of its query."""
        prepared = self.prepare("pv", [], "SELECT * FROM tv")
        self.execute(prepared, "")
        assert prepared.cached is not None
        self.assertEqual(list(prepared.cached.views), ["tv"])
        self.assertEqual(set(prepared.cached.tables), {"t", "tv"})
        self.execute(prepared, "")
        self.assertEqual(prepared.plans, 1)

        session = self.engine.session()
        session.execute("DROP VIEW tv")
        session.execute("CREATE VIEW tv AS SELECT v FROM t WHERE id = 2")
        self.execute(prepared, "")
        self.assertEqual(prepared.plans, 2)
        session.execute("DROP VIEW tv")
        with self.assertRaises(Error) as cm:
            self.execute(prepared, "")
        self.assertEqual(str(cm.exception), "invalid input: table tv does not exist")
        self.assertEqual(prepared.plans, 3)
        session.close()

    def test_recording_catalog(self) -> None:
        """The recording catalog records every table and view lookup,
        including names that don't exist."""
        txn = self.engine.begin_read_only()
        catalog = RecordingCatalog(txn)
        Plan.build(Parser.parse("SELECT * FROM t, tv"), catalog)
        self.assertEqual(catalog.tables, {"t": txn.get_table("t"), "tv": None})
        self.assertEqual(catalog.views, {"tv": txn.get_view("tv")})
        txn.rollback()

    def test_identical(self) -> None:
        """Arguments are identical if they have the same type and are equal,
        with NULL identical to NULL and NaN to NaN, but not 0.0 to -0.0."""
        self.assertTrue(identical(Value.Null(), Value.Null()))
        self.assertTrue(identical(Value.Float(math.nan), Value.Float(math.nan)))
        self.assertTrue(identical(Value.String("a"), Value.String("a")))
        self.assertFalse(identical(Value.Integer(1), Value.Float(1.0)))
        self.assertFalse(identical(Value.Integer(1), Value.Integer(2)))
        self.assertFalse(identical(Value.Null(), Value.Boolean(False)))
        self.assertTrue(identical(Value.Float(-math.nan), Value.Float(math.nan)))
        self.assertFalse(identical(Value.Float(0.0), Value.Float(-0.0)))

    def test_rows(self) -> None:
        """SHOW PREPARED lists the statements by name (code points), with
        their types, canonical text and counts."""
        self.prepare("b", [DataType.Integer, DataType.String], "select * from T where ID = $1")
        self.prepare("B", [], "SELECT 1")
        self.prepare("a", [DataType.Boolean], "DELETE FROM t WHERE $1")
        self.execute(self.prepared.get("b"), "1, 'x'")
        self.execute(self.prepared.get("b"), "1, 'x'")
        self.prepared.get("b").executions = 2
        self.assertEqual(
            self.prepared.rows(),
            [
                [
                    Value.String("B"),
                    Value.String(""),
                    Value.String("SELECT 1"),
                    Value.Integer(0),
                    Value.Integer(0),
                ],
                [
                    Value.String("a"),
                    Value.String("BOOLEAN"),
                    Value.String("DELETE FROM t WHERE $1"),
                    Value.Integer(0),
                    Value.Integer(0),
                ],
                [
                    Value.String("b"),
                    Value.String("INTEGER, STRING"),
                    Value.String("SELECT * FROM t WHERE id = $1"),
                    Value.Integer(2),
                    Value.Integer(1),
                ],
            ],
        )


if __name__ == "__main__":
    unittest.main()
