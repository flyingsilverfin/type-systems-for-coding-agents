"""Tests for MVCC. Mirrors the tests in `src/storage/mvcc.rs`.

Most storage tests are Goldenscripts under tests/storage/testscripts.
"""

import json
import queue
import tempfile
import unittest
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from tests import goldenscript
from tests.goldenscript import CommandParser, Context, arg
from tests.storage.test_engine import BinaryString, Emit, KeyRange, Mirror, Operation
from toydb.encoding import format
from toydb.fmt import debug
from toydb.ops import Bound
from toydb.storage.bitcask import BitCask
from toydb.storage.memory import Memory
from toydb.storage.mvcc import (
    MVCC,
    Key,
    KeyPrefix,
    ScanIterator,
    Transaction,
    TransactionState,
    Version,
)

# Don't fsync in tests, and use a small scan buffer to exercise the buffering
# code (like `cfg!(test)` in Rust).
BitCask.FSYNC = False
ScanIterator.BUFFER_SIZE = 2


def test_goldenscript(path: Path) -> None:
    """Runs the goldenscript tests in tests/storage/testscripts/mvcc."""
    runner = MVCCRunner()
    try:
        goldenscript.run(runner, path)
    finally:
        runner.close()


type TestEngine = Emit[Mirror[BitCask, Memory]]


class Command(ABC):
    """Commands accepted by the MVCC Goldenscript runner."""


@dataclass
class Begin(Command):
    """Begins a transaction selected by the command prefix."""

    readonly: str | None
    """The optional `readonly` transaction mode."""
    as_of: Version | None = arg(key=True)
    """The historical version for a read-only transaction."""


@dataclass
class Commit(Command):
    """Commits the transaction selected by the command prefix."""


@dataclass
class Delete(Command):
    """Deletes keys in the selected transaction."""

    _0: list[BinaryString]
    """The keys to delete."""


@dataclass
class Dump(Command):
    """Dumps all raw MVCC storage entries."""


@dataclass
class Get(Command):
    """Fetches keys from the selected transaction."""

    _0: list[BinaryString]
    """The keys to fetch."""


@dataclass
class GetUnversioned(Command):
    """Fetches unversioned keys."""

    _0: list[BinaryString]
    """The unversioned keys to fetch."""


@dataclass
class Import(Command):
    """Imports key/value pairs at an optional version."""

    version: Version | None
    """The version to import at."""
    entries: list[tuple[BinaryString, BinaryString]]
    """The key/value pairs to import."""


@dataclass
class Resume(Command):
    """Resumes a transaction from serialized state."""

    _0: str


@dataclass
class Rollback(Command):
    """Rolls back the transaction selected by the command prefix."""


@dataclass
class Scan(Command):
    """Scans a key range in the selected transaction."""

    _0: KeyRange = arg(optional=True)
    """The key range, or the full range if omitted."""


@dataclass
class ScanPrefix(Command):
    """Scans keys with a prefix in the selected transaction."""

    _0: BinaryString


@dataclass
class Set(Command):
    """Sets key/value pairs in the selected transaction."""

    _0: list[tuple[BinaryString, BinaryString]]


@dataclass
class SetUnversioned(Command):
    """Sets unversioned key/value pairs."""

    _0: list[tuple[BinaryString, BinaryString]]


@dataclass
class State(Command):
    """Displays the selected transaction state."""


@dataclass
class Status(Command):
    """Displays MVCC status."""


class MVCCRunner(goldenscript.Runner[Command]):
    """Runs MVCC goldenscript tests."""

    PARSER: ClassVar[CommandParser[Command]] = CommandParser.derive(Command)

    def __init__(self) -> None:
        # Use both a BitCask and a Memory engine, and mirror operations
        # across them. Emit engine operations to op_rx.
        self.op_rx: queue.Queue[Operation] = queue.Queue()
        self._tempdir = tempfile.TemporaryDirectory(prefix="toydb")
        self._bitcask = BitCask.new(Path(self._tempdir.name) / "bitcask")
        memory = Memory()
        engine = Emit(Mirror(self._bitcask, memory), self.op_rx)
        self.mvcc: MVCC[TestEngine] = MVCC(engine)
        self.txns: dict[str, Transaction[TestEngine]] = {}

    def close(self) -> None:
        """Closes the database and removes its temporary directory."""
        self._bitcask.close()
        self._tempdir.cleanup()

    def get_txn(self, prefix: str | None) -> Transaction[TestEngine]:
        """Fetches the named transaction from a command prefix."""
        name = self.txn_name(prefix)
        txn = self.txns.get(name)
        if txn is None:
            raise ValueError(f"unknown txn {name}")
        return txn

    @staticmethod
    def txn_name(prefix: str | None) -> str:
        """Fetches the txn name from a command prefix, or errors."""
        if prefix is None:
            raise ValueError("no txn name")
        return prefix

    @staticmethod
    def no_txn(name: str, context: Context) -> None:
        """Errors if a txn prefix is given."""
        if context.prefix is not None:
            raise ValueError(f"can't run {name} with txn {context.prefix}")

    def parse_command(self, command: goldenscript.Command) -> Command:
        return self.PARSER.try_from(command)

    def run(self, command: Command, context: Context) -> str:
        output = ""
        tags = set(context.tags)

        match command:
            case Begin(readonly_arg, as_of):
                name = self.txn_name(context.prefix)
                if name in self.txns:
                    raise ValueError(f"txn {name} already exists")
                match readonly_arg:
                    case "readonly":
                        readonly = True
                    case None:
                        readonly = False
                    case v:
                        raise ValueError(f"invalid argument {v}")
                match (readonly, as_of):
                    case (False, None):
                        txn = self.mvcc.begin()
                    case (True, None):
                        txn = self.mvcc.begin_read_only()
                    case (True, version):
                        txn = self.mvcc.begin_as_of(version)
                    case _:
                        raise ValueError("as_of only valid for read-only txn")
                self.txns[name] = txn

            case Commit():
                name = self.txn_name(context.prefix)
                if name not in self.txns:
                    raise ValueError(f"unknown txn {name}")
                self.txns.pop(name).commit()

            case Delete(keys):
                txn = self.get_txn(context.prefix)
                for key in keys:
                    txn.delete(bytes(key))

            case Dump():
                with self.mvcc.engine.lock() as engine:
                    for key, value in engine.scan((Bound.Unbounded(), Bound.Unbounded())):
                        fmtkv = format.MVCC(format.Raw()).key_value(key, value)
                        rawkv = format.Raw().key_value(key, value)
                        output += f"{fmtkv} [{rawkv}]\n"

            case Get(keys):
                txn = self.get_txn(context.prefix)
                for key in keys:
                    value = txn.get(bytes(key))
                    output += f"{format.Raw().key_maybe_value(key, value)}\n"

            case GetUnversioned(keys):
                self.no_txn("get_unversioned", context)
                for key in keys:
                    value = self.mvcc.get_unversioned(bytes(key))
                    output += f"{format.Raw().key_maybe_value(key, value)}\n"

            case Import(version, entries):
                self.no_txn("import", context)
                txn = self.mvcc.begin()
                if version is not None:
                    if txn.version() > version:
                        raise ValueError(f"version {version} already used")
                    while txn.version() < version:
                        txn = self.mvcc.begin()
                for key, value in entries:
                    if not value:
                        txn.delete(bytes(key))
                    else:
                        txn.set(bytes(key), bytes(value))
                txn.commit()

            case Resume(raw):
                name = self.txn_name(context.prefix)
                fields = json.loads(raw)
                state = TransactionState(
                    version=fields["version"],
                    read_only=fields["read_only"],
                    active=set(fields["active"]),
                )
                self.txns[name] = self.mvcc.resume(state)

            case Rollback():
                name = self.txn_name(context.prefix)
                if name not in self.txns:
                    raise ValueError(f"unknown txn {name}")
                self.txns.pop(name).rollback()

            case Scan(range):
                txn = self.get_txn(context.prefix)

                kvs = list(txn.scan(range.bounds()))
                for key, value in kvs:
                    output += f"{format.Raw().key_value(key, value)}\n"

            case ScanPrefix(prefix):
                txn = self.get_txn(context.prefix)

                kvs = list(txn.scan_prefix(bytes(prefix)))
                for key, value in kvs:
                    output += f"{format.Raw().key_value(key, value)}\n"

            case Set(entries):
                txn = self.get_txn(context.prefix)
                for key, value in entries:
                    txn.set(bytes(key), bytes(value))

            case SetUnversioned(entries):
                self.no_txn("set_unversioned", context)
                for key, value in entries:
                    self.mvcc.set_unversioned(bytes(key), bytes(value))

            case State():
                txn = self.get_txn(context.prefix)
                state = txn.state()
                output += "v{} {} active={{{}}}".format(
                    state.version,
                    "ro" if state.read_only else "rw",
                    ",".join(str(version) for version in sorted(state.active)),
                )

            case Status():
                output += f"{debug(self.mvcc.status(), pretty=True)}\n"

            case _:
                raise TypeError(f"unknown command {command!r}")

        # If requested, output engine operations.
        if "ops" in tags:
            tags.remove("ops")
            while True:
                try:
                    op = self.op_rx.get_nowait()
                except queue.Empty:
                    break
                match op:
                    case Operation.Delete(key):
                        fmtkey = format.MVCC(format.Raw()).key(key)
                        rawkey = format.Raw().key(key)
                        output += f"engine delete {fmtkey} [{rawkey}]\n"
                    case Operation.Flush():
                        output += "engine flush\n"
                    case Operation.Set(key, value):
                        fmtkv = format.MVCC(format.Raw()).key_value(key, value)
                        rawkv = format.Raw().key_value(key, value)
                        output += f"engine set {fmtkv} [{rawkv}]\n"
                    case _:
                        raise TypeError(f"unknown operation {op!r}")

        if tags:
            raise ValueError(f"unknown tag {min(tags)}")

        return output

    def end_command(self, command: Command, context: Context) -> str:
        # Drain unhandled engine operations.
        while True:
            try:
                self.op_rx.get_nowait()
            except queue.Empty:
                break
        return ""


class TestMVCC(unittest.TestCase):
    """The unit tests in src/storage/mvcc.rs."""

    def test_key_prefix(self) -> None:
        """Tests that key prefixes are actually prefixes of keys."""
        cases: list[tuple[KeyPrefix, Key]] = [
            (KeyPrefix.NextVersion(), Key.NextVersion()),
            (KeyPrefix.TxnActive(), Key.TxnActive(1)),
            (KeyPrefix.TxnActiveSnapshot(), Key.TxnActiveSnapshot(1)),
            (KeyPrefix.TxnWrite(1), Key.TxnWrite(1, b"foo")),
            (KeyPrefix.Version(b"foo"), Key.Version(b"foo", 1)),
            (KeyPrefix.Unversioned(), Key.Unversioned(b"foo")),
        ]
        for prefix, key in cases:
            with self.subTest(prefix=prefix):
                encoded_prefix = prefix.encode()
                encoded_key = key.encode()
                self.assertEqual(encoded_prefix, encoded_key[: len(encoded_prefix)])

    def test_goldenscripts(self) -> None:
        """Runs the MVCC goldenscripts."""
        scripts = Path(__file__).resolve().parent / "testscripts/mvcc"
        for path in sorted(p for p in scripts.iterdir() if p.is_file()):
            with self.subTest(path=path):
                test_goldenscript(path)


if __name__ == "__main__":
    unittest.main()
