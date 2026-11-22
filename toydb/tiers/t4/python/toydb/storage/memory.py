"""An in-memory storage engine. Mirrors `src/storage/memory.rs`."""

from toydb.ops import Range
from toydb.storage import engine
from toydb.storage.btree import BTreeMap, MapRange
from toydb.storage.engine import Engine, Status


class Memory(Engine):
    """An in-memory key-value storage engine using an ordered map. Data is not
    persisted. Primarily for testing."""

    def __init__(self) -> None:
        """Creates a new Memory key-value storage engine."""
        self.data: BTreeMap[bytes, bytes] = BTreeMap()

    def delete(self, key: bytes) -> None:
        self.data.remove(key)

    def flush(self) -> None:
        pass

    def get(self, key: bytes) -> bytes | None:
        return self.data.get(key)

    def scan(self, range: Range[bytes]) -> "ScanIterator":
        return ScanIterator(self.data.range(range))

    def set(self, key: bytes, value: bytes) -> None:
        self.data.insert(bytes(key), bytes(value))

    def status(self) -> Status:
        return Status(
            name="memory",
            keys=len(self.data),
            size=sum(len(k) + len(v) for k, v in self.data),
            disk_size=0,
            live_disk_size=0,
        )


class ScanIterator(engine.ScanIterator):
    """A scan iterator over a range of the in-memory map."""

    def __init__(self, inner: MapRange[bytes, bytes]) -> None:
        self.inner = inner

    def __next__(self) -> tuple[bytes, bytes]:
        return next(self.inner)

    def next_back(self) -> tuple[bytes, bytes]:
        return self.inner.next_back()
