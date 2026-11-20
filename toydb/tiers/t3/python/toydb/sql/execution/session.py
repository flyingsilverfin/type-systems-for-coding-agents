"""SQL client sessions.

Mirrors `src/sql/execution/session.rs`.
"""

import logging
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Generic, TypeVar

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error, errdata, errinput, panic
from toydb.fmt import debug, debug_tuple
from toydb.iter import IntoIter
from toydb.sql.engine import Engine, Transaction
from toydb.sql.execution.analyze import COLUMNS as STATISTICS_COLUMNS
from toydb.sql.execution.analyze import analyze, show_statistics
from toydb.sql.execution.digest import StatementDigests, digest
from toydb.sql.execution.executor import ExecutionResult
from toydb.sql.execution.export import export
from toydb.sql.execution.importer import import_lines
from toydb.sql.parser import Parser, ast
from toydb.sql.planner import Plan
from toydb.sql.types import Label, Row, Rows, Value
from toydb.storage import mvcc

if TYPE_CHECKING:
    from toydb.sql.engine import raft

log = logging.getLogger(__name__)

T = TypeVar("T", bound=Transaction)
R = TypeVar("R")


class Session(Generic[T]):
    """A SQL client session. Parses and executes raw SQL statements and
    handles transaction control."""

    def __init__(self, engine: Engine[T]) -> None:
        """Creates a new session using the given SQL engine."""
        self.engine = engine
        """The SQL engine."""
        self.txn: T | None = None
        """The current transaction, if any."""
        self.digests = StatementDigests()
        """Statistics of the statements executed by the session."""

    def execute(self, statement: str) -> "StatementResult":
        """Executes a client statement, and records it in the session's
        statement statistics by its digest. Statements that fail to parse
        are not recorded, nor is SHOW STATEMENTS."""
        parsed = Parser.parse(statement)
        if isinstance(parsed, ast.Statement.ShowStatements):
            return StatementResult.Select(StatementDigests.COLUMNS, self.digests.rows())
        statement_digest = digest(parsed)
        try:
            result, rows = self.execute_statement(parsed)
        except Error:
            self.digests.record_error(statement_digest)
            raise
        self.digests.record(statement_digest, rows)
        return result

    def execute_statement(self, statement: ast.Statement) -> tuple["StatementResult", int]:
        """Executes a parsed statement. Returns the result and the number of
        rows it returned or wrote, for the statement statistics."""
        # Transaction control and the extension statements are handled here,
        # other statements are handled by the SQL executor.
        match statement:
            # BEGIN: starts a new transaction and returns its state.
            case ast.Statement.Begin(read_only, as_of):
                if self.txn is not None:
                    raise errinput("already in a transaction")
                match (read_only, as_of):
                    case (False, None):
                        txn = self.engine.begin()
                    case (True, None):
                        txn = self.engine.begin_read_only()
                    case (True, version):
                        txn = self.engine.begin_as_of(version)
                    case _:
                        raise errinput("can't start read-write transaction in a given version")
                state = txn.state().clone()
                self.txn = txn
                return StatementResult.Begin(state), 0

            # COMMIT: commits the currently open transaction, if any.
            case ast.Statement.Commit():
                if (txn := self.txn) is None:
                    raise errinput("not in a transaction")
                self.txn = None
                version = txn.state().version
                txn.commit()
                return StatementResult.Commit(version), 0

            # ROLLBACK: rolls back the currently open transaction, if any.
            case ast.Statement.Rollback():
                if (txn := self.txn) is None:
                    raise errinput("not in a transaction")
                self.txn = None
                version = txn.state().version
                txn.rollback()
                return StatementResult.Rollback(version), 0

            # EXPLAIN: returns the given SQL query's plan. The extension
            # statements are not planned, and can't be explained.
            case ast.Statement.Explain(
                ast.Statement.ShowStatements() | ast.Statement.ShowStatistics()
            ):
                raise errinput("EXPLAIN is not supported for SHOW")
            case ast.Statement.Explain(ast.Statement.Analyze()):
                raise errinput("EXPLAIN is not supported for ANALYZE")
            case ast.Statement.Explain(ast.Statement.Export()):
                raise errinput("EXPLAIN is not supported for EXPORT")
            case ast.Statement.Explain(ast.Statement.Import()):
                raise errinput("EXPLAIN is not supported for IMPORT")
            case ast.Statement.Explain(inner):
                plan = self.with_txn(True, lambda txn: Plan.build(inner, txn).optimize())
                return StatementResult.Explain(plan), 0

            # SHOW STATEMENTS is handled by execute(), since it isn't recorded.
            case ast.Statement.ShowStatements():
                panic("SHOW STATEMENTS must be handled by execute()")

            # ANALYZE: computes and stores column statistics.
            case ast.Statement.Analyze(table):
                tables = self.with_txn(False, lambda txn: analyze(txn, table))
                return StatementResult.Analyze(tables), 0

            # SHOW STATISTICS: returns a table's column statistics.
            case ast.Statement.ShowStatistics(table):
                rows = self.with_txn(True, lambda txn: show_statistics(txn, table))
                return StatementResult.Select(STATISTICS_COLUMNS, rows), len(rows)

            # EXPORT: exports a table or the database as text lines.
            case ast.Statement.Export(table, format):
                exported = self.with_txn(True, lambda txn: export(txn, table, format))
                return StatementResult.Export(format, exported.lines), exported.rows

            # IMPORT: imports text lines into a table, like an INSERT.
            case ast.Statement.Import(table, format, lines):
                count = self.with_txn(False, lambda txn: import_lines(txn, table, format, lines))
                return StatementResult.Insert(count), count

            # Other statements (SELECT etc.) are handled by the SQL executor.
            case other:
                read_only = isinstance(other, ast.Statement.Select)
                result = self.with_txn(
                    read_only,
                    lambda txn: StatementResult.try_from(
                        Plan.build(other, txn).optimize().execute(txn)
                    ),
                )
                return result, result.row_count()

    def with_txn(self, read_only: bool, f: Callable[[T], R]) -> R:
        """Runs a closure in the session's transaction, if there is one,
        otherwise a temporary implicit transaction. If read_only is true, uses
        a read-only implicit transaction. Does not automatically retry
        errors."""
        # Use the current explicit transaction, if there is one.
        if self.txn is not None:
            return f(self.txn)
        # Otherwise, use an implicit transaction. Doing this session-side
        # results in additional Raft roundtrips to begin and complete the
        # transaction -- we could avoid this if the Raft SQL state machine
        # supported implicit transactions, but we keep it simple.
        txn = self.engine.begin_read_only() if read_only else self.engine.begin()
        try:
            result = f(txn)
        except Error:
            txn.rollback()
            raise
        txn.commit()
        return result

    def status(self: "Session[raft.Transaction]") -> "raft.Status":
        """Returns the Raft SQL engine status. Only available for sessions
        using the `Raft` engine (`impl Session<'_, Raft>` in Rust)."""
        from toydb.sql.engine.raft import Raft

        assert isinstance(self.engine, Raft), "status requires a Raft engine"
        return self.engine.status()

    def close(self) -> None:
        """If the session has an open transaction when closed, roll it back.
        This is the Rust `Drop` implementation, and must be called when the
        session is no longer used."""
        if (txn := self.txn) is None:
            return
        self.txn = None
        try:
            txn.rollback()
        except Error as error:
            log.error("implicit transaction rollback failed: %s", error)


class StatementResult(encoding.Value, ABC):
    """A session statement result, returned over the network to SQL clients."""

    Begin: ClassVar[type["Begin"]]
    Commit: ClassVar[type["Commit"]]
    Rollback: ClassVar[type["Rollback"]]
    Explain: ClassVar[type["Explain"]]
    CreateTable: ClassVar[type["CreateTable"]]
    DropTable: ClassVar[type["DropTable"]]
    Delete: ClassVar[type["Delete"]]
    Insert: ClassVar[type["Insert"]]
    Update: ClassVar[type["Update"]]
    Select: ClassVar[type["Select"]]
    Analyze: ClassVar[type["Analyze"]]
    Export: ClassVar[type["Export"]]

    @staticmethod
    def try_from(result: ExecutionResult) -> "StatementResult":
        """Converts an execution result into a statement result."""
        match result:
            case ExecutionResult.CreateTable(name):
                return CreateTable(name)
            case ExecutionResult.DropTable(name, existed):
                return DropTable(name, existed)
            case ExecutionResult.Delete(count):
                return Delete(count)
            case ExecutionResult.Insert(count):
                return Insert(count)
            case ExecutionResult.Update(count):
                return Update(count)
            case ExecutionResult.Select(columns, rows):
                # We buffer the entire set of rows, for simplicity.
                return Select(columns, list(rows))
            case _:
                panic(f"unknown execution result {result!r}")

    def row_count(self) -> int:
        """Returns the number of rows the statement returned (SELECT) or
        wrote (INSERT, UPDATE and DELETE), or 0 for other statements."""
        match self:
            case Select(rows=rows):
                return len(rows)
            case Insert(count) | Update(count) | Delete(count):
                return count
            case _:
                return 0

    def try_into_rows(self) -> Rows:
        """Attempts to convert a SELECT result into a row iterator."""
        match self:
            case Select(rows=rows):
                return IntoIter(rows)
            case _:
                raise errdata(f"expected select result, found {debug(self)}")

    def try_into_row(self) -> Row:
        """Extracts the first row from a SELECT result."""
        if (row := next(self.try_into_rows(), None)) is None:
            raise errdata("no rows returned")
        return row

    def try_into_value(self) -> Value:
        """Extracts the value of the first column in the first row."""
        row = self.try_into_row()
        if not row:
            raise errdata("no columns returned")
        return row[0]

    def try_into_bool(self) -> bool:
        """Extracts the first boolean value of the first column in the first
        row."""
        return self.try_into_value().try_into_bool()

    def try_into_f64(self) -> float:
        """Extracts the first f64 value of the first column in the first row."""
        return self.try_into_value().try_into_f64()

    def try_into_i64(self) -> int:
        """Extracts the first i64 value of the first column in the first row."""
        return self.try_into_value().try_into_i64()

    def try_into_string(self) -> str:
        """Extracts the first string value of the first column in the first
        row."""
        return self.try_into_value().try_into_string()

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case Begin(state):
                serializer.serialize_newtype_variant(0)
                state.serialize(serializer)
            case Commit(version):
                serializer.serialize_struct_variant(1)
                serializer.serialize_u64(version)
            case Rollback(version):
                serializer.serialize_struct_variant(2)
                serializer.serialize_u64(version)
            case Explain(plan):
                serializer.serialize_newtype_variant(3)
                plan.serialize(serializer)
            case CreateTable(name):
                serializer.serialize_struct_variant(4)
                serializer.serialize_str(name)
            case DropTable(name, existed):
                serializer.serialize_struct_variant(5)
                serializer.serialize_str(name)
                serializer.serialize_bool(existed)
            case Delete(count):
                serializer.serialize_struct_variant(6)
                serializer.serialize_u64(count)
            case Insert(count):
                serializer.serialize_struct_variant(7)
                serializer.serialize_u64(count)
            case Update(count):
                serializer.serialize_struct_variant(8)
                serializer.serialize_u64(count)
            case Select(columns, rows):
                serializer.serialize_struct_variant(9)
                COLUMNS.serialize(columns, serializer)
                ROWS.serialize(rows, serializer)
            case Analyze(tables):
                serializer.serialize_struct_variant(10)
                TABLES.serialize(tables, serializer)
            case Export(format, lines):
                serializer.serialize_struct_variant(11)
                format.serialize(serializer)
                LINES.serialize(lines, serializer)
            case _:
                panic(f"unknown statement result {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "StatementResult":
        match deserializer.deserialize_enum():
            case 0:
                return Begin(mvcc.TransactionState.deserialize(deserializer))
            case 1:
                return Commit(deserializer.deserialize_u64())
            case 2:
                return Rollback(deserializer.deserialize_u64())
            case 3:
                return Explain(Plan.deserialize(deserializer))
            case 4:
                return CreateTable(deserializer.deserialize_str())
            case 5:
                return DropTable(deserializer.deserialize_str(), deserializer.deserialize_bool())
            case 6:
                return Delete(deserializer.deserialize_u64())
            case 7:
                return Insert(deserializer.deserialize_u64())
            case 8:
                return Update(deserializer.deserialize_u64())
            case 9:
                return Select(COLUMNS.deserialize(deserializer), ROWS.deserialize(deserializer))
            case 10:
                return Analyze(TABLES.deserialize(deserializer))
            case 11:
                format = ast.ExportFormat.deserialize(deserializer)
                return Export(format, LINES.deserialize(deserializer))
            case index:
                raise deserializer.invalid_variant(index, 12)


@dataclass
class Begin(StatementResult):
    state: mvcc.TransactionState

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Begin", [self.state], pretty)


@dataclass
class Commit(StatementResult):
    version: mvcc.Version


@dataclass
class Rollback(StatementResult):
    version: mvcc.Version


@dataclass
class Explain(StatementResult):
    plan: Plan

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Explain", [self.plan], pretty)


@dataclass
class CreateTable(StatementResult):
    name: str


@dataclass
class DropTable(StatementResult):
    name: str
    existed: bool


@dataclass
class Delete(StatementResult):
    count: int


@dataclass
class Insert(StatementResult):
    count: int


@dataclass
class Update(StatementResult):
    count: int


@dataclass
class Select(StatementResult):
    """For simplicity, we buffer and send the entire set of rows as a list
    instead of streaming them to the client. Streaming reads haven't been
    implemented from Raft either, so they're buffered all the way through."""

    columns: list[Label]
    rows: list[Row]


@dataclass
class Analyze(StatementResult):
    tables: list[str]
    """The analyzed tables, in the order they were analyzed."""


@dataclass
class Export(StatementResult):
    format: ast.ExportFormat
    lines: list[str]


StatementResult.Begin = Begin
StatementResult.Commit = Commit
StatementResult.Rollback = Rollback
StatementResult.Explain = Explain
StatementResult.CreateTable = CreateTable
StatementResult.DropTable = DropTable
StatementResult.Delete = Delete
StatementResult.Insert = Insert
StatementResult.Update = Update
StatementResult.Select = Select
StatementResult.Analyze = Analyze
StatementResult.Export = Export

COLUMNS: serde.Serde[list[Label]] = serde.Vec(Label)
"""The encoding of the result columns, a `Vec<Label>`."""

ROWS: serde.Serde[list[Row]] = serde.Vec(serde.Vec(Value))
"""The encoding of the result rows, a `Vec<Row>`."""

TABLES: serde.Serde[list[str]] = serde.Vec(serde.String)
"""The encoding of the analyzed tables, a `Vec<String>`."""

LINES: serde.Serde[list[str]] = serde.Vec(serde.String)
"""The encoding of the exported lines, a `Vec<String>`."""
