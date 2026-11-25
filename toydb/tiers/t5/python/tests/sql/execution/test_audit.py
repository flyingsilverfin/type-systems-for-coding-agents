"""Tests for toydb.sql.execution.audit.

The audit log and the session list are mostly covered by the SQL golden
scripts in `tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these
tests cover the classes, statement names and entries directly, and the
recording by sessions.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution import StatementResult
from toydb.sql.execution.audit import (
    AuditClass,
    AuditLog,
    Entry,
    SessionList,
    classify,
    statement_name,
)
from toydb.sql.execution.privileges import Access, AccessKind
from toydb.sql.parser import Parser, ast
from toydb.sql.types import Value
from toydb.storage.memory import Memory


class TestAudit(unittest.TestCase):
    def test_classify(self) -> None:
        """The class is the first that applies: ROLE by statement, then DDL,
        WRITE and READ by the accesses, otherwise MISC."""
        cases: list[tuple[str, list[AccessKind], AuditClass]] = [
            ("CREATE USER u", [], AuditClass.Role),
            ("DROP USER u", [], AuditClass.Role),
            ("GRANT SELECT ON t TO u", [AccessKind.Grant], AuditClass.Role),
            ("REVOKE g FROM u", [], AuditClass.Role),
            (
                "CREATE VIEW v AS SELECT * FROM t",
                [AccessKind.Create, AccessKind.Read],
                AuditClass.Ddl,
            ),
            ("DROP TABLE t", [AccessKind.Drop], AuditClass.Ddl),
            ("SELECT 1", [AccessKind.Alter], AuditClass.Ddl),
            ("SELECT 1", [AccessKind.Read, AccessKind.Policy], AuditClass.Ddl),
            ("INSERT INTO t VALUES (1)", [AccessKind.Insert], AuditClass.Write),
            ("SELECT 1", [AccessKind.Read, AccessKind.Refresh], AuditClass.Write),
            ("DELETE FROM t", [AccessKind.Delete], AuditClass.Write),
            ("UPDATE t SET v = 1", [AccessKind.Update], AuditClass.Write),
            ("SELECT * FROM t", [AccessKind.Read], AuditClass.Read),
            ("ANALYZE t", [AccessKind.Analyze], AuditClass.Misc),
            ("SELECT 1", [], AuditClass.Misc),
            ("SHOW SESSIONS", [], AuditClass.Misc),
        ]
        for sql, kinds, expect in cases:
            with self.subTest(sql=sql, kinds=kinds):
                accesses = [Access("t", kind) for kind in kinds]
                self.assertEqual(classify(Parser.parse(sql), accesses), expect)

    def test_statement_names(self) -> None:
        """Statement names are the leading keywords in lower case, joined by
        `_`, without optional words."""
        cases = [
            ("BEGIN READ ONLY", "begin"),
            ("COMMIT", "commit"),
            ("ROLLBACK", "rollback"),
            ("EXPLAIN SELECT 1", "explain"),
            ("CREATE TABLE t (id INT PRIMARY KEY)", "create_table"),
            ("DROP TABLE IF EXISTS t", "drop_table"),
            ("DELETE FROM t", "delete"),
            ("INSERT INTO t VALUES (1)", "insert"),
            ("UPDATE t SET v = 1", "update"),
            ("SELECT 1", "select"),
            ("SHOW STATEMENTS", "show_statements"),
            ("ANALYZE", "analyze"),
            ("SHOW STATISTICS t", "show_statistics"),
            ("EXPORT AS SQL", "export"),
            ("IMPORT INTO t FROM CSV 'id'", "import"),
            ("FORMAT SELECT 1", "format"),
            ("CREATE VIEW v AS SELECT 1", "create_view"),
            ("DROP VIEW v", "drop_view"),
            ("SHOW VIEWS", "show_views"),
            ("DESCRIBE t", "describe"),
            ("ESTIMATE SELECT 1", "estimate"),
            ("PREPARE p AS SELECT 1", "prepare"),
            ("EXECUTE p", "execute"),
            ("DEALLOCATE p", "deallocate"),
            ("SHOW PREPARED", "show_prepared"),
            ("LINT SELECT 1", "lint"),
            ("SHOW CHANGES SINCE 1", "show_changes"),
            ("CREATE USER u PASSWORD 'p'", "create_user"),
            ("DROP USER u", "drop_user"),
            ("GRANT g TO u", "grant"),
            ("REVOKE SELECT ON t FROM u", "revoke"),
            ("SET ROLE u", "set_role"),
            ("RESET ROLE", "reset_role"),
            ("SHOW USERS", "show_users"),
            ("SHOW GRANTS", "show_grants"),
            ("SHOW AUDIT SINCE 1", "show_audit"),
            ("SHOW SESSIONS", "show_sessions"),
            ("CREATE POLICY p ON t USING (TRUE)", "create_policy"),
            ("DROP POLICY IF EXISTS p ON t", "drop_policy"),
            ("CREATE MATERIALIZED VIEW m (a) AS SELECT 1", "create_materialized_view"),
            ("REFRESH MATERIALIZED VIEW m", "refresh_materialized_view"),
            ("DROP MATERIALIZED VIEW IF EXISTS m", "drop_materialized_view"),
        ]
        for sql, expect in cases:
            with self.subTest(sql=sql):
                self.assertEqual(statement_name(Parser.parse(sql)), expect)

    def test_log(self) -> None:
        """Entries are numbered from 1; since() returns those above a
        number."""
        log = AuditLog()
        error = "invalid input: user bob does not exist"
        log.record("admin", Parser.parse("SELECT * FROM t"), [Access("t", AccessKind.Read)], None)
        log.record("alice", Parser.parse("SET ROLE bob"), [], error)
        self.assertEqual([entry.seq for entry in log.since(0)], [1, 2])
        self.assertEqual(
            log.since(1), [Entry(2, "alice", AuditClass.Misc, "set_role", [], error)]
        )
        self.assertEqual(log.since(2), [])
        self.assertEqual(log.since(18446744073709551615), [])

    def test_entry_row(self) -> None:
        objects = [Access("v", AccessKind.Create), Access("t", AccessKind.Read)]
        entry = Entry(3, "admin", AuditClass.Ddl, "create_view", objects, None)
        self.assertEqual(
            entry.row(),
            [
                Value.Integer(3),
                Value.String("admin"),
                Value.String("DDL"),
                Value.String("create_view"),
                Value.String("v (create), t (read)"),
                Value.Null(),
            ],
        )
        entry = Entry(4, "alice", AuditClass.Misc, "select", [], "permission denied: x")
        self.assertEqual(entry.row()[4:], [Value.String(""), Value.String("permission denied: x")])


class TestSessions(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.session = self.engine.session()
        self.session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING)")

    def tearDown(self) -> None:
        self.session.close()

    def audit(self) -> list[tuple[str, str, str, str, str | None]]:
        """Returns the audit log entries as native values, without seq."""
        with self.engine.audit.lock() as log:
            return [
                (
                    entry.user,
                    str(entry.class_),
                    entry.statement,
                    ", ".join(map(str, entry.objects)),
                    entry.error,
                )
                for entry in log.since(0)
            ]

    def sessions(self) -> list[list[Value]]:
        """Returns the SHOW SESSIONS rows of the session."""
        result = self.session.execute("SHOW SESSIONS")
        assert isinstance(result, StatementResult.Select)
        return result.rows

    def test_recording(self) -> None:
        """Statements are recorded with the user they began with, also when
        they fail; parse errors and SHOW AUDIT are not recorded."""
        self.session.execute("CREATE USER alice")
        self.session.execute("SET ROLE alice")
        with self.assertRaises(Error):
            self.session.execute("INSERT INTO t VALUES (1, 'a')")
        with self.assertRaises(Error):
            self.session.execute("SHOW AUDIT")
        with self.assertRaises(Error):
            self.session.execute("SELECT FROM t")
        with self.assertRaises(Error):
            self.session.execute("EXECUTE nosuch (1)")
        self.session.execute("RESET ROLE")
        self.session.execute("SHOW AUDIT")
        self.session.execute("SHOW STATEMENTS")
        self.session.execute("PREPARE p (INT) AS DELETE FROM t WHERE id = $1")
        with self.assertRaises(Error):
            self.session.execute("EXECUTE p ($1)")
        with self.assertRaises(Error):
            self.session.execute("EXPLAIN ANALYZE t")
        with self.assertRaises(Error):
            self.session.execute("SELECT * FROM t WHERE id = $1")
        self.assertEqual(
            self.audit(),
            [
                ("admin", "DDL", "create_table", "t (create)", None),
                ("admin", "ROLE", "create_user", "", None),
                ("admin", "MISC", "set_role", "", None),
                (
                    "alice",
                    "WRITE",
                    "insert",
                    "t (insert)",
                    "permission denied: user alice has no INSERT privilege on t",
                ),
                (
                    "alice",
                    "MISC",
                    "execute",
                    "",
                    "invalid input: prepared statement nosuch does not exist",
                ),
                ("alice", "MISC", "reset_role", "", None),
                ("admin", "MISC", "show_statements", "", None),
                ("admin", "MISC", "prepare", "", None),
                (
                    "admin",
                    "MISC",
                    "execute",
                    "",
                    "invalid input: parameter $1 can only be used in a prepared statement",
                ),
                (
                    "admin",
                    "MISC",
                    "explain",
                    "t (analyze)",
                    "invalid input: EXPLAIN is not supported for ANALYZE",
                ),
                (
                    "admin",
                    "READ",
                    "select",
                    "t (read)",
                    "invalid input: parameter $1 can only be used in a prepared statement",
                ),
            ],
        )

    def test_copy(self) -> None:
        """COPY commands are recorded as copy, with the class and objects of
        the EXPORT or IMPORT statement they execute as, and counted as
        executed statements."""
        self.session.record_copy("admin", ast.Statement.Export("t", ast.ExportFormat.Csv), None)
        statement = ast.Statement.Import("t", ast.ImportFormat.Csv, [])
        self.session.record_copy("alice", statement, "COPY from stdin failed: x")
        self.assertEqual(
            self.audit()[-2:],
            [
                ("admin", "READ", "copy", "t (read)", None),
                ("alice", "WRITE", "copy", "t (insert)", "COPY from stdin failed: x"),
            ],
        )
        self.assertEqual(self.sessions()[0][4], Value.Integer(3))

    def test_sessions(self) -> None:
        """Sessions are numbered in creation order, show their users, open
        transaction and executed statements, and leave the list when
        closed."""
        other = self.engine.session()
        other.execute("BEGIN")
        assert other.txn is not None
        version = other.txn.state().version
        third = self.engine.session()
        third.close()
        admin = Value.String("admin")
        self.assertEqual(
            self.sessions(),
            [
                [Value.Integer(1), admin, admin, Value.Null(), Value.Integer(1)],
                [Value.Integer(2), admin, admin, Value.Integer(version), Value.Integer(1)],
            ],
        )
        other.close()
        self.assertEqual([row[0] for row in self.sessions()], [Value.Integer(1)])
        fourth = self.engine.session()
        self.assertEqual(fourth.number, 4)
        fourth.close()

    def test_engines(self) -> None:
        """Each engine has its own log and sessions."""
        engine = Local(Memory())
        session = engine.session()
        self.assertEqual(session.number, 1)
        session.execute("SELECT 1")
        with engine.audit.lock() as log:
            self.assertEqual(len(log.since(0)), 1)
        session.close()
        self.assertEqual(len(self.audit()), 1)


if __name__ == "__main__":
    unittest.main()
