"""Tests for toydb.pgwire.connection and toydb.pgwire.client.

The connection is mostly covered by the SQL golden scripts
`tests/sql/testscripts/ext5/pg_*` and the cluster script
`tests/e2e/scripts/ext5/pg_cluster`; these tests cover its functions directly,
and a connection to a local engine through the client.
"""

import hashlib
import math
import socket
import threading
import unittest

from toydb.error import Error
from toydb.pgwire.client import Client
from toydb.pgwire.connection import (
    Connection,
    column_name,
    command_tag,
    error_code,
    format_copy_row,
    md5_response,
    parse_copy,
    read_copy_data,
    read_field,
    result_formats,
    select_items,
    split_statements,
)
from toydb.pgwire.message import (
    PROTOCOL_VERSION,
    AuthenticationMD5Password,
    AuthenticationOk,
    Bind,
    CommandComplete,
    CopyData,
    CopyDone,
    CopyInResponse,
    DataRow,
    ErrorResponse,
    Execute,
    Flush,
    Parse,
    PgError,
    Query,
    ReadyForQuery,
    RowDescription,
    StartupMessage,
)
from toydb.sql.engine import Local
from toydb.sql.execution import StatementResult
from toydb.sql.execution.users import hash_password
from toydb.sql.parser import Lexer, Parser, ast
from toydb.sql.types import Column, DataType, Label, Table, Value
from toydb.storage.memory import Memory


class TestFunctions(unittest.TestCase):
    def test_split_statements(self) -> None:
        """Statements are split at ; tokens, not at ; in strings or quoted
        identifiers, and empty statements are skipped."""
        cases: list[tuple[str, list[str]]] = [
            ("", []),
            (" ; ;", []),
            ("SELECT 1", ["SELECT 1"]),
            ("SELECT 1;", ["SELECT 1"]),
            ("SELECT 1; ; SELECT 'a;b';", ["SELECT 1", " SELECT 'a;b'"]),
            ('SELECT "x;y" FROM t;SELECT 2', ['SELECT "x;y" FROM t', "SELECT 2"]),
        ]
        for sql, expect in cases:
            with self.subTest(sql=sql):
                self.assertEqual([text for text, _ in split_statements(sql)], expect)
        with self.assertRaises(Error.InvalidInput):
            split_statements("SELECT 1; SELECT 'x")

    def test_command_tag(self) -> None:
        """Results have their statement names as tags, rows and writes with
        their counts."""
        cases: list[tuple[StatementResult, str, str]] = [
            (StatementResult.Select([Label.None_()], [[]] * 3), "SELECT 3", "SELECT 0"),
            (StatementResult.Insert(2), "INSERT 0 2", "INSERT 0 0"),
            (StatementResult.Update(1), "UPDATE 1", "UPDATE 0"),
            (StatementResult.Delete(4), "DELETE 4", "DELETE 0"),
            (StatementResult.Commit(3), "COMMIT", "COMMIT"),
            (StatementResult.CreateTable("t"), "CREATE TABLE", "CREATE TABLE"),
            (StatementResult.DropView("v", True), "DROP VIEW", "DROP VIEW"),
            (StatementResult.Format(["SELECT 1"]), "FORMAT", "FORMAT"),
            (StatementResult.Grant(None), "GRANT", "GRANT"),
            (StatementResult.SetRole("u"), "SET ROLE", "SET ROLE"),
            (StatementResult.DropPolicy("t", "p", False), "DROP POLICY", "DROP POLICY"),
            (
                StatementResult.CreateMaterializedView("m", 2),
                "CREATE MATERIALIZED VIEW",
                "CREATE MATERIALIZED VIEW",
            ),
            (
                StatementResult.RefreshMaterializedView("m", 2),
                "REFRESH MATERIALIZED VIEW",
                "REFRESH MATERIALIZED VIEW",
            ),
            (
                StatementResult.DropMaterializedView("m", True),
                "DROP MATERIALIZED VIEW",
                "DROP MATERIALIZED VIEW",
            ),
        ]
        for result, tag, completed in cases:
            with self.subTest(result=result):
                self.assertEqual(command_tag(result), tag)
                self.assertEqual(command_tag(result, count=0), completed)

    def test_column_names(self) -> None:
        """Columns are named by their header without qualifier, or by their
        select-list item as PostgreSQL names it."""
        sql = "SELECT id AS n, t.v, count(*), SQRT(f), -i, 1, NULL, TRUE, i + 1, i IS NULL FROM t"
        select = Parser.parse(sql)
        labels = [Label.Unqualified("n"), Label.Qualified("t", "v")] + [Label.None_()] * 8
        items = select_items(select, len(labels))
        names = [column_name(label, item) for label, item in zip(labels, items)]
        expect = ["n", "v", "count", "sqrt"] + ["?column?"] * 6
        self.assertEqual(names, expect)
        self.assertEqual(column_name(Label.None_(), ast.Expression.Column(None, "c")), "c")
        self.assertEqual(column_name(Label.None_(), None), "?column?")

    def test_select_items(self) -> None:
        """* expands to the columns it stands for, other statements have no
        items."""
        items = select_items(Parser.parse("SELECT *, 1, * FROM t"), 7)
        self.assertEqual(items[:3], [None, None, None])
        self.assertEqual(items[3], ast.Expression.Literal(ast.Literal.Integer(1)))
        self.assertEqual(items[4:], [None, None, None])
        self.assertEqual(select_items(Parser.parse("SHOW VIEWS"), 3), [None, None, None])

    def test_result_formats(self) -> None:
        """No format code means text, one applies to all columns, otherwise
        one per column."""
        self.assertEqual(result_formats([], 2), [0, 0])
        self.assertEqual(result_formats([1], 3), [1, 1, 1])
        self.assertEqual(result_formats([0, 1], 2), [0, 1])
        self.assertEqual(result_formats([1], 0), [])
        with self.assertRaises(PgError) as raised:
            result_formats([0, 1], 3)
        self.assertEqual(
            raised.exception.message, "bind message has 2 result formats but query has 3 columns"
        )

    def test_error_codes(self) -> None:
        """Errors have SQLSTATE codes by kind."""
        cases: list[tuple[Error, str]] = [
            (Error.InvalidInput("x"), "42000"),
            (Error.PermissionDenied("x"), "42501"),
            (Error.ReadOnly(), "25006"),
            (Error.Serialization(), "40001"),
            (Error.Abort(), "40003"),
            (Error.InvalidData("x"), "XX000"),
            (Error.IO("x"), "58030"),
        ]
        for error, code in cases:
            with self.subTest(error=error):
                self.assertEqual(error_code(error), code)

    def test_md5_response(self) -> None:
        """The MD5 response is md5 and the MD5 of the stored hash (without md5)
        followed by the salt, as PostgreSQL computes it."""
        stored = hash_password("alice", "secret")
        salt = b"\x01\x02\x03\x04"
        expect = "md5" + hashlib.md5(stored[3:].encode() + salt).hexdigest()
        self.assertEqual(md5_response(stored, salt), expect)
        self.assertNotEqual(md5_response(stored, b"\x01\x02\x03\x05"), expect)

    def test_parse_copy(self) -> None:
        """COPY commands, in any letter case; anything else is a syntax
        error."""
        cases: list[tuple[str, str, list[str] | None, bool]] = [
            ("COPY t TO STDOUT", "t", None, True),
            ('copy "T" (a, "B") to stdout', "T", ["a", "B"], True),
            ("Copy t (a) From Stdin", "t", ["a"], False),
        ]
        for sql, table, columns, to_stdout in cases:
            with self.subTest(sql=sql):
                command = parse_copy(list(Lexer(sql)))
                self.assertEqual(
                    (command.table, command.columns, command.to_stdout),
                    (table, columns, to_stdout),
                )
        for sql in (
            "COPY",
            "COPY t",
            "COPY t TO stdin",
            "COPY t FROM STDOUT",
            "COPY t () TO STDOUT",
            "COPY t (a, ) TO STDOUT",
            "COPY t (a TO STDOUT",
            "COPY user TO STDOUT",
            "COPY t TO STDOUT x",
            "COPY 1 TO STDOUT",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(PgError) as raised:
                    parse_copy(list(Lexer(sql)))
                self.assertEqual(
                    (raised.exception.code, raised.exception.message),
                    ("42601", "syntax error in COPY command"),
                )

    def test_copy_data(self) -> None:
        """COPY data escapes backslashes, tabs, newlines and carriage returns,
        and writes NULL as \\N."""
        row = [Value.Integer(1), Value.Null(), Value.String("a\\b\tc\nd\re"), Value.Float(-0.5)]
        line = format_copy_row(row)
        self.assertEqual(line, "1\t\\N\ta\\\\b\\tc\\nd\\re\t-0.5\n")

        table = Table(
            name="t",
            primary_key=0,
            columns=[
                column("id", DataType.Integer),
                column("b", DataType.Boolean),
                column("s", DataType.String),
                column("f", DataType.Float),
            ],
        )
        rows = read_copy_data(table, [0, 1, 2, 3], line + "2\tTRUE\tx\\y\\\tinf")
        self.assertEqual(rows[0], ({0: 0, 1: 1, 2: 2, 3: 3}, row))
        self.assertEqual(
            rows[1][1],
            [Value.Integer(2), Value.Boolean(True), Value.String("xy\\"), Value.Float(math.inf)],
        )
        rows = read_copy_data(table, [3, 0], "1.5\t7\n")
        self.assertEqual(rows, [({3: 0, 0: 1}, [Value.Float(1.5), Value.Integer(7)])])
        self.assertEqual(read_copy_data(table, [0], ""), [])
        with self.assertRaises(Error.InvalidInput) as raised:
            read_copy_data(table, [0, 1], "1\tt\n2\n")
        self.assertEqual(
            str(raised.exception),
            "invalid input: wrong number of fields on line 2: expected 2, found 1",
        )
        with self.assertRaises(Error.InvalidInput) as raised:
            read_copy_data(table, [0, 1], "1\tyes")
        self.assertEqual(
            str(raised.exception), "invalid input: invalid value for BOOLEAN column b on line 1"
        )

    def test_read_field(self) -> None:
        """Fields are read as text parameters of their column's type."""
        self.assertEqual(read_field("+5", DataType.Integer), Value.Integer(5))
        self.assertEqual(read_field("f", DataType.Boolean), Value.Boolean(False))
        self.assertEqual(read_field("-Infinity", DataType.Float), Value.Float(-math.inf))
        self.assertEqual(read_field("", DataType.String), Value.String(""))
        with self.assertRaises(PgError):
            read_field("1.5", DataType.Integer)


def column(name: str, datatype: DataType) -> Column:
    """Returns a column without constraints."""
    return Column(
        name=name,
        datatype=datatype,
        nullable=True,
        default=None,
        unique=False,
        index=False,
        references=None,
    )


class TestConnection(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING)")
        session.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        session.execute("CREATE USER alice PASSWORD 'secret'")
        session.close()
        self.threads: list[threading.Thread] = []

    def tearDown(self) -> None:
        for thread in self.threads:
            thread.join()

    def connect(self) -> Client:
        """Connects a client to a connection served by a thread."""
        server, client = socket.socketpair()

        def serve() -> None:
            try:
                with server.makefile("rb") as reader, server.makefile("wb") as writer:
                    Connection(self.engine, reader, writer).serve()
            finally:
                server.close()

        thread = threading.Thread(target=serve)
        thread.start()
        self.threads.append(thread)
        reader, writer = client.makefile("rb"), client.makefile("wb")
        client.close()
        return Client(reader, writer)

    def test_startup(self) -> None:
        """A user with a password is asked for it; the session is started as
        the user, and closed with the connection. A failed startup has no
        session."""
        with self.connect() as client:
            messages = client.startup("nosuch")
            self.assertEqual(
                messages, [ErrorResponse("FATAL", "28000", "user nosuch does not exist")]
            )
        with self.connect() as client:
            messages = client.startup("alice", "secret")
            self.assertIsInstance(messages[0], AuthenticationMD5Password)
            self.assertEqual(messages[1], AuthenticationOk())
            self.assertEqual(messages[-1], ReadyForQuery("I"))
            messages = client.query("SHOW SESSIONS")
            self.assertEqual(messages[1], DataRow([b"2", b"alice", b"alice", None, b"0"]))
            client.terminate()
            self.assertTrue(client.closed)
        with self.engine.sessions.lock() as sessions:
            self.assertEqual(sessions.rows(), [])

        failed = ErrorResponse("FATAL", "28P01", "password authentication failed for user alice")
        with self.connect() as client:
            messages = client.startup("alice", "wrong")
            self.assertEqual(messages[-1], failed)
            self.assertTrue(client.closed)

        # Another message in place of the password fails too.
        with self.connect() as client:
            client.send(StartupMessage(PROTOCOL_VERSION, {"user": "alice"}))
            client.flush()
            self.assertIsInstance(client.receive(), AuthenticationMD5Password)
            client.send(Query("SELECT 1"))
            client.flush()
            self.assertEqual(client.receive(), failed)
            self.assertIsNone(client.receive())

    def test_query(self) -> None:
        """A query's statements are executed one by one."""
        with self.connect() as client:
            client.startup("admin")
            messages = client.query("BEGIN; SELECT v FROM t WHERE id = 2")
            self.assertEqual(
                messages,
                [
                    CommandComplete("BEGIN"),
                    RowDescription(messages[1].fields),  # type: ignore[attr-defined]
                    DataRow([b"b"]),
                    CommandComplete("SELECT 1"),
                    ReadyForQuery("T"),
                ],
            )
            self.assertEqual(client.query("ROLLBACK")[-1], ReadyForQuery("I"))

    def test_extended(self) -> None:
        """Parse, Bind and Execute of a statement with a binary parameter and
        binary results."""
        with self.connect() as client:
            client.startup("admin")
            client.send(Parse("s", "SELECT id FROM t WHERE id > $1", [23]))
            client.send(Bind("", "s", [1], [b"\x00\x00\x00\x01"], [1]))
            client.send(Execute("", 0))
            messages = client.sync()
            self.assertEqual(
                [type(message).__name__ for message in messages],
                ["ParseComplete", "BindComplete", "DataRow", "CommandComplete", "ReadyForQuery"],
            )
            self.assertEqual(messages[2], DataRow([b"\x00" * 7 + b"\x02"]))

    def test_protocol_errors(self) -> None:
        """After an error, messages up to Sync are skipped, also a Query;
        COPY messages outside of COPY are ignored; text that isn't UTF-8 and
        other messages during COPY FROM STDIN are errors."""
        with self.connect() as client:
            client.startup("admin")
            client.send(CopyDone())
            client.send(Bind("", "nosuch", [], [], []))
            client.send(Query("SELECT 1"))
            client.send(Flush())
            self.assertEqual(
                client.sync(),
                [
                    ErrorResponse("ERROR", "26000", "prepared statement nosuch does not exist"),
                    ReadyForQuery("I"),
                ],
            )

            invalid = ErrorResponse("ERROR", "22021", "invalid byte sequence for encoding UTF8")
            client.send(Parse("", "SELECT $1", [25]))
            client.send(Bind("", "", [], [b"\xff"], []))
            self.assertEqual(client.sync()[1:], [invalid, ReadyForQuery("I")])

            client.send(Query("COPY t (id) FROM STDIN"))
            client.flush()
            self.assertEqual(client.receive(), CopyInResponse(1))
            client.send(CopyData(b"\xff\n"))
            client.send(CopyDone())
            client.flush()
            self.assertEqual(client.receive_until_ready(), [invalid, ReadyForQuery("I")])

            client.send(Query("COPY t (id) FROM STDIN"))
            client.flush()
            self.assertEqual(client.receive(), CopyInResponse(1))
            client.send(Parse("", "SELECT 1", []))
            client.flush()
            unexpected = "unexpected message type P during COPY from stdin"
            self.assertEqual(
                client.receive_until_ready(),
                [ErrorResponse("ERROR", "08P01", unexpected), ReadyForQuery("I")],
            )
            self.assertEqual(client.query("SELECT count(*) FROM t")[1], DataRow([b"2"]))


if __name__ == "__main__":
    unittest.main()
