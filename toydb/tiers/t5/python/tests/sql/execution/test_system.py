"""Tests for toydb.sql.execution.system.

System tables are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these tests cover
the schemas, the language tables, name resolution and the rows directly.
"""

import unittest

from toydb.error import Error, Panic
from toydb.sql.engine import Local
from toydb.sql.execution import Session, StatementResult
from toydb.sql.execution.system import (
    FUNCTIONS,
    TABLES,
    is_system_table,
    keyword_category,
    rows,
    schema,
    spellings,
)
from toydb.sql.parser.lexer import Keyword
from toydb.sql.types import DataType, Value
from toydb.storage.memory import Memory


class TestSystem(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.session: Session = self.engine.session()

    def tearDown(self) -> None:
        self.session.close()

    def query(self, sql: str) -> list[list[Value]]:
        """Executes a query in the session and returns its rows."""
        result = self.session.execute(sql)
        assert isinstance(result, StatementResult.Select)
        return result.rows

    def test_schema(self) -> None:
        """System tables have the columns of TABLES, named by the table, and
        other names have no schema."""
        table = schema("toydb_policies")
        assert table is not None
        self.assertEqual(table.name, "toydb_policies")
        self.assertEqual(
            [(c.name, c.datatype, c.nullable) for c in table.columns],
            [
                ("table_name", DataType.String, False),
                ("name", DataType.String, False),
                ("grantee", DataType.String, True),
                ("condition", DataType.String, False),
                ("check_condition", DataType.String, True),
            ],
        )
        for name in TABLES:
            with self.subTest(name=name):
                table = schema(name)
                assert table is not None
                self.assertTrue(name.startswith("toydb_") or name == "pg_type")
                self.assertTrue(table.columns)
        self.assertIsNone(schema("toydb_nosuch"))
        self.assertIsNone(schema("TOYDB_TABLES"))

    def test_is_system_table(self) -> None:
        """A table or view of the same name comes before a system table."""
        txn = self.engine.begin()
        self.assertTrue(is_system_table(txn, "toydb_tables"))
        self.assertFalse(is_system_table(txn, "toydb_nosuch"))
        txn.rollback()
        self.session.execute("CREATE TABLE toydb_tables (id INT PRIMARY KEY)")
        self.session.execute("CREATE VIEW toydb_users AS SELECT 1 AS x")
        self.session.execute("CREATE MATERIALIZED VIEW toydb_views AS SELECT 1 AS x")
        txn = self.engine.begin()
        self.assertFalse(is_system_table(txn, "toydb_tables"))
        self.assertFalse(is_system_table(txn, "toydb_users"))
        self.assertFalse(is_system_table(txn, "toydb_views"))
        self.assertTrue(is_system_table(txn, "toydb_columns"))
        txn.rollback()

    def test_keyword_category(self) -> None:
        """The category of a keyword is the first that applies."""
        cases = [
            (Keyword.Select, "statement"),
            (Keyword.Set, "statement"),
            (Keyword.Reset, "statement"),
            (Keyword.Grant, "statement"),
            (Keyword.Delete, "statement"),
            (Keyword.Varchar, "type"),
            (Keyword.Double, "type"),
            (Keyword.String, "type"),
            (Keyword.Null, "literal"),
            (Keyword.NaN, "literal"),
            (Keyword.True_, "literal"),
            (Keyword.Is, "operator"),
            (Keyword.Not, "operator"),
            (Keyword.Like, "operator"),
            (Keyword.From, "reserved"),
            (Keyword.Using, "reserved"),
            (Keyword.Grants, "reserved"),
            (Keyword.Key, "reserved"),
        ]
        for keyword, category in cases:
            with self.subTest(keyword=keyword):
                self.assertEqual(keyword_category(keyword), category)

    def test_keywords(self) -> None:
        """toydb_keywords has every keyword, in upper case, by word."""
        keywords = self.query("SELECT * FROM toydb_keywords")
        words = [row[0].try_into_string() for row in keywords]
        self.assertEqual(len(words), len(Keyword))
        self.assertEqual(words, sorted(str(keyword) for keyword in Keyword))
        self.assertEqual(keywords[0], [Value.String("ANALYZE"), Value.String("statement")])

    def test_functions(self) -> None:
        """toydb_functions has every function, by name."""
        self.assertEqual(
            self.query("SELECT * FROM toydb_functions"),
            [
                [Value.String(name), Value.String(kind), Value.Integer(arguments)]
                for name, kind, arguments in sorted(FUNCTIONS)
            ],
        )
        self.assertIn(("sqrt", "scalar", 1), FUNCTIONS)

    def test_types(self) -> None:
        """toydb_types has every data type, by backup tag, with its sorted
        spellings and its PostgreSQL type."""
        self.assertEqual(spellings(DataType.String), ["STRING", "TEXT", "VARCHAR"])
        self.assertEqual(spellings(DataType.Float), ["DOUBLE", "FLOAT"])
        types = self.query("SELECT name, backup_tag FROM toydb_types")
        self.assertEqual(len(types), len(DataType))
        self.assertEqual(
            types,
            [
                [Value.String(str(datatype)), Value.Integer(tag)]
                for tag, datatype in enumerate(DataType, start=1)
            ],
        )
        self.assertEqual(
            self.query("SELECT pg_type, pg_oid FROM toydb_types WHERE name = 'FLOAT'"),
            [[Value.String("float8"), Value.Integer(701)]],
        )

    def test_pg_type(self) -> None:
        """pg_type has the PostgreSQL types of the wire protocol, by OID."""
        oids = [row[0].try_into_i64() for row in self.query("SELECT oid FROM pg_type")]
        self.assertEqual(oids, [16, 20, 21, 23, 25, 700, 701, 1043])

    def test_catalog(self) -> None:
        """The catalog tables show what the statement's transaction sees."""
        self.session.execute("CREATE TABLE b (id STRING PRIMARY KEY, n INT DEFAULT 3 INDEX)")
        self.session.execute("CREATE TABLE a (x BOOLEAN PRIMARY KEY, CHECK (x))")
        self.session.execute("BEGIN")
        self.session.execute("CREATE TABLE c (id INT PRIMARY KEY)")
        self.assertEqual(
            self.query("SELECT * FROM toydb_tables"),
            [
                [Value.String("a"), Value.String("admin"), Value.String("x")]
                + [Value.Integer(1), Value.Integer(1), Value.Integer(0)],
                [Value.String("b"), Value.String("admin"), Value.String("id")]
                + [Value.Integer(2), Value.Integer(0), Value.Integer(0)],
                [Value.String("c"), Value.String("admin"), Value.String("id")]
                + [Value.Integer(1), Value.Integer(0), Value.Integer(0)],
            ],
        )
        self.session.execute("ROLLBACK")
        self.assertEqual(len(self.query("SELECT * FROM toydb_tables")), 2)
        self.assertEqual(
            self.query("SELECT * FROM toydb_columns WHERE table_name = 'b' AND name = 'n'"),
            [
                [Value.String("b"), Value.String("n"), Value.Integer(2), Value.String("INTEGER")]
                + [Value.Boolean(True), Value.String("3"), Value.Boolean(False)]
                + [Value.Boolean(False), Value.Boolean(True), Value.Null()]
            ],
        )
        self.assertEqual(
            self.query("SELECT * FROM toydb_checks"),
            [[Value.String("a"), Value.String("a_check1"), Value.String("x")]],
        )

    def test_matviews(self) -> None:
        """toydb_matviews has one row per materialized view, by name, with
        its owner, columns, query, dependencies and number of stored rows."""
        self.session.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        self.session.execute("INSERT INTO t VALUES (1), (2)")
        self.session.execute("CREATE USER alice SUPERUSER")
        self.session.execute("SET ROLE alice")
        self.session.execute('CREATE MATERIALIZED VIEW m ("Id") AS SELECT t.id FROM t JOIN toydb_users ON TRUE')
        self.session.execute("RESET ROLE")
        self.session.execute("CREATE MATERIALIZED VIEW a AS SELECT 1 AS one")
        self.assertEqual(
            self.query("SELECT * FROM toydb_matviews"),
            [
                [Value.String("a"), Value.String("admin"), Value.String("one")]
                + [Value.String("SELECT 1 AS one"), Value.String(""), Value.Integer(1)],
                [Value.String("m"), Value.String("alice"), Value.String('"Id"')]
                + [Value.String("SELECT t.id FROM t JOIN toydb_users ON TRUE")]
                + [Value.String("t, toydb_users"), Value.Integer(4)],
            ],
        )

    def test_statistics(self) -> None:
        """toydb_statistics has the columns of analyzed tables, with their
        minimum and maximum as displayed in query results."""
        self.session.execute("CREATE TABLE t (id INT PRIMARY KEY, s STRING)")
        self.session.execute("CREATE TABLE u (id INT PRIMARY KEY)")
        self.session.execute("INSERT INTO t VALUES (1, NULL), (2, NULL)")
        self.session.execute("ANALYZE t")
        self.assertEqual(
            self.query("SELECT * FROM toydb_statistics"),
            [
                [Value.String("t"), Value.String("id"), Value.Integer(2), Value.Integer(0)]
                + [Value.Integer(2), Value.String("1"), Value.String("2")],
                [Value.String("t"), Value.String("s"), Value.Integer(2), Value.Integer(2)]
                + [Value.Integer(0), Value.Null(), Value.Null()],
            ],
        )

    def test_session_tables(self) -> None:
        """toydb_prepared and toydb_statements are the reading session's;
        toydb_audit requires a superuser."""
        other = self.engine.session()
        other.execute("PREPARE p AS SELECT 1")
        self.session.execute("PREPARE q AS SELECT 2")
        self.assertEqual(
            [row[0] for row in self.query("SELECT * FROM toydb_prepared")], [Value.String("q")]
        )
        self.assertEqual(
            self.query("SELECT digest, calls FROM toydb_statements"),
            [
                [Value.String("PREPARE q AS SELECT ?"), Value.Integer(1)],
                [Value.String("SELECT * FROM toydb_prepared"), Value.Integer(1)],
            ],
        )
        self.assertEqual(
            self.query("SELECT session, executed FROM toydb_sessions"),
            [[Value.Integer(1), Value.Integer(3)], [Value.Integer(2), Value.Integer(1)]],
        )
        self.assertEqual(len(self.query("SELECT * FROM toydb_audit")), 5)
        other.execute("CREATE USER alice")
        other.execute("SET ROLE alice")
        with self.assertRaises(Error) as cm:
            other.execute("SELECT * FROM toydb_audit")
        self.assertEqual(str(cm.exception), "permission denied: user alice is not a superuser")
        other.close()

    def test_rows_unknown_table(self) -> None:
        """Computing the rows of a name that isn't a system table panics."""
        txn = self.engine.begin_read_only()
        with self.assertRaises(Panic):
            rows("toydb_nosuch", txn, self.session)
        txn.rollback()


if __name__ == "__main__":
    unittest.main()
