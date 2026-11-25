"""The `pg` command of the golden-script runners (`tests/sql/test_mod.py` and
`tests/e2e/test_tests.py`): drives a PostgreSQL connection per command prefix
with the PostgreSQL client, and prints the backend messages it receives, one
per line (consecutive ParameterStatus messages on one line).

    pg startup [user=<u>] [password=<p>] [application_name=<a>] [protocol=<major>.<minor>] [ssl]
    pg query "<sql>"
    pg parse [name=<s>] "<sql>" [types=<oid>,...]
    pg bind [portal=<p>] [statement=<s>] [params=<v>,...] [formats=<f>,...] [results=<f>,...]
    pg describe statement=<s> | portal=<p>
    pg execute [portal=<p>] [max_rows=<n>]
    pg close statement=<s> | portal=<p>
    pg sync
    pg terminate
    pg copy "<sql>" [data="<text>"] [fail="<message>"]

parse, bind, describe, execute and close queue their message until sync. In
params, NULL is a NULL value and 0x<hex> a value given as bytes.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from tests import goldenscript
from toydb.pgwire import types
from toydb.pgwire.client import Client
from toydb.pgwire.connection import format_code
from toydb.pgwire.message import (
    AuthenticationMD5Password,
    AuthenticationOk,
    Backend,
    BackendKeyData,
    Bind,
    BindComplete,
    Close,
    CloseComplete,
    CommandComplete,
    CopyData,
    CopyDone,
    CopyInResponse,
    CopyOutResponse,
    DataRow,
    Describe,
    EmptyQueryResponse,
    ErrorResponse,
    Execute,
    NoData,
    ParameterDescription,
    ParameterStatus,
    Parse,
    ParseComplete,
    PortalSuspended,
    ReadyForQuery,
    RowDescription,
)
from toydb.sql.types import Value


@dataclass
class Connection:
    """A connection of a command prefix."""

    client: Client
    portals: dict[str, list[int]] = field(default_factory=dict)
    """The result formats of the bound portals."""
    executes: list[list[int]] = field(default_factory=list)
    """The result formats of the queued Execute messages, whose rows are yet
    to be printed."""


class PgRunner:
    """Runs `pg` commands, with a PostgreSQL connection per command prefix."""

    def __init__(self, connect: Callable[[], Client]) -> None:
        """Creates a runner that opens connections with the given function."""
        self.connect = connect
        self.connections: dict[str, Connection] = {}

    def close(self) -> None:
        """Closes the connections."""
        for connection in self.connections.values():
            connection.client.close()
        self.connections.clear()

    def run(self, prefix: str, command: goldenscript.Command) -> str:
        """Runs a pg command for a prefix, returning its output."""
        args = command.consume_args()
        subcommand = args.next_pos()
        if subcommand == "startup":
            output = self.startup(prefix, args)
            args.reject_next()
            return output
        if (connection := self.connections.get(prefix)) is None:
            raise ValueError("no PostgreSQL connection")
        client = connection.client
        messages: list[Backend] = []
        match subcommand:
            case "query":
                messages = client.query(required(args.next_pos(), "query"))
            case "copy":
                sql = required(args.next_pos(), "query")
                messages = client.query(sql, args.take_key("data"), args.take_key("fail"))
            case "parse":
                name = args.take_key("name") or ""
                sql = required(args.next_pos(), "query")
                oids = [int(oid) for oid in split_list(args.take_key("types"))]
                client.send(Parse(name, sql, oids))
            case "bind":
                portal = args.take_key("portal") or ""
                statement = args.take_key("statement") or ""
                params = [parameter(value) for value in split_list(args.take_key("params"))]
                formats = [int(code) for code in split_list(args.take_key("formats"))]
                results = [int(code) for code in split_list(args.take_key("results"))]
                client.send(Bind(portal, statement, formats, params, results))
                connection.portals[portal] = results
            case "describe":
                client.send(Describe(*target(args)))
            case "execute":
                portal = args.take_key("portal") or ""
                max_rows = int(args.take_key("max_rows") or 0)
                client.send(Execute(portal, max_rows))
                connection.executes.append(connection.portals.get(portal, []))
            case "close":
                client.send(Close(*target(args)))
            case "sync":
                messages = client.sync()
            case "terminate":
                args.reject_next()
                client.terminate()
                del self.connections[prefix]
                return ""
            case _:
                raise ValueError(f"unknown pg command {subcommand}")
        args.reject_next()

        output = self.format(connection, messages)
        if subcommand == "sync":
            connection.portals.clear()
            connection.executes.clear()
        if client.closed:
            del self.connections[prefix]
        return output

    def startup(self, prefix: str, args: goldenscript.ArgumentConsumer) -> str:
        """Runs pg startup: opens a connection and starts it."""
        if prefix in self.connections:
            raise ValueError("PostgreSQL connection already open")
        user = args.take_key("user")
        password = args.take_key("password")
        parameters = {}
        if (application_name := args.take_key("application_name")) is not None:
            parameters["application_name"] = application_name
        major, minor = (args.take_key("protocol") or "3.0").split(".")
        ssl = args.next_pos()
        if ssl not in (None, "ssl"):
            raise ValueError(f"unexpected argument {ssl}")

        client = self.connect()
        connection = Connection(client)
        output = ""
        if ssl is not None:
            output += f"SSLResponse {client.request_ssl().decode()}\n"
        version = int(major) << 16 | int(minor)
        messages = client.startup("admin" if user is None else user, password, parameters, version)
        output += self.format(connection, messages)
        if not client.closed:
            self.connections[prefix] = connection
        return output

    def format(self, connection: Connection, messages: list[Backend]) -> str:
        """Formats received messages, one per line, and Closed if the
        connection is closed."""
        lines: list[str] = []
        for message in messages:
            match message:
                case ParameterStatus(name, value):
                    if lines and lines[-1].startswith("ParameterStatus "):
                        lines[-1] += f" {name}={value}"
                    else:
                        lines.append(f"ParameterStatus {name}={value}")
                    continue
                case DataRow(values):
                    formats = connection.executes[0] if connection.executes else []
                    line = ", ".join(
                        format_value(value, format_code(formats, i))
                        for i, value in enumerate(values)
                    )
                    lines.append(f"DataRow {line}")
                    continue
                case CommandComplete() | PortalSuspended() | EmptyQueryResponse():
                    if connection.executes:
                        connection.executes.pop(0)
                case ErrorResponse():
                    connection.executes.clear()
            lines.append(format_message(message))
        if connection.client.closed:
            lines.append("Closed")
        return "".join(f"{line}\n" for line in lines)


def format_message(message: Backend) -> str:
    """Formats a backend message other than ParameterStatus and DataRow."""
    match message:
        case AuthenticationOk():
            return "AuthenticationOk"
        case AuthenticationMD5Password():
            return "AuthenticationMD5Password"
        case BackendKeyData():
            return "BackendKeyData"
        case ReadyForQuery("I"):
            return "ReadyForQuery idle"
        case ReadyForQuery("T"):
            return "ReadyForQuery transaction"
        case ReadyForQuery(status):
            return f"ReadyForQuery {status}"
        case RowDescription(fields):
            columns = []
            for column in fields:
                text = f"{column.name} {type_name(column.type_oid)}"
                text += f"({column.type_oid},{column.type_size})"
                columns.append(text + (" binary" if column.format == 1 else ""))
            return " ".join(["RowDescription", ", ".join(columns)]).rstrip()
        case CommandComplete(tag):
            return f"CommandComplete {tag}"
        case ErrorResponse(severity, code, text):
            return f"ErrorResponse {severity} {code} {text}"
        case ParameterDescription(oids):
            parameters = ", ".join(f"{type_name(oid)}({oid})" for oid in oids)
            return " ".join(["ParameterDescription", parameters]).rstrip()
        case CopyInResponse(columns):
            return f"CopyInResponse {columns} columns"
        case CopyOutResponse(columns):
            return f"CopyOutResponse {columns} columns"
        case CopyData(data):
            return f"CopyData {Value.String(data.decode())}"
        case (
            ParseComplete()
            | BindComplete()
            | CloseComplete()
            | NoData()
            | PortalSuspended()
            | EmptyQueryResponse()
            | CopyDone()
        ):
            return type(message).__name__
        case _:
            raise ValueError(f"unexpected message {message!r}")


def format_value(value: bytes | None, format: int) -> str:
    """Formats a DataRow value of the given format."""
    if value is None:
        return "NULL"
    if format == 1:
        return f"\\x{value.hex()}"
    return str(Value.String(value.decode()))


def type_name(oid: int) -> str:
    """Returns the name of a type OID."""
    pgtype = types.TYPES.get(oid)
    return pgtype.name if pgtype is not None else "unknown"


def required(value: str | None, name: str) -> str:
    """Returns a required positional argument."""
    if value is None:
        raise ValueError(f"argument {name} not given")
    return value


def split_list(value: str | None) -> list[str]:
    """Splits a comma-separated argument, none if not given."""
    return value.split(",") if value is not None else []


def parameter(value: str) -> bytes | None:
    """Converts a params item to a parameter value."""
    if value == "NULL":
        return None
    if value.startswith("0x"):
        return bytes.fromhex(value[2:])
    return value.encode()


def target(args: goldenscript.ArgumentConsumer) -> tuple[str, str]:
    """Returns the kind and name of a statement=<s> or portal=<p> argument."""
    if (statement := args.take_key("statement")) is not None:
        return "S", statement
    if (portal := args.take_key("portal")) is not None:
        return "P", portal
    raise ValueError("statement or portal not given")
