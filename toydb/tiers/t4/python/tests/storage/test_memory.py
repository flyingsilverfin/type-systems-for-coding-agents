"""Tests for the Memory engine. Mirrors the tests in `src/storage/memory.rs`.

Most storage tests are Goldenscripts under tests/storage/testscripts.
"""

import unittest
from pathlib import Path

from tests import goldenscript
from tests.storage.test_engine import Runner
from toydb.storage.memory import Memory


def test_goldenscript(path: Path) -> None:
    """Runs the common goldenscript tests in tests/storage/testscripts/engine,
    and the Memory-specific tests in tests/storage/testscripts/memory."""
    goldenscript.run(Runner(Memory()), path)


class TestMemory(unittest.TestCase):
    """The tests in src/storage/memory.rs."""

    def test_goldenscripts(self) -> None:
        """Runs the engine and Memory goldenscripts."""
        scripts = Path(__file__).resolve().parent / "testscripts"
        for name in ("engine", "memory"):
            for path in sorted(p for p in (scripts / name).iterdir() if p.is_file()):
                with self.subTest(path=path):
                    test_goldenscript(path)


if __name__ == "__main__":
    unittest.main()
