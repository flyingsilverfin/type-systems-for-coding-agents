"""Tests for toydb.pgwire.message and the message encoding of
toydb.pgwire.client.

Frontend messages are encoded by the client and decoded by the server, and
backend messages the other way around; these tests check both directions and
the wire format of some messages.
"""

import io
import unittest

from toydb.error import Error
from toydb.pgwire import client, message
from toydb.pgwire.message import (
    AuthenticationMD5Password,
    AuthenticationOk,
    Backend,
    BackendKeyData,
    Bind,
    BindComplete,
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
)


class TestFrontend(unittest.TestCase):
    def test_startup(self) -> None:
        """Startup-phase messages round-trip, without a type byte."""
        cases: list[Frontend] = [
            SSLRequest(),
            GSSENCRequest(),
            CancelRequest(7, 123456),
            StartupMessage(message.PROTOCOL_VERSION, {}),
            StartupMessage(message.PROTOCOL_VERSION, {"user": "alice", "application_name": "psql"}),
        ]
        for case in cases:
            with self.subTest(message=case):
                self.assertEqual(message.read_startup(io.BytesIO(client.encode(case))), case)
        # The parameters of other protocol versions aren't read.
        body = b"\x00\x00\x00\x09\x00\x02\x00\x00x"
        self.assertEqual(message.read_startup(io.BytesIO(body)), StartupMessage(2 << 16, {}))
        self.assertEqual(client.encode(SSLRequest()), b"\x00\x00\x00\x08\x04\xd2\x16\x2f")

    def test_messages(self) -> None:
        """Messages after startup round-trip."""
        cases: list[Frontend] = [
            PasswordMessage("md5abc"),
            Query("SELECT 'é'; SELECT 2"),
            Parse("s1", "SELECT $1", [20, 25]),
            Parse("", "", []),
            Bind("p", "s", [0, 1], [b"1", None, b""], [1]),
            Bind("", "", [], [], []),
            Describe("S", "s1"),
            Describe("P", ""),
            Execute("p", 10),
            Close("P", "p"),
            Sync(),
            Flush(),
            Terminate(),
            CopyData(b"1\ta\n"),
            CopyDone(),
            CopyFail("gave up"),
        ]
        stream = io.BytesIO(b"".join(client.encode(case) for case in cases))
        for case in cases:
            with self.subTest(message=case):
                self.assertEqual(message.read_message(stream), case)
        self.assertIsNone(message.read_message(stream))
        self.assertEqual(client.encode(Query("x")), b"Q\x00\x00\x00\x06x\x00")

    def test_closed(self) -> None:
        """A connection closed before or within a message has no message."""
        self.assertIsNone(message.read_startup(io.BytesIO(b"")))
        self.assertIsNone(message.read_startup(io.BytesIO(b"\x00\x00\x00\x08\x04")))
        self.assertIsNone(message.read_message(io.BytesIO(b"Q\x00\x00\x00\x06x")))

    def test_errors(self) -> None:
        """Unknown message types and malformed messages are fatal protocol
        violations."""
        cases: list[tuple[bytes, str]] = [
            (b"y\x00\x00\x00\x04", "invalid frontend message type y"),
            (b"Q\x00\x00\x00\x05x", "invalid message format"),
            (b"Q\x00\x00\x00\x07x\x00y", "invalid message format"),
            (b"E\x00\x00\x00\x06p\x00", "invalid message format"),
            (b"Q\x00\x00\x00\x02", "invalid message format"),
            (b"D\x00\x00\x00\x06X\x00", "invalid message format"),
        ]
        for data, expect in cases:
            with self.subTest(data=data):
                with self.assertRaises(PgError) as raised:
                    message.read_message(io.BytesIO(data))
                self.assertEqual(raised.exception.code, "08P01")
                self.assertEqual(raised.exception.message, expect)
                self.assertTrue(raised.exception.fatal)


class TestBackend(unittest.TestCase):
    def test_messages(self) -> None:
        """Backend messages round-trip."""
        cases: list[Backend] = [
            AuthenticationOk(),
            AuthenticationMD5Password(b"\x01\x02\x03\x04"),
            ParameterStatus("client_encoding", "UTF8"),
            BackendKeyData(3, 12345),
            ReadyForQuery("I"),
            ReadyForQuery("T"),
            RowDescription([Field("id", 20, 8, 0), Field("?column?", 25, -1, 1)]),
            RowDescription([]),
            DataRow([b"1", None, b""]),
            CommandComplete("INSERT 0 2"),
            ErrorResponse("FATAL", "28000", "no user name given"),
            EmptyQueryResponse(),
            ParseComplete(),
            BindComplete(),
            CloseComplete(),
            NoData(),
            PortalSuspended(),
            ParameterDescription([16, 1043]),
            ParameterDescription([]),
            CopyInResponse(3),
            CopyOutResponse(0),
            CopyData(b"\\N\n"),
            CopyDone(),
        ]
        for case in cases:
            with self.subTest(message=case):
                encoded = message.encode(case)
                self.assertEqual(client.decode(encoded[:1], encoded[5:]), case)

    def test_format(self) -> None:
        """The wire format of some backend messages."""
        cases: list[tuple[Backend, bytes]] = [
            (AuthenticationOk(), b"R\x00\x00\x00\x08\x00\x00\x00\x00"),
            (ReadyForQuery("I"), b"Z\x00\x00\x00\x05I"),
            (
                DataRow([None, b"ab"]),
                b"D\x00\x00\x00\x10\x00\x02\xff\xff\xff\xff\x00\x00\x00\x02ab",
            ),
            (
                RowDescription([Field("n", 20, 8, 1)]),
                b"T\x00\x00\x00\x1a\x00\x01n\x00\x00\x00\x00\x00\x00\x00"
                b"\x00\x00\x00\x14\x00\x08\xff\xff\xff\xff\x00\x01",
            ),
            (
                ErrorResponse("ERROR", "42000", "m"),
                b"E\x00\x00\x00\x1dSERROR\x00VERROR\x00C42000\x00Mm\x00\x00",
            ),
            (CopyInResponse(2), b"G\x00\x00\x00\x0b\x00\x00\x02\x00\x00\x00\x00"),
        ]
        for case, expect in cases:
            with self.subTest(message=case):
                self.assertEqual(message.encode(case), expect)

    def test_decode_errors(self) -> None:
        """The client rejects unknown and malformed backend messages."""
        for type, body in ((b"?", b""), (b"C", b"no terminator"), (b"R", b"\x00\x00\x00\x03")):
            with self.subTest(type=type, body=body):
                with self.assertRaises(Error.InvalidData):
                    client.decode(type, body)


if __name__ == "__main__":
    unittest.main()
