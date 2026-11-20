"""A Raft-based SQL engine.

Mirrors `src/sql/engine/raft.rs`.
"""

import queue
from abc import ABC
from dataclasses import dataclass
from typing import ClassVar, Generic, Protocol, TypeVar

from toydb import encoding, raft, storage
from toydb.encoding import bincode, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error, assert_eq, errdata, panic
from toydb.fmt import debug
from toydb.iter import IntoIter
from toydb.sql.engine import engine as sql_engine
from toydb.sql.engine.local import Local
from toydb.sql.types import Expression, Row, Rows, Table, Value
from toydb.storage import mvcc
from toydb.storage.btree import BTreeMap, BTreeSet

E = TypeVar("E", bound=storage.Engine)
V = TypeVar("V")

type ResponseSender = queue.Queue[raft.Response | Error]
"""Sends a Raft response or error back to the requester (a
`Sender<Result<raft::Response>>` in Rust)."""


class RequestSender(Protocol):
    """Sends requests to the local Raft node, along with a response channel (a
    `Sender<(raft::Request, Sender<Result<raft::Response>>)>` in Rust). This is
    typically a `queue.Queue`, possibly shared with other message types."""

    def put(self, item: tuple[raft.Request, ResponseSender], /) -> None:
        """Sends a request and its response channel."""
        ...


class _Rows:
    """The encoding of updated rows, a `BTreeMap<Value, Row>`."""

    def serialize(self, value: BTreeMap[Value, Row], serializer: Serializer, /) -> None:
        serializer.serialize_map(len(value))
        for id, row in value:
            id.serialize(serializer)
            ROWS.inner.serialize(row, serializer)

    def deserialize(self, deserializer: Deserializer, /) -> BTreeMap[Value, Row]:
        # Key the decoded map by position: `Value` hashing and equality differ
        # from its ordering, which is what the BTreeMap uses.
        ids: list[Value] = []

        def key() -> int:
            ids.append(Value.deserialize(deserializer))
            return len(ids) - 1

        rows: BTreeMap[Value, Row] = BTreeMap()
        decoded = deserializer.deserialize_map(key, lambda: ROWS.inner.deserialize(deserializer))
        for index, row in decoded.items():
            rows.insert(ids[index], row)
        return rows


VALUES: serde.Serde[list[Value]] = serde.Vec(Value)
"""The encoding of a `Vec<Value>` or `[Value]`, e.g. primary keys. This is
also the encoding of a `BTreeSet<Value>`, in sorted order."""

ROWS: serde.Vec[Row] = serde.Vec(serde.Vec(Value))
"""The encoding of a `Vec<Row>`."""

UPDATES: serde.Serde[BTreeMap[Value, Row]] = _Rows()
"""The encoding of a `BTreeMap<Value, Row>`."""

TABLES: serde.Serde[list[Table]] = serde.Vec(Table)
"""The encoding of a `Vec<Table>`."""


class Read(encoding.Value, ABC):
    """A read command, submitted via Raft and executed on the leader. Each
    command corresponds to a SQL engine method and parameters."""

    BeginReadOnly: ClassVar[type["BeginReadOnly"]]
    Status: ClassVar[type["ReadStatus"]]
    Get: ClassVar[type["Get"]]
    LookupIndex: ClassVar[type["LookupIndex"]]
    Scan: ClassVar[type["Scan"]]
    GetTable: ClassVar[type["GetTable"]]
    ListTables: ClassVar[type["ListTables"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case BeginReadOnly(as_of):
                serializer.serialize_struct_variant(0)
                serde.Option(serde.U64).serialize(as_of, serializer)
            case ReadStatus():
                serializer.serialize_unit_variant(1)
            case Get(txn, table, ids):
                serializer.serialize_struct_variant(2)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                VALUES.serialize(ids, serializer)
            case LookupIndex(txn, table, column, values):
                serializer.serialize_struct_variant(3)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                serializer.serialize_str(column)
                VALUES.serialize(values, serializer)
            case Scan(txn, table, filter):
                serializer.serialize_struct_variant(4)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                serde.Option(Expression).serialize(filter, serializer)
            case GetTable(txn, table):
                serializer.serialize_struct_variant(5)
                txn.serialize(serializer)
                serializer.serialize_str(table)
            case ListTables(txn):
                serializer.serialize_struct_variant(6)
                txn.serialize(serializer)
            case _:
                panic(f"unknown read {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Read":
        index = deserializer.deserialize_enum()
        if index > 6:
            raise deserializer.invalid_variant(index, 7)
        if index == 1:
            return ReadStatus()
        deserializer.deserialize_struct_variant()
        if index == 0:
            return BeginReadOnly(as_of=serde.Option(serde.U64).deserialize(deserializer))
        txn = mvcc.TransactionState.deserialize(deserializer)
        match index:
            case 2:
                return Get(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    ids=VALUES.deserialize(deserializer),
                )
            case 3:
                return LookupIndex(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    column=deserializer.deserialize_str(),
                    values=VALUES.deserialize(deserializer),
                )
            case 4:
                return Scan(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    filter=serde.Option(Expression).deserialize(deserializer),
                )
            case 5:
                return GetTable(txn=txn, table=deserializer.deserialize_str())
            case _:
                return ListTables(txn=txn)


@dataclass(frozen=True)
class BeginReadOnly(Read):
    as_of: mvcc.Version | None


@dataclass(frozen=True)
class ReadStatus(Read):
    """This is `Read::Status` in Rust: use it as `Read.Status`, since the bare
    name clashes with the `Status` struct."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return "Status"


@dataclass(frozen=True)
class Get(Read):
    txn: mvcc.TransactionState
    table: str
    ids: list[Value]


@dataclass(frozen=True)
class LookupIndex(Read):
    txn: mvcc.TransactionState
    table: str
    column: str
    values: list[Value]


@dataclass(frozen=True)
class Scan(Read):
    txn: mvcc.TransactionState
    table: str
    filter: Expression | None


@dataclass(frozen=True)
class GetTable(Read):
    txn: mvcc.TransactionState
    table: str


@dataclass(frozen=True)
class ListTables(Read):
    txn: mvcc.TransactionState


Read.BeginReadOnly = BeginReadOnly
Read.Status = ReadStatus
Read.Get = Get
Read.LookupIndex = LookupIndex
Read.Scan = Scan
Read.GetTable = GetTable
Read.ListTables = ListTables


class Write(encoding.Value, ABC):
    """A write command, submitted via Raft and executed on all nodes. Each
    command corresponds to a SQL engine method and parameters."""

    Begin: ClassVar[type["Begin"]]
    Commit: ClassVar[type["Commit"]]
    Rollback: ClassVar[type["Rollback"]]
    Delete: ClassVar[type["Delete"]]
    Insert: ClassVar[type["Insert"]]
    Update: ClassVar[type["Update"]]
    CreateTable: ClassVar[type["CreateTable"]]
    DropTable: ClassVar[type["DropTable"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case Begin():
                serializer.serialize_unit_variant(0)
            case Commit(txn):
                serializer.serialize_newtype_variant(1)
                txn.serialize(serializer)
            case Rollback(txn):
                serializer.serialize_newtype_variant(2)
                txn.serialize(serializer)
            case Delete(txn, table, ids):
                serializer.serialize_struct_variant(3)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                VALUES.serialize(ids, serializer)
            case Insert(txn, table, rows):
                serializer.serialize_struct_variant(4)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                ROWS.serialize(rows, serializer)
            case Update(txn, table, rows):
                serializer.serialize_struct_variant(5)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                UPDATES.serialize(rows, serializer)
            case CreateTable(txn, schema):
                serializer.serialize_struct_variant(6)
                txn.serialize(serializer)
                schema.serialize(serializer)
            case DropTable(txn, table, if_exists):
                serializer.serialize_struct_variant(7)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                serializer.serialize_bool(if_exists)
            case _:
                panic(f"unknown write {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Write":
        index = deserializer.deserialize_enum()
        if index > 7:
            raise deserializer.invalid_variant(index, 8)
        if index == 0:
            return Begin()
        if index > 2:
            deserializer.deserialize_struct_variant()
        txn = mvcc.TransactionState.deserialize(deserializer)
        match index:
            case 1:
                return Commit(txn)
            case 2:
                return Rollback(txn)
            case 3:
                return Delete(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    ids=VALUES.deserialize(deserializer),
                )
            case 4:
                return Insert(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    rows=ROWS.deserialize(deserializer),
                )
            case 5:
                return Update(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    rows=UPDATES.deserialize(deserializer),
                )
            case 6:
                return CreateTable(txn=txn, schema=Table.deserialize(deserializer))
            case _:
                return DropTable(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    if_exists=deserializer.deserialize_bool(),
                )


@dataclass(frozen=True)
class Begin(Write):
    pass


@dataclass(frozen=True)
class Commit(Write):
    txn: mvcc.TransactionState


@dataclass(frozen=True)
class Rollback(Write):
    txn: mvcc.TransactionState


@dataclass(frozen=True)
class Delete(Write):
    txn: mvcc.TransactionState
    table: str
    ids: list[Value]


@dataclass(frozen=True)
class Insert(Write):
    txn: mvcc.TransactionState
    table: str
    rows: list[Row]


@dataclass(frozen=True)
class Update(Write):
    txn: mvcc.TransactionState
    table: str
    rows: BTreeMap[Value, Row]


@dataclass(frozen=True)
class CreateTable(Write):
    txn: mvcc.TransactionState
    schema: Table


@dataclass(frozen=True)
class DropTable(Write):
    txn: mvcc.TransactionState
    table: str
    if_exists: bool


Write.Begin = Begin
Write.Commit = Commit
Write.Rollback = Rollback
Write.Delete = Delete
Write.Insert = Insert
Write.Update = Update
Write.CreateTable = CreateTable
Write.DropTable = DropTable


@dataclass
class Status:
    """Raft SQL engine status."""

    raft: raft.Status
    mvcc: mvcc.Status

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        self.raft.serialize(serializer)
        self.mvcc.serialize(serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Status":
        deserializer.deserialize_struct()
        return cls(
            raft=raft.Status.deserialize(deserializer),
            mvcc=mvcc.Status.deserialize(deserializer),
        )


class Raft(sql_engine.Engine["Transaction"]):
    """A Raft-based SQL engine. This dispatches to the `Local` engine for local
    storage and processing on each node, but sends read and write commands
    through Raft for distributed consensus.

    The `Raft` engine itself is simply a Raft client which sends `raft.Request`
    to the local Raft node for processing. These requests are applied to the
    Raft SQL engine's `State` state machine running below Raft on each node,
    which executes the commands on a `Local` SQL engine using a
    `storage.Engine` for local storage.

    For more details on how SQL statements flow through the engine, see the
    `sql` module documentation.
    """

    APPLIED_INDEX_KEY: ClassVar[bytes] = b"applied_index"
    """The unversioned key used to store the applied index. Just uses a string
    for simplicity."""

    def __init__(self, tx: RequestSender) -> None:
        """Creates a new Raft-based SQL engine, with a channel to send requests
        to the local Raft node."""
        self.tx = tx
        """Sends requests to the local Raft node, along with a response channel."""

    @staticmethod
    def new_state(engine: E) -> "State[E]":
        """Creates the Raft-managed state machine for the Raft engine. Receives
        commands from the Raft engine and executes them on a `Local` engine."""
        return State(engine)

    def request(self, request: raft.Request) -> raft.Response:
        """Executes a request against the Raft cluster, waiting for the response."""
        response_tx: ResponseSender = queue.Queue(1)
        self.tx.put((request, response_tx))
        response = response_tx.get()
        if isinstance(response, Error):
            raise response
        return response

    def write(self, write: Write, serde: serde.Serde[V]) -> V:
        """Writes through Raft, deserializing the response into the return type."""
        match self.request(raft.Request.Write(write.encode())):
            case raft.Response.Write(response):
                return bincode.deserialize(serde, response)
            case response:
                raise errdata(f"unexpected Raft write response {debug(response)}")

    def read(self, read: Read, serde: serde.Serde[V]) -> V:
        """Reads from Raft, deserializing the response into the return type."""
        match self.request(raft.Request.Read(read.encode())):
            case raft.Response.Read(response):
                return bincode.deserialize(serde, response)
            case response:
                raise errdata(f"unexpected Raft read response {debug(response)}")

    def status(self) -> Status:
        """Raft SQL engine status."""
        match self.request(raft.Request.Status()):
            case raft.Response.Status(status):
                pass
            case response:
                raise errdata(f"unexpected Raft status response {debug(response)}")
        return Status(raft=status, mvcc=self.read(Read.Status(), mvcc.Status))

    def begin(self) -> "Transaction":
        return Transaction.begin(self, False, None)

    def begin_read_only(self) -> "Transaction":
        return Transaction.begin(self, True, None)

    def begin_as_of(self, version: mvcc.Version) -> "Transaction":
        return Transaction.begin(self, True, version)


class Transaction(sql_engine.Transaction):
    """A Raft SQL engine transaction.

    This keeps track of the transaction state in memory. An `mvcc.Transaction`
    normally manages this, but since `mvcc.Transaction` runs below Raft, it
    can't maintain this state between individual requests (which could execute
    on different leaders). Instead, it uses `mvcc.Transaction.resume` to
    resume the transaction from the provided transaction state for each request.
    """

    def __init__(self, raft: Raft, state: mvcc.TransactionState) -> None:
        self.raft = raft
        """The Raft SQL engine client, used to communicate with Raft."""
        self._state = state
        """The MVCC transaction state."""

    @classmethod
    def begin(cls, raft: Raft, read_only: bool, as_of: mvcc.Version | None) -> "Transaction":
        """Starts a transaction in the given mode."""
        assert as_of is None or read_only, "can't use as_of without read_only"
        # Read-only transactions don't allocate a new MVCC version, so they
        # don't write anything -- they just grab the current transaction state.
        # Submit them as reads to avoid a replication roundtrip.
        if read_only or as_of is not None:
            state = raft.read(Read.BeginReadOnly(as_of), mvcc.TransactionState)
        else:
            state = raft.write(Write.Begin(), mvcc.TransactionState)
        return cls(raft, state)

    def state(self) -> mvcc.TransactionState:
        return self._state

    def commit(self) -> None:
        if self._state.read_only:
            return  # noop
        self.raft.write(Write.Commit(self._state), serde.Unit)

    def rollback(self) -> None:
        if self._state.read_only:
            return  # noop
        self.raft.write(Write.Rollback(self._state), serde.Unit)

    def delete(self, table: str, ids: list[Value]) -> None:
        self.raft.write(Write.Delete(txn=self._state, table=table, ids=ids), serde.Unit)

    def get(self, table: str, ids: list[Value]) -> list[Row]:
        return self.raft.read(Read.Get(txn=self._state, table=table, ids=ids), ROWS)

    def insert(self, table: str, rows: list[Row]) -> None:
        self.raft.write(Write.Insert(txn=self._state, table=table, rows=rows), serde.Unit)

    def lookup_index(self, table: str, column: str, values: list[Value]) -> BTreeSet[Value]:
        read = Read.LookupIndex(txn=self._state, table=table, column=column, values=values)
        return BTreeSet(self.raft.read(read, VALUES))

    def scan(self, table: str, filter: Expression | None) -> Rows:
        scan = self.raft.read(Read.Scan(txn=self._state, table=table, filter=filter), ROWS)
        return IntoIter(scan)

    def update(self, table: str, rows: BTreeMap[Value, Row]) -> None:
        self.raft.write(Write.Update(txn=self._state, table=table, rows=rows), serde.Unit)

    def create_table(self, table: Table) -> None:
        self.raft.write(Write.CreateTable(txn=self._state, schema=table), serde.Unit)

    def drop_table(self, table: str, if_exists: bool) -> bool:
        write = Write.DropTable(txn=self._state, table=table, if_exists=if_exists)
        return self.raft.write(write, serde.Bool)

    def get_table(self, table: str) -> Table | None:
        return self.raft.read(Read.GetTable(txn=self._state, table=table), serde.Option(Table))

    def list_tables(self) -> list[Table]:
        return self.raft.read(Read.ListTables(txn=self._state), TABLES)


class State(raft.State, Generic[E]):
    """The state machine for the Raft SQL engine. Receives commands via Raft and
    dispatches to a `Local` SQL engine which does the actual work, using a
    `storage.Engine` for storage.

    For simplicity, we don't attempt to stream large requests or responses,
    instead just delivering them as one large chunk. This means that e.g. a full
    table scan will pull the entire table into memory, serialize it, and send it
    across the network as one message, but that's fine for toyDB.
    """

    def __init__(self, engine: E) -> None:
        """Creates a new Raft state maching using the given storage engine for
        local storage."""
        self.local: Local[E] = Local(engine)
        """The local SQL engine, used for actual storage."""
        applied_index = self.local.get_unversioned(Raft.APPLIED_INDEX_KEY)
        self.applied_index: raft.Index = (
            bincode.deserialize(serde.U64, applied_index) if applied_index is not None else 0
        )
        """The last applied index. This tells Raft which command to apply next."""

    def write(self, command: Write) -> bytes:
        """Executes a write command. This is executed on all nodes, but the
        response is returned from the Raft leader.

        The response is encoded using Bincode. The caller will know what
        response type to expect for each command and deserialize into it.
        """
        match command:
            case Write.Begin():
                return self.local.begin().state().encode()
            case Write.Commit(txn):
                return bincode.serialize(self.local.resume(txn).commit(), serde.Unit)
            case Write.Rollback(txn):
                return bincode.serialize(self.local.resume(txn).rollback(), serde.Unit)

            case Write.Delete(txn, table, ids):
                return bincode.serialize(self.local.resume(txn).delete(table, ids), serde.Unit)
            case Write.Insert(txn, table, rows):
                return bincode.serialize(self.local.resume(txn).insert(table, rows), serde.Unit)
            case Write.Update(txn, table, rows):
                return bincode.serialize(self.local.resume(txn).update(table, rows), serde.Unit)

            case Write.CreateTable(txn, schema):
                return bincode.serialize(self.local.resume(txn).create_table(schema), serde.Unit)
            case Write.DropTable(txn, table, if_exists):
                existed = self.local.resume(txn).drop_table(table, if_exists)
                return bincode.serialize(existed, serde.Bool)
            case _:
                panic(f"unknown write {command!r}")

    def get_applied_index(self) -> raft.Index:
        return self.applied_index

    def apply(self, entry: raft.Entry) -> bytes:
        assert_eq(entry.index, self.applied_index + 1, "entry index not after applied index")

        result: bytes | Error
        if entry.command is not None:
            command = Write.decode(entry.command)
            try:
                result = self.write(command)
            except Error as error:
                # Panic on non-deterministic apply failures, to prevent node
                # state divergence. See `raft.State` docs for details.
                if not error.is_deterministic():
                    panic(f"non-deterministic apply failure: {error}")
                result = error
        else:
            # Raft submits noop commands on leader changes. Ignore them, but
            # record the applied index below.
            result = b""

        # Persist the applied index. We don't have to flush, because it's ok to
        # lose a tail of the state machine writes (e.g. if the machine
        # crashes). Raft will replay the log from the last known applied index.
        self.applied_index = entry.index
        self.local.set_unversioned(
            Raft.APPLIED_INDEX_KEY, bincode.serialize(entry.index, serde.U64)
        )
        if isinstance(result, Error):
            raise result
        return result

    def read(self, command: bytes) -> bytes:
        match Read.decode(command):
            case Read.BeginReadOnly(as_of):
                if as_of is not None:
                    txn = self.local.begin_as_of(as_of)
                else:
                    txn = self.local.begin_read_only()
                return txn.state().encode()
            case Read.Status():
                return self.local.mvcc.status().encode()

            case Read.Get(txn, table, ids):
                return bincode.serialize(self.local.resume(txn).get(table, ids), ROWS)
            case Read.LookupIndex(txn, table, column, values):
                ids = self.local.resume(txn).lookup_index(table, column, values)
                return bincode.serialize(list(ids), VALUES)
            case Read.Scan(txn, table, filter):
                # For simplicity, buffer the entire scan. See `State` comment.
                return bincode.serialize(list(self.local.resume(txn).scan(table, filter)), ROWS)

            case Read.GetTable(txn, table):
                schema = self.local.resume(txn).get_table(table)
                return bincode.serialize(schema, serde.Option(Table))
            case Read.ListTables(txn):
                return bincode.serialize(self.local.resume(txn).list_tables(), TABLES)
            case read:
                panic(f"unknown read {read!r}")
