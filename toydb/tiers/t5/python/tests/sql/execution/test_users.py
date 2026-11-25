"""Tests for toydb.sql.execution.users.

The users statements are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5` (see `tests/sql/test_mod.py`); these tests cover
the stored catalog entries and the checks of the users module directly.
"""

import unittest

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution.users import (
    check_login,
    create_user,
    drop_user,
    grant,
    grant_membership,
    hash_password,
    revoke,
    revoke_membership,
    show_grants,
    show_users,
)
from toydb.sql.types import Table
from toydb.sql.types.policy import Policy
from toydb.sql.types.user import Grant, Privilege, User
from toydb.storage.memory import Memory


class TestUsers(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        session.execute("CREATE VIEW v AS SELECT * FROM t")
        session.close()
        self.txn = self.engine.begin()
        create_user(self.txn, "alice", "pw", False)
        create_user(self.txn, "bob", None, False)

    def tearDown(self) -> None:
        self.txn.rollback()

    def error(self, f: object) -> str:
        """Runs a closure, returning the message of the error it raises."""
        with self.assertRaises(Error) as cm:
            f()  # type: ignore[operator]
        return str(cm.exception)

    def test_hash_password(self) -> None:
        """Passwords are stored as md5 and the MD5 of password and user name."""
        self.assertEqual(hash_password("c", "ab"), "md5900150983cd24fb0d6963f7d28e17f72")
        alice = User("alice", False, hash_password("alice", "pw"))
        self.assertEqual(self.txn.get_user("alice"), alice)
        self.assertEqual(self.txn.get_user("bob"), User("bob", False, None))

    def test_create_user(self) -> None:
        self.assertEqual(
            self.error(lambda: create_user(self.txn, "", None, False)),
            "invalid input: user name can't be empty",
        )
        for name in ("alice", "admin"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.error(lambda: create_user(self.txn, name, None, True)),
                    f"invalid input: user {name} already exists",
                )
        self.assertEqual([user.name for user in self.txn.list_users()], ["admin", "alice", "bob"])

    def test_drop_user(self) -> None:
        """Dropping a user removes its memberships both ways and its grants."""
        create_user(self.txn, "staff", None, False)
        grant_membership(self.txn, "staff", "alice")
        grant_membership(self.txn, "alice", "bob")
        grant(self.txn, [Privilege.Select], "t", "alice")
        grant(self.txn, [Privilege.Select], "t", "staff")
        self.assertTrue(drop_user(self.txn, "alice", False, "admin", "admin"))
        self.assertIsNone(self.txn.get_user("alice"))
        self.assertEqual(self.txn.list_memberships(), [])
        self.assertEqual(self.txn.list_grants(), [Grant("t", "staff", [Privilege.Select])])
        self.assertFalse(drop_user(self.txn, "alice", True, "admin", "admin"))

    def test_drop_user_errors(self) -> None:
        self.txn.set_owner("v", "bob")
        self.txn.set_owner("t", "bob")
        cases: list[tuple[str, bool, str, str]] = [
            ("nosuch", False, "user nosuch does not exist", "admin"),
            ("admin", True, "user admin can't be dropped", "admin"),
            ("alice", False, "can't drop the current user alice", "alice"),
            ("bob", False, "user bob owns t", "admin"),
        ]
        for name, if_exists, message, current in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    self.error(lambda: drop_user(self.txn, name, if_exists, current, "admin")),
                    f"invalid input: {message}",
                )
        self.assertEqual(
            self.error(lambda: drop_user(self.txn, "alice", False, "admin", "alice")),
            "invalid input: can't drop the current user alice",
        )

    def test_drop_user_policies(self) -> None:
        """A user that a policy names can't be dropped; the error names the
        first such policy by table name, then policy name."""
        self.txn.create_table(Table("a", 0, self.txn.must_get_table("t").columns))
        self.txn.create_policy(Policy("t", "p", "alice", "TRUE", None))
        self.txn.create_policy(Policy("a", "z", "alice", "TRUE", None))
        self.txn.create_policy(Policy("a", "y", "alice", "TRUE", None))
        self.txn.create_policy(Policy("a", "x", None, "TRUE", None))
        self.assertEqual(
            self.error(lambda: drop_user(self.txn, "alice", False, "admin", "admin")),
            "invalid input: user alice is used by policy y on table a",
        )
        self.assertTrue(drop_user(self.txn, "bob", False, "admin", "admin"))

    def test_grant_revoke(self) -> None:
        """Grants merge with the held privileges; revoking all of them removes
        the grant."""
        grant(self.txn, [Privilege.Delete, Privilege.Select, Privilege.Select], "t", "alice")
        grant(self.txn, [Privilege.Insert], "t", "alice")
        grant(self.txn, [Privilege.Select], "v", "alice")
        self.assertEqual(
            self.txn.list_grants(),
            [
                Grant("t", "alice", [Privilege.Select, Privilege.Insert, Privilege.Delete]),
                Grant("v", "alice", [Privilege.Select]),
            ],
        )
        revoke(self.txn, [Privilege.Select, Privilege.Update], "t", "alice")
        revoke(self.txn, [Privilege.Select], "v", "alice")
        revoke(self.txn, [Privilege.Select], "v", "bob")
        self.assertEqual(
            self.txn.list_grants(), [Grant("t", "alice", [Privilege.Insert, Privilege.Delete])]
        )

    def test_grant_errors(self) -> None:
        cases: list[tuple[list[Privilege], str, str, str]] = [
            ([Privilege.Select], "nosuch", "nosuch", "table nosuch does not exist"),
            (
                [Privilege.Select, Privilege.Insert],
                "v",
                "nosuch",
                "only SELECT can be granted on view v",
            ),
            ([Privilege.Select], "v", "nosuch", "user nosuch does not exist"),
        ]
        for privileges, object, user, message in cases:
            for f in (grant, revoke):
                with self.subTest(f=f.__name__, message=message):
                    self.assertEqual(
                        self.error(lambda: f(self.txn, privileges, object, user)),
                        f"invalid input: {message}",
                    )

    def test_memberships(self) -> None:
        """Memberships can't form a cycle; granting one that exists, or
        revoking one that doesn't, does nothing."""
        create_user(self.txn, "staff", None, False)
        grant_membership(self.txn, "staff", "alice")
        grant_membership(self.txn, "staff", "alice")
        grant_membership(self.txn, "alice", "bob")
        self.assertEqual(self.txn.list_memberships(), [("alice", "staff"), ("bob", "alice")])
        cases: list[tuple[str, str, str]] = [
            ("nosuch", "alice", "user nosuch does not exist"),
            ("alice", "nosuch", "user nosuch does not exist"),
            ("alice", "alice", "user alice can't be a member of itself"),
            ("bob", "staff", "granting bob to staff would create a cycle"),
            ("alice", "staff", "granting alice to staff would create a cycle"),
        ]
        for group, user, message in cases:
            with self.subTest(group=group, user=user):
                self.assertEqual(
                    self.error(lambda: grant_membership(self.txn, group, user)),
                    f"invalid input: {message}",
                )
        revoke_membership(self.txn, "staff", "bob")
        revoke_membership(self.txn, "staff", "alice")
        self.assertEqual(self.txn.list_memberships(), [("bob", "alice")])
        self.assertEqual(
            self.error(lambda: revoke_membership(self.txn, "nosuch", "bob")),
            "invalid input: user nosuch does not exist",
        )

    def test_check_login(self) -> None:
        check_login(self.txn, "alice", "pw")
        check_login(self.txn, "bob", None)
        check_login(self.txn, "admin", None)
        cases: list[tuple[str, str | None, str]] = [
            ("nosuch", None, "invalid input: user nosuch does not exist"),
            ("alice", None, "permission denied: password authentication failed for user alice"),
            ("alice", "PW", "permission denied: password authentication failed for user alice"),
            ("bob", "", "permission denied: password authentication failed for user bob"),
        ]
        for user, password, message in cases:
            with self.subTest(user=user, password=password):
                self.assertEqual(self.error(lambda: check_login(self.txn, user, password)), message)

    def test_show(self) -> None:
        create_user(self.txn, "Root", None, True)
        grant_membership(self.txn, "bob", "alice")
        grant_membership(self.txn, "Root", "alice")
        grant(self.txn, [Privilege.Update, Privilege.Select], "t", "bob")
        grant(self.txn, [Privilege.Select], "t", "alice")
        self.txn.set_owner("v", "alice")
        users = [[str(value) for value in row] for row in show_users(self.txn)]
        self.assertEqual(
            users,
            [
                ["'Root'", "TRUE", "FALSE", "''"],
                ["'admin'", "TRUE", "FALSE", "''"],
                ["'alice'", "FALSE", "TRUE", "'Root, bob'"],
                ["'bob'", "FALSE", "FALSE", "''"],
            ],
        )
        grants = [[str(value) for value in row] for row in show_grants(self.txn)]
        self.assertEqual(
            grants,
            [
                ["'t'", "'admin'", "'OWNER'"],
                ["'t'", "'alice'", "'SELECT'"],
                ["'t'", "'bob'", "'SELECT'"],
                ["'t'", "'bob'", "'UPDATE'"],
                ["'v'", "'alice'", "'OWNER'"],
            ],
        )

    def test_drop_objects(self) -> None:
        """Dropping a table or view removes its owner and the privileges
        granted on it."""
        self.txn.set_owner("v", "alice")
        grant(self.txn, [Privilege.Select], "v", "bob")
        grant(self.txn, [Privilege.Select], "t", "bob")
        self.txn.drop_view("v", False)
        self.assertEqual(self.txn.get_owner("v"), "admin")
        self.assertEqual(self.txn.list_grants(), [Grant("t", "bob", [Privilege.Select])])
        self.txn.drop_table("t", False)
        self.assertEqual(self.txn.list_grants(), [])

    def test_read_only(self) -> None:
        """In a read-only transaction, statements that write fail."""
        txn = self.engine.begin_read_only()
        with self.assertRaises(Error.ReadOnly):
            create_user(txn, "x", None, False)
        self.assertFalse(drop_user(txn, "nosuch", True, "admin", "admin"))
        txn.rollback()


if __name__ == "__main__":
    unittest.main()
