"""toyDB errors.

Mirrors `src/error.rs`. The Rust `Error` enum is ported as an exception
hierarchy: `Error` is the base class, with one subclass per enum variant.
A Rust `Result<T>` is ported as returning `T` or raising an `Error`.

Rust panics (`panic!`, `assert!`, `unreachable!`, `expect()`, etc.) are ported
as `AssertionError`, typically via `assert` or `raise Panic(...)`.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, NoReturn

from toydb.fmt import debug, debug_tuple, display_utf8_error

if TYPE_CHECKING:
    from toydb.encoding.serde import Deserializer, Serializer


class Panic(AssertionError):
    """A Rust panic, i.e. a bug or broken invariant. Not part of the `Error`
    hierarchy, since it is never returned as a `Result`."""


class Error(Exception):
    """toyDB errors."""

    Abort: ClassVar[type["Abort"]]
    InvalidData: ClassVar[type["InvalidData"]]
    InvalidInput: ClassVar[type["InvalidInput"]]
    IO: ClassVar[type["IO"]]
    ReadOnly: ClassVar[type["ReadOnly"]]
    Serialization: ClassVar[type["Serialization"]]

    def __str__(self) -> str:
        """Formats the error like the Rust `Display` implementation."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return self.fmt_debug()

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.args == other.args  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self), self.args))

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the error like the Rust `Debug` implementation."""
        return type(self).__name__

    def is_deterministic(self) -> bool:
        """Returns whether the error is considered deterministic. Raft state
        machine application needs to know whether a command failure is
        deterministic on the input command -- if it is, the command can be
        considered applied and the error returned to the client, but otherwise
        the state machine must panic to prevent node divergence."""
        match self:
            # Aborts don't happen during application, only leader changes. But
            # we consider them non-deterministic in case an abort should happen
            # unexpectedly below Raft.
            case Abort():
                return False
            # Possible data corruption local to this node.
            case InvalidData():
                return False
            # Input errors are (likely) deterministic. They might not be in
            # case data was corrupted in flight, but we ignore this case.
            case InvalidInput():
                return True
            # IO errors are typically local to the node (e.g. faulty disk).
            case IO():
                return False
            # Write commands in read-only transactions are deterministic.
            case ReadOnly():
                return True
            # Write conflicts are determinstic.
            case Serialization():
                return True
            case _:
                raise Panic(f"unknown error {self!r}")

    def serialize(self, serializer: "Serializer") -> None:
        """Serializes the error, like `#[derive(Serialize)]`."""
        match self:
            case Abort():
                serializer.serialize_unit_variant(0)
            case InvalidData(message=message):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_str(message)
            case InvalidInput(message=message):
                serializer.serialize_newtype_variant(2)
                serializer.serialize_str(message)
            case IO(message=message):
                serializer.serialize_newtype_variant(3)
                serializer.serialize_str(message)
            case ReadOnly():
                serializer.serialize_unit_variant(4)
            case Serialization():
                serializer.serialize_unit_variant(5)
            case _:
                raise Panic(f"unknown error {self!r}")

    @classmethod
    def deserialize(cls, deserializer: "Deserializer") -> "Error":
        """Deserializes an error, like `#[derive(Deserialize)]`."""
        match deserializer.deserialize_enum():
            case 0:
                return Abort()
            case 1:
                return InvalidData(deserializer.deserialize_str())
            case 2:
                return InvalidInput(deserializer.deserialize_str())
            case 3:
                return IO(deserializer.deserialize_str())
            case 4:
                return ReadOnly()
            case 5:
                return Serialization()
            case index:
                raise deserializer.invalid_variant(index, 6)

    @classmethod
    def from_exception(cls, error: BaseException) -> "Error":
        """Converts a Python exception into an `Error`, like the Rust `From`
        implementations: OS and IO errors become `Error.IO`, and e.g. integer
        or float parse errors become `Error.InvalidInput`."""
        match error:
            case Error():
                return error
            case UnicodeDecodeError():
                return InvalidData(display_utf8_error(error))
            case OSError() if error.errno is not None and error.strerror is not None:
                return IO(f"{error.strerror} (os error {error.errno})")
            case OSError():
                return IO(str(error))
            case ValueError():
                return InvalidInput(str(error))
            case _:
                raise error


class Abort(Error):
    """The operation was aborted and must be retried. This typically happens
    with e.g. Raft leader changes. This is used instead of implementing
    complex retry logic and replay protection in Raft."""

    def __str__(self) -> str:
        return "operation aborted"


class _MessageError(Error):
    """An error variant carrying a message."""

    PREFIX: ClassVar[str]
    __match_args__ = ("message",)

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return f"{self.PREFIX}: {self.message}"

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple(type(self).__name__, [self.message], pretty)


class InvalidData(_MessageError):
    """Invalid data, typically decoding errors or unexpected internal values."""

    PREFIX = "invalid data"


class InvalidInput(_MessageError):
    """Invalid user input, typically parser or query errors."""

    PREFIX = "invalid input"


class IO(_MessageError):
    """An IO error."""

    PREFIX = "io error"


class ReadOnly(Error):
    """A write was attempted in a read-only transaction."""

    def __str__(self) -> str:
        return "read-only transaction"


class Serialization(Error):
    """A write transaction conflicted with a different writer and lost. The
    transaction must be retried."""

    def __str__(self) -> str:
        return "serialization failure, retry transaction"


Error.Abort = Abort
Error.InvalidData = InvalidData
Error.InvalidInput = InvalidInput
Error.IO = IO
Error.ReadOnly = ReadOnly
Error.Serialization = Serialization


def errdata(message: str) -> InvalidData:
    """Constructs an Error.InvalidData for the given message. Unlike the Rust
    `errdata!` macro the caller formats the message (with an f-string), and
    must `raise` the returned error."""
    return InvalidData(message)


def errinput(message: str) -> InvalidInput:
    """Constructs an Error.InvalidInput for the given message. Unlike the Rust
    `errinput!` macro the caller formats the message (with an f-string), and
    must `raise` the returned error."""
    return InvalidInput(message)


def panic(message: str) -> NoReturn:
    """Panics with the given message, like Rust's `panic!`."""
    raise Panic(message)


def assert_eq[T](
    left: T, right: T, message: str | None = None, fmt: Callable[[T], str] = debug
) -> None:
    """Panics if the values differ, like Rust's `assert_eq!`. The values are
    formatted with `fmt` (their `Debug` representation by default)."""
    if left != right:
        raise Panic(
            f"assertion `left == right` failed{f': {message}' if message is not None else ''}"
            f"\n  left: {fmt(left)}\n right: {fmt(right)}"
        )


def assert_ne[T](
    left: T, right: T, message: str | None = None, fmt: Callable[[T], str] = debug
) -> None:
    """Panics if the values are equal, like Rust's `assert_ne!`."""
    if left == right:
        raise Panic(
            f"assertion `left != right` failed{f': {message}' if message is not None else ''}"
            f"\n  left: {fmt(left)}\n right: {fmt(right)}"
        )
