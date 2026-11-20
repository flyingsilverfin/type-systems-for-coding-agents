"""Bincode is used to encode values, both in key/value stores and the toyDB
network protocol. It is a Rust-specific encoding that depends on the
internal data structures being stable, but it's sufficient for toyDB. See:
<https://github.com/bincode-org/bincode>

This module reimplements the `bincode` crate (version 2) with the standard
config, for the data model in `toydb.encoding.serde`:

* `bool`: a single byte, `0x00` or `0x01`.
* `u8` and `i8`: a single byte.
* Other unsigned integers: a varint. Values below 251 are a single byte,
  otherwise a marker byte (251, 252, 253 for `u16`, `u32`, `u64`) followed by
  the little-endian integer of that size.
* Other signed integers: zigzag-encoded, then encoded as an unsigned varint.
* `f64`: little-endian IEEE 754.
* Strings, byte strings, sequences and maps: the length as a `u64` varint,
  then the contents.
* `Option`: `0x00` for `None`, or `0x01` followed by the value.
* Enums: the variant index as a `u32` varint, then the fields in order.
* Tuples and structs: the fields in order.
"""

import struct
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable
from typing import BinaryIO, TypeVar

from toydb.encoding import serde
from toydb.error import Error, Panic
from toydb.fmt import debug_str, utf8_error

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

# Varint marker bytes.
_SINGLE_BYTE_MAX = 250
_U16_BYTE = 251
_U32_BYTE = 252
_U64_BYTE = 253
_U128_BYTE = 254


def serialize(value: T, serde: serde.Serde[T] | None = None) -> bytes:
    """Serializes a value using Bincode. The serde describes the value's Rust
    type, and defaults to the value's class (which must then implement
    `serialize`)."""
    serializer = Serializer()
    # Panic on failure, as this is a problem with the data structure.
    try:
        _serialize(value, serde, serializer)
    except (Error, OverflowError, struct.error) as error:
        raise Panic(f"value must be serializable: {error!r}") from error
    return bytes(serializer.output)


def deserialize(serde: serde.Serde[T], input: bytes) -> T:
    """Deserializes a value of the given type using Bincode. Like the Rust
    implementation, trailing bytes are ignored."""
    return serde.deserialize(Deserializer(_SliceReader(input)))


def serialize_into(writer: BinaryIO, value: T, serde: serde.Serde[T] | None = None) -> None:
    """Serializes a value to a writer using Bincode."""
    serializer = Serializer()
    try:
        _serialize(value, serde, serializer)
        writer.write(serializer.output)
    except OSError as error:
        raise Error.from_exception(error) from error


def deserialize_from(serde: serde.Serde[T], reader: BinaryIO) -> T:
    """Deserializes a value of the given type from a reader using Bincode."""
    try:
        return serde.deserialize(Deserializer(_IoReader(reader)))
    except _IoError as error:
        raise error.into_error() from error


def maybe_deserialize_from(serde: serde.Serde[T], reader: BinaryIO) -> T | None:
    """Deserializes a value of the given type from a reader using Bincode, or
    returns None if the reader is closed."""
    try:
        return serde.deserialize(Deserializer(_IoReader(reader)))
    except _IoError as error:
        if error.inner is None or isinstance(error.inner, ConnectionResetError):
            return None
        raise error.into_error() from error


def _serialize(value: T, serde: serde.Serde[T] | None, serializer: "Serializer") -> None:
    if serde is not None:
        serde.serialize(value, serializer)
    else:
        value.serialize(serializer)  # type: ignore[attr-defined]


class Serializer(serde.Serializer):
    """Serializes values as Bincode."""

    def __init__(self) -> None:
        self.output = bytearray()

    def _varint(self, v: int, bits: int) -> None:
        """Encodes an unsigned varint for an integer of the given size."""
        if not 0 <= v < 1 << bits:
            raise OverflowError(f"integer {v} out of range for {bits} bits")
        if v <= _SINGLE_BYTE_MAX:
            self.output.append(v)
        elif v < 1 << 16:
            self.output.append(_U16_BYTE)
            self.output += v.to_bytes(2, "little")
        elif v < 1 << 32:
            self.output.append(_U32_BYTE)
            self.output += v.to_bytes(4, "little")
        else:
            self.output.append(_U64_BYTE)
            self.output += v.to_bytes(8, "little")

    def _zigzag(self, v: int, bits: int) -> None:
        """Encodes a signed varint for an integer of the given size."""
        if not -(1 << (bits - 1)) <= v < 1 << (bits - 1):
            raise OverflowError(f"integer {v} out of range for {bits} bits")
        self._varint(v << 1 if v >= 0 else (-v << 1) - 1, bits)

    def serialize_bool(self, v: bool) -> None:
        self.output.append(1 if v else 0)

    def serialize_i8(self, v: int) -> None:
        self.output += v.to_bytes(1, "little", signed=True)

    def serialize_i16(self, v: int) -> None:
        self._zigzag(v, 16)

    def serialize_i32(self, v: int) -> None:
        self._zigzag(v, 32)

    def serialize_i64(self, v: int) -> None:
        self._zigzag(v, 64)

    def serialize_u8(self, v: int) -> None:
        self.output += v.to_bytes(1, "little")

    def serialize_u16(self, v: int) -> None:
        self._varint(v, 16)

    def serialize_u32(self, v: int) -> None:
        self._varint(v, 32)

    def serialize_u64(self, v: int) -> None:
        self._varint(v, 64)

    def serialize_f64(self, v: float) -> None:
        self.output += struct.pack("<d", v)

    def serialize_str(self, v: str) -> None:
        self.serialize_bytes(v.encode())

    def serialize_bytes(self, v: bytes) -> None:
        self._varint(len(v), 64)
        self.output += v

    def serialize_none(self) -> None:
        self.output.append(0)

    def serialize_some(self) -> None:
        self.output.append(1)

    def serialize_unit(self) -> None:
        pass

    def serialize_unit_variant(self, index: int) -> None:
        self._varint(index, 32)

    def serialize_newtype_variant(self, index: int) -> None:
        self._varint(index, 32)

    def serialize_tuple_variant(self, index: int) -> None:
        self._varint(index, 32)

    def serialize_struct_variant(self, index: int) -> None:
        self._varint(index, 32)

    def serialize_seq(self, len: int) -> None:
        self._varint(len, 64)

    def serialize_tuple(self, len: int) -> None:
        pass

    def serialize_map(self, len: int) -> None:
        self._varint(len, 64)

    def serialize_struct(self) -> None:
        pass


class _IoError(Exception):
    """An IO error while reading from a reader. An inner error of None is an
    unexpected EOF."""

    def __init__(self, inner: OSError | None, additional: int) -> None:
        super().__init__(inner, additional)
        self.inner = inner
        self.additional = additional

    def into_error(self) -> Error:
        """Converts the IO error into an Error::InvalidData, like the Rust
        `From<bincode::error::DecodeError>` implementation."""
        if self.inner is None:
            inner = 'Error { kind: UnexpectedEof, message: "failed to fill whole buffer" }'
        else:
            inner = repr(self.inner)
        return Error.InvalidData(f"Io {{ inner: {inner}, additional: {self.additional} }}")


class _Reader(ABC):
    """A source of bytes to decode."""

    @abstractmethod
    def read(self, n: int) -> bytes:
        """Reads exactly n bytes, or errors."""


class _SliceReader(_Reader):
    """Reads from a byte slice."""

    def __init__(self, input: bytes) -> None:
        self.input = input
        self.position = 0

    def read(self, n: int) -> bytes:
        end = self.position + n
        if end > len(self.input):
            additional = end - len(self.input)
            raise Error.InvalidData(f"UnexpectedEnd {{ additional: {additional} }}")
        data = self.input[self.position : end]
        self.position = end
        return data


class _IoReader(_Reader):
    """Reads from a binary reader, e.g. a file or socket."""

    def __init__(self, reader: BinaryIO) -> None:
        self.reader = reader

    def read(self, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            try:
                chunk = self.reader.read(n - len(data))
            except OSError as error:
                raise _IoError(error, n) from error
            if not chunk:
                raise _IoError(None, n)
            data += chunk
        return bytes(data)


class Deserializer(serde.Deserializer):
    """Deserializes values from Bincode."""

    def __init__(self, reader: _Reader) -> None:
        self.reader = reader

    def custom(self, message: str) -> Error:
        return Error.InvalidData(f"OtherString({debug_str(message)})")

    def _varint(self, bits: int, signed: bool = False) -> int:
        """Decodes an unsigned varint for an integer of the given size."""
        byte = self.reader.read(1)[0]
        if byte <= _SINGLE_BYTE_MAX:
            return byte
        found = {_U16_BYTE: 16, _U32_BYTE: 32, _U64_BYTE: 64, _U128_BYTE: 128}.get(byte)
        if found is None or found > bits:
            prefix = "I" if signed else "U"
            found_type = f"{prefix}{found}" if found is not None else "Reserved"
            raise Error.InvalidData(
                f"InvalidIntegerType {{ expected: {prefix}{bits}, found: {found_type} }}"
            )
        return int.from_bytes(self.reader.read(found // 8), "little")

    def _zigzag(self, bits: int) -> int:
        """Decodes a signed varint for an integer of the given size."""
        v = self._varint(bits, signed=True)
        return v >> 1 if v & 1 == 0 else -((v + 1) >> 1)

    def _len(self) -> int:
        """Decodes a length prefix."""
        return self._varint(64)

    def deserialize_bool(self) -> bool:
        match self.reader.read(1)[0]:
            case 0:
                return False
            case 1:
                return True
            case byte:
                raise Error.InvalidData(f"InvalidBooleanValue({byte})")

    def deserialize_i8(self) -> int:
        return int.from_bytes(self.reader.read(1), "little", signed=True)

    def deserialize_i16(self) -> int:
        return self._zigzag(16)

    def deserialize_i32(self) -> int:
        return self._zigzag(32)

    def deserialize_i64(self) -> int:
        return self._zigzag(64)

    def deserialize_u8(self) -> int:
        return self.reader.read(1)[0]

    def deserialize_u16(self) -> int:
        return self._varint(16)

    def deserialize_u32(self) -> int:
        return self._varint(32)

    def deserialize_u64(self) -> int:
        return self._varint(64)

    def deserialize_f64(self) -> float:
        return struct.unpack("<d", self.reader.read(8))[0]

    def deserialize_str(self) -> str:
        data = self.deserialize_bytes()
        try:
            return data.decode()
        except UnicodeDecodeError as error:
            valid_up_to, error_len = utf8_error(error)
            fmtlen = "None" if error_len is None else f"Some({error_len})"
            raise Error.InvalidData(
                f"Utf8 {{ inner: Utf8Error {{ valid_up_to: {valid_up_to}, error_len: {fmtlen} }} }}"
            ) from error

    def deserialize_bytes(self) -> bytes:
        return self.reader.read(self._len())

    def deserialize_option(self) -> bool:
        match self.reader.read(1)[0]:
            case 0:
                return False
            case 1:
                return True
            case byte:
                raise Error.InvalidData(
                    f'UnexpectedVariant {{ type_name: "Option<T>", '
                    f"allowed: Range {{ min: 0, max: 1 }}, found: {byte} }}"
                )

    def deserialize_unit(self) -> None:
        pass

    def deserialize_enum(self) -> int:
        return self._varint(32)

    def deserialize_struct_variant(self) -> None:
        pass

    def deserialize_seq(self, element: Callable[[], T]) -> list[T]:
        return [element() for _ in range(self._len())]

    def deserialize_tuple(self, len: int) -> None:
        pass

    def deserialize_map(self, key: Callable[[], K], value: Callable[[], V]) -> dict[K, V]:
        entries: dict[K, V] = {}
        for _ in range(self._len()):
            k = key()
            entries[k] = value()
        return entries

    def deserialize_struct(self) -> None:
        pass
