"""Tests for toydb.sql.types.user."""

import unittest

from toydb.encoding import bincode
from toydb.sql.types.user import ADMIN, MEMBERSHIP, PRIVILEGES, Grant, Privilege, User


class TestUser(unittest.TestCase):
    def test_encoding(self) -> None:
        """A user is encoded as its name, superuser flag and optional
        password hash."""
        cases: list[tuple[User, str]] = [
            (User("a", False, None), "01610000"),
            (User("a", True, "md5"), "0161010103" + "md5".encode().hex()),
        ]
        for user, expect in cases:
            with self.subTest(user=user):
                self.assertEqual(user.encode().hex(), expect)
                self.assertEqual(User.decode(user.encode()), user)

    def test_admin(self) -> None:
        self.assertEqual(User.admin(), User(ADMIN, True, None))

    def test_display(self) -> None:
        self.assertEqual(str(User("alice", False, "md5ab")), "alice superuser=false password=md5ab")
        self.assertEqual(str(User.admin()), "admin superuser=true password=None")


class TestPrivilege(unittest.TestCase):
    def test_display_order(self) -> None:
        """Privileges display as their keywords, and are ordered SELECT,
        INSERT, UPDATE, DELETE."""
        displayed = [str(privilege) for privilege in Privilege]
        self.assertEqual(displayed, ["SELECT", "INSERT", "UPDATE", "DELETE"])
        self.assertEqual(
            sorted([Privilege.Delete, Privilege.Select, Privilege.Update]),
            [Privilege.Select, Privilege.Update, Privilege.Delete],
        )

    def test_encoding(self) -> None:
        privileges = [Privilege.Select, Privilege.Delete]
        encoded = bincode.serialize(privileges, PRIVILEGES)
        self.assertEqual(encoded.hex(), "020003")
        self.assertEqual(bincode.deserialize(PRIVILEGES, encoded), privileges)

    def test_grant_membership_encoding(self) -> None:
        grant = Grant("t", "u", [Privilege.Insert])
        self.assertEqual(grant.encode().hex(), "0174017501" + "01")
        self.assertEqual(Grant.decode(grant.encode()), grant)
        encoded = bincode.serialize(("a", "b"), MEMBERSHIP)
        self.assertEqual(encoded.hex(), "01610162")
        self.assertEqual(bincode.deserialize(MEMBERSHIP, encoded), ("a", "b"))


if __name__ == "__main__":
    unittest.main()
