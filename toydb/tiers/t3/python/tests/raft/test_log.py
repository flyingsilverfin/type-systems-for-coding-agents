"""Tests for the Raft log. Mirrors the tests in `src/raft/log.rs`.

Most Raft tests are Goldenscripts under tests/raft/testscripts.
"""

import queue
import re
import tempfile
import unittest
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from tests import goldenscript
from tests.goldenscript import CommandParser, Context, arg
from tests.storage.test_engine import Emit, Mirror, Operation
from toydb.encoding import format
from toydb.fmt import debug
from toydb.ops import Bound, Range
from toydb.raft.log import Entry, Index, Log
from toydb.raft.node import NodeID, Term
from toydb.storage.bitcask import BitCask
from toydb.storage.memory import Memory

# Don't fsync in tests (like `cfg!(test)` in Rust).
BitCask.FSYNC = False


def test_goldenscript(path: Path) -> None:
    """Runs the goldenscript tests in tests/raft/testscripts/log."""
    runner = TestRunner()
    try:
        goldenscript.run(runner, path)
    finally:
        runner.close()


@dataclass(frozen=True)
class IndexTerm:
    """An index and term pair parsed from INDEX@TERM."""

    index: Index
    term: Term

    @classmethod
    def from_str(cls, value: str) -> "IndexTerm":
        groups = re.match(r"^(\d+)@(\d+)$", value)
        if groups is None:
            raise ValueError(f"invalid index/term {value}")
        return cls(int(groups[1]), int(groups[2]))


@dataclass(frozen=True)
class IndexRange:
    """An index range parsed from Rust range syntax."""

    start: Bound[Index] = field(default_factory=Bound.Unbounded)
    end: Bound[Index] = field(default_factory=Bound.Unbounded)

    @classmethod
    def from_str(cls, value: str) -> "IndexRange":
        groups = re.match(r"^(\d+)?\.\.(=)?(\d+)?", value)
        if groups is None:
            raise ValueError(f"invalid range {value}")
        start: Bound[Index] = Bound.Unbounded()
        end: Bound[Index] = Bound.Unbounded()
        if groups[1] is not None:
            start = Bound.Included(int(groups[1]))
        if groups[3] is not None:
            index = int(groups[3])
            end = Bound.Included(index) if groups[2] is not None else Bound.Excluded(index)
        return cls(start, end)

    def bounds(self) -> Range[Index]:
        """Returns the range as a pair of bounds, for scans."""
        return (self.start, self.end)


class Command(ABC):
    """Commands accepted by the TestRunner."""


@dataclass
class Append(Command):
    """Appends an entry to the Raft log."""

    _0: str | None
    """The entry command. Appending a None entry is valid, and happens
    on leader changes."""


@dataclass
class Commit(Command):
    """Commits a Raft log entry."""

    _0: Index
    """The index to commit."""


@dataclass
class Dump(Command):
    """Dumps all raw Raft storage entries."""


@dataclass
class Get(Command):
    """Fetches Raft log entries."""

    _0: list[Index]
    """The indexes to fetch."""


@dataclass
class GetTerm(Command):
    """Displays the current term and vote."""


@dataclass
class Has(Command):
    """Checks whether index/term pairs exist."""

    _0: list[IndexTerm]
    """The index/term pairs to check."""


@dataclass
class Reload(Command):
    """Reloads the Raft log from storage."""


@dataclass
class Scan(Command):
    """Scans a Raft log index range."""

    _0: IndexRange = arg(optional=True)
    """The index range, or the full range if omitted."""


@dataclass
class ScanApply(Command):
    """Scans entries to apply after an index."""

    _0: Index
    """The last applied index."""


@dataclass
class SetTerm(Command):
    """Sets the current term and optional vote."""

    _0: Term
    """The term to set."""
    _1: NodeID | None
    """The node we voted for in this term, if any."""


@dataclass
class Splice(Command):
    """Splices entries into the Raft log."""

    _0: list[tuple[IndexTerm, str]] = arg(optional=True)
    """The index/term and command entries to splice."""


@dataclass
class Status(Command):
    """Displays Raft log status."""

    engine: bool = arg(key=True, optional=True)
    """Whether to include storage engine status."""


class TestRunner(goldenscript.Runner[Command]):
    """Runs Raft log goldenscript tests."""

    __test__ = False  # not a unittest/pytest test class

    PARSER: ClassVar[CommandParser[Command]] = CommandParser.derive(Command)

    def __init__(self) -> None:
        # Use both a BitCask and a Memory engine, and mirror operations
        # across them. Emit write events to op_rx.
        self.op_rx: queue.Queue[Operation] = queue.Queue()
        self._tempdir = tempfile.TemporaryDirectory(prefix="toydb")
        self._bitcask = BitCask.new(Path(self._tempdir.name) / "bitcask")
        memory = Memory()
        engine = Emit(Mirror(self._bitcask, memory), self.op_rx)
        self.log = Log(engine)

    def close(self) -> None:
        """Closes the log engine and removes its temporary directory."""
        self._bitcask.close()
        self._tempdir.cleanup()

    def parse_command(self, command: goldenscript.Command) -> Command:
        return self.PARSER.try_from(command)

    def run(self, command: Command, context: Context) -> str:
        output = ""
        raft = format.Raft(format.Raw())

        match command:
            case Append(value):
                index = self.log.append(value.encode() if value is not None else None)
                entry = self.log.get(index)
                assert entry is not None, "entry not found"
                output += f"append → {raft.entry(entry)}\n"

            case Commit(index):
                index = self.log.commit(index)
                entry = self.log.get(index)
                assert entry is not None, "entry not found"
                output += f"commit → {raft.entry(entry)}\n"

            case Dump():
                range: Range[bytes] = (Bound.Unbounded(), Bound.Unbounded())
                for key, value in self.log.engine.scan_dyn(range):
                    fmtkv = raft.key_value(key, value)
                    rawkv = format.Raw().key_value(key, value)
                    output += f"{fmtkv} [{rawkv}]\n"

            case Get(indexes):
                for index in indexes:
                    entry = self.log.get(index)
                    fmtentry = raft.entry(entry) if entry is not None else "None"
                    output += f"{fmtentry}\n"

            case GetTerm():
                term, vote = self.log.get_term_vote()
                output += f"term={term} vote={'None' if vote is None else vote}\n"

            case Has(indexes):
                for index_term in indexes:
                    has = self.log.has(index_term.index, index_term.term)
                    output += f"{'true' if has else 'false'}\n"

            case Reload():
                self.log = Log(self.log.engine)

            case Scan(index_range):
                for entry in self.log.scan(index_range.bounds()):
                    output += f"{raft.entry(entry)}\n"

            case ScanApply(applied_index):
                for entry in self.log.scan_apply(applied_index):
                    output += f"{raft.entry(entry)}\n"

            case SetTerm(term, vote):
                self.log.set_term_vote(term, vote)

            case Splice(values):
                entries = []
                for index_term, value in values:
                    entry_command = value.encode() if value != "" else None
                    entries.append(
                        Entry(index=index_term.index, term=index_term.term, command=entry_command)
                    )
                index = self.log.splice(entries)
                entry = self.log.get(index)
                assert entry is not None, "entry not found"
                output += f"splice → {raft.entry(entry)}\n"

            case Status(engine):
                term, vote = self.log.get_term_vote()
                last_index, last_term = self.log.get_last_index()
                commit_index, commit_term = self.log.get_commit_index()
                output += (
                    f"term={term} last={last_index}@{last_term} "
                    f"commit={commit_index}@{commit_term} vote={'None' if vote is None else vote}"
                )
                if engine:
                    output += f" engine={debug(self.log.status(), pretty=True)}"
                output += "\n"

            case _:
                raise TypeError(f"unknown command {command!r}")

        # If requested, output engine operations.
        tags = set(context.tags)
        if "ops" in tags:
            tags.remove("ops")
            while True:
                try:
                    op = self.op_rx.get_nowait()
                except queue.Empty:
                    break
                match op:
                    case Operation.Delete(key):
                        output += f"engine delete {raft.key(key)} [{format.Raw().key(key)}]\n"
                    case Operation.Flush():
                        output += "engine flush\n"
                    case Operation.Set(key, value):
                        fmtkv = raft.key_value(key, value)
                        rawkv = format.Raw().key_value(key, value)
                        output += f"engine set {fmtkv} [{rawkv}]\n"
                    case _:
                        raise TypeError(f"unknown operation {op!r}")

        if tags:
            raise ValueError(f"unknown tag {next(iter(tags))}")

        return output

    def end_command(self, command: Command, context: Context) -> str:
        # Drain any remaining engine operations.
        while not self.op_rx.empty():
            self.op_rx.get_nowait()
        return ""


class Goldenscripts(unittest.TestCase):
    """Runs the Raft log goldenscripts as unit tests."""

    def test_goldenscripts(self) -> None:
        scripts = Path(__file__).resolve().parent / "testscripts/log"
        for path in sorted(p for p in scripts.iterdir() if p.is_file()):
            with self.subTest(path=path):
                test_goldenscript(path)


if __name__ == "__main__":
    unittest.main()
