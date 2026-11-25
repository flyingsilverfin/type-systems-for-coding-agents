"""The toyDB client.

Mirrors `src/client.rs`.
"""

import random
import time
from collections.abc import Callable
from typing import BinaryIO, Self

from toydb import net
from toydb.encoding import bincode
from toydb.error import Error, errdata
from toydb.fmt import debug
from toydb.server import RESPONSE_RESULT, Request, Response, Status
from toydb.sql.execution import StatementResult
from toydb.sql.types import Table
from toydb.storage import mvcc


class Client:
    """A toyDB client. Connects to a server via TCP and submits SQL statements
    and other requests.

    The connection is closed by `close()` (the Rust `Drop`), or by using the
    client as a context manager.
    """

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        """Creates a client from an inbound response stream and an outbound
        request stream. Use `connect()` to connect to a server."""
        self.reader = reader
        """Inbound response stream."""
        self.writer = writer
        """Outbound request stream."""
        self._txn: mvcc.TransactionState | None = None
        """The current transaction, if any."""

    @classmethod
    def connect(cls, addr: net.Addr) -> "Client":
        """Connects to a toyDB server, creating a new client."""
        try:
            sock = net.connect(addr)
        except OSError as error:
            raise Error.from_exception(error) from error
        reader = sock.makefile("rb")
        writer = sock.makefile("wb")
        sock.close()  # the file objects keep the connection open
        return cls(reader, writer)

    def close(self) -> None:
        """Closes the connection to the server."""
        self.writer.close()
        self.reader.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(self, request: Request) -> Response:
        """Sends a request to the server, returning the response."""
        request.encode_into(self.writer)
        try:
            self.writer.flush()
        except OSError as error:
            raise Error.from_exception(error) from error
        response = bincode.deserialize_from(RESPONSE_RESULT, self.reader)
        if isinstance(response, Error):
            raise response
        return response

    def execute(self, statement: str) -> StatementResult:
        """Executes a SQL statement."""
        match self.request(Request.Execute(statement)):
            case Response.Execute(result):
                pass
            case response:
                raise errdata(f"unexpected response {debug(response)}")
        # Update the transaction state.
        match result:
            case StatementResult.Begin(state):
                self._txn = state.clone()
            case StatementResult.Commit():
                self._txn = None
            case StatementResult.Rollback():
                self._txn = None
            case _:
                pass
        return result

    def authenticate(self, user: str, password: str | None = None) -> None:
        """Logs in as a user."""
        match self.request(Request.Authenticate(user, password)):
            case Response.Authenticate():
                pass
            case response:
                raise errdata(f"unexpected response: {debug(response)}")

    def backup(self, as_of: mvcc.Version | None = None) -> bytes:
        """Creates a backup of the database, as of a version if given."""
        match self.request(Request.Backup(as_of)):
            case Response.Backup(data):
                return data
            case response:
                raise errdata(f"unexpected response: {debug(response)}")

    def restore(self, data: bytes) -> None:
        """Restores a backup."""
        match self.request(Request.Restore(data)):
            case Response.Restore():
                pass
            case response:
                raise errdata(f"unexpected response: {debug(response)}")

    def get_table(self, table: str) -> Table:
        """Fetches a table schema."""
        match self.request(Request.GetTable(table)):
            case Response.GetTable(schema):
                return schema
            case response:
                raise errdata(f"unexpected response: {debug(response)}")

    def list_tables(self) -> list[str]:
        """Lists database tables."""
        match self.request(Request.ListTables()):
            case Response.ListTables(tables):
                return tables
            case response:
                raise errdata(f"unexpected response: {debug(response)}")

    def status(self) -> Status:
        """Returns server status."""
        match self.request(Request.Status()):
            case Response.Status(status):
                return status
            case response:
                raise errdata(f"unexpected response: {debug(response)}")

    def txn(self) -> mvcc.TransactionState | None:
        """Returns the transaction state."""
        return self._txn

    def with_retry[T](self, f: Callable[["Client"], T]) -> T:
        """Runs the given closure, automatically retrying serialization and
        abort errors. If a transaction is open following an error, it is
        automatically rolled back. It is the caller's responsibility to use a
        transaction in the closure where appropriate (i.e. when it is not
        idempotent)."""
        MAX_RETRIES = 10
        MIN_WAIT = 10
        MAX_WAIT = 2_000
        retries = 0
        while True:
            try:
                return f(self)
            except (Error.Serialization, Error.Abort):
                if retries >= MAX_RETRIES:
                    self._rollback_quietly()
                    raise
                if self.txn() is not None:
                    self.execute("ROLLBACK")
                # Use exponential backoff starting at MIN_WAIT doubling up to
                # MAX_WAIT, but randomize the wait time in this interval to
                # reduce the chance of collisions.
                wait = min(MAX_WAIT, MIN_WAIT * 2**retries)
                wait = random.randint(MIN_WAIT, wait)
                time.sleep(wait / 1000)
                retries += 1
            except Error:
                self._rollback_quietly()
                raise

    def _rollback_quietly(self) -> None:
        """Rolls back the open transaction, if any, ignoring errors."""
        if self.txn() is not None:
            try:
                self.execute("ROLLBACK")
            except Error:
                pass  # ignore rollback error
