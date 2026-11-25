"""The messages of the PostgreSQL protocol: decoding of frontend messages and
encoding of backend messages.

A message is a type byte, a 4-byte big-endian length that counts itself, and
the body; the startup-phase messages have no type byte. Integers are
big-endian and strings are zero-terminated UTF-8.
"""

import struct
from dataclasses import dataclass
from typing import BinaryIO, ClassVar

from toydb.error import panic

PROTOCOL_VERSION = 196608
"""The protocol version 3.0."""

SSL_REQUEST_CODE = 80877103
"""The request code of SSLRequest."""

GSSENC_REQUEST_CODE = 80877104
"""The request code of GSSENCRequest."""

CANCEL_REQUEST_CODE = 80877102
"""The request code of CancelRequest."""


class PgError(Exception):
    """An error sent to the client as ErrorResponse."""

    def __init__(self, code: str, message: str, fatal: bool = False) -> None:
        super().__init__(code, message, fatal)
        self.code = code
        """The SQLSTATE code."""
        self.message = message
        """The message."""
        self.fatal = fatal
        """Whether the connection is closed after the error."""

    def __str__(self) -> str:
        return self.message

    def response(self) -> "ErrorResponse":
        """Returns the ErrorResponse of the error."""
        return ErrorResponse("FATAL" if self.fatal else "ERROR", self.code, self.message)


def invalid_format() -> PgError:
    """Returns the error for a malformed message."""
    return PgError("08P01", "invalid message format", fatal=True)


class Frontend:
    """A message from the client."""

    TYPE: ClassVar[bytes]
    """The type byte (none for startup-phase messages)."""


class Backend:
    """A message to the client."""

    TYPE: ClassVar[bytes]
    """The type byte."""


@dataclass(frozen=True)
class SSLRequest(Frontend):
    """Asks for an SSL connection."""


@dataclass(frozen=True)
class GSSENCRequest(Frontend):
    """Asks for a GSSAPI-encrypted connection."""


@dataclass(frozen=True)
class CancelRequest(Frontend):
    """Asks to cancel the query of another connection."""

    process_id: int
    secret_key: int


@dataclass(frozen=True)
class StartupMessage(Frontend):
    """Starts a connection."""

    version: int
    parameters: dict[str, str]


@dataclass(frozen=True)
class PasswordMessage(Frontend):
    """A password."""

    TYPE = b"p"
    password: str


@dataclass(frozen=True)
class Query(Frontend):
    """A simple query."""

    TYPE = b"Q"
    sql: str


@dataclass(frozen=True)
class Parse(Frontend):
    """Prepares a statement."""

    TYPE = b"P"
    name: str
    query: str
    types: list[int]
    """The parameter type OIDs."""


@dataclass(frozen=True)
class Bind(Frontend):
    """Binds parameter values to a prepared statement, creating a portal."""

    TYPE = b"B"
    portal: str
    statement: str
    parameter_formats: list[int]
    parameters: list[bytes | None]
    """The parameter values, None for NULL."""
    result_formats: list[int]


@dataclass(frozen=True)
class Describe(Frontend):
    """Describes a prepared statement or a portal."""

    TYPE = b"D"
    kind: str
    name: str


@dataclass(frozen=True)
class Execute(Frontend):
    """Executes a portal."""

    TYPE = b"E"
    portal: str
    max_rows: int


@dataclass(frozen=True)
class Close(Frontend):
    """Closes a prepared statement or a portal."""

    TYPE = b"C"
    kind: str
    name: str


@dataclass(frozen=True)
class Sync(Frontend):
    """Ends an extended query."""

    TYPE = b"S"


@dataclass(frozen=True)
class Flush(Frontend):
    """Asks for the pending responses."""

    TYPE = b"H"


@dataclass(frozen=True)
class Terminate(Frontend):
    """Closes the connection."""

    TYPE = b"X"


@dataclass(frozen=True)
class CopyData(Frontend, Backend):
    """COPY data, in either direction."""

    TYPE = b"d"
    data: bytes


@dataclass(frozen=True)
class CopyDone(Frontend, Backend):
    """The end of COPY data, in either direction."""

    TYPE = b"c"


@dataclass(frozen=True)
class CopyFail(Frontend):
    """Aborts COPY FROM STDIN."""

    TYPE = b"f"
    message: str


@dataclass(frozen=True)
class AuthenticationOk(Backend):
    """The client is authenticated."""

    TYPE = b"R"


@dataclass(frozen=True)
class AuthenticationMD5Password(Backend):
    """Asks for an MD5-hashed password."""

    TYPE = b"R"
    salt: bytes


@dataclass(frozen=True)
class ParameterStatus(Backend):
    """The value of a server parameter."""

    TYPE = b"S"
    name: str
    value: str


@dataclass(frozen=True)
class BackendKeyData(Backend):
    """The connection's process ID and secret key, for cancel requests."""

    TYPE = b"K"
    process_id: int
    secret_key: int


@dataclass(frozen=True)
class ReadyForQuery(Backend):
    """The server is ready for a new query."""

    TYPE = b"Z"
    status: str
    """The transaction status."""


@dataclass(frozen=True)
class Field:
    """A column of a RowDescription."""

    name: str
    type_oid: int
    type_size: int
    format: int


@dataclass(frozen=True)
class RowDescription(Backend):
    """The columns of the rows that follow."""

    TYPE = b"T"
    fields: list[Field]


@dataclass(frozen=True)
class DataRow(Backend):
    """A row."""

    TYPE = b"D"
    values: list[bytes | None]
    """The encoded values, None for NULL."""


@dataclass(frozen=True)
class CommandComplete(Backend):
    """A statement completed."""

    TYPE = b"C"
    tag: str


@dataclass(frozen=True)
class ErrorResponse(Backend):
    """An error."""

    TYPE = b"E"
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class EmptyQueryResponse(Backend):
    """The response to an empty query."""

    TYPE = b"I"


@dataclass(frozen=True)
class ParseComplete(Backend):
    """A Parse completed."""

    TYPE = b"1"


@dataclass(frozen=True)
class BindComplete(Backend):
    """A Bind completed."""

    TYPE = b"2"


@dataclass(frozen=True)
class CloseComplete(Backend):
    """A Close completed."""

    TYPE = b"3"


@dataclass(frozen=True)
class NoData(Backend):
    """The described statement or portal returns no rows."""

    TYPE = b"n"


@dataclass(frozen=True)
class PortalSuspended(Backend):
    """An Execute reached its maximum number of rows."""

    TYPE = b"s"


@dataclass(frozen=True)
class ParameterDescription(Backend):
    """The parameter types of a prepared statement."""

    TYPE = b"t"
    types: list[int]
    """The parameter type OIDs."""


@dataclass(frozen=True)
class CopyInResponse(Backend):
    """The server is ready for COPY data from the client."""

    TYPE = b"G"
    columns: int


@dataclass(frozen=True)
class CopyOutResponse(Backend):
    """The server sends COPY data to the client."""

    TYPE = b"H"
    columns: int


class Buffer:
    """A message body being decoded."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def bytes(self, length: int) -> bytes:
        """Decodes a number of bytes."""
        if length < 0 or self.pos + length > len(self.data):
            raise invalid_format()
        data = self.data[self.pos : self.pos + length]
        self.pos += length
        return data

    def byte(self) -> int:
        """Decodes a byte."""
        return self.bytes(1)[0]

    def int16(self) -> int:
        """Decodes a signed 16-bit integer."""
        value: int = struct.unpack(">h", self.bytes(2))[0]
        return value

    def int32(self) -> int:
        """Decodes a signed 32-bit integer."""
        value: int = struct.unpack(">i", self.bytes(4))[0]
        return value

    def string(self) -> str:
        """Decodes a zero-terminated string."""
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise invalid_format()
        data = self.data[self.pos : end]
        self.pos = end + 1
        try:
            return data.decode()
        except UnicodeDecodeError:
            raise invalid_format() from None

    def target(self) -> str:
        """Decodes the target of a Describe or Close: S or P."""
        if (target := chr(self.byte())) not in ("S", "P"):
            raise invalid_format()
        return target

    def rest(self) -> bytes:
        """Decodes the rest of the body."""
        return self.bytes(len(self.data) - self.pos)

    def finish(self) -> None:
        """Checks that the whole body was decoded."""
        if self.pos != len(self.data):
            raise invalid_format()


def int16(value: int) -> bytes:
    """Encodes a signed 16-bit integer."""
    return struct.pack(">h", value)


def int32(value: int) -> bytes:
    """Encodes a signed 32-bit integer."""
    return struct.pack(">i", value)


def string(value: str) -> bytes:
    """Encodes a zero-terminated string."""
    return value.encode() + b"\0"


def read_exact(reader: BinaryIO, length: int) -> bytes | None:
    """Reads a number of bytes, or returns None if the connection is closed
    before."""
    data = reader.read(length)
    if len(data) < length:
        return None
    return data


def read_startup(reader: BinaryIO) -> Frontend | None:
    """Reads a startup-phase message, or returns None if the connection is
    closed."""
    if (header := read_exact(reader, 4)) is None:
        return None
    length = struct.unpack(">i", header)[0]
    if length < 8:
        raise invalid_format()
    if (body := read_exact(reader, length - 4)) is None:
        return None
    buffer = Buffer(body)
    match buffer.int32():
        case code if code == SSL_REQUEST_CODE:
            message: Frontend = SSLRequest()
        case code if code == GSSENC_REQUEST_CODE:
            message = GSSENCRequest()
        case code if code == CANCEL_REQUEST_CODE:
            message = CancelRequest(buffer.int32(), buffer.int32())
        case version if version != PROTOCOL_VERSION:
            # Only protocol 3.0 is supported, so the rest isn't read.
            return StartupMessage(version, {})
        case version:
            parameters: dict[str, str] = {}
            while (name := buffer.string()) != "":
                parameters[name] = buffer.string()
            message = StartupMessage(version, parameters)
    buffer.finish()
    return message


def read_message(reader: BinaryIO) -> Frontend | None:
    """Reads a frontend message after the startup phase, or returns None if
    the connection is closed."""
    if (header := read_exact(reader, 5)) is None:
        return None
    length = struct.unpack(">i", header[1:])[0]
    if length < 4:
        raise invalid_format()
    if (body := read_exact(reader, length - 4)) is None:
        return None
    return decode(header[:1], body)


def decode(type: bytes, body: bytes) -> Frontend:
    """Decodes a frontend message from its type byte and body."""
    buffer = Buffer(body)
    message: Frontend
    match type:
        case PasswordMessage.TYPE:
            message = PasswordMessage(buffer.string())
        case Query.TYPE:
            message = Query(buffer.string())
        case Parse.TYPE:
            name, query = buffer.string(), buffer.string()
            message = Parse(name, query, [buffer.int32() for _ in range(buffer.int16())])
        case Bind.TYPE:
            portal, statement = buffer.string(), buffer.string()
            formats = [buffer.int16() for _ in range(buffer.int16())]
            parameters: list[bytes | None] = []
            for _ in range(buffer.int16()):
                length = buffer.int32()
                parameters.append(None if length == -1 else buffer.bytes(length))
            results = [buffer.int16() for _ in range(buffer.int16())]
            message = Bind(portal, statement, formats, parameters, results)
        case Describe.TYPE:
            message = Describe(buffer.target(), buffer.string())
        case Execute.TYPE:
            message = Execute(buffer.string(), buffer.int32())
        case Close.TYPE:
            message = Close(buffer.target(), buffer.string())
        case Sync.TYPE:
            message = Sync()
        case Flush.TYPE:
            message = Flush()
        case Terminate.TYPE:
            message = Terminate()
        case CopyData.TYPE:
            message = CopyData(buffer.rest())
        case CopyDone.TYPE:
            message = CopyDone()
        case CopyFail.TYPE:
            message = CopyFail(buffer.string())
        case _:
            raise PgError("08P01", f"invalid frontend message type {chr(type[0])}", fatal=True)
    buffer.finish()
    return message


def encode(message: Backend) -> bytes:
    """Encodes a backend message."""
    match message:
        case AuthenticationOk():
            body = int32(0)
        case AuthenticationMD5Password(salt):
            body = int32(5) + salt
        case ParameterStatus(name, value):
            body = string(name) + string(value)
        case BackendKeyData(process_id, secret_key):
            body = int32(process_id) + int32(secret_key)
        case ReadyForQuery(status):
            body = status.encode()
        case RowDescription(fields):
            body = int16(len(fields))
            for field in fields:
                body += string(field.name) + int32(0) + int16(0)
                body += int32(field.type_oid) + int16(field.type_size) + int32(-1)
                body += int16(field.format)
        case DataRow(values):
            body = int16(len(values))
            for value in values:
                body += int32(-1) if value is None else int32(len(value)) + value
        case CommandComplete(tag):
            body = string(tag)
        case ErrorResponse(severity, code, text):
            body = b"S" + string(severity) + b"V" + string(severity)
            body += b"C" + string(code) + b"M" + string(text) + b"\0"
        case ParameterDescription(types):
            body = int16(len(types)) + b"".join(int32(oid) for oid in types)
        case CopyInResponse(columns) | CopyOutResponse(columns):
            body = b"\0" + int16(columns) + int16(0) * columns
        case CopyData(data):
            body = data
        case (
            EmptyQueryResponse()
            | ParseComplete()
            | BindComplete()
            | CloseComplete()
            | NoData()
            | PortalSuspended()
            | CopyDone()
        ):
            body = b""
        case _:
            panic(f"unknown backend message {message!r}")
    return message.TYPE + int32(len(body) + 4) + body
