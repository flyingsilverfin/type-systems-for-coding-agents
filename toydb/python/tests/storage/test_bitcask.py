"""Tests for the BitCask engine. Mirrors the tests in `src/storage/bitcask.rs`.

Most storage tests are Goldenscripts under src/storage/testscripts.
"""

import io
import os
import shutil
import tempfile
import unittest
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from tests import goldenscript
from tests.goldenscript import CommandParser, Context, arg
from tests.storage.test_engine import Runner
from toydb.encoding import format
from toydb.error import Error
from toydb.ops import Bound
from toydb.storage.bitcask import BitCask, Log, read_exact

# Don't fsync in tests, to speed them up (like `#[cfg(not(test))]` in Rust).
BitCask.FSYNC = False


def test_goldenscript(path: Path) -> None:
    """Runs the common goldenscript tests in src/storage/testscripts/engine,
    and the BitCask-specific tests in src/storage/testscripts/bitcask."""
    runner = BitCaskRunner()
    try:
        goldenscript.run(runner, path)
    finally:
        runner.close()


class BitCaskCommand(ABC):
    """BitCask-specific Goldenscript commands."""


@dataclass
class Compact(BitCaskCommand):
    """Compacts the BitCask entry log."""


@dataclass
class Dump(BitCaskCommand):
    """Dumps the BitCask entry log."""


@dataclass
class Reopen(BitCaskCommand):
    """Closes and reopens the BitCask database."""

    compact_fraction: float | None = arg(key=True)
    """The garbage fraction that triggers compaction on open."""


@goldenscript.command(other=True)
@dataclass
class BitCaskCommandEngine(BitCaskCommand):
    """Delegates all other commands to the engine runner."""

    _0: goldenscript.Command


class BitCaskRunner(goldenscript.Runner[BitCaskCommand]):
    """A BitCask-specific goldenscript runner, which dispatches through to the
    standard Engine runner."""

    PARSER: ClassVar[CommandParser[BitCaskCommand]] = CommandParser.derive(BitCaskCommand)

    def __init__(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="toydb")
        engine = BitCask.new(Path(self.tempdir.name) / "bitcask")
        self.inner: Runner[BitCask] = Runner(engine)

    def close(self) -> None:
        """Closes the database and removes its temporary directory."""
        self.inner.engine.close()
        self.tempdir.cleanup()

    def parse_command(self, command: goldenscript.Command) -> BitCaskCommand:
        return self.PARSER.try_from(command)

    def run(self, command: BitCaskCommand, context: Context) -> str:
        output = ""
        match command:
            case Compact():
                self.inner.engine.compact()

            case Dump():
                output += self.dump()

            case Reopen(compact_fraction):
                # We need to close the file before we can reopen it.
                path = self.inner.engine.log.path
                self.inner.engine.close()
                if compact_fraction is not None:
                    self.inner.engine = BitCask.new_maybe_compact(path, compact_fraction, 0)
                else:
                    self.inner.engine = BitCask.new(path)

            case BitCaskCommandEngine(engine_command):
                return self.inner.run(self.inner.parse_command(engine_command), context)

            case _:
                raise TypeError(f"unknown command {command!r}")
        return output

    def dump(self) -> str:
        """Dumps the full BitCask entry log."""
        output = ""
        file = self.inner.engine.log.file
        file_len = os.fstat(file.fileno()).st_size
        r = io.BufferedReader(file)
        try:
            pos = r.seek(0)
            idx = 0

            while pos < file_len:
                if idx > 0:
                    output += "--------\n"
                output += f"{f'{idx}@{pos}':<7}"

                len_buf = read_exact(r, 4)
                key_len = int.from_bytes(len_buf, "big")
                output += f" keylen={key_len} [{len_buf.hex()}]"

                len_buf = read_exact(r, 4)
                # NB: -1 for tombstones
                value_len_or_tombstone = int.from_bytes(len_buf, "big", signed=True)
                value_len = max(value_len_or_tombstone, 0)
                output += f" valuelen={value_len_or_tombstone} [{len_buf.hex()}]\n"

                key = read_exact(r, key_len)
                value = read_exact(r, value_len)
                size = 4 + 4 + key_len + value_len
                if value_len_or_tombstone == -1:
                    fmtvalue = "tombstone"
                else:
                    fmtvalue = f"value={format.Raw.bytes(value)} [{value.hex()}]"
                output += (
                    f"{f'{size}b':<7} key={format.Raw().key(key)} [{key.hex()}] {fmtvalue}\n"
                )

                pos += size
                idx += 1
        finally:
            r.detach()  # don't close the file with the reader
        return output


class TestBitCask(unittest.TestCase):
    """The unit tests in src/storage/bitcask.rs."""

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="toydb")
        self.addCleanup(tempdir.cleanup)
        self.dir = Path(tempdir.name)

    def test_lock(self) -> None:
        """Tests that exclusive locks are taken out on log files, erroring if
        held, and released when the database is closed."""
        path = self.dir / "bitcask"
        engine = BitCask.new(path)

        # Opening another database with the same file should error.
        with self.assertRaises(Error.IO):
            BitCask.new(path)

        # Opening another database after the current is closed works.
        engine.close()
        BitCask.new(path).close()

    def test_recovery(self) -> None:
        """Tests that a log with an incomplete write at the end can be
        recovered by discarding the last entry."""
        # Create an initial log file with a few entries. Keep track of where
        # each entry ends.
        path = self.dir / "complete"
        log = Log(path)

        ends = [
            log.write_entry(b"deleted", bytes([1, 2, 3])).end(),
            log.write_entry(b"deleted", None).end(),
            log.write_entry(b"", b"").end(),
            log.write_entry(b"key", bytes([1, 2, 3, 4, 5])).end(),
        ]
        log.close()

        # Copy the file, and truncate it at each byte, then try to open it
        # and assert that we always retain a prefix of entries.
        truncpath = self.dir / "truncated"
        size = path.stat().st_size
        for pos in range(size + 1):
            shutil.copyfile(path, truncpath)
            os.truncate(truncpath, pos)

            expect: list[tuple[bytes, bytes]] = []
            if pos >= ends[0]:
                expect.append((b"deleted", bytes([1, 2, 3])))
            if pos >= ends[1]:
                expect.pop()  # "deleted" key removed
            if pos >= ends[2]:
                expect.append((b"", b""))
            if pos >= ends[3]:
                expect.append((b"key", bytes([1, 2, 3, 4, 5])))

            with self.assertLogs("toydb.storage.bitcask", "INFO"):
                engine = BitCask.new(truncpath)
            self.assertEqual(expect, list(engine.scan((Bound.Unbounded(), Bound.Unbounded()))))
            engine.close()

    def test_point_ops_sizes(self) -> None:
        """Tests key/value sizes up to 64 MB."""
        engine = BitCask.new(self.dir / "bitcask")
        self.addCleanup(engine.close)

        # Generate keys/values for increasing powers of two.
        for size in (1 << i for i in range(1, 27)):
            value = b"x" * size
            key = value

            self.assertIsNone(engine.get(key))
            engine.set(key, value)
            self.assertEqual(engine.get(key), value)
            engine.delete(key)
            self.assertIsNone(engine.get(key))

    def test_goldenscripts(self) -> None:
        """Runs the engine and BitCask goldenscripts."""
        scripts = Path(__file__).resolve().parents[3] / "rust/src/storage/testscripts"
        for name in ("engine", "bitcask"):
            for path in sorted(p for p in (scripts / name).iterdir() if p.is_file()):
                with self.subTest(path=path):
                    test_goldenscript(path)


if __name__ == "__main__":
    unittest.main()
