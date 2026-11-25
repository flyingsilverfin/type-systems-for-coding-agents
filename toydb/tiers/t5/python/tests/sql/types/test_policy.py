"""Tests for toydb.sql.types.policy."""

import unittest

from toydb.encoding import bincode
from toydb.sql.types.policy import POLICIES, Policy


class TestPolicy(unittest.TestCase):
    def test_encoding(self) -> None:
        """A policy is encoded as its table, name, optional user and the text
        of its conditions."""
        policy = Policy("t", "p", None, "TRUE", None)
        self.assertEqual(policy.encode().hex(), "0174" + "0170" + "00" + "04" + b"TRUE".hex() + "00")
        self.assertEqual(Policy.decode(policy.encode()), policy)
        policy = Policy("t", "p", "u", "id > 0", "id > 1")
        self.assertEqual(Policy.decode(policy.encode()), policy)
        policies = [policy, Policy("t", "q", None, "FALSE", None)]
        self.assertEqual(
            bincode.deserialize(POLICIES, bincode.serialize(policies, POLICIES)), policies
        )

    def test_display(self) -> None:
        """A policy is displayed as its CREATE POLICY statement, with names
        quoted as in canonical text."""
        self.assertEqual(
            str(Policy("t", "p", None, "id > 0", None)), "CREATE POLICY p ON t USING (id > 0)"
        )
        self.assertEqual(
            str(Policy("T", "select", "Alice", "TRUE", "v != 'it''s'")),
            'CREATE POLICY "select" ON "T" TO "Alice" USING (TRUE) WITH CHECK (v != \'it\'\'s\')',
        )


if __name__ == "__main__":
    unittest.main()
