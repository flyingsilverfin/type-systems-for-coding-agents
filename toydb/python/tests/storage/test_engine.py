"""Test helpers for engines. Mirrors the `#[cfg(test)] pub mod test` in
`src/storage/engine.rs`."""

import queue
import re
from abc import ABC
from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar

from tests import goldenscript
from tests.goldenscript import CommandParser, Context, arg
from toydb.encoding import format
from toydb.fmt import debug
from toydb.ops import Bound, Range
from toydb.storage import engine
from toydb.storage.engine import Engine, ScanIterator

E = TypeVar("E", bound=Engine)
A = TypeVar("A", bound=Engine)
B = TypeVar("B", bound=Engine)


class BinaryString(bytes):
    """A string parsed into its binary byte representation.

    Code points U+0080 through U+00FF are converted directly to bytes 0x80
    through 0xff. This allows using e.g. `\\xff` in an input string literal
    to represent the byte 0xff rather than its UTF-8 encoding 0xc3bf.
    """

    @classmethod
    def from_str(cls, value: str) -> "BinaryString":
        data = bytearray()
        for c in value:
            # ord() is the Unicode code point, not the UTF-8 encoding.
            if 0x80 <= ord(c) <= 0xFF:
                data.append(ord(c))
            else:
                data += c.encode()
        return cls(data)


@dataclass(frozen=True)
class KeyRange:
    """A binary key range parsed from Rust range syntax and BinaryString."""

    start: Bound[bytes] = field(default_factory=Bound.Unbounded)
    end: Bound[bytes] = field(default_factory=Bound.Unbounded)

    @classmethod
    def from_str(cls, value: str) -> "KeyRange":
        groups = re.match(r"^(\S+)?\.\.(=)?(\S+)?", value)
        if groups is None:
            raise ValueError(f"invalid range {value}")
        start: Bound[bytes] = Bound.Unbounded()
        end: Bound[bytes] = Bound.Unbounded()
        if groups[1] is not None:
            start = Bound.Included(bytes(BinaryString.from_str(groups[1])))
        if groups[3] is not None:
            key = bytes(BinaryString.from_str(groups[3]))
            end = Bound.Included(key) if groups[2] is not None else Bound.Excluded(key)
        return cls(start, end)

    def bounds(self) -> Range[bytes]:
        """Returns the range as a pair of bounds, for scans."""
        return (self.start, self.end)


class Command(ABC):
    """Commands accepted by the engine Goldenscript runner."""


@dataclass
class Delete(Command):
    """Deletes a key."""

    _0: BinaryString


@dataclass
class Get(Command):
    """Fetches a key."""

    _0: BinaryString


@dataclass
class Scan(Command):
    """Scans a key range."""

    range: KeyRange = arg(optional=True)
    """The key range in Rust range syntax, or the full range if omitted."""
    reverse: bool = arg(key=True, optional=True)
    """Whether to scan in reverse."""


@dataclass
class ScanPrefix(Command):
    """Scans all keys with the given prefix."""

    _0: BinaryString


@dataclass
class Set(Command):
    """Sets a key/value pair."""

    _0: list[tuple[BinaryString, BinaryString]]
    """The single key/value pair to set."""


@dataclass
class Status(Command):
    """Displays engine status."""


class Runner(goldenscript.Runner[Command], Generic[E]):
    """Goldenscript runner for engines. All engines use a common set of
    goldenscripts in src/storage/testscripts/engine, as well as their own
    engine-specific tests."""

    PARSER: ClassVar[CommandParser[Command]] = CommandParser.derive(Command)

    def __init__(self, engine: E) -> None:
        self.engine = engine

    def parse_command(self, command: goldenscript.Command) -> Command:
        return self.PARSER.try_from(command)

    def run(self, command: Command, context: Context) -> str:
        output = ""
        match command:
            case Delete(key):
                self.engine.delete(bytes(key))

            case Get(key):
                value = self.engine.get(bytes(key))
                output += f"{format.Raw().key_maybe_value(key, value)}\n"

            case Scan(range, reverse):
                scan = self.engine.scan(range.bounds())
                items = list(scan.rev()) if reverse else list(scan)
                for key, value in items:
                    output += f"{format.Raw().key_value(key, value)}\n"

            case ScanPrefix(prefix):
                for key, value in self.engine.scan_prefix(bytes(prefix)):
                    output += f"{format.Raw().key_value(key, value)}\n"

            case Set(entries):
                if len(entries) != 1:
                    raise ValueError("must specify one key=value pair")
                [(key, value)] = entries
                self.engine.set(bytes(key), bytes(value))

            case Status():
                output += f"{debug(self.engine.status(), pretty=True)}\n"

            case _:
                raise TypeError(f"unknown command {command!r}")
        return output


class Operation(ABC):
    """An engine operation emitted by the Emit engine."""

    Delete: ClassVar[type["OperationDelete"]]
    Flush: ClassVar[type["OperationFlush"]]
    Set: ClassVar[type["OperationSet"]]


@dataclass(frozen=True)
class OperationDelete(Operation):
    key: bytes


@dataclass(frozen=True)
class OperationFlush(Operation):
    pass


@dataclass(frozen=True)
class OperationSet(Operation):
    key: bytes
    value: bytes


Operation.Delete = OperationDelete
Operation.Flush = OperationFlush
Operation.Set = OperationSet


class Emit(Engine, Generic[E]):
    """Wraps another engine and emits write events to the given channel."""

    def __init__(self, inner: E, tx: "queue.Queue[Operation]") -> None:
        self.inner = inner
        """The wrapped engine."""
        self.tx = tx
        """Sends operation events."""

    def flush(self) -> None:
        self.inner.flush()
        self.tx.put(Operation.Flush())

    def delete(self, key: bytes) -> None:
        self.inner.delete(key)
        self.tx.put(Operation.Delete(key=bytes(key)))

    def get(self, key: bytes) -> bytes | None:
        return self.inner.get(key)

    def scan(self, range: Range[bytes]) -> ScanIterator:
        return self.inner.scan(range)

    def set(self, key: bytes, value: bytes) -> None:
        self.inner.set(key, value)
        self.tx.put(Operation.Set(key=bytes(key), value=bytes(value)))

    def status(self) -> engine.Status:
        return self.inner.status()


class Mirror(Engine, Generic[A, B]):
    """An engine that wraps two others and mirrors operations across them,
    panicking if they produce different results. Engine implementations
    should not have any observable differences in behavior."""

    def __init__(self, a: A, b: B) -> None:
        self.a = a
        self.b = b

    def delete(self, key: bytes) -> None:
        self.a.delete(key)
        self.b.delete(key)

    def flush(self) -> None:
        self.a.flush()
        self.b.flush()

    def get(self, key: bytes) -> bytes | None:
        a = self.a.get(key)
        b = self.b.get(key)
        assert a == b, f"{a!r} != {b!r}"
        return a

    def scan(self, range: Range[bytes]) -> "MirrorIterator":
        a = self.a.scan(range)
        b = self.b.scan(range)
        return MirrorIterator(a, b)

    def set(self, key: bytes, value: bytes) -> None:
        self.a.set(key, value)
        self.b.set(key, value)

    def status(self) -> engine.Status:
        a = self.a.status()
        b = self.b.status()
        # Only some items are comparable.
        assert a.keys == b.keys, f"{a.keys} != {b.keys}"
        assert a.size == b.size, f"{a.size} != {b.size}"
        return a


class MirrorIterator(ScanIterator):
    """Iterates over both of the mirrored engines, comparing their items."""

    def __init__(self, a: ScanIterator, b: ScanIterator) -> None:
        self.a = a
        self.b = b

    def __next__(self) -> tuple[bytes, bytes]:
        a = next(self.a, None)
        b = next(self.b, None)
        assert a == b, f"{a!r} != {b!r}"
        if a is None:
            raise StopIteration
        return a

    def next_back(self) -> tuple[bytes, bytes]:
        a = _next_back(self.a)
        b = _next_back(self.b)
        assert a == b, f"{a!r} != {b!r}"
        if a is None:
            raise StopIteration
        return a


def _next_back(iterator: ScanIterator) -> tuple[bytes, bytes] | None:
    """Returns the next item from the back of the iterator, or None."""
    try:
        return iterator.next_back()
    except StopIteration:
        return None
