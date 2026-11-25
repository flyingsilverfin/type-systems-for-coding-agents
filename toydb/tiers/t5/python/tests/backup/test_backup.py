"""Tests for toydb.backup.

Backups are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5/bk_*` and the cluster script
`tests/e2e/scripts/ext5/bk_cluster`; these tests cover creating and restoring
backups directly, and the session's backup and restore.
"""

import unittest

from toydb import backup
from toydb.backup import format
from toydb.backup.format import Record
from toydb.error import Error
from toydb.server import Request, Response
from toydb.sql.engine import Local
from toydb.sql.types import Value
from toydb.sql.types.check import Check
from toydb.storage.memory import Memory

SETUP = [
    "CREATE USER alice PASSWORD 'pw'",
    "CREATE USER staff",
    "GRANT staff TO alice",
    "CREATE TABLE b (id INT PRIMARY KEY, n INT CHECK (n > 0))",
    "CREATE TABLE a (id INT PRIMARY KEY, b INT REFERENCES b)",
    "INSERT INTO b VALUES (2, 20), (1, 10)",
    "INSERT INTO a VALUES (1, 2)",
    "CREATE VIEW w AS SELECT id FROM b",
    "CREATE VIEW v AS SELECT * FROM w",
    "CREATE MATERIALIZED VIEW m AS SELECT * FROM v WHERE id > 1",
    "GRANT SELECT ON b TO staff",
    "CREATE POLICY p ON b TO staff USING (n > 10)",
]


class TestBackup(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        self.session = self.engine.session()
        for statement in SETUP:
            self.session.execute(statement)

    def tearDown(self) -> None:
        self.session.close()

    def test_records(self) -> None:
        """The records are in file order: tables after the tables they
        reference, views and materialized views after the views and
        materialized views they depend on."""
        txn = self.engine.begin_read_only()
        records = list(backup.records(txn))
        txn.commit()
        lines = [format.format_record(record) for record in records]
        self.assertEqual(
            lines,
            [
                "user alice password",
                "user staff",
                "member alice of staff",
                "table b owner admin: CREATE TABLE b (id INTEGER PRIMARY KEY, n INTEGER DEFAULT NULL)",
                "check b.b_check1: n > 0",
                "row b: 1, 10",
                "row b: 2, 20",
                "table a owner admin: CREATE TABLE a (id INTEGER PRIMARY KEY, "
                "b INTEGER DEFAULT NULL INDEX REFERENCES b)",
                "row a: 1, 2",
                "view w owner admin (id): SELECT id FROM b",
                "view v owner admin (id): SELECT * FROM w",
                "materialized view m owner admin (id): SELECT * FROM v WHERE id > 1",
                "grant b to staff: SELECT",
                "policy b.p to staff: n > 10",
            ],
        )

    def test_restore(self) -> None:
        """Restoring into an empty database re-creates the database, so that
        a second backup is the same."""
        data = self.session.backup()
        other = Local(Memory())
        session = other.session()
        session.restore(data)
        self.assertEqual(session.backup(), data)
        result = session.execute("SELECT * FROM v")
        self.assertEqual(list(result.try_into_rows()), [[Value.Integer(1)], [Value.Integer(2)]])
        result = session.execute("SELECT * FROM m")
        self.assertEqual(list(result.try_into_rows()), [[Value.Integer(2)]])
        session.close()

    def test_restore_error(self) -> None:
        """A restore that fails restores nothing."""
        data = self.session.backup()
        self.session.execute("DROP MATERIALIZED VIEW m")
        self.session.execute("DROP VIEW v")
        self.session.execute("DROP VIEW w")
        with self.assertRaises(Error) as cm:
            self.session.restore(data)
        self.assertEqual(str(cm.exception), "invalid input: user alice already exists")
        result = self.session.execute("SHOW VIEWS")
        self.assertEqual(list(result.try_into_rows()), [])

    def test_apply_check(self) -> None:
        """A CHECK record is the table's next constraint, checked like one of
        CREATE TABLE."""
        txn = self.engine.begin()
        backup.apply(txn, Record.Check("a", Check("positive", "id > 0")))
        self.assertEqual(txn.get_checks("a"), [Check("positive", "id > 0")])
        for check, error in [
            (Check("positive", "id > 1"), "duplicate check constraint positive in table a"),
            (Check("x", "nope > 1"), "unknown column nope"),
        ]:
            with self.subTest(check=check):
                with self.assertRaises(Error) as cm:
                    backup.apply(txn, Record.Check("a", check))
                self.assertIn(error, str(cm.exception))
        with self.assertRaises(Error) as cm:
            backup.apply(txn, Record.Check("x", Check("c", "TRUE")))
        self.assertEqual(str(cm.exception), "invalid input: table x does not exist")
        txn.rollback()

    def test_apply_owner(self) -> None:
        """A TABLE or VIEW record's owner must exist; a VIEW's query must be a
        SELECT."""
        txn = self.engine.begin()
        table = txn.must_get_table("a")
        txn.drop_table("a", False)
        for record, error in [
            (Record.Table(table, "bob"), "invalid input: user bob does not exist"),
            (Record.View("x", "bob", ["id"], "SELECT id FROM b"), "invalid input: user bob does not exist"),
            (
                Record.View("x", "admin", ["id"], "DELETE FROM b"),
                "invalid input: view query is not a SELECT: DELETE FROM b",
            ),
            (Record.MatView("x", "bob", ["id"], "SELECT id FROM b"), "invalid input: user bob does not exist"),
            (
                Record.MatView("x", "admin", ["id"], "DELETE FROM b"),
                "invalid input: view query is not a SELECT: DELETE FROM b",
            ),
        ]:
            with self.subTest(record=record):
                with self.assertRaises(Error) as cm:
                    backup.apply(txn, record)
                self.assertEqual(str(cm.exception), error)
        self.assertIsNone(txn.get_table("a"))
        txn.rollback()

    def test_apply_matview(self) -> None:
        """A MATVIEW record is created like CREATE MATERIALIZED VIEW with its
        column list, owned by the recorded owner: its query is executed as
        admin, so its rows are those of the restored tables."""
        txn = self.engine.begin()
        backup.apply(txn, Record.MatView("x", "alice", ["k", "n"], "SELECT * FROM b WHERE n > 0"))
        self.assertEqual(txn.get_owner("x"), "alice")
        self.assertEqual(
            txn.get_matview_rows("x"),
            [[Value.Integer(1), Value.Integer(10)], [Value.Integer(2), Value.Integer(20)]],
        )
        with self.assertRaises(Error) as cm:
            backup.apply(txn, Record.MatView("y", "admin", ["k"], "SELECT * FROM b"))
        self.assertEqual(
            str(cm.exception), "invalid input: wrong number of column names for view y: expected 2, found 1"
        )
        txn.rollback()

    def test_apply_row(self) -> None:
        """A ROW record is inserted with the checks of INSERT and of the
        table's constraints."""
        txn = self.engine.begin()
        with self.assertRaises(Error) as cm:
            backup.apply(txn, Record.Row("b", [Value.Integer(3), Value.Integer(0)]))
        self.assertEqual(str(cm.exception), "invalid input: row 3 violates check constraint b_check1")
        with self.assertRaises(Error) as cm:
            backup.apply(txn, Record.Row("b", [Value.Integer(1), Value.Integer(5)]))
        self.assertIn("primary key 1 already exists", str(cm.exception))
        txn.rollback()

    def test_superuser(self) -> None:
        """Backups and restores require a superuser."""
        data = self.session.backup()
        self.session.execute("SET ROLE alice")
        for f in (self.session.backup, lambda: self.session.restore(data)):
            with self.assertRaises(Error) as cm:
                f()
            self.assertEqual(str(cm.exception), "permission denied: user alice is not a superuser")

    def test_as_of(self) -> None:
        """A backup as of a version sees the changes of earlier versions."""
        txn = self.engine.begin_as_of(5)
        expect = backup.create(txn)
        txn.commit()
        self.assertEqual(self.session.backup(5), expect)
        self.assertNotEqual(self.session.backup(5), self.session.backup())
        with self.assertRaises(Error) as cm:
            self.session.backup(99)
        self.assertEqual(str(cm.exception), "invalid input: version 99 does not exist")
        self.session.execute("BEGIN")
        with self.assertRaises(Error) as cm:
            self.session.backup(1)
        self.assertEqual(str(cm.exception), "invalid input: already in a transaction")

    def test_not_recorded(self) -> None:
        """Backups and restores are not recorded as statements."""
        executed = self.session.executed
        self.session.restore(format.encode([]))
        self.session.backup()
        self.assertEqual(self.session.executed, executed)


class TestRequests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        """The backup requests and responses encode and decode."""
        for request in (Request.Backup(None), Request.Backup(7), Request.Restore(b"\x00\x01")):
            with self.subTest(request=request):
                self.assertEqual(Request.decode(request.encode()), request)
        for response in (Response.Backup(b"\xff"), Response.Restore()):
            with self.subTest(response=response):
                self.assertEqual(Response.decode(response.encode()), response)


if __name__ == "__main__":
    unittest.main()
