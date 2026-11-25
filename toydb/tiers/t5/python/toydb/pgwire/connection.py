"""A PostgreSQL connection: startup, simple and extended query.

* Startup accepts only protocol 3.0, refuses SSL and GSS encryption, asks a
  user with a password for it with the MD5 method, and then sends
  ParameterStatus, BackendKeyData and ReadyForQuery.
* A Query message is split into statements at the SQL lexer's `;` tokens;
  each is executed as if sent alone, and the first error ends the query.
* Results: rows as RowDescription, DataRows and `SELECT <n>`; the lines of
  EXPLAIN (column `QUERY PLAN`), FORMAT and EXPORT as rows of one text column;
  `INSERT 0 <n>`, `UPDATE <n>`, `DELETE <n>`; every other result, also one
  added later, as its statement's name in upper case with spaces
  (`CREATE TABLE`, `SET ROLE`).
* A result column is named by its header without table qualifier. An unnamed
  column is named as PostgreSQL names it, from the select-list item: a
  function or aggregate call by the function's name, an expression atom that
  begins with a keyword and is not a literal by that keyword in lower case,
  every other expression `?column?`. Constructs added later are named by what
  they are. In a simple query, a column's type is that of its first non-NULL
  value (text if none).
* Extended query: Parse (one statement, typed parameters `$n`), Bind
  (parameter values, result formats), Describe (as DESCRIBE describes the
  statement, its parameters replaced by NULL for a statement, bound for a
  portal), Execute (with a row limit), Close, Sync. After an error, messages
  up to Sync are skipped; Sync closes the portals.
* COPY (a command of the connection, recognized by its first word):
  `COPY <table> [(<columns>)] TO STDOUT` sends the rows as EXPORT reads them,
  and `... FROM STDIN` inserts the rows the client sends as IMPORT inserts
  them, in PostgreSQL's text format (tab-separated, `\\N` for NULL, backslash
  escapes). A field is read by its column's data type as a text parameter of
  that type is read; a value of a data type added later as IMPORT reads it.
* Errors carry a SQLSTATE code by kind: invalid input 42000, permission denied
  42501, read-only transaction 25006, serialization failure 40001, operation
  aborted 40003, invalid data XX000, io error 58030.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from toydb.error import Error, errinput, panic
from toydb.pgwire import types
from toydb.pgwire.message import (
    PROTOCOL_VERSION,
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
    encode,
    read_message,
    read_startup,
)
from toydb.sql.engine import Engine
from toydb.sql.execution import Session, StatementResult
from toydb.sql.execution.export import read_rows
from toydb.sql.execution.importer import SourceRow, column_index, insert_rows, invalid_value
from toydb.sql.execution.prepared import substitute
from toydb.sql.parser import Lexer, Parser, Token, ast
from toydb.sql.parser.lexer import Keyword
from toydb.sql.types import DataType, Label, Row, Table, Value
from toydb.sql.types.formats import from_text

_COPY_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})
"""The escapes of COPY data fields."""

_COPY_UNESCAPES: dict[str, str] = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}
"""The characters of COPY data escapes, by the character after the backslash."""


@dataclass
class PreparedStatement:
    """A statement prepared by Parse."""

    statement: ast.Statement | None
    """The statement, None if the query is empty."""
    types: list[int]
    """The parameter type OIDs."""


@dataclass
class Portal:
    """A statement bound by Bind, and its result once executed."""

    statement: ast.Statement | None
    """The statement with its parameters bound, None if the query is empty."""
    formats: list[int]
    """The result format codes."""
    result: StatementResult | None = None
    """The result, once executed."""
    rows: "ResultRows | None" = None
    """The result rows, if the result has rows."""
    sent: int = 0
    """The number of rows sent."""
    completed: bool = False
    """Whether CommandComplete was sent."""


@dataclass
class ResultRows:
    """The rows of a statement result, with the names of its columns."""

    names: list[str]
    rows: list[Row]


@dataclass(frozen=True)
class CopyCommand:
    """A COPY command."""

    table: str
    columns: list[str] | None
    """The columns, None for all."""
    to_stdout: bool
    """True for TO STDOUT, False for FROM STDIN."""


@dataclass
class Connection:
    """A PostgreSQL connection and its session, prepared statements and
    portals."""

    engine: Engine[Any]
    reader: BinaryIO
    writer: BinaryIO
    session: Session[Any] = field(init=False)
    """The session, created by a successful startup."""
    statements: dict[str, PreparedStatement] = field(default_factory=dict)
    """The prepared statements, by name ('' for the unnamed one)."""
    portals: dict[str, Portal] = field(default_factory=dict)
    """The portals, by name ('' for the unnamed one)."""
    skipping: bool = False
    """Whether messages are skipped until Sync, after an error."""

    def serve(self) -> None:
        """Serves the connection until it is closed or terminated."""
        try:
            if not self.startup():
                return
            try:
                while (message := read_message(self.reader)) is not None:
                    if not self.handle(message):
                        return
            finally:
                self.session.close()
        except PgError as error:
            self.send(error.response())
            self.flush()

    def send(self, message: Backend) -> None:
        """Sends a message (buffered until flushed)."""
        self.writer.write(encode(message))

    def flush(self) -> None:
        """Sends the buffered messages."""
        self.writer.flush()

    def send_ready(self) -> None:
        """Sends ReadyForQuery with the session's transaction status."""
        self.send(ReadyForQuery("I" if self.session.txn is None else "T"))

    def send_error(self, error: Error | PgError) -> None:
        """Sends an error as ErrorResponse."""
        match error:
            case PgError():
                self.send(error.response())
            case Error():
                self.send(ErrorResponse("ERROR", error_code(error), str(error)))

    def startup(self) -> bool:
        """Runs the startup phase. Returns false if the connection was closed."""
        while True:
            match read_startup(self.reader):
                case None | CancelRequest():
                    return False
                case SSLRequest() | GSSENCRequest():
                    self.writer.write(b"N")
                    self.flush()
                case StartupMessage(version, parameters):
                    break
                case message:
                    panic(f"unexpected startup message {message!r}")

        if version != PROTOCOL_VERSION:
            major, minor = version >> 16, version & 0xFFFF
            raise PgError("0A000", f"unsupported frontend protocol {major}.{minor}", fatal=True)
        if not (name := parameters.get("user", "")):
            raise PgError("28000", "no user name given", fatal=True)
        txn = self.engine.begin_read_only()
        try:
            user = txn.get_user(name)
        finally:
            txn.commit()
        if user is None:
            raise PgError("28000", f"user {name} does not exist", fatal=True)
        if user.password is not None:
            salt = secrets.token_bytes(4)
            self.send(AuthenticationMD5Password(salt))
            self.flush()
            match read_message(self.reader):
                case None:
                    return False
                case PasswordMessage(password) if password == md5_response(user.password, salt):
                    pass
                case _:
                    raise PgError(
                        "28P01", f"password authentication failed for user {name}", fatal=True
                    )

        self.session = self.engine.session()
        self.session.set_login(name)
        self.send(AuthenticationOk())
        for parameter, value in (
            ("application_name", parameters.get("application_name", "")),
            ("client_encoding", "UTF8"),
            ("DateStyle", "ISO"),
            ("integer_datetimes", "on"),
            ("is_superuser", "on" if user.superuser else "off"),
            ("server_encoding", "UTF8"),
            ("server_version", "14.0"),
            ("session_authorization", name),
            ("standard_conforming_strings", "on"),
        ):
            self.send(ParameterStatus(parameter, value))
        self.send(BackendKeyData(self.session.number, secrets.randbits(31)))
        self.send_ready()
        self.flush()
        return True

    def handle(self, message: Frontend) -> bool:
        """Handles a message. Returns false if the connection is to be
        closed."""
        if self.skipping and not isinstance(message, (Sync, Terminate)):
            return True
        match message:
            case Query(sql):
                self.query(sql)
                self.send_ready()
                self.flush()
            case Parse() | Bind() | Describe() | Execute() | Close():
                try:
                    self.extended(message)
                except (Error, PgError) as error:
                    if isinstance(error, PgError) and error.fatal:
                        raise
                    self.send_error(error)
                    self.skipping = True
            case Sync():
                self.skipping = False
                self.portals.clear()
                self.send_ready()
                self.flush()
            case Flush():
                self.flush()
            case Terminate():
                return False
            case CopyData() | CopyDone() | CopyFail() | PasswordMessage():
                pass  # outside of COPY FROM STDIN and the startup
            case _:
                panic(f"unexpected message {message!r}")
        return True

    def query(self, sql: str) -> None:
        """Executes the statements of a Query message."""
        try:
            statements = split_statements(sql)
        except Error as error:
            self.send_error(error)
            return
        if not statements:
            self.send(EmptyQueryResponse())
            return
        for text, tokens in statements:
            try:
                if tokens[0] == Token.Ident("copy"):
                    self.copy(tokens)
                    continue
                statement = Parser.parse(text)
                result = self.session.execute_parsed(statement)
                rows = self.result_rows(statement, result)
                if rows is not None:
                    self.send(RowDescription(describe_rows(rows)))
                    for row in rows.rows:
                        self.send(DataRow([types.encode(value, 0) for value in row]))
                self.send(CommandComplete(command_tag(result)))
            except (Error, PgError) as error:
                if isinstance(error, PgError) and error.fatal:
                    raise
                self.send_error(error)
                return

    def result_rows(self, statement: ast.Statement, result: StatementResult) -> ResultRows | None:
        """Returns the rows of a statement result and the names of its
        columns, or None if the result has no rows."""
        match result:
            case StatementResult.Select(columns, rows):
                items = select_items(self.query_statement(statement), len(columns))
                names = [column_name(label, item) for label, item in zip(columns, items)]
                return ResultRows(names, rows)
            case StatementResult.Explain(plan):
                return ResultRows(["QUERY PLAN"], lines_rows(str(plan).split("\n")))
            case StatementResult.Format(lines):
                return ResultRows(["format"], lines_rows(lines))
            case StatementResult.Export(lines=lines):
                return ResultRows(["export"], lines_rows(lines))
            case _:
                return None

    def query_statement(self, statement: ast.Statement) -> ast.Statement:
        """Returns the statement whose select list names a statement's result
        columns."""
        match statement:
            case ast.Statement.Execute(name):
                return self.session.prepared.get(name).statement
            case _:
                return statement

    def extended(self, message: Frontend) -> None:
        """Handles a message of the extended query protocol."""
        match message:
            case Parse(name, query, oids):
                self.parse(name, query, oids)
            case Bind(portal, statement, formats, values, results):
                self.bind(portal, statement, formats, values, results)
            case Describe("S", name):
                prepared = self.prepared(name)
                self.send(ParameterDescription(prepared.types))
                nulls = [Value.Null()] * len(prepared.types)
                bound = substitute(prepared.statement, nulls) if prepared.statement else None
                self.describe(bound, [])
            case Describe("P", name):
                portal = self.portal(name)
                self.describe(portal.statement, portal.formats)
            case Execute(name, max_rows):
                self.execute(self.portal(name), max_rows)
            case Close("S", name):
                self.statements.pop(name, None)
                self.send(CloseComplete())
            case Close("P", name):
                self.portals.pop(name, None)
                self.send(CloseComplete())
            case _:
                panic(f"unexpected message {message!r}")

    def prepared(self, name: str) -> PreparedStatement:
        """Fetches a prepared statement, or errors if it doesn't exist."""
        if (prepared := self.statements.get(name)) is None:
            if not name:
                raise PgError("26000", "unnamed prepared statement does not exist")
            raise PgError("26000", f"prepared statement {name} does not exist")
        return prepared

    def portal(self, name: str) -> Portal:
        """Fetches a portal, or errors if it doesn't exist."""
        if (portal := self.portals.get(name)) is None:
            if not name:
                raise PgError("34000", "unnamed portal does not exist")
            raise PgError("34000", f"portal {name} does not exist")
        return portal

    def parse(self, name: str, query: str, oids: list[int]) -> None:
        """Handles Parse: prepares a statement."""
        if name and name in self.statements:
            raise PgError("42P05", f"prepared statement {name} already exists")
        statements = split_statements(query)
        if len(statements) > 1:
            raise PgError("42601", "cannot insert multiple commands into a prepared statement")
        statement = Parser.parse(statements[0][0]) if statements else None
        numbers = statement.parameters() if statement is not None else []
        parameter_types: list[int] = []
        for number in range(1, max(numbers[-1] if numbers else 0, len(oids)) + 1):
            oid = oids[number - 1] if number <= len(oids) else 0
            if oid == 0:
                raise PgError("42P18", f"could not determine data type of parameter ${number}")
            types.parameter_type(oid)
            parameter_types.append(oid)
        if statement is not None and parameter_types:
            if (keyword := Session.unplanned_keyword(statement)) is not None:
                raise errinput(f"PREPARE is not supported for {keyword}")
        self.statements[name] = PreparedStatement(statement, parameter_types)
        self.send(ParseComplete())

    def bind(
        self,
        name: str,
        statement: str,
        formats: list[int],
        values: list[bytes | None],
        results: list[int],
    ) -> None:
        """Handles Bind: creates a portal."""
        prepared = self.prepared(statement)
        if len(formats) > 1 and len(formats) != len(values):
            raise PgError(
                "08P01",
                f"bind message has {len(formats)} parameter formats but {len(values)} "
                "parameters",
            )
        if len(values) != len(prepared.types):
            raise PgError(
                "08P01",
                f"bind message supplies {len(values)} parameters, but prepared statement "
                f"requires {len(prepared.types)}",
            )
        for code in formats:
            types.check_format(code)
        arguments = [
            types.decode_parameter(oid, format_code(formats, i), value, i + 1)
            for i, (oid, value) in enumerate(zip(prepared.types, values))
        ]
        for code in results:
            types.check_format(code)
        bound = substitute(prepared.statement, arguments) if prepared.statement else None
        self.portals[name] = Portal(bound, results)
        self.send(BindComplete())

    def describe(self, statement: ast.Statement | None, formats: list[int]) -> None:
        """Sends the RowDescription of a statement's result columns, or NoData
        if it has none."""
        if statement is None:
            self.send(NoData())
            return
        columns = self.session.describe_columns(statement)
        if not columns:
            self.send(NoData())
            return
        codes = result_formats(formats, len(columns))
        items = select_items(self.query_statement(statement), len(columns))
        fields: list[Field] = []
        for (label, description), item, code in zip(columns, items, codes):
            pgtype = types.pg_type(description.datatype)
            fields.append(Field(column_name(label, item), pgtype.oid, pgtype.size, code))
        self.send(RowDescription(fields))

    def execute(self, portal: Portal, max_rows: int) -> None:
        """Handles Execute: executes a portal's statement and sends its
        rows."""
        if portal.statement is None:
            self.send(EmptyQueryResponse())
            return
        if portal.completed:
            assert portal.result is not None
            self.send(CommandComplete(command_tag(portal.result, count=0)))
            return
        if portal.result is None:
            portal.result = self.session.execute_parsed(portal.statement)
            portal.rows = self.result_rows(portal.statement, portal.result)
        if portal.rows is not None:
            codes = result_formats(portal.formats, len(portal.rows.names))
            rows = portal.rows.rows
            end = len(rows) if max_rows <= 0 else min(len(rows), portal.sent + max_rows)
            for row in rows[portal.sent : end]:
                self.send(DataRow([types.encode(value, code) for value, code in zip(row, codes)]))
            portal.sent = end
            if end < len(rows):
                self.send(PortalSuspended())
                return
        portal.completed = True
        self.send(CommandComplete(command_tag(portal.result)))

    def copy(self, tokens: list[Token]) -> None:
        """Executes a COPY command, and records it in the audit log."""
        command = parse_copy(tokens)
        user = self.session.user
        statement: ast.Statement
        if command.to_stdout:
            statement = ast.Statement.Export(command.table, ast.ExportFormat.Csv)
        else:
            statement = ast.Statement.Import(command.table, ast.ImportFormat.Csv, [])
        try:
            if command.to_stdout:
                count = self.copy_out(command, statement)
            else:
                count = self.copy_in(command, statement)
        except (Error, PgError) as error:
            self.session.record_copy(user, statement, str(error))
            raise
        self.session.record_copy(user, statement, None)
        self.send(CommandComplete(f"COPY {count}"))

    def copy_out(self, command: CopyCommand, statement: ast.Statement) -> int:
        """Executes COPY TO STDOUT, returning the number of rows."""

        def read(txn: Any) -> tuple[int, list[Row]]:
            table = txn.must_get_table(command.table)
            indexes = copy_columns(table, command.columns)
            rows = read_rows(txn, table, self.session.user)
            return len(indexes), [[row[i] for i in indexes] for row in rows]

        columns, rows = self.session.with_checked_txn(True, statement, read)
        self.send(CopyOutResponse(columns))
        for row in rows:
            self.send(CopyData(format_copy_row(row).encode()))
        self.send(CopyDone())
        return len(rows)

    def copy_in(self, command: CopyCommand, statement: ast.Statement) -> int:
        """Executes COPY FROM STDIN, returning the number of inserted rows."""
        columns = self.session.with_checked_txn(
            True,
            statement,
            lambda txn: len(copy_columns(txn.must_get_table(command.table), command.columns)),
        )
        self.send(CopyInResponse(columns))
        self.flush()
        data = self.receive_copy_data()

        def insert(txn: Any) -> int:
            table = txn.must_get_table(command.table)
            source = read_copy_data(table, copy_columns(table, command.columns), data)
            return insert_rows(txn, table, source, self.session.user)

        return self.session.with_checked_txn(False, statement, insert)

    def receive_copy_data(self) -> str:
        """Receives the data of COPY FROM STDIN, until CopyDone."""
        chunks: list[bytes] = []
        while True:
            match read_message(self.reader):
                case None:
                    raise PgError("08P01", "connection closed during COPY")
                case CopyData(data):
                    chunks.append(data)
                case CopyDone():
                    break
                case CopyFail(message):
                    raise PgError("57014", f"COPY from stdin failed: {message}")
                case Flush() | Sync():
                    pass
                case message:
                    raise PgError(
                        "08P01",
                        f"unexpected message type {message.TYPE.decode()} during COPY from stdin",
                    )
        try:
            return b"".join(chunks).decode()
        except UnicodeDecodeError:
            raise types.invalid_encoding() from None


def md5_response(hash: str, salt: bytes) -> str:
    """Returns the response to an MD5 password request for a stored password
    hash."""
    return "md5" + hashlib.md5(hash.removeprefix("md5").encode() + salt).hexdigest()


def error_code(error: Error) -> str:
    """Returns the SQLSTATE code of an error."""
    match error:
        case Error.InvalidInput():
            return "42000"
        case Error.PermissionDenied():
            return "42501"
        case Error.ReadOnly():
            return "25006"
        case Error.Serialization():
            return "40001"
        case Error.Abort():
            return "40003"
        case Error.InvalidData():
            return "XX000"
        case Error.IO():
            return "58030"
        case _:
            panic(f"unknown error {error!r}")


def split_statements(sql: str) -> list[tuple[str, list[Token]]]:
    """Splits SQL text into its statements, returning the text and the
    tokens of each."""
    lexer = Lexer(sql)
    statements: list[tuple[str, list[Token]]] = []
    tokens: list[Token] = []
    start = 0
    for token in lexer:
        if isinstance(token, Token.Semicolon):
            if tokens:
                statements.append((sql[start : lexer.pos - 1], tokens))
            tokens, start = [], lexer.pos
        else:
            tokens.append(token)
    if tokens:
        statements.append((sql[start:], tokens))
    return statements


def command_tag(result: StatementResult, count: int | None = None) -> str:
    """Returns the command tag of a statement result."""
    match result:
        case StatementResult.Select(rows=rows):
            return f"SELECT {len(rows) if count is None else count}"
        case StatementResult.Insert(inserted):
            return f"INSERT 0 {inserted if count is None else count}"
        case StatementResult.Update(updated):
            return f"UPDATE {updated if count is None else count}"
        case StatementResult.Delete(deleted):
            return f"DELETE {deleted if count is None else count}"
        case StatementResult.Begin():
            return "BEGIN"
        case StatementResult.Commit():
            return "COMMIT"
        case StatementResult.Rollback():
            return "ROLLBACK"
        case StatementResult.Explain():
            return "EXPLAIN"
        case StatementResult.CreateTable():
            return "CREATE TABLE"
        case StatementResult.DropTable():
            return "DROP TABLE"
        case StatementResult.Analyze():
            return "ANALYZE"
        case StatementResult.Export():
            return "EXPORT"
        case StatementResult.Format():
            return "FORMAT"
        case StatementResult.CreateView():
            return "CREATE VIEW"
        case StatementResult.DropView():
            return "DROP VIEW"
        case StatementResult.Prepare():
            return "PREPARE"
        case StatementResult.Deallocate():
            return "DEALLOCATE"
        case StatementResult.CreateUser():
            return "CREATE USER"
        case StatementResult.DropUser():
            return "DROP USER"
        case StatementResult.Grant():
            return "GRANT"
        case StatementResult.Revoke():
            return "REVOKE"
        case StatementResult.SetRole():
            return "SET ROLE"
        case StatementResult.ResetRole():
            return "RESET ROLE"
        case StatementResult.CreatePolicy():
            return "CREATE POLICY"
        case StatementResult.DropPolicy():
            return "DROP POLICY"
        case StatementResult.CreateMaterializedView():
            return "CREATE MATERIALIZED VIEW"
        case StatementResult.RefreshMaterializedView():
            return "REFRESH MATERIALIZED VIEW"
        case StatementResult.DropMaterializedView():
            return "DROP MATERIALIZED VIEW"
        case _:
            panic(f"unknown statement result {result!r}")


def column_name(label: Label, item: ast.Expression | None) -> str:
    """Returns the name of a result column: its header, or the name
    PostgreSQL gives an unnamed expression."""
    match label:
        case Label.Qualified(_, name) | Label.Unqualified(name):
            return name
    match item:
        case ast.Expression.Column(_, name):
            return name
        case ast.Expression.Function(name):
            return name.lower()
        case (
            ast.Expression.Literal()
            | ast.Expression.Operator()
            | ast.Expression.Parameter()
            | ast.Expression.All()
            | None
        ):
            return "?column?"
        case _:
            panic(f"unknown expression {item!r}")


def select_items(statement: ast.Statement, columns: int) -> list[ast.Expression | None]:
    """Returns the select-list item of each of the given number of result
    columns of a statement, None where there is none."""
    match statement:
        case ast.Statement.Select(select=select):
            stars = sum(1 for expr, _ in select if isinstance(expr, ast.Expression.All))
            width = (columns - len(select) + stars) // stars if stars else 0
            items: list[ast.Expression | None] = []
            for expr, _ in select:
                if isinstance(expr, ast.Expression.All):
                    items.extend([None] * width)
                else:
                    items.append(expr)
            return items
        case _:
            return [None] * columns


def describe_rows(rows: ResultRows) -> list[Field]:
    """Describes the columns of result rows in a simple query, with the type
    of each column's first non-NULL value."""
    fields: list[Field] = []
    for index, name in enumerate(rows.names):
        datatype = next(
            (datatype for row in rows.rows if (datatype := row[index].datatype()) is not None),
            None,
        )
        pgtype = types.pg_type(datatype)
        fields.append(Field(name, pgtype.oid, pgtype.size, 0))
    return fields


def lines_rows(lines: list[str]) -> list[Row]:
    """Returns lines as rows of one text column."""
    return [[Value.String(line)] for line in lines]


def format_code(formats: list[int], index: int) -> int:
    """Returns the format code of a parameter or column from a message's
    format codes."""
    if len(formats) > 1:
        return formats[index]
    return formats[0] if formats else 0


def result_formats(formats: list[int], columns: int) -> list[int]:
    """Returns the format codes of the given number of result columns."""
    if len(formats) > 1 and len(formats) != columns:
        raise PgError(
            "08P01",
            f"bind message has {len(formats)} result formats but query has {columns} columns",
        )
    return [format_code(formats, index) for index in range(columns)]


def parse_copy(tokens: list[Token]) -> CopyCommand:
    """Parses a COPY command from its tokens."""
    error = PgError("42601", "syntax error in COPY command")
    match tokens:
        case [_, Token.Ident(table), *rest]:
            pass
        case _:
            raise error
    columns: list[str] | None = None
    if rest[:1] == [Token.OpenParen()]:
        columns, rest = [], rest[1:]
        while True:
            match rest:
                case [Token.Ident(column), Token.Comma(), *rest]:
                    columns.append(column)
                case [Token.Ident(column), Token.CloseParen(), *rest]:
                    columns.append(column)
                    break
                case _:
                    raise error
    match rest:
        case [Token.Keyword(Keyword.To), Token.Ident("stdout")]:
            return CopyCommand(table, columns, to_stdout=True)
        case [Token.Keyword(Keyword.From), Token.Ident("stdin")]:
            return CopyCommand(table, columns, to_stdout=False)
        case _:
            raise error


def copy_columns(table: Table, columns: list[str] | None) -> list[int]:
    """Returns the indexes of the columns of a COPY command in a table."""
    if columns is None:
        return list(range(len(table.columns)))
    indexes: list[int] = []
    for name in columns:
        index = column_index(table, name)
        if index in indexes:
            raise errinput(f"column {name} given multiple times")
        indexes.append(index)
    return indexes


def format_copy_row(row: Row) -> str:
    """Formats a row as a line of COPY data."""
    fields = [
        "\\N"
        if isinstance(value, Value.Null)
        else types.encode_text(value).translate(_COPY_ESCAPES)
        for value in row
    ]
    return "\t".join(fields) + "\n"


def read_copy_data(table: Table, indexes: list[int], data: str) -> list[SourceRow]:
    """Reads the rows of COPY FROM STDIN data for the given columns of a
    table."""
    lines = data.split("\n")
    if lines[-1] == "":
        lines.pop()
    column_map = {index: i for i, index in enumerate(indexes)}
    rows: list[SourceRow] = []
    for number, line in enumerate(lines, start=1):
        fields = line.split("\t")
        if len(fields) != len(indexes):
            raise errinput(
                f"wrong number of fields on line {number}: "
                f"expected {len(indexes)}, found {len(fields)}"
            )
        row: Row = []
        for index, text in zip(indexes, fields):
            column = table.columns[index]
            if text == "\\N":
                row.append(Value.Null())
                continue
            text = re.sub(r"\\(.)", lambda m: _COPY_UNESCAPES.get(m[1], m[1]), text, flags=re.S)
            try:
                row.append(read_field(text, column.datatype))
            except (PgError, ValueError):
                raise invalid_value(column, number) from None
        rows.append((column_map, row))
    return rows


def read_field(text: str, datatype: DataType) -> Value:
    """Reads a field of COPY FROM STDIN as a value of its column's data
    type."""
    match datatype:
        case DataType.Boolean:
            return types.decode_text(types.BOOL, text)
        case DataType.Integer:
            return types.decode_text(types.INT8, text)
        case DataType.Float:
            return types.decode_text(types.FLOAT8, text)
        case DataType.String:
            return types.decode_text(types.TEXT, text)
        case _:
            return from_text(text, datatype)
