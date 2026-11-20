"""A minimal serialization data model, standing in for Rust's `serde` crate.

In Rust, toyDB types derive `serde::Serialize` and `serde::Deserialize`, and
the `keycode` and `bincode` modules provide the actual encodings. This module
ports the part of that data model which toyDB uses:

* `Serializer` and `Deserializer` are the interfaces implemented by the
  encodings (`keycode` and `bincode`).

* `Serde[T]` describes how values of type `T` map onto the data model, i.e. it
  is the equivalent of `T: Serialize + Deserialize`. It has two methods:
  `serialize(value, serializer)` and `deserialize(deserializer)`.

* Primitive descriptors (`Bool`, `U64`, `I64`, `F64`, `String`, `Bytes`, ...)
  and combinators (`Option`, `Vec`, `Tuple`, `BTreeSet`, `HashMap`, ...)
  describe the Rust standard types, which have no distinct Python type (e.g.
  `u8` and `i64` are both `int`).

* User types implement the equivalent of `#[derive(Serialize, Deserialize)]`
  as a `serialize(self, serializer)` method and a `deserialize(cls,
  deserializer)` classmethod. The class object itself then satisfies
  `Serde[T]`, so it can be passed wherever a descriptor is expected, e.g.
  `bincode.deserialize(Option(Entry), data)`.

  Structs serialize their fields in declaration order. Enums first serialize
  the variant index (in declaration order, starting at 0) with one of the
  `serialize_*_variant` methods, then the variant's fields in order:

      def serialize(self, serializer: Serializer) -> None:
          match self:
              case Key.Entry(index):
                  serializer.serialize_newtype_variant(0)
                  serializer.serialize_u64(index)
              case Key.TermVote():
                  serializer.serialize_unit_variant(1)

      @classmethod
      def deserialize(cls, deserializer: Deserializer) -> "Key":
          match deserializer.deserialize_enum():
              case 0:
                  return Key.Entry(deserializer.deserialize_u64())
              case 1:
                  return Key.TermVote()
              case index:
                  raise deserializer.invalid_variant(index, 2)
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from toydb.error import Error

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")
H = TypeVar("H", bound=Hashable)


class Serializer(ABC):
    """A data format that can serialize the serde data model.

    Compound types are serialized by first calling the appropriate method for
    the compound (e.g. `serialize_seq`, `serialize_some` or
    `serialize_tuple_variant`), then serializing the contained values in order.
    """

    @abstractmethod
    def serialize_bool(self, v: bool) -> None: ...

    @abstractmethod
    def serialize_i8(self, v: int) -> None: ...

    @abstractmethod
    def serialize_i16(self, v: int) -> None: ...

    @abstractmethod
    def serialize_i32(self, v: int) -> None: ...

    @abstractmethod
    def serialize_i64(self, v: int) -> None: ...

    @abstractmethod
    def serialize_u8(self, v: int) -> None: ...

    @abstractmethod
    def serialize_u16(self, v: int) -> None: ...

    @abstractmethod
    def serialize_u32(self, v: int) -> None: ...

    @abstractmethod
    def serialize_u64(self, v: int) -> None: ...

    @abstractmethod
    def serialize_f64(self, v: float) -> None: ...

    @abstractmethod
    def serialize_str(self, v: str) -> None: ...

    @abstractmethod
    def serialize_bytes(self, v: bytes) -> None:
        """Serializes a byte string, i.e. a `Vec<u8>` or `&[u8]` using
        `serde_bytes`. A `Vec<u8>` without `serde_bytes` is a sequence of `u8`."""

    @abstractmethod
    def serialize_none(self) -> None:
        """Serializes an `Option::None`."""

    @abstractmethod
    def serialize_some(self) -> None:
        """Begins an `Option::Some`, followed by the value."""

    @abstractmethod
    def serialize_unit(self) -> None:
        """Serializes a unit value `()`."""

    @abstractmethod
    def serialize_unit_variant(self, index: int) -> None:
        """Serializes an enum variant without fields, e.g. `Key::NextVersion`."""

    @abstractmethod
    def serialize_newtype_variant(self, index: int) -> None:
        """Begins an enum variant with a single unnamed field, e.g.
        `Key::Entry(index)`, followed by the field."""

    @abstractmethod
    def serialize_tuple_variant(self, index: int) -> None:
        """Begins an enum variant with several unnamed fields, e.g.
        `Key::Version(key, version)`, followed by the fields."""

    @abstractmethod
    def serialize_struct_variant(self, index: int) -> None:
        """Begins an enum variant with named fields, e.g. `Message::Read { seq
        }`, followed by the fields."""

    @abstractmethod
    def serialize_seq(self, len: int) -> None:
        """Begins a variable-length sequence, followed by the elements."""

    @abstractmethod
    def serialize_tuple(self, len: int) -> None:
        """Begins a tuple or fixed-size array, followed by the elements."""

    @abstractmethod
    def serialize_map(self, len: int) -> None:
        """Begins a map, followed by alternating keys and values."""

    @abstractmethod
    def serialize_struct(self) -> None:
        """Begins a struct with named fields, followed by the fields."""


class Deserializer(ABC):
    """A data format that can deserialize the serde data model.

    The supported formats are not self-describing, so the caller must know
    which type to deserialize. Compound types are deserialized by first
    calling the appropriate method for the compound, then deserializing the
    contained values in order.
    """

    @abstractmethod
    def deserialize_bool(self) -> bool: ...

    @abstractmethod
    def deserialize_i8(self) -> int: ...

    @abstractmethod
    def deserialize_i16(self) -> int: ...

    @abstractmethod
    def deserialize_i32(self) -> int: ...

    @abstractmethod
    def deserialize_i64(self) -> int: ...

    @abstractmethod
    def deserialize_u8(self) -> int: ...

    @abstractmethod
    def deserialize_u16(self) -> int: ...

    @abstractmethod
    def deserialize_u32(self) -> int: ...

    @abstractmethod
    def deserialize_u64(self) -> int: ...

    @abstractmethod
    def deserialize_f64(self) -> float: ...

    @abstractmethod
    def deserialize_str(self) -> str: ...

    @abstractmethod
    def deserialize_bytes(self) -> bytes: ...

    @abstractmethod
    def deserialize_option(self) -> bool:
        """Deserializes an `Option` marker. If it returns true, the option is
        `Some` and the caller must deserialize the value next."""

    @abstractmethod
    def deserialize_unit(self) -> None:
        """Deserializes a unit value `()`."""

    @abstractmethod
    def deserialize_enum(self) -> int:
        """Begins an enum by deserializing the variant index. The caller must
        deserialize the variant's fields (if any) next. A variant with named
        fields must call `deserialize_struct_variant` first."""

    @abstractmethod
    def deserialize_struct_variant(self) -> None:
        """Begins the named fields of an enum variant, following
        `deserialize_enum`."""

    @abstractmethod
    def deserialize_seq(self, element: Callable[[], T]) -> list[T]:
        """Deserializes a variable-length sequence, calling the given function
        to deserialize each element."""

    @abstractmethod
    def deserialize_tuple(self, len: int) -> None:
        """Begins a tuple or fixed-size array, followed by the elements."""

    @abstractmethod
    def deserialize_map(self, key: Callable[[], K], value: Callable[[], V]) -> dict[K, V]:
        """Deserializes a map, calling the given functions to deserialize each
        key and value."""

    @abstractmethod
    def deserialize_struct(self) -> None:
        """Begins a struct with named fields, followed by the fields."""

    def custom(self, message: str) -> Error:
        """Returns a deserialization error with the given message, like
        `serde::de::Error::custom`. Formats may wrap the message."""
        return Error.InvalidData(message)

    def invalid_variant(self, index: int, variants: int) -> Error:
        """Returns the error for an unknown enum variant index, where variants
        is the number of variants in the enum."""
        return self.custom(
            f"invalid value: integer `{index}`, expected variant index 0 <= i < {variants}"
        )


class Serde(Protocol[T]):
    """Describes how to serialize and deserialize values of type `T`.

    Classes with a `serialize(self, serializer)` method and a
    `deserialize(cls, deserializer)` classmethod satisfy this protocol
    themselves (as class objects).
    """

    def serialize(self, value: T, serializer: Serializer, /) -> None:
        """Serializes a value into the given serializer."""
        ...

    def deserialize(self, deserializer: Deserializer, /) -> T:
        """Deserializes a value from the given deserializer."""
        ...


@dataclass(frozen=True)
class _Primitive(Serde[T]):
    """A primitive type, dispatching to the named serializer method."""

    name: str

    def serialize(self, value: T, serializer: Serializer, /) -> None:
        getattr(serializer, f"serialize_{self.name}")(value)

    def deserialize(self, deserializer: Deserializer, /) -> T:
        return getattr(deserializer, f"deserialize_{self.name}")()

    def __repr__(self) -> str:
        return self.name


class _Unit(Serde[None]):
    def serialize(self, value: None, serializer: Serializer, /) -> None:
        serializer.serialize_unit()

    def deserialize(self, deserializer: Deserializer, /) -> None:
        deserializer.deserialize_unit()


Bool: Serde[bool] = _Primitive("bool")
"""A Rust `bool`."""
I8: Serde[int] = _Primitive("i8")
"""A Rust `i8`."""
I16: Serde[int] = _Primitive("i16")
"""A Rust `i16`."""
I32: Serde[int] = _Primitive("i32")
"""A Rust `i32`."""
I64: Serde[int] = _Primitive("i64")
"""A Rust `i64` (or `isize`)."""
U8: Serde[int] = _Primitive("u8")
"""A Rust `u8`."""
U16: Serde[int] = _Primitive("u16")
"""A Rust `u16`."""
U32: Serde[int] = _Primitive("u32")
"""A Rust `u32`."""
U64: Serde[int] = _Primitive("u64")
"""A Rust `u64` (or `usize`)."""
F64: Serde[float] = _Primitive("f64")
"""A Rust `f64`."""
String: Serde[str] = _Primitive("str")
"""A Rust `String` or `&str`."""
Bytes: Serde[bytes] = _Primitive("bytes")
"""A Rust byte string using `serde_bytes`, or `Vec<u8>` under Bincode (which
encodes both identically). Under Keycode a plain `Vec<u8>` is unsupported."""
Unit: Serde[None] = _Unit()
"""The Rust unit type `()`."""


@dataclass(frozen=True)
class Option(Serde[T | None]):
    """A Rust `Option<T>`, as a Python `T | None`."""

    inner: Serde[T]

    def serialize(self, value: T | None, serializer: Serializer, /) -> None:
        if value is None:
            serializer.serialize_none()
        else:
            serializer.serialize_some()
            self.inner.serialize(value, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> T | None:
        if deserializer.deserialize_option():
            return self.inner.deserialize(deserializer)
        return None


@dataclass(frozen=True)
class Vec(Serde[list[T]]):
    """A Rust `Vec<T>` (or `VecDeque<T>`), as a Python list."""

    inner: Serde[T]

    def serialize(self, value: list[T], serializer: Serializer, /) -> None:
        serializer.serialize_seq(len(value))
        for item in value:
            self.inner.serialize(item, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> list[T]:
        return deserializer.deserialize_seq(lambda: self.inner.deserialize(deserializer))


class Tuple(Serde[tuple[Any, ...]]):
    """A Rust tuple `(A, B, ...)`, as a Python tuple."""

    def __init__(self, *inner: Serde[Any]) -> None:
        self.inner = inner

    def serialize(self, value: tuple[Any, ...], serializer: Serializer, /) -> None:
        assert len(value) == len(self.inner), "tuple length mismatch"
        serializer.serialize_tuple(len(self.inner))
        for serde, item in zip(self.inner, value):
            serde.serialize(item, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> tuple[Any, ...]:
        deserializer.deserialize_tuple(len(self.inner))
        return tuple(serde.deserialize(deserializer) for serde in self.inner)


@dataclass(frozen=True)
class BTreeSet(Serde[set[H]]):
    """A Rust `BTreeSet<T>`, as a Python set. Serialized in sorted order."""

    inner: Serde[H]

    def serialize(self, value: set[H], serializer: Serializer, /) -> None:
        serializer.serialize_seq(len(value))
        for item in sorted(value):  # type: ignore[type-var]
            self.inner.serialize(item, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> set[H]:
        return set(deserializer.deserialize_seq(lambda: self.inner.deserialize(deserializer)))


@dataclass(frozen=True)
class HashSet(Serde[set[H]]):
    """A Rust `HashSet<T>`, as a Python set. Serialized in iteration order."""

    inner: Serde[H]

    def serialize(self, value: set[H], serializer: Serializer, /) -> None:
        serializer.serialize_seq(len(value))
        for item in value:
            self.inner.serialize(item, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> set[H]:
        return set(deserializer.deserialize_seq(lambda: self.inner.deserialize(deserializer)))


@dataclass(frozen=True)
class BTreeMap(Serde[dict[K, V]]):
    """A Rust `BTreeMap<K, V>`, as a Python dict. Serialized in key order."""

    key: Serde[K]
    value: Serde[V]

    def serialize(self, value: dict[K, V], serializer: Serializer, /) -> None:
        serializer.serialize_map(len(value))
        for key in sorted(value):  # type: ignore[type-var]
            self.key.serialize(key, serializer)
            self.value.serialize(value[key], serializer)

    def deserialize(self, deserializer: Deserializer, /) -> dict[K, V]:
        return deserializer.deserialize_map(
            lambda: self.key.deserialize(deserializer),
            lambda: self.value.deserialize(deserializer),
        )


@dataclass(frozen=True)
class HashMap(Serde[dict[K, V]]):
    """A Rust `HashMap<K, V>`, as a Python dict. Serialized in iteration order."""

    key: Serde[K]
    value: Serde[V]

    def serialize(self, value: dict[K, V], serializer: Serializer, /) -> None:
        serializer.serialize_map(len(value))
        for key, item in value.items():
            self.key.serialize(key, serializer)
            self.value.serialize(item, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> dict[K, V]:
        return deserializer.deserialize_map(
            lambda: self.key.deserialize(deserializer),
            lambda: self.value.deserialize(deserializer),
        )


@dataclass(frozen=True)
class Result(Serde[T | Error]):
    """A toyDB `Result<T>`, i.e. `std::result::Result<T, Error>`. Python has no
    result type, so it is represented as either the value or an (unraised)
    `Error`. Serialized as an enum with variants `Ok(T)` and `Err(Error)`."""

    inner: Serde[T]

    def serialize(self, value: T | Error, serializer: Serializer, /) -> None:
        if isinstance(value, Error):
            serializer.serialize_newtype_variant(1)
            value.serialize(serializer)
        else:
            serializer.serialize_newtype_variant(0)
            self.inner.serialize(value, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> T | Error:
        match deserializer.deserialize_enum():
            case 0:
                return self.inner.deserialize(deserializer)
            case 1:
                return Error.deserialize(deserializer)
            case index:
                raise deserializer.invalid_variant(index, 2)
