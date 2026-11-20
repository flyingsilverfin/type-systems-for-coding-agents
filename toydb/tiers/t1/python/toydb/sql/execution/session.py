"""SQL client sessions.

Mirrors `src/sql/execution/session.rs`.
"""

import logging
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error, errdata, errinput, panic
from toydb.fmt import debug, debug_tuple
from toydb.iter import IntoIter
from toydb.sql.engine import Engine, Transaction
from toydb.sql.execution.executor import ExecutionResult
from toydb.sql.parser import Parser, ast
from toydb.sql.planner import Plan
from toydb.sql.types import Label, Row, Rows, Value
from toydb.storage import mvcc

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

    def execute(self, statement: str) -> "StatementResult":
        """Executes a client statement."""
        # Parse and execute the statement. Transaction control is handled
        # here, other statements are handled by the SQL executor.
        match Parser.parse(statement):
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
                return StatementResult.Begin(state)

            # COMMIT: commits the currently open transaction, if any.
            case ast.Statement.Commit():
                if (txn := self.txn) is None:
                    raise errinput("not in a transaction")
                self.txn = None
                version = txn.state().version
                txn.commit()
                return StatementResult.Commit(version)

            # ROLLBACK: rolls back the currently open transaction, if any.
            case ast.Statement.Rollback():
                if (txn := self.txn) is None:
                    raise errinput("not in a transaction")
                self.txn = None
                version = txn.state().version
                txn.rollback()
                return StatementResult.Rollback(version)

            # EXPLAIN: returns the given SQL query's plan.
            case ast.Statement.Explain(inner):
                return self.with_txn(
                    True, lambda txn: StatementResult.Explain(Plan.build(inner, txn).optimize())
                )

            # Other statements (SELECT etc.) are handled by the SQL executor.
            case other:
                read_only = isinstance(other, ast.Statement.Select)
                return self.with_txn(
                    read_only,
                    lambda txn: StatementResult.try_from(
                        Plan.build(other, txn).optimize().execute(txn)
                    ),
                )

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
            case index:
                raise deserializer.invalid_variant(index, 10)


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

COLUMNS: serde.Serde[list[Label]] = serde.Vec(Label)
"""The encoding of the result columns, a `Vec<Label>`."""

ROWS: serde.Serde[list[Row]] = serde.Vec(serde.Vec(Value))
"""The encoding of the result rows, a `Vec<Row>`."""
