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
from toydb.sql.engine.changes import Change
from toydb.sql.engine.local import Local
from toydb.sql.types import Expression, Row, Rows, Table, Value
from toydb.sql.types.check import CHECKS, Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.policy import POLICIES, Policy
from toydb.sql.types.statistics import TableStatistics
from toydb.sql.types.user import MEMBERSHIP, PRIVILEGES, Grant, Membership, Privilege, User
from toydb.sql.types.view import View
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

VIEWS: serde.Serde[list[View]] = serde.Vec(View)
"""The encoding of a `Vec<View>`."""

CHANGES: serde.Serde[list[Change]] = serde.Vec(Change)
"""The encoding of a `Vec<Change>`."""

USERS: serde.Serde[list[User]] = serde.Vec(User)
"""The encoding of a `Vec<User>`."""

MEMBERSHIPS: serde.Serde[list[Membership]] = serde.Vec(MEMBERSHIP)
"""The encoding of a `Vec<(String, String)>` of memberships."""

GRANTS: serde.Serde[list[Grant]] = serde.Vec(Grant)
"""The encoding of a `Vec<Grant>`."""

MATVIEWS: serde.Serde[list[MaterializedView]] = serde.Vec(MaterializedView)
"""The encoding of a `Vec<MaterializedView>`."""


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
    GetStatistics: ClassVar[type["GetStatistics"]]
    GetView: ClassVar[type["GetView"]]
    ListViews: ClassVar[type["ListViews"]]
    Changes: ClassVar[type["Changes"]]
    GetUser: ClassVar[type["GetUser"]]
    ListUsers: ClassVar[type["ListUsers"]]
    ListMemberships: ClassVar[type["ListMemberships"]]
    ListGrants: ClassVar[type["ListGrants"]]
    GetOwner: ClassVar[type["GetOwner"]]
    GetChecks: ClassVar[type["GetChecks"]]
    ListPolicies: ClassVar[type["ListPolicies"]]
    GetMatView: ClassVar[type["GetMatView"]]
    GetMatViewRows: ClassVar[type["GetMatViewRows"]]
    ListMatViews: ClassVar[type["ListMatViews"]]

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
            case GetStatistics(txn, table):
                serializer.serialize_struct_variant(7)
                txn.serialize(serializer)
                serializer.serialize_str(table)
            case GetView(txn, view):
                serializer.serialize_struct_variant(8)
                txn.serialize(serializer)
                serializer.serialize_str(view)
            case ListViews(txn):
                serializer.serialize_struct_variant(9)
                txn.serialize(serializer)
            case Changes(since):
                serializer.serialize_struct_variant(10)
                serializer.serialize_u64(since)
            case GetUser(txn, user):
                serializer.serialize_struct_variant(11)
                txn.serialize(serializer)
                serializer.serialize_str(user)
            case ListUsers(txn):
                serializer.serialize_struct_variant(12)
                txn.serialize(serializer)
            case ListMemberships(txn):
                serializer.serialize_struct_variant(13)
                txn.serialize(serializer)
            case ListGrants(txn):
                serializer.serialize_struct_variant(14)
                txn.serialize(serializer)
            case GetOwner(txn, object):
                serializer.serialize_struct_variant(15)
                txn.serialize(serializer)
                serializer.serialize_str(object)
            case GetChecks(txn, table):
                serializer.serialize_struct_variant(16)
                txn.serialize(serializer)
                serializer.serialize_str(table)
            case ListPolicies(txn, table):
                serializer.serialize_struct_variant(17)
                txn.serialize(serializer)
                serializer.serialize_str(table)
            case GetMatView(txn, name):
                serializer.serialize_struct_variant(18)
                txn.serialize(serializer)
                serializer.serialize_str(name)
            case GetMatViewRows(txn, name):
                serializer.serialize_struct_variant(19)
                txn.serialize(serializer)
                serializer.serialize_str(name)
            case ListMatViews(txn):
                serializer.serialize_struct_variant(20)
                txn.serialize(serializer)
            case _:
                panic(f"unknown read {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Read":
        index = deserializer.deserialize_enum()
        if index > 20:
            raise deserializer.invalid_variant(index, 21)
        if index == 1:
            return ReadStatus()
        deserializer.deserialize_struct_variant()
        if index == 0:
            return BeginReadOnly(as_of=serde.Option(serde.U64).deserialize(deserializer))
        if index == 10:
            return Changes(since=deserializer.deserialize_u64())
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
            case 6:
                return ListTables(txn=txn)
            case 7:
                return GetStatistics(txn=txn, table=deserializer.deserialize_str())
            case 8:
                return GetView(txn=txn, view=deserializer.deserialize_str())
            case 9:
                return ListViews(txn=txn)
            case 11:
                return GetUser(txn=txn, user=deserializer.deserialize_str())
            case 12:
                return ListUsers(txn=txn)
            case 13:
                return ListMemberships(txn=txn)
            case 14:
                return ListGrants(txn=txn)
            case 15:
                return GetOwner(txn=txn, object=deserializer.deserialize_str())
            case 16:
                return GetChecks(txn=txn, table=deserializer.deserialize_str())
            case 17:
                return ListPolicies(txn=txn, table=deserializer.deserialize_str())
            case 18:
                return GetMatView(txn=txn, name=deserializer.deserialize_str())
            case 19:
                return GetMatViewRows(txn=txn, name=deserializer.deserialize_str())
            case _:
                return ListMatViews(txn=txn)


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


@dataclass(frozen=True)
class GetStatistics(Read):
    txn: mvcc.TransactionState
    table: str


@dataclass(frozen=True)
class GetView(Read):
    txn: mvcc.TransactionState
    view: str


@dataclass(frozen=True)
class ListViews(Read):
    txn: mvcc.TransactionState


@dataclass(frozen=True)
class Changes(Read):
    since: mvcc.Version


@dataclass(frozen=True)
class GetUser(Read):
    txn: mvcc.TransactionState
    user: str


@dataclass(frozen=True)
class ListUsers(Read):
    txn: mvcc.TransactionState


@dataclass(frozen=True)
class ListMemberships(Read):
    txn: mvcc.TransactionState


@dataclass(frozen=True)
class ListGrants(Read):
    txn: mvcc.TransactionState


@dataclass(frozen=True)
class GetOwner(Read):
    txn: mvcc.TransactionState
    object: str


@dataclass(frozen=True)
class GetChecks(Read):
    txn: mvcc.TransactionState
    table: str


@dataclass(frozen=True)
class ListPolicies(Read):
    txn: mvcc.TransactionState
    table: str


@dataclass(frozen=True)
class GetMatView(Read):
    txn: mvcc.TransactionState
    name: str


@dataclass(frozen=True)
class GetMatViewRows(Read):
    txn: mvcc.TransactionState
    name: str


@dataclass(frozen=True)
class ListMatViews(Read):
    txn: mvcc.TransactionState


Read.BeginReadOnly = BeginReadOnly
Read.Status = ReadStatus
Read.Get = Get
Read.LookupIndex = LookupIndex
Read.Scan = Scan
Read.GetTable = GetTable
Read.ListTables = ListTables
Read.GetStatistics = GetStatistics
Read.GetView = GetView
Read.ListViews = ListViews
Read.Changes = Changes
Read.GetUser = GetUser
Read.ListUsers = ListUsers
Read.ListMemberships = ListMemberships
Read.ListGrants = ListGrants
Read.GetOwner = GetOwner
Read.GetChecks = GetChecks
Read.ListPolicies = ListPolicies
Read.GetMatView = GetMatView
Read.GetMatViewRows = GetMatViewRows
Read.ListMatViews = ListMatViews


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
    SetStatistics: ClassVar[type["SetStatistics"]]
    CreateView: ClassVar[type["CreateView"]]
    DropView: ClassVar[type["DropView"]]
    CreateUser: ClassVar[type["CreateUser"]]
    DropUser: ClassVar[type["DropUser"]]
    SetMembership: ClassVar[type["SetMembership"]]
    SetGrant: ClassVar[type["SetGrant"]]
    SetOwner: ClassVar[type["SetOwner"]]
    SetChecks: ClassVar[type["SetChecks"]]
    CreatePolicy: ClassVar[type["CreatePolicy"]]
    DropPolicy: ClassVar[type["DropPolicy"]]
    CreateMatView: ClassVar[type["CreateMatView"]]
    RefreshMatView: ClassVar[type["RefreshMatView"]]
    DropMatView: ClassVar[type["DropMatView"]]

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
            case SetStatistics(txn, table, statistics):
                serializer.serialize_struct_variant(8)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                statistics.serialize(serializer)
            case CreateView(txn, view):
                serializer.serialize_struct_variant(9)
                txn.serialize(serializer)
                view.serialize(serializer)
            case DropView(txn, view, if_exists):
                serializer.serialize_struct_variant(10)
                txn.serialize(serializer)
                serializer.serialize_str(view)
                serializer.serialize_bool(if_exists)
            case CreateUser(txn, user):
                serializer.serialize_struct_variant(11)
                txn.serialize(serializer)
                user.serialize(serializer)
            case DropUser(txn, user):
                serializer.serialize_struct_variant(12)
                txn.serialize(serializer)
                serializer.serialize_str(user)
            case SetMembership(txn, member, user, is_member):
                serializer.serialize_struct_variant(13)
                txn.serialize(serializer)
                serializer.serialize_str(member)
                serializer.serialize_str(user)
                serializer.serialize_bool(is_member)
            case SetGrant(txn, object, grantee, privileges):
                serializer.serialize_struct_variant(14)
                txn.serialize(serializer)
                serializer.serialize_str(object)
                serializer.serialize_str(grantee)
                PRIVILEGES.serialize(privileges, serializer)
            case SetOwner(txn, object, owner):
                serializer.serialize_struct_variant(15)
                txn.serialize(serializer)
                serializer.serialize_str(object)
                serializer.serialize_str(owner)
            case SetChecks(txn, table, checks):
                serializer.serialize_struct_variant(16)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                CHECKS.serialize(checks, serializer)
            case CreatePolicy(txn, policy):
                serializer.serialize_struct_variant(17)
                txn.serialize(serializer)
                policy.serialize(serializer)
            case DropPolicy(txn, table, name):
                serializer.serialize_struct_variant(18)
                txn.serialize(serializer)
                serializer.serialize_str(table)
                serializer.serialize_str(name)
            case CreateMatView(txn, matview, rows):
                serializer.serialize_struct_variant(19)
                txn.serialize(serializer)
                matview.serialize(serializer)
                ROWS.serialize(rows, serializer)
            case RefreshMatView(txn, name, rows):
                serializer.serialize_struct_variant(20)
                txn.serialize(serializer)
                serializer.serialize_str(name)
                ROWS.serialize(rows, serializer)
            case DropMatView(txn, name):
                serializer.serialize_struct_variant(21)
                txn.serialize(serializer)
                serializer.serialize_str(name)
            case _:
                panic(f"unknown write {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Write":
        index = deserializer.deserialize_enum()
        if index > 21:
            raise deserializer.invalid_variant(index, 22)
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
            case 7:
                return DropTable(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    if_exists=deserializer.deserialize_bool(),
                )
            case 8:
                return SetStatistics(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    statistics=TableStatistics.deserialize(deserializer),
                )
            case 9:
                return CreateView(txn=txn, view=View.deserialize(deserializer))
            case 10:
                return DropView(
                    txn=txn,
                    view=deserializer.deserialize_str(),
                    if_exists=deserializer.deserialize_bool(),
                )
            case 11:
                return CreateUser(txn=txn, user=User.deserialize(deserializer))
            case 12:
                return DropUser(txn=txn, user=deserializer.deserialize_str())
            case 13:
                return SetMembership(
                    txn=txn,
                    member=deserializer.deserialize_str(),
                    user=deserializer.deserialize_str(),
                    is_member=deserializer.deserialize_bool(),
                )
            case 14:
                return SetGrant(
                    txn=txn,
                    object=deserializer.deserialize_str(),
                    grantee=deserializer.deserialize_str(),
                    privileges=PRIVILEGES.deserialize(deserializer),
                )
            case 15:
                return SetOwner(
                    txn=txn,
                    object=deserializer.deserialize_str(),
                    owner=deserializer.deserialize_str(),
                )
            case 16:
                return SetChecks(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    checks=CHECKS.deserialize(deserializer),
                )
            case 17:
                return CreatePolicy(txn=txn, policy=Policy.deserialize(deserializer))
            case 18:
                return DropPolicy(
                    txn=txn,
                    table=deserializer.deserialize_str(),
                    name=deserializer.deserialize_str(),
                )
            case 19:
                return CreateMatView(
                    txn=txn,
                    matview=MaterializedView.deserialize(deserializer),
                    rows=ROWS.deserialize(deserializer),
                )
            case 20:
                return RefreshMatView(
                    txn=txn,
                    name=deserializer.deserialize_str(),
                    rows=ROWS.deserialize(deserializer),
                )
            case _:
                return DropMatView(txn=txn, name=deserializer.deserialize_str())


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


@dataclass(frozen=True)
class SetStatistics(Write):
    txn: mvcc.TransactionState
    table: str
    statistics: TableStatistics


@dataclass(frozen=True)
class CreateView(Write):
    txn: mvcc.TransactionState
    view: View


@dataclass(frozen=True)
class DropView(Write):
    txn: mvcc.TransactionState
    view: str
    if_exists: bool


@dataclass(frozen=True)
class CreateUser(Write):
    txn: mvcc.TransactionState
    user: User


@dataclass(frozen=True)
class DropUser(Write):
    txn: mvcc.TransactionState
    user: str


@dataclass(frozen=True)
class SetMembership(Write):
    txn: mvcc.TransactionState
    member: str
    user: str
    is_member: bool


@dataclass(frozen=True)
class SetGrant(Write):
    txn: mvcc.TransactionState
    object: str
    grantee: str
    privileges: list[Privilege]


@dataclass(frozen=True)
class SetOwner(Write):
    txn: mvcc.TransactionState
    object: str
    owner: str


@dataclass(frozen=True)
class SetChecks(Write):
    txn: mvcc.TransactionState
    table: str
    checks: list[Check]


@dataclass(frozen=True)
class CreatePolicy(Write):
    txn: mvcc.TransactionState
    policy: Policy


@dataclass(frozen=True)
class DropPolicy(Write):
    txn: mvcc.TransactionState
    table: str
    name: str


@dataclass(frozen=True)
class CreateMatView(Write):
    txn: mvcc.TransactionState
    matview: MaterializedView
    rows: list[Row]


@dataclass(frozen=True)
class RefreshMatView(Write):
    txn: mvcc.TransactionState
    name: str
    rows: list[Row]


@dataclass(frozen=True)
class DropMatView(Write):
    txn: mvcc.TransactionState
    name: str


Write.Begin = Begin
Write.Commit = Commit
Write.Rollback = Rollback
Write.Delete = Delete
Write.Insert = Insert
Write.Update = Update
Write.CreateTable = CreateTable
Write.DropTable = DropTable
Write.SetStatistics = SetStatistics
Write.CreateView = CreateView
Write.DropView = DropView
Write.CreateUser = CreateUser
Write.DropUser = DropUser
Write.SetMembership = SetMembership
Write.SetGrant = SetGrant
Write.SetOwner = SetOwner
Write.SetChecks = SetChecks
Write.CreatePolicy = CreatePolicy
Write.DropPolicy = DropPolicy
Write.CreateMatView = CreateMatView
Write.RefreshMatView = RefreshMatView
Write.DropMatView = DropMatView


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
        super().__init__()
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

    def changes(self, since: mvcc.Version) -> list[Change]:
        return self.read(Read.Changes(since), CHANGES)


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

    def get_statistics(self, table: str) -> TableStatistics | None:
        read = Read.GetStatistics(txn=self._state, table=table)
        return self.raft.read(read, serde.Option(TableStatistics))

    def set_statistics(self, table: str, statistics: TableStatistics) -> None:
        write = Write.SetStatistics(txn=self._state, table=table, statistics=statistics)
        self.raft.write(write, serde.Unit)

    def create_view(self, view: View) -> None:
        self.raft.write(Write.CreateView(txn=self._state, view=view), serde.Unit)

    def drop_view(self, view: str, if_exists: bool) -> bool:
        write = Write.DropView(txn=self._state, view=view, if_exists=if_exists)
        return self.raft.write(write, serde.Bool)

    def get_view(self, view: str) -> View | None:
        return self.raft.read(Read.GetView(txn=self._state, view=view), serde.Option(View))

    def list_views(self) -> list[View]:
        return self.raft.read(Read.ListViews(txn=self._state), VIEWS)

    def create_user(self, user: User) -> None:
        self.raft.write(Write.CreateUser(txn=self._state, user=user), serde.Unit)

    def drop_user(self, user: str) -> None:
        self.raft.write(Write.DropUser(txn=self._state, user=user), serde.Unit)

    def get_user(self, user: str) -> User | None:
        return self.raft.read(Read.GetUser(txn=self._state, user=user), serde.Option(User))

    def list_users(self) -> list[User]:
        return self.raft.read(Read.ListUsers(txn=self._state), USERS)

    def set_membership(self, member: str, user: str, is_member: bool) -> None:
        write = Write.SetMembership(txn=self._state, member=member, user=user, is_member=is_member)
        self.raft.write(write, serde.Unit)

    def list_memberships(self) -> list[Membership]:
        return self.raft.read(Read.ListMemberships(txn=self._state), MEMBERSHIPS)

    def set_grant(self, object: str, grantee: str, privileges: list[Privilege]) -> None:
        write = Write.SetGrant(
            txn=self._state, object=object, grantee=grantee, privileges=privileges
        )
        self.raft.write(write, serde.Unit)

    def list_grants(self) -> list[Grant]:
        return self.raft.read(Read.ListGrants(txn=self._state), GRANTS)

    def set_owner(self, object: str, owner: str) -> None:
        write = Write.SetOwner(txn=self._state, object=object, owner=owner)
        self.raft.write(write, serde.Unit)

    def get_owner(self, object: str) -> str:
        return self.raft.read(Read.GetOwner(txn=self._state, object=object), serde.String)

    def set_checks(self, table: str, checks: list[Check]) -> None:
        write = Write.SetChecks(txn=self._state, table=table, checks=checks)
        self.raft.write(write, serde.Unit)

    def get_checks(self, table: str) -> list[Check]:
        return self.raft.read(Read.GetChecks(txn=self._state, table=table), CHECKS)

    def create_policy(self, policy: Policy) -> None:
        self.raft.write(Write.CreatePolicy(txn=self._state, policy=policy), serde.Unit)

    def drop_policy(self, table: str, name: str) -> bool:
        write = Write.DropPolicy(txn=self._state, table=table, name=name)
        return self.raft.write(write, serde.Bool)

    def list_policies(self, table: str) -> list[Policy]:
        read = Read.ListPolicies(txn=self._state, table=table)
        return self.raft.read(read, POLICIES)

    def create_matview(self, matview: MaterializedView, rows: list[Row]) -> None:
        write = Write.CreateMatView(txn=self._state, matview=matview, rows=rows)
        self.raft.write(write, serde.Unit)

    def refresh_matview(self, name: str, rows: list[Row]) -> None:
        write = Write.RefreshMatView(txn=self._state, name=name, rows=rows)
        self.raft.write(write, serde.Unit)

    def drop_matview(self, name: str) -> bool:
        return self.raft.write(Write.DropMatView(txn=self._state, name=name), serde.Bool)

    def get_matview(self, name: str) -> MaterializedView | None:
        read = Read.GetMatView(txn=self._state, name=name)
        return self.raft.read(read, serde.Option(MaterializedView))

    def get_matview_rows(self, name: str) -> list[Row]:
        return self.raft.read(Read.GetMatViewRows(txn=self._state, name=name), ROWS)

    def list_matviews(self) -> list[MaterializedView]:
        return self.raft.read(Read.ListMatViews(txn=self._state), MATVIEWS)


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
            case Write.SetStatistics(txn, table, statistics):
                self.local.resume(txn).set_statistics(table, statistics)
                return bincode.serialize(None, serde.Unit)
            case Write.CreateView(txn, view):
                return bincode.serialize(self.local.resume(txn).create_view(view), serde.Unit)
            case Write.DropView(txn, view, if_exists):
                existed = self.local.resume(txn).drop_view(view, if_exists)
                return bincode.serialize(existed, serde.Bool)

            case Write.CreateUser(txn, user):
                return bincode.serialize(self.local.resume(txn).create_user(user), serde.Unit)
            case Write.DropUser(txn, user):
                return bincode.serialize(self.local.resume(txn).drop_user(user), serde.Unit)
            case Write.SetMembership(txn, member, user, is_member):
                self.local.resume(txn).set_membership(member, user, is_member)
                return bincode.serialize(None, serde.Unit)
            case Write.SetGrant(txn, object, grantee, privileges):
                self.local.resume(txn).set_grant(object, grantee, privileges)
                return bincode.serialize(None, serde.Unit)
            case Write.SetOwner(txn, object, owner):
                self.local.resume(txn).set_owner(object, owner)
                return bincode.serialize(None, serde.Unit)
            case Write.SetChecks(txn, table, checks):
                self.local.resume(txn).set_checks(table, checks)
                return bincode.serialize(None, serde.Unit)
            case Write.CreatePolicy(txn, policy):
                self.local.resume(txn).create_policy(policy)
                return bincode.serialize(None, serde.Unit)
            case Write.DropPolicy(txn, table, name):
                existed = self.local.resume(txn).drop_policy(table, name)
                return bincode.serialize(existed, serde.Bool)

            case Write.CreateMatView(txn, matview, rows):
                self.local.resume(txn).create_matview(matview, rows)
                return bincode.serialize(None, serde.Unit)
            case Write.RefreshMatView(txn, name, rows):
                self.local.resume(txn).refresh_matview(name, rows)
                return bincode.serialize(None, serde.Unit)
            case Write.DropMatView(txn, name):
                existed = self.local.resume(txn).drop_matview(name)
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
            case Read.GetStatistics(txn, table):
                statistics = self.local.resume(txn).get_statistics(table)
                return bincode.serialize(statistics, serde.Option(TableStatistics))
            case Read.GetView(txn, view):
                definition = self.local.resume(txn).get_view(view)
                return bincode.serialize(definition, serde.Option(View))
            case Read.ListViews(txn):
                return bincode.serialize(self.local.resume(txn).list_views(), VIEWS)
            case Read.Changes(since):
                return bincode.serialize(self.local.changes(since), CHANGES)

            case Read.GetUser(txn, user):
                found = self.local.resume(txn).get_user(user)
                return bincode.serialize(found, serde.Option(User))
            case Read.ListUsers(txn):
                return bincode.serialize(self.local.resume(txn).list_users(), USERS)
            case Read.ListMemberships(txn):
                memberships = self.local.resume(txn).list_memberships()
                return bincode.serialize(memberships, MEMBERSHIPS)
            case Read.ListGrants(txn):
                return bincode.serialize(self.local.resume(txn).list_grants(), GRANTS)
            case Read.GetOwner(txn, object):
                owner = self.local.resume(txn).get_owner(object)
                return bincode.serialize(owner, serde.String)
            case Read.GetChecks(txn, table):
                checks = self.local.resume(txn).get_checks(table)
                return bincode.serialize(checks, CHECKS)
            case Read.ListPolicies(txn, table):
                policies = self.local.resume(txn).list_policies(table)
                return bincode.serialize(policies, POLICIES)

            case Read.GetMatView(txn, name):
                matview = self.local.resume(txn).get_matview(name)
                return bincode.serialize(matview, serde.Option(MaterializedView))
            case Read.GetMatViewRows(txn, name):
                rows = self.local.resume(txn).get_matview_rows(name)
                return bincode.serialize(rows, ROWS)
            case Read.ListMatViews(txn):
                return bincode.serialize(self.local.resume(txn).list_matviews(), MATVIEWS)
            case read:
                panic(f"unknown read {read!r}")
