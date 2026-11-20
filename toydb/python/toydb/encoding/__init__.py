"""Binary data encodings.

* keycode: used for keys in the key/value store.
* bincode: used for values in the key/value store and network protocols.
* serde: the data model shared by the encodings, standing in for the Rust
  `serde` crate. Types describe their encoding once, for both formats.
* format: formats raw keys and values for debugging and tests.

Mirrors `src/encoding/mod.rs`. Submodules are not imported here: import them
explicitly, e.g. `from toydb.encoding import bincode`.
"""

from abc import ABC, abstractmethod
from typing import BinaryIO, Self

from toydb.encoding.serde import Deserializer, Serializer

__all__ = ["Key", "Value"]


class Key(ABC):
    """Adds automatic Keycode encode/decode methods to key enums. These are used
    as keys in the key/value store.

    Implementors must provide `serialize` and `deserialize`, the equivalent of
    `#[derive(Serialize, Deserialize)]` (see `toydb.encoding.serde`).
    """

    @abstractmethod
    def serialize(self, serializer: Serializer) -> None:
        """Serializes the key into the given serializer."""

    @classmethod
    @abstractmethod
    def deserialize(cls, deserializer: Deserializer) -> Self:
        """Deserializes a key from the given deserializer."""

    @classmethod
    def decode(cls, bytes: bytes) -> Self:
        """Decodes a key from a byte slice using Keycode."""
        from toydb.encoding import keycode

        return keycode.deserialize(cls, bytes)

    def encode(self) -> bytes:
        """Encodes a key to a byte vector using Keycode.

        In the common case, the encoded key is borrowed for a storage engine
        call and then thrown away. We could avoid a bunch of allocations by
        taking a reusable byte vector to encode into and return a reference to
        it, but we keep it simple.
        """
        from toydb.encoding import keycode

        return keycode.serialize(self)


class Value(ABC):
    """Adds automatic Bincode encode/decode methods to value types. These are used
    for values in key/value storage engines, and also for e.g. network protocol
    messages and other values.

    Implementors must provide `serialize` and `deserialize`, the equivalent of
    `#[derive(Serialize, Deserialize)]` (see `toydb.encoding.serde`).

    The Rust blanket implementations for wrapper types (`Option<V>`,
    `Result<V>`, `Vec<V>`, `(V1, V2)`, `HashSet<V>` and `BTreeSet<V>`) have no
    Python class to attach to. Encode these with `bincode.serialize(value,
    serde)` and `bincode.deserialize(serde, bytes)` instead, using e.g.
    `serde.Option(V)` or `serde.Vec(V)`.
    """

    @abstractmethod
    def serialize(self, serializer: Serializer) -> None:
        """Serializes the value into the given serializer."""

    @classmethod
    @abstractmethod
    def deserialize(cls, deserializer: Deserializer) -> Self:
        """Deserializes a value from the given deserializer."""

    @classmethod
    def decode(cls, bytes: bytes) -> Self:
        """Decodes a value from a byte slice using Bincode."""
        from toydb.encoding import bincode

        return bincode.deserialize(cls, bytes)

    @classmethod
    def decode_from(cls, reader: BinaryIO) -> Self:
        """Decodes a value from a reader using Bincode."""
        from toydb.encoding import bincode

        return bincode.deserialize_from(cls, reader)

    @classmethod
    def maybe_decode_from(cls, reader: BinaryIO) -> Self | None:
        """Decodes a value from a reader using Bincode, or returns None if the
        reader is closed."""
        from toydb.encoding import bincode

        return bincode.maybe_deserialize_from(cls, reader)

    def encode(self) -> bytes:
        """Encodes a value to a byte vector using Bincode."""
        from toydb.encoding import bincode

        return bincode.serialize(self)

    def encode_into(self, writer: BinaryIO) -> None:
        """Encodes a value into a writer using Bincode."""
        from toydb.encoding import bincode

        bincode.serialize_into(writer, self)
