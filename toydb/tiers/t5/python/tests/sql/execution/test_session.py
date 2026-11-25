"""Tests for the extension statements of toydb.sql.execution.session.

The session is mostly covered by the SQL golden scripts (see
`tests/sql/test_mod.py`); these tests cover the statement statistics, the
transactions used by the extension statements, and the encoding of the
extension results.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.engine.local import Transaction
from toydb.sql.execution import Session, StatementResult
from toydb.sql.parser import Parser, ast
from toydb.sql.types import DataType, Label, Value
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
            ("FORMAT SELECT 1", "FORMAT"),
            ("CREATE VIEW v AS SELECT 1 AS a", "CREATE VIEW"),
            ("DROP VIEW v", "DROP VIEW"),
            ("SHOW VIEWS", "SHOW"),
            ("DESCRIBE t", "DESCRIBE"),
            ("DESCRIBE SELECT 1", "DESCRIBE"),
            ("ESTIMATE SELECT 1", "ESTIMATE"),
            ("PREPARE p AS SELECT 1", "PREPARE"),
            ("DEALLOCATE p", "DEALLOCATE"),
            ("SHOW PREPARED", "SHOW"),
        ]
        for statement, keyword in cases:
            with self.subTest(statement=statement), self.assertRaises(Error) as cm:
                self.session.execute(f"EXPLAIN {statement}")
            self.assertEqual(str(cm.exception), f"invalid input: EXPLAIN is not supported for {keyword}")

    def test_format(self) -> None:
        """FORMAT returns the statement's lines without executing it, and
        uses no transaction, also inside a read-only one."""
        self.session.execute("BEGIN READ ONLY")
        result = self.session.execute("FORMAT insert into t values (1, 'a')")
        self.assertEqual(result, StatementResult.Format(["INSERT INTO t VALUES", "  (1, 'a')"]))
        self.session.execute("COMMIT")
        self.assertEqual(self.session.execute("SELECT COUNT(*) FROM t").try_into_i64(), 0)
        self.assertEqual(
            self.session.execute("FORMAT SELECT * FROM nosuch"),
            StatementResult.Format(["SELECT *", "FROM nosuch"]),
        )

    def test_views(self) -> None:
        """The view statements run in the session's transaction, or an
        implicit one: read-write for CREATE and DROP VIEW (also when they
        fail), read-only for SHOW VIEWS."""
        version = self.session.execute("BEGIN").state.version  # type: ignore[attr-defined]
        self.session.execute("ROLLBACK")
        self.assertEqual(
            self.session.execute("CREATE VIEW v AS SELECT id FROM t"), StatementResult.CreateView("v")
        )
        with self.assertRaises(Error):
            self.session.execute("CREATE VIEW v AS SELECT id FROM t")
        result = self.session.execute("SHOW VIEWS")
        assert isinstance(result, StatementResult.Select)
        self.assertEqual([str(column) for column in result.columns], ["name", "columns", "query"])
        self.assertEqual(
            result.rows, [[Value.String("v"), Value.String("id"), Value.String("SELECT id FROM t")]]
        )
        self.assertEqual(self.session.execute("DROP VIEW IF EXISTS x"), StatementResult.DropView("x", False))
        self.assertEqual(self.session.execute("BEGIN").state.version, version + 4)  # type: ignore[attr-defined]

        # ROLLBACK undoes DROP VIEW.
        self.assertEqual(self.session.execute("DROP VIEW v"), StatementResult.DropView("v", True))
        self.assertEqual(self.session.execute("SHOW VIEWS").row_count(), 0)
        self.session.execute("ROLLBACK")
        self.assertEqual(self.session.execute("SELECT COUNT(*) FROM v").try_into_i64(), 0)
        self.assertEqual(self.session.execute("SHOW VIEWS").row_count(), 1)

    def test_describe(self) -> None:
        """DESCRIBE runs in the session's transaction, or an implicit
        read-only one, and describes planned statements without executing
        them."""
        version = self.session.execute("BEGIN").state.version  # type: ignore[attr-defined]
        self.session.execute("ROLLBACK")
        result = self.session.execute("DESCRIBE t")
        assert isinstance(result, StatementResult.Select)
        self.assertEqual([str(column) for column in result.columns], ["column", "type", "nullable"])
        self.assertEqual(
            result.rows,
            [
                [Value.String("id"), Value.String("INTEGER"), Value.Boolean(False)],
                [Value.String("v"), Value.String("STRING"), Value.Boolean(True)],
            ],
        )
        self.assertEqual(
            self.session.execute("DESCRIBE INSERT INTO t VALUES (1, 'a')"),
            StatementResult.Select(result.columns, []),
        )
        self.assertEqual(self.session.execute("BEGIN").state.version, version + 1)  # type: ignore[attr-defined]
        self.session.execute("CREATE TABLE u (id INT PRIMARY KEY)")
        self.assertEqual(self.session.execute("DESCRIBE SELECT * FROM u").row_count(), 1)
        self.session.execute("ROLLBACK")
        self.assertEqual(self.session.execute("SELECT COUNT(*) FROM t").try_into_i64(), 0)

        # Only planned statements can be described.
        for statement, keyword in [("BEGIN", "BEGIN"), ("DROP VIEW v", "DROP VIEW"), ("DESCRIBE t", "DESCRIBE")]:
            with self.subTest(statement=statement), self.assertRaises(Error) as cm:
                self.session.execute(f"DESCRIBE {statement}")
            self.assertEqual(str(cm.exception), f"invalid input: DESCRIBE is not supported for {keyword}")

    def test_describe_columns(self) -> None:
        """describe_columns() describes a planned statement with its column
        labels, without executing or recording it."""
        statement = Parser.parse("SELECT id, v, id + 1 FROM t")
        columns = self.session.describe_columns(statement)
        self.assertEqual(
            [(label, description.datatype) for label, description in columns],
            [
                (Label.Qualified("t", "id"), DataType.Integer),
                (Label.Qualified("t", "v"), DataType.String),
                (Label.None_(), DataType.Integer),
            ],
        )
        self.session.execute("INSERT INTO t VALUES (1, 'a')")
        self.assertEqual(self.session.describe_columns(Parser.parse("DELETE FROM t")), [])
        self.assertEqual(self.session.execute("SELECT COUNT(*) FROM t").try_into_i64(), 1)
        self.assertEqual(self.session.executed, 3)
        with self.assertRaises(Error) as cm:
            self.session.describe_columns(Parser.parse("BEGIN"))
        self.assertEqual(str(cm.exception), "invalid input: DESCRIBE is not supported for BEGIN")

    def test_execute_parsed(self) -> None:
        """execute_parsed() executes and records a parsed statement like
        execute()."""
        result = self.session.execute_parsed(Parser.parse("INSERT INTO t VALUES (1, 'a')"))
        self.assertEqual(result, StatementResult.Insert(1))
        self.assertEqual(self.session.executed, 2)
        self.assertIn(("INSERT INTO t VALUES (?, ?)", 1, 0, 1), self.statements())

    def test_estimate(self) -> None:
        """ESTIMATE runs in the session's transaction, or an implicit
        read-only one, and estimates planned statements without executing
        them."""
        version = self.session.execute("BEGIN").state.version  # type: ignore[attr-defined]
        self.session.execute("ROLLBACK")
        result = self.session.execute("ESTIMATE SELECT v FROM t")
        assert isinstance(result, StatementResult.Select)
        self.assertEqual([str(column) for column in result.columns], ["node", "rows"])
        self.assertEqual(
            result.rows,
            [
                [Value.String("Projection: t.v"), Value.Integer(1000)],
                [Value.String("└─ Scan: t"), Value.Integer(1000)],
            ],
        )
        self.assertEqual(
            self.session.execute("ESTIMATE INSERT INTO t VALUES (1, 'a')").row_count(), 2
        )
        self.assertEqual(self.session.execute("BEGIN").state.version, version + 1)  # type: ignore[attr-defined]
        self.session.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        self.session.execute("ANALYZE t")
        result = self.session.execute("ESTIMATE SELECT * FROM t")
        self.assertEqual(result.try_into_row(), [Value.String("Scan: t"), Value.Integer(2)])
        self.session.execute("ROLLBACK")
        self.assertEqual(self.session.execute("SELECT COUNT(*) FROM t").try_into_i64(), 0)
        result = self.session.execute("ESTIMATE SELECT * FROM t")
        self.assertEqual(result.try_into_row(), [Value.String("Scan: t"), Value.Integer(1000)])

        # Only planned statements can be estimated.
        for statement, keyword in [("BEGIN", "BEGIN"), ("SHOW VIEWS", "SHOW"), ("ESTIMATE SELECT 1", "ESTIMATE")]:
            with self.subTest(statement=statement), self.assertRaises(Error) as cm:
                self.session.execute(f"ESTIMATE {statement}")
            self.assertEqual(str(cm.exception), f"invalid input: ESTIMATE is not supported for {keyword}")

    def test_prepared(self) -> None:
        """PREPARE and DEALLOCATE use no transaction and aren't undone by
        ROLLBACK; EXECUTE runs in the transaction its statement would use, and
        is recorded with the rows of that statement."""
        version = self.session.execute("BEGIN").state.version  # type: ignore[attr-defined]
        self.session.execute("ROLLBACK")
        self.assertEqual(
            self.session.execute("PREPARE ins (INT) AS INSERT INTO t VALUES ($1, 'a')"),
            StatementResult.Prepare("ins"),
        )
        self.session.execute("PREPARE get AS SELECT * FROM t")
        self.assertEqual(self.session.execute("EXECUTE get").row_count(), 0)
        self.assertEqual(self.session.execute("BEGIN").state.version, version + 1)  # type: ignore[attr-defined]
        self.session.execute("ROLLBACK")
        self.assertEqual(self.session.execute("EXECUTE ins (1)"), StatementResult.Insert(1))
        self.assertEqual(self.session.execute("BEGIN").state.version, version + 3)  # type: ignore[attr-defined]

        # In a transaction: ROLLBACK undoes the execution, but not PREPARE or
        # DEALLOCATE.
        self.session.execute("PREPARE mine AS SELECT COUNT(*) FROM t")
        self.session.execute("EXECUTE ins (2)")
        self.assertEqual(self.session.execute("DEALLOCATE get"), StatementResult.Deallocate("get"))
        self.assertEqual(self.session.execute("EXECUTE mine").try_into_i64(), 2)
        self.session.execute("ROLLBACK")
        self.assertEqual(self.session.execute("EXECUTE mine").try_into_i64(), 1)
        with self.assertRaises(Error):
            self.session.execute("EXECUTE get")

        # In a read-only transaction, PREPARE works and prepared writes fail.
        self.session.execute("BEGIN READ ONLY")
        self.session.execute("PREPARE del AS DELETE FROM t")
        with self.assertRaises(Error) as cm:
            self.session.execute("EXECUTE del")
        self.assertEqual(str(cm.exception), "read-only transaction")
        self.session.execute("COMMIT")

        result = self.session.execute("SHOW PREPARED")
        assert isinstance(result, StatementResult.Select)
        self.assertEqual(
            [str(column) for column in result.columns],
            ["name", "parameters", "statement", "executions", "plans"],
        )
        self.assertEqual([row[0] for row in result.rows], [Value.String(n) for n in ("del", "ins", "mine")])
        self.assertIn(("EXECUTE ins (?)", 2, 0, 2), self.statements())
        self.assertIn(("EXECUTE mine", 2, 0, 2), self.statements())

        # EXPLAIN, DESCRIBE and ESTIMATE of an EXECUTE don't count as
        # executions.
        self.session.execute("EXPLAIN EXECUTE ins (3)")
        self.session.execute("DESCRIBE EXECUTE mine")
        self.session.execute("ESTIMATE EXECUTE mine")
        rows = self.session.execute("SHOW PREPARED").try_into_rows()
        self.assertEqual(
            [(row[0].try_into_string(), row[3].try_into_i64(), row[4].try_into_i64()) for row in rows],
            [("del", 1, 1), ("ins", 2, 2), ("mine", 2, 1)],
        )

    def test_import_atomic(self) -> None:
        """A failing import inserts nothing."""
        with self.assertRaises(Error) as cm:
            self.session.execute("""IMPORT INTO t FROM JSON '{"id":1}', '{"id":1}'""")
        self.assertEqual(str(cm.exception), "invalid input: primary key 1 already exists")
        self.assertEqual(self.session.execute("SELECT COUNT(*) FROM t").try_into_i64(), 0)


class TestUsers(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.session = self.engine.session()
        self.session.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        self.session.execute("CREATE USER alice PASSWORD 'pw'")
        self.session.execute("CREATE USER bob")

    def tearDown(self) -> None:
        self.session.close()

    def error(self, session: Session[Transaction[Memory]], statement: str) -> str:
        """Executes a statement, returning the message of its error."""
        with self.assertRaises(Error) as cm:
            session.execute(statement)
        return str(cm.exception)

    def test_authenticate(self) -> None:
        """A session logs in once, before its first statement; a failed login
        can be retried."""
        session = self.engine.session()
        with self.assertRaises(Error.PermissionDenied):
            session.authenticate("alice", "wrong")
        with self.assertRaises(Error.InvalidInput):
            session.authenticate("nosuch", None)
        session.authenticate("alice", "pw")
        self.assertEqual((session.login, session.user), ("alice", "alice"))
        with self.assertRaises(Error) as cm:
            session.authenticate("bob", None)
        self.assertEqual(str(cm.exception), "invalid input: session already started")
        self.assertEqual(
            self.error(session, "SELECT * FROM t"),
            "permission denied: user alice has no SELECT privilege on t",
        )
        session.close()

        session = self.engine.session()
        session.execute("SELECT 1")
        with self.assertRaises(Error) as cm:
            session.authenticate("bob", None)
        self.assertEqual(str(cm.exception), "invalid input: session already started")
        session.close()

    def test_set_role(self) -> None:
        """SET ROLE and RESET ROLE aren't transactional. SET ROLE is checked
        against the login user."""
        self.session.execute("BEGIN")
        self.assertEqual(self.session.execute("SET ROLE alice"), StatementResult.SetRole("alice"))
        self.session.execute("ROLLBACK")
        self.assertEqual(self.session.user, "alice")
        self.assertEqual(
            self.error(self.session, "SET ROLE nosuch"), "invalid input: user nosuch does not exist"
        )
        self.assertEqual(self.session.execute("SET ROLE bob"), StatementResult.SetRole("bob"))
        self.assertEqual(self.session.execute("RESET ROLE"), StatementResult.ResetRole("admin"))
        self.assertEqual((self.session.login, self.session.user), ("admin", "admin"))

    def test_ownership(self) -> None:
        """Tables and views are owned by the user that created them; objects
        of admin store no owner."""
        self.session.execute("SET ROLE alice")
        self.session.execute("CREATE TABLE a (id INT PRIMARY KEY)")
        self.session.execute("CREATE VIEW v AS SELECT * FROM a")
        self.session.execute("PREPARE p AS CREATE TABLE b (id INT PRIMARY KEY)")
        self.session.execute("EXECUTE p")
        txn = self.engine.begin_read_only()
        owners = [txn.get_owner(name) for name in ("a", "v", "b", "t")]
        self.assertEqual(owners, ["alice", "alice", "alice", "admin"])
        txn.rollback()


class TestStatementResult(unittest.TestCase):
    def test_encoding(self) -> None:
        """The extension results are encoded after the existing variants."""
        cases: list[tuple[StatementResult, str]] = [
            (StatementResult.Analyze(["a", "b"]), "0a0201610162"),
            (StatementResult.Export(ast.ExportFormat.Json, ["{}"]), "0b0101027b7d"),
            (StatementResult.Format(["a", "b"]), "0c0201610162"),
            (StatementResult.CreateView("v"), "0d0176"),
            (StatementResult.DropView("v", True), "0e017601"),
            (StatementResult.Prepare("p"), "0f0170"),
            (StatementResult.Deallocate("p"), "100170"),
            (StatementResult.CreateUser("u"), "110175"),
            (StatementResult.DropUser("u", False), "12017500"),
            (StatementResult.Grant("t"), "13010174"),
            (StatementResult.Revoke(None), "1400"),
            (StatementResult.SetRole("u"), "150175"),
            (StatementResult.ResetRole("u"), "160175"),
            (StatementResult.CreatePolicy("t", "p"), "1701740170"),
            (StatementResult.DropPolicy("t", "p", True), "180174017001"),
            (StatementResult.CreateMaterializedView("m", 2), "19016d02"),
            (StatementResult.RefreshMaterializedView("m", 0), "1a016d00"),
            (StatementResult.DropMaterializedView("m", False), "1b016d00"),
            (StatementResult.Select([Label.Unqualified("x")], []), "090101017800"),
        ]
        for result, expect in cases:
            with self.subTest(result=result):
                encoded = result.encode()
                self.assertEqual(encoded.hex(), expect)
                self.assertEqual(StatementResult.decode(encoded), result)


if __name__ == "__main__":
    unittest.main()
