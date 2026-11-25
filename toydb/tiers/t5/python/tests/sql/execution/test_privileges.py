"""Tests for toydb.sql.execution.privileges.

The privilege checks are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these tests cover
the accesses of each statement and the checks directly.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution.prepared import PreparedStatements
from toydb.sql.execution.privileges import (
    Access,
    AccessKind,
    accesses,
    acts_for,
    check,
    has_privilege,
    is_superuser,
)
from toydb.sql.parser import Parser
from toydb.sql.types import DataType
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.user import Privilege, User
from toydb.storage.memory import Memory


class TestPrivileges(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE b (id INT PRIMARY KEY)")
        session.execute("CREATE TABLE a (id INT PRIMARY KEY, b_id INT REFERENCES b)")
        session.execute("CREATE VIEW v AS SELECT * FROM a")
        session.close()
        self.txn = self.engine.begin()
        self.prepared = PreparedStatements()
        self.prepared.prepare("p", [DataType.Integer], Parser.parse("DELETE FROM a WHERE id = $1"))
        for name, superuser in (("alice", False), ("bob", False), ("root", True)):
            self.txn.create_user(User(name, superuser, None))

    def tearDown(self) -> None:
        self.txn.rollback()

    def accesses(self, sql: str) -> list[str]:
        """Returns the accesses of a statement, formatted."""
        return [str(access) for access in accesses(self.txn, Parser.parse(sql), self.prepared)]

    def check(self, sql: str, user: str, login: str = "admin") -> str | None:
        """Checks a statement, returning the error message if it fails."""
        try:
            check(self.txn, Parser.parse(sql), user, login, self.prepared)
        except Error.PermissionDenied as error:
            return str(error)
        return None

    def test_accesses(self) -> None:
        """Accesses are in the order of the statement's text, each name and
        kind once."""
        cases: list[tuple[str, list[str]]] = [
            ("SELECT * FROM a JOIN b ON a.b_id = b.id, a AS x", ["a (read)", "b (read)"]),
            ("SELECT * FROM nosuch", ["nosuch (read)"]),
            ("SELECT * FROM toydb_tables JOIN a ON TRUE", ["a (read)"]),
            ("CREATE VIEW w AS SELECT * FROM toydb_users", ["w (create)"]),
            ("SELECT 1", []),
            ("INSERT INTO a VALUES (1, NULL)", ["a (insert)"]),
            ("UPDATE a SET id = 1", ["a (update)"]),
            ("DELETE FROM a", ["a (delete)"]),
            ("CREATE TABLE c (id INT PRIMARY KEY, a_id INT REFERENCES a)", ["c (create)"]),
            ("DROP TABLE IF EXISTS a", ["a (drop)"]),
            ("CREATE VIEW w AS SELECT * FROM v, b", ["w (create)", "v (read)", "b (read)"]),
            ("CREATE VIEW w AS SELECT * FROM w", ["w (create)", "w (read)"]),
            ("DROP VIEW v", ["v (drop)"]),
            ("ANALYZE a", ["a (analyze)"]),
            ("ANALYZE", ["a (analyze)", "b (analyze)"]),
            ("SHOW STATISTICS a", ["a (read)"]),
            ("EXPORT a AS CSV", ["a (read)"]),
            ("EXPORT AS SQL", ["a (read)", "b (read)", "v (read)"]),
            ("IMPORT INTO a FROM CSV 'id'", ["a (insert)"]),
            ("EXPLAIN SELECT * FROM v", ["v (read)"]),
            ("DESCRIBE DELETE FROM b", ["b (delete)"]),
            ("ESTIMATE UPDATE b SET id = 2", ["b (update)"]),
            ("EXECUTE p (1)", ["a (delete)"]),
            ("EXPLAIN EXECUTE p (1)", ["a (delete)"]),
            ("GRANT SELECT ON v TO alice", ["v (grant)"]),
            ("REVOKE DELETE ON a FROM alice", ["a (grant)"]),
            ("GRANT bob TO alice", []),
            ("CREATE POLICY p ON a TO bob USING (id > 0)", ["a (policy)"]),
            ("DROP POLICY IF EXISTS p ON nosuch", ["nosuch (policy)"]),
            ("DESCRIBE a", []),
            ("FORMAT SELECT * FROM a", []),
            ("LINT DELETE FROM a", []),
            ("PREPARE q AS SELECT * FROM a", []),
            ("BEGIN", []),
            ("SHOW CHANGES", []),
            ("CREATE USER x", []),
            ("SET ROLE alice", []),
            ("SHOW GRANTS", []),
            ("SHOW AUDIT", []),
            ("SHOW SESSIONS", []),
            ("CREATE MATERIALIZED VIEW m AS SELECT * FROM v, b", ["m (create)", "v (read)", "b (read)"]),
            ("REFRESH MATERIALIZED VIEW m", ["m (refresh)"]),
            ("DROP MATERIALIZED VIEW IF EXISTS m", ["m (drop)"]),
        ]
        for sql, expect in cases:
            with self.subTest(sql=sql):
                self.assertEqual(self.accesses(sql), expect)

    def test_matviews(self) -> None:
        """Materialized views are objects like views: reading one requires
        SELECT, refreshing and dropping it acting for its owner, and EXPORT
        AS SQL reads it."""
        self.txn.create_matview(MaterializedView("m", ["id"], "SELECT id FROM b", ["b"]), [])
        self.assertEqual(self.accesses("EXPORT AS SQL"), ["a (read)", "b (read)", "m (read)", "v (read)"])
        cases: list[tuple[str, str | None]] = [
            ("SELECT * FROM m", "user alice has no SELECT privilege on m"),
            ("REFRESH MATERIALIZED VIEW m", "user alice is not the owner of m"),
            ("DROP MATERIALIZED VIEW m", "user alice is not the owner of m"),
            ("REFRESH MATERIALIZED VIEW nosuch", None),
            ("CREATE MATERIALIZED VIEW n AS SELECT * FROM m", "user alice has no SELECT privilege on m"),
        ]
        for sql, expect in cases:
            with self.subTest(sql=sql):
                message = None if expect is None else f"permission denied: {expect}"
                self.assertEqual(self.check(sql, "alice"), message)
        self.txn.set_owner("m", "alice")
        for sql in ("SELECT * FROM m", "REFRESH MATERIALIZED VIEW m", "DROP MATERIALIZED VIEW m"):
            with self.subTest(sql=sql):
                self.assertIsNone(self.check(sql, "alice"))

    def test_access_display(self) -> None:
        self.assertEqual(str(Access("t", AccessKind.Policy)), "t (policy)")
        kinds = [str(kind) for kind in AccessKind]
        self.assertEqual(kinds[:4], ["read", "insert", "update", "delete"])

    def test_superusers(self) -> None:
        """admin and other superusers may do everything; admin isn't stored."""
        self.assertTrue(is_superuser(self.txn, "admin"))
        self.assertTrue(is_superuser(self.txn, "root"))
        self.assertFalse(is_superuser(self.txn, "alice"))
        self.assertFalse(is_superuser(self.txn, "nosuch"))
        for sql in ("DROP TABLE a", "CREATE USER x", "SHOW CHANGES", "EXPORT AS SQL"):
            with self.subTest(sql=sql):
                self.assertIsNone(self.check(sql, "root"))

    def test_check(self) -> None:
        """The accesses are checked in order, then the own requirement."""
        self.txn.set_owner("b", "alice")
        cases: list[tuple[str, str | None]] = [
            ("SELECT * FROM b, a", "user alice has no SELECT privilege on a"),
            ("SELECT * FROM b, nosuch", None),
            ("INSERT INTO b VALUES (1)", None),
            ("INSERT INTO a VALUES (1, 1)", "user alice has no INSERT privilege on a"),
            ("EXECUTE p (1)", "user alice has no DELETE privilege on a"),
            ("DROP TABLE a", "user alice is not the owner of a"),
            ("DROP TABLE nosuch", None),
            ("ANALYZE", "user alice is not the owner of a"),
            ("ANALYZE b", None),
            ("GRANT SELECT ON v TO bob", "user alice is not the owner of v"),
            ("GRANT SELECT ON b TO bob", None),
            ("CREATE POLICY p ON a USING (TRUE)", "user alice is not the owner of a"),
            ("DROP POLICY p ON b", None),
            ("CREATE TABLE c (id INT PRIMARY KEY)", None),
            ("CREATE VIEW w AS SELECT * FROM v", "user alice has no SELECT privilege on v"),
            ("CREATE USER x", "user alice is not a superuser"),
            ("DROP USER bob", "user alice is not a superuser"),
            ("GRANT bob TO alice", "user alice is not a superuser"),
            ("REVOKE bob FROM alice", "user alice is not a superuser"),
            ("SHOW CHANGES", "user alice is not a superuser"),
            ("SHOW USERS", None),
            ("SHOW AUDIT SINCE 1", "user alice is not a superuser"),
            ("SHOW SESSIONS", None),
            ("PREPARE q AS SELECT * FROM a", None),
        ]
        for sql, expect in cases:
            with self.subTest(sql=sql):
                message = None if expect is None else f"permission denied: {expect}"
                self.assertEqual(self.check(sql, "alice"), message)

    def test_set_role(self) -> None:
        """SET ROLE is checked against the login user, not the current one."""
        self.txn.set_membership("alice", "bob", True)
        self.assertIsNone(self.check("SET ROLE bob", "bob", "alice"))
        self.assertEqual(
            self.check("SET ROLE root", "root", "alice"),
            "permission denied: user alice can't set role root",
        )
        self.assertIsNone(self.check("SET ROLE alice", "alice", "root"))

    def test_has_privilege(self) -> None:
        """A user has a privilege if it is a superuser, acts for the owner, or
        acts for a user it was granted to."""
        self.txn.create_user(User("staff", False, None))
        self.txn.set_membership("alice", "staff", True)
        self.txn.set_grant("a", "staff", [Privilege.Select])
        self.txn.set_owner("b", "staff")
        self.assertTrue(has_privilege(self.txn, "alice", Privilege.Select, "a"))
        self.assertFalse(has_privilege(self.txn, "alice", Privilege.Insert, "a"))
        self.assertTrue(has_privilege(self.txn, "alice", Privilege.Delete, "b"))
        self.assertFalse(has_privilege(self.txn, "bob", Privilege.Select, "a"))
        self.assertTrue(has_privilege(self.txn, "root", Privilege.Delete, "a"))
        self.assertFalse(has_privilege(self.txn, "staff", Privilege.Select, "v"))

    def test_acts_for(self) -> None:
        """Membership is transitive, and superuser status isn't passed on."""
        self.txn.set_membership("alice", "bob", True)
        self.txn.set_membership("bob", "root", True)
        self.assertEqual(acts_for(self.txn, "alice"), {"alice", "bob", "root"})
        self.assertEqual(acts_for(self.txn, "root"), {"root"})
        self.assertEqual(
            self.check("CREATE USER x", "alice"), "permission denied: user alice is not a superuser"
        )


if __name__ == "__main__":
    unittest.main()
