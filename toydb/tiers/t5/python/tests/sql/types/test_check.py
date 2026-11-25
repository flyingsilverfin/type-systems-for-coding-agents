"""Tests for toydb.sql.types.check."""

import unittest

from toydb.encoding import bincode
from toydb.sql.types.check import CHECKS, Check


class TestCheck(unittest.TestCase):
    def test_encoding(self) -> None:
        """A constraint is encoded as its name and condition; a table's
        constraints as a list of them."""
        check = Check("c", "id > 0")
        self.assertEqual(check.encode().hex(), "0163" + "06" + b"id > 0".hex())
        self.assertEqual(Check.decode(check.encode()), check)
        checks = [check, Check("t_check2", "n IS NOT NULL")]
        self.assertEqual(bincode.deserialize(CHECKS, bincode.serialize(checks, CHECKS)), checks)
        self.assertEqual(bincode.serialize([], CHECKS).hex(), "00")

    def test_display(self) -> None:
        """A constraint is displayed as a table constraint, with its name as
        in the schema display."""
        self.assertEqual(str(Check("t_check1", "id > 0")), "CONSTRAINT t_check1 CHECK (id > 0)")
        self.assertEqual(str(Check("Max", "id < 9")), "CONSTRAINT Max CHECK (id < 9)")
        self.assertEqual(
            str(Check("check", "s != 'it''s'")), "CONSTRAINT \"check\" CHECK (s != 'it''s')"
        )
        self.assertEqual(str(Check("a b", "TRUE")), 'CONSTRAINT "a b" CHECK (TRUE)')


if __name__ == "__main__":
    unittest.main()
