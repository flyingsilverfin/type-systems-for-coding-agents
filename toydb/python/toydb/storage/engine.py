"""The storage engine interface. Mirrors `src/storage/engine.rs`.

The engine test helpers (`#[cfg(test)] pub mod test`) are in
`tests/storage/test_engine.py`.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from toydb.encoding import keycode
from toydb.encoding.serde import Deserializer, Serializer
from toydb.ops import Range
from toydb.storage.btree import DoubleEndedIterator


class ScanIterator(DoubleEndedIterator[tuple[bytes, bytes]], ABC):
    """A scan iterator over key/value pairs, returned by `Engine.scan()`. It
    can be iterated from both ends, see `DoubleEndedIterator`. Raises
    `toydb.error.Error` if an item can't be read."""


class Engine(ABC):
    """A key/value storage engine, which stores arbitrary byte strings. Keys are
    maintained in lexicographical order, which allows for range scans. This is
    needed e.g. to scan all rows in a specific SQL table (where all table rows
    have a common key prefix), or to scan the tail of the Raft log (after a
    given log entry index).

    Keys should use the Keycode order-preserving encoding, see
    `toydb.encoding.keycode`.

    Writes are only guaranteed durable after calling `Engine.flush()`.

    For simplicity, this only supports a single user at a time. This isn't that
    big of a deal since Raft execution is serial anyway.
    """

    @abstractmethod
    def delete(self, key: bytes) -> None:
        """Deletes a key, or does nothing if it does not exist."""

    @abstractmethod
    def flush(self) -> None:
        """Flushes any buffered data to disk."""

    @abstractmethod
    def get(self, key: bytes) -> bytes | None:
        """Gets a value for a key, if it exists."""

    @abstractmethod
    def scan(self, range: Range[bytes]) -> ScanIterator:
        """Iterates over an ordered range of key/value pairs."""

    def scan_dyn(self, range: Range[bytes]) -> ScanIterator:
        """Like scan, but can be used from trait objects (with dynamic
        dispatch). In Python this is the same as scan."""
        return self.scan(range)

    def scan_prefix(self, prefix: bytes) -> ScanIterator:
        """Iterates over all key/value pairs starting with the given prefix."""
        return self.scan(keycode.prefix_range(prefix))

    @abstractmethod
    def set(self, key: bytes, value: bytes) -> None:
        """Sets a value for a key, replacing the existing value if any."""

    @abstractmethod
    def status(self) -> "Status":
        """Returns the engine status."""


@dataclass(frozen=True)
class Status:
    """Engine status."""

    name: str
    """The name of the storage engine."""
    keys: int
    """The number of live keys in the engine."""
    size: int
    """The logical size of live key/value pairs."""
    disk_size: int
    """The on-disk size of all data, live and garbage."""
    live_disk_size: int
    """The on-disk size of live data, excluding garbage."""

    def garbage_disk_size(self) -> int:
        """The on-disk size of garbage data."""
        return self.disk_size - self.live_disk_size

    def garbage_disk_percent(self) -> float:
        """The ratio of on-disk garbage to total size."""
        if self.disk_size == 0:
            return 0.0
        return self.garbage_disk_size() / self.disk_size * 100.0

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.name)
        serializer.serialize_u64(self.keys)
        serializer.serialize_u64(self.size)
        serializer.serialize_u64(self.disk_size)
        serializer.serialize_u64(self.live_disk_size)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Status":
        deserializer.deserialize_struct()
        return cls(
            name=deserializer.deserialize_str(),
            keys=deserializer.deserialize_u64(),
            size=deserializer.deserialize_u64(),
            disk_size=deserializer.deserialize_u64(),
            live_disk_size=deserializer.deserialize_u64(),
        )
