"""A PostgreSQL client for the messages the server supports: startup with the
MD5 password method, simple and extended query, COPY. `toysql --pg` and the
test runners use it.
"""

import struct
from typing import BinaryIO, Self

from toydb import net
from toydb.error import Error, errdata, panic
from toydb.pgwire.connection import md5_response
from toydb.pgwire.message import (
    CANCEL_REQUEST_CODE,
    GSSENC_REQUEST_CODE,
    PROTOCOL_VERSION,
    SSL_REQUEST_CODE,
    AuthenticationMD5Password,
    AuthenticationOk,
    Backend,
    BackendKeyData,
    Bind,
    BindComplete,
    Buffer,
    CancelRequest,
    Close,
    CloseComplete,
    CommandComplete,
    CopyData,
    CopyDone,
    CopyFail,
    CopyInResponse,
    CopyOutResponse,
    DataRow,
    Describe,
    EmptyQueryResponse,
    ErrorResponse,
    Execute,
    Field,
    Flush,
    Frontend,
    GSSENCRequest,
    NoData,
    ParameterDescription,
    ParameterStatus,
    Parse,
    ParseComplete,
    PasswordMessage,
    PgError,
    PortalSuspended,
    Query,
    ReadyForQuery,
    RowDescription,
    SSLRequest,
    StartupMessage,
    Sync,
    Terminate,
    int16,
    int32,
    read_exact,
    string,
)
from toydb.sql.execution.users import hash_password


class Client:
    """A PostgreSQL client connection.

    The connection is closed by `close()`, or by using the client as a context
    manager.
    """

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        """Creates a client from an inbound and an outbound stream. Use
        `connect()` to connect to a server."""
        self.reader = reader
        self.writer = writer
        self.closed = False
        """Whether the connection is closed."""

    @classmethod
    def connect(cls, addr: net.Addr) -> "Client":
        """Connects to the PostgreSQL port of a toyDB server."""
        try:
            sock = net.connect(addr)
        except OSError as error:
            raise Error.from_exception(error) from error
        reader = sock.makefile("rb")
        writer = sock.makefile("wb")
        sock.close()  # the file objects keep the connection open
        return cls(reader, writer)

    def close(self) -> None:
        """Closes the connection."""
        self.closed = True
        for stream in (self.writer, self.reader):
            try:
                stream.close()
            except OSError:
                pass  # the server is already gone

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send(self, message: Frontend) -> None:
        """Sends a message (buffered until flushed)."""
        try:
            self.writer.write(encode(message))
        except OSError as error:
            raise Error.from_exception(error) from error

    def flush(self) -> None:
        """Sends the buffered messages."""
        try:
            self.writer.flush()
        except OSError as error:
            raise Error.from_exception(error) from error

    def receive(self) -> Backend | None:
        """Receives a message, or returns None if the server closed the
        connection."""
        try:
            header = read_exact(self.reader, 5)
            body = None
            if header is not None:
                if (length := struct.unpack(">i", header[1:])[0]) < 4:
                    raise errdata(f"invalid message length {length}")
                body = read_exact(self.reader, length - 4)
        except OSError as error:
            raise Error.from_exception(error) from error
        if header is None or body is None:
            self.close()
            return None
        return decode(header[:1], body)

    def receive_until_ready(self) -> list[Backend]:
        """Receives messages until ReadyForQuery."""
        messages: list[Backend] = []
        while (message := self.receive()) is not None:
            messages.append(message)
            if isinstance(message, ReadyForQuery):
                break
        return messages

    def request_ssl(self) -> bytes:
        """Sends SSLRequest, returning the server's one-byte answer."""
        self.send(SSLRequest())
        self.flush()
        try:
            return self.reader.read(1)
        except OSError as error:
            raise Error.from_exception(error) from error

    def startup(
        self,
        user: str,
        password: str | None = None,
        parameters: dict[str, str] | None = None,
        version: int = PROTOCOL_VERSION,
    ) -> list[Backend]:
        """Starts the connection as a user, with the MD5 password method, and
        returns the messages received."""
        self.send(StartupMessage(version, {"user": user, **(parameters or {})}))
        self.flush()
        messages: list[Backend] = []
        while (message := self.receive()) is not None:
            messages.append(message)
            match message:
                case AuthenticationMD5Password(salt) if password is None:
                    self.close()
                    break
                case AuthenticationMD5Password(salt):
                    response = md5_response(hash_password(user, password), salt)
                    self.send(PasswordMessage(response))
                    self.flush()
                case ReadyForQuery():
                    break
        return messages

    def query(self, sql: str, data: str | None = None, fail: str | None = None) -> list[Backend]:
        """Executes a simple query, with the data or the failure message of a
        COPY FROM STDIN, and returns the messages received."""
        self.send(Query(sql))
        self.flush()
        messages: list[Backend] = []
        while (message := self.receive()) is not None:
            messages.append(message)
            match message:
                case CopyInResponse() if fail is not None:
                    self.send(CopyFail(fail))
                    self.flush()
                case CopyInResponse():
                    if data is not None:
                        self.send(CopyData(data.encode()))
                    self.send(CopyDone())
                    self.flush()
                case ReadyForQuery():
                    break
        return messages

    def sync(self) -> list[Backend]:
        """Sends Sync after the queued messages, and returns the messages
        received."""
        self.send(Sync())
        self.flush()
        return self.receive_until_ready()

    def terminate(self) -> None:
        """Closes the connection with Terminate, and waits for the server to close it."""
        self.send(Terminate())
        self.flush()
        while self.receive() is not None:
            pass


def encode(message: Frontend) -> bytes:
    """Encodes a frontend message."""
    match message:
        case SSLRequest():
            return int32(8) + int32(SSL_REQUEST_CODE)
        case GSSENCRequest():
            return int32(8) + int32(GSSENC_REQUEST_CODE)
        case CancelRequest(process_id, secret_key):
            return int32(16) + int32(CANCEL_REQUEST_CODE) + int32(process_id) + int32(secret_key)
        case StartupMessage(version, parameters):
            body = int32(version)
            for name, value in parameters.items():
                body += string(name) + string(value)
            body += b"\0"
            return int32(len(body) + 4) + body
        case PasswordMessage(password):
            body = string(password)
        case Query(sql):
            body = string(sql)
        case Parse(name, query, types):
            body = string(name) + string(query) + int16(len(types))
            body += b"".join(int32(oid) for oid in types)
        case Bind(portal, statement, formats, parameters, results):
            body = string(portal) + string(statement)
            body += int16(len(formats)) + b"".join(int16(code) for code in formats)
            body += int16(len(parameters))
            for parameter in parameters:
                body += int32(-1) if parameter is None else int32(len(parameter)) + parameter
            body += int16(len(results)) + b"".join(int16(code) for code in results)
        case Describe(kind, name) | Close(kind, name):
            body = kind.encode() + string(name)
        case Execute(portal, max_rows):
            body = string(portal) + int32(max_rows)
        case CopyData(data):
            body = data
        case CopyFail(text):
            body = string(text)
        case Sync() | Flush() | Terminate() | CopyDone():
            body = b""
        case _:
            panic(f"unknown frontend message {message!r}")
    return message.TYPE + int32(len(body) + 4) + body


def decode(type: bytes, body: bytes) -> Backend:
    """Decodes a backend message from its type byte and body."""
    buffer = Buffer(body)
    message: Backend
    try:
        match type:
            case AuthenticationOk.TYPE:
                match buffer.int32():
                    case 0:
                        message = AuthenticationOk()
                    case 5:
                        message = AuthenticationMD5Password(buffer.bytes(4))
                    case method:
                        raise errdata(f"unsupported authentication method {method}")
            case ParameterStatus.TYPE:
                message = ParameterStatus(buffer.string(), buffer.string())
            case BackendKeyData.TYPE:
                message = BackendKeyData(buffer.int32(), buffer.int32())
            case ReadyForQuery.TYPE:
                message = ReadyForQuery(chr(buffer.byte()))
            case RowDescription.TYPE:
                fields: list[Field] = []
                for _ in range(buffer.int16()):
                    name = buffer.string()
                    buffer.int32()  # table OID
                    buffer.int16()  # column number
                    oid, size = buffer.int32(), buffer.int16()
                    buffer.int32()  # type modifier
                    fields.append(Field(name, oid, size, buffer.int16()))
                message = RowDescription(fields)
            case DataRow.TYPE:
                values: list[bytes | None] = []
                for _ in range(buffer.int16()):
                    length = buffer.int32()
                    values.append(None if length == -1 else buffer.bytes(length))
                message = DataRow(values)
            case CommandComplete.TYPE:
                message = CommandComplete(buffer.string())
            case ErrorResponse.TYPE:
                error: dict[str, str] = {}
                while (code := buffer.byte()) != 0:
                    error[chr(code)] = buffer.string()
                message = ErrorResponse(
                    error.get("V", error.get("S", "")), error.get("C", ""), error.get("M", "")
                )
            case ParameterDescription.TYPE:
                message = ParameterDescription([buffer.int32() for _ in range(buffer.int16())])
            case CopyInResponse.TYPE | CopyOutResponse.TYPE:
                buffer.byte()  # the overall format
                columns = buffer.int16()
                for _ in range(columns):
                    buffer.int16()
                kind = CopyInResponse if type == CopyInResponse.TYPE else CopyOutResponse
                message = kind(columns)
            case CopyData.TYPE:
                message = CopyData(buffer.rest())
            case CopyDone.TYPE:
                message = CopyDone()
            case EmptyQueryResponse.TYPE:
                message = EmptyQueryResponse()
            case ParseComplete.TYPE:
                message = ParseComplete()
            case BindComplete.TYPE:
                message = BindComplete()
            case CloseComplete.TYPE:
                message = CloseComplete()
            case NoData.TYPE:
                message = NoData()
            case PortalSuspended.TYPE:
                message = PortalSuspended()
            case _:
                raise errdata(f"unknown backend message type {chr(type[0])}")
        buffer.finish()
    except PgError as error:
        raise errdata(str(error)) from None
    return message
