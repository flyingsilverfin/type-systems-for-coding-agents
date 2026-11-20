"""Tests for the extension statements of toydb.sql.execution.session.

The session is mostly covered by the SQL golden scripts (see
`tests/sql/test_mod.py`); these tests cover the statement statistics, the
transactions used by the extension statements, and the encoding of the
extension results.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution import StatementResult
from toydb.sql.parser import ast
from toydb.sql.types import Label, Value
from toydb.storage.memory import Memory


class TestSession(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.session = self.engine.session()
        self.session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING)")

    def tearDown(self) -> None:
        self.session.close()

    def statements(self) -> list[tuple[str, int, int, int]]:
        """Returns the SHOW STATEMENTS rows as native values."""
        result = self.session.execute("SHOW STATEMENTS")
        assert isinstance(result, StatementResult.Select)
        self.assertEqual([str(column) for column in result.columns], ["digest", "calls", "errors", "rows"])
        return [
            (row[0].try_into_string(), row[1].try_into_i64(), row[2].try_into_i64(), row[3].try_into_i64())
            for row in result.rows
        ]

    def test_statement_statistics(self) -> None:
        """Statements are recorded after execution, failed ones as errors.
        Parse errors and SHOW STATEMENTS are not recorded."""
        self.session.execute("IMPORT INTO t FROM CSV 'id,v', '1,a', '2,b'")
        self.session.execute("select * from T where id = 1")
        self.session.execute("SELECT * FROM t WHERE ID = 2")
        with self.assertRaises(Error):
            self.session.execute("SELECT * FROM nosuch")
        with self.assertRaises(Error):
            self.session.execute("SELECT FROM t")
        self.session.execute("EXPORT t AS SQL")
        self.assertEqual(
            self.statements(),
            [
                ("CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING)", 1, 0, 0),
                ("IMPORT INTO t FROM CSV ?, ?, ?", 1, 0, 2),
                ("SELECT * FROM t WHERE id = ?", 2, 0, 2),
                ("SELECT * FROM nosuch", 1, 1, 0),
                ("EXPORT t AS SQL", 1, 0, 2),
            ],
        )

    def test_sessions(self) -> None:
        """Each session has its own statistics."""
        other = self.engine.session()
        other.execute("SELECT 1")
        self.assertEqual(self.statements(), [("CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING)", 1, 0, 0)])
        other.close()

    def test_statistics_transactions(self) -> None:
        """ANALYZE writes in the session's transaction, and ROLLBACK discards
        the statistics. A read-only transaction can't analyze."""
        self.session.execute("INSERT INTO t VALUES (1, 'abc')")
        self.session.execute("BEGIN")
        self.assertEqual(self.session.execute("ANALYZE"), StatementResult.Analyze(["t"]))
        self.assertEqual(self.session.execute("SHOW STATISTICS t").try_into_row()[1], Value.Integer(1))
        self.session.execute("ROLLBACK")
        self.assertEqual(self.session.execute("SHOW STATISTICS t").try_into_row()[1], Value.Null())

        self.session.execute("BEGIN READ ONLY")
        with self.assertRaises(Error.ReadOnly):
            self.session.execute("ANALYZE t")
        self.session.execute("ROLLBACK")

        self.session.execute("ANALYZE t")
        result = self.session.execute("SHOW STATISTICS t")
        assert isinstance(result, StatementResult.Select)
        self.assertEqual([str(column) for column in result.columns][:3], ["column", "rows", "nulls"])
        self.assertEqual(
            result.rows[1],
            [
                Value.String("v"),
                Value.Integer(1),
                Value.Integer(0),
                Value.Integer(1),
                Value.String("abc"),
                Value.String("abc"),
                Value.String("abc"),
                Value.Integer(1),
                Value.Float(3.0),
            ],
        )

        # DROP TABLE removes the statistics.
        self.session.execute("DROP TABLE t")
        self.session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING)")
        self.assertEqual(self.session.execute("SHOW STATISTICS t").try_into_row()[1], Value.Null())

    def test_explain(self) -> None:
        """The extension statements can't be explained."""
        cases = [
            ("SHOW STATEMENTS", "SHOW"),
            ("SHOW STATISTICS t", "SHOW"),
            ("ANALYZE", "ANALYZE"),
            ("EXPORT t AS CSV", "EXPORT"),
            ("IMPORT INTO t FROM CSV 'id'", "IMPORT"),
        ]
        for statement, keyword in cases:
            with self.subTest(statement=statement), self.assertRaises(Error) as cm:
                self.session.execute(f"EXPLAIN {statement}")
            self.assertEqual(str(cm.exception), f"invalid input: EXPLAIN is not supported for {keyword}")

    def test_import_atomic(self) -> None:
        """A failing import inserts nothing."""
        with self.assertRaises(Error) as cm:
            self.session.execute("""IMPORT INTO t FROM JSON '{"id":1}', '{"id":1}'""")
        self.assertEqual(str(cm.exception), "invalid input: primary key 1 already exists")
        self.assertEqual(self.session.execute("SELECT COUNT(*) FROM t").try_into_i64(), 0)


class TestStatementResult(unittest.TestCase):
    def test_encoding(self) -> None:
        """The extension results are encoded after the existing variants."""
        cases: list[tuple[StatementResult, str]] = [
            (StatementResult.Analyze(["a", "b"]), "0a0201610162"),
            (StatementResult.Export(ast.ExportFormat.Json, ["{}"]), "0b0101027b7d"),
            (StatementResult.Select([Label.Unqualified("x")], []), "090101017800"),
        ]
        for result, expect in cases:
            with self.subTest(result=result):
                encoded = result.encode()
                self.assertEqual(encoded.hex(), expect)
                self.assertEqual(StatementResult.decode(encoded), result)


if __name__ == "__main__":
    unittest.main()
