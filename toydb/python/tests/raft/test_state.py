"""Test helper state machines. Mirrors the `#[cfg(test)] pub mod test` in
`src/raft/state.rs`."""

import queue
from dataclasses import dataclass
from typing import ClassVar

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error, Panic
from toydb.raft.log import Entry, Index
from toydb.raft.state import State


class Channel[T](queue.Queue[T]):
    """An unbounded channel, standing in for a `crossbeam::channel` sender and
    receiver pair. Unlike a plain queue it can be disconnected, like a channel
    whose receiver has been dropped: sends then fail with an IO error."""

    def __init__(self) -> None:
        super().__init__()
        self.disconnected = False

    def send(self, item: T) -> None:
        """Sends an item, or errors if the channel is disconnected."""
        if self.disconnected:
            raise Error.IO("sending on a disconnected channel")
        self.put(item)

    def try_iter(self) -> list[T]:
        """Receives all pending items, without blocking."""
        items = []
        while True:
            try:
                items.append(self.get_nowait())
            except queue.Empty:
                return items

    def close(self) -> None:
        """Disconnects the channel, like dropping its receiver."""
        self.disconnected = True


class Emit(State):
    """Wraps a state machine and emits applied entries to the provided channel."""

    def __init__(self, inner: State, tx: Channel[Entry]) -> None:
        self.inner = inner
        self.tx = tx

    def get_applied_index(self) -> Index:
        return self.inner.get_applied_index()

    def apply(self, entry: Entry) -> bytes:
        response = self.inner.apply(entry)
        self.tx.send(entry)
        return response

    def read(self, command: bytes) -> bytes:
        return self.inner.read(command)


class KV(State):
    """A simple string key/value store. Takes KVCommands."""

    def __init__(self) -> None:
        self.applied_index: Index = 0
        self.data: dict[str, str] = {}

    def get_applied_index(self) -> Index:
        return self.applied_index

    def apply(self, entry: Entry) -> bytes:
        command = KVCommand.decode(entry.command) if entry.command is not None else None
        match command:
            case KVCommand.Put(key, value):
                self.data[key] = value
                response = KVResponse.Put(entry.index).encode()
            case KVCommand.Get() | KVCommand.Scan():
                raise Panic(f"{command} submitted as write command")
            case _:
                response = b""
        self.applied_index = entry.index
        return response

    def read(self, command: bytes) -> bytes:
        match KVCommand.decode(command):
            case KVCommand.Get(key):
                return KVResponse.Get(self.data.get(key)).encode()
            case KVCommand.Scan():
                return KVResponse.Scan(dict(sorted(self.data.items()))).encode()
            case c:
                raise Panic(f"{c} submitted as read command")


class KVCommand(encoding.Value):
    """A KV command. Returns the corresponding KVResponse."""

    Get: ClassVar[type["KVCommandGet"]]
    Put: ClassVar[type["KVCommandPut"]]
    Scan: ClassVar[type["KVCommandScan"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case KVCommandGet(key):
                serializer.serialize_struct_variant(0)
                serializer.serialize_str(key)
            case KVCommandPut(key, value):
                serializer.serialize_struct_variant(1)
                serializer.serialize_str(key)
                serializer.serialize_str(value)
            case KVCommandScan():
                serializer.serialize_unit_variant(2)
            case _:
                raise TypeError(f"unknown command {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "KVCommand":
        match deserializer.deserialize_enum():
            case 0:
                deserializer.deserialize_struct_variant()
                return KVCommandGet(key=deserializer.deserialize_str())
            case 1:
                deserializer.deserialize_struct_variant()
                return KVCommandPut(
                    key=deserializer.deserialize_str(), value=deserializer.deserialize_str()
                )
            case 2:
                return KVCommandScan()
            case index:
                raise deserializer.invalid_variant(index, 3)


@dataclass(frozen=True)
class KVCommandGet(KVCommand):
    """Fetches the value of the given key."""

    key: str

    def __str__(self) -> str:
        return f"get {self.key}"


@dataclass(frozen=True)
class KVCommandPut(KVCommand):
    """Stores the given key/value pair, returning the applied index."""

    key: str
    value: str

    def __str__(self) -> str:
        return f"put {self.key}={self.value}"


@dataclass(frozen=True)
class KVCommandScan(KVCommand):
    """Returns all key/value pairs."""

    def __str__(self) -> str:
        return "scan"


KVCommand.Get = KVCommandGet
KVCommand.Put = KVCommandPut
KVCommand.Scan = KVCommandScan


class KVResponse(encoding.Value):
    """A KVCommand response."""

    Get: ClassVar[type["KVResponseGet"]]
    Put: ClassVar[type["KVResponsePut"]]
    Scan: ClassVar[type["KVResponseScan"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case KVResponseGet(value):
                serializer.serialize_newtype_variant(0)
                serde.Option(serde.String).serialize(value, serializer)
            case KVResponsePut(applied_index):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_u64(applied_index)
            case KVResponseScan(kvs):
                serializer.serialize_newtype_variant(2)
                serde.BTreeMap(serde.String, serde.String).serialize(kvs, serializer)
            case _:
                raise TypeError(f"unknown response {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "KVResponse":
        match deserializer.deserialize_enum():
            case 0:
                return KVResponseGet(serde.Option(serde.String).deserialize(deserializer))
            case 1:
                return KVResponsePut(deserializer.deserialize_u64())
            case 2:
                return KVResponseScan(
                    serde.BTreeMap(serde.String, serde.String).deserialize(deserializer)
                )
            case index:
                raise deserializer.invalid_variant(index, 3)


@dataclass(frozen=True)
class KVResponseGet(KVResponse):
    """Get returns the key's value, or None if it does not exist."""

    value: str | None

    def __str__(self) -> str:
        return "None" if self.value is None else self.value


@dataclass(frozen=True)
class KVResponsePut(KVResponse):
    """Put returns the applied index of the command."""

    applied_index: Index

    def __str__(self) -> str:
        return str(self.applied_index)


@dataclass(frozen=True)
class KVResponseScan(KVResponse):
    """Scan returns the key/value pairs."""

    kvs: dict[str, str]

    def __str__(self) -> str:
        return ",".join(f"{k}={v}" for k, v in self.kvs.items())


KVResponse.Get = KVResponseGet
KVResponse.Put = KVResponsePut
KVResponse.Scan = KVResponseScan


class Noop(State):
    """A state machine which does nothing. All commands are ignored."""

    def __init__(self) -> None:
        self.applied_index: Index = 0

    def get_applied_index(self) -> Index:
        return self.applied_index

    def apply(self, entry: Entry) -> bytes:
        self.applied_index = entry.index
        return b""

    def read(self, command: bytes) -> bytes:
        return b""
