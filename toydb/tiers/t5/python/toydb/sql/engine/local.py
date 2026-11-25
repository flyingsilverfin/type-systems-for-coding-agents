"""A SQL engine using local storage.

Mirrors `src/sql/engine/local.rs`.
"""

from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from toydb import encoding, storage
from toydb.encoding import bincode, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import errinput, panic
from toydb.fmt import debug_tuple
from toydb.iter import FilterMap, Map
from toydb.sql.engine import engine as sql_engine
from toydb.sql.engine.changes import Change, ChangeKind, ChangeLog, format_row, format_table
from toydb.sql.engine.engine import Catalog
from toydb.sql.types import Column, Expression, Row, Rows, Table, Value
from toydb.sql.types.check import CHECKS, Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.policy import Policy
from toydb.sql.types.statistics import TableStatistics
from toydb.sql.types.user import ADMIN, PRIVILEGES, Grant, Membership, Privilege, User
from toydb.sql.types.view import View
from toydb.storage import mvcc
from toydb.storage.btree import BTreeMap, BTreeSet

E = TypeVar("E", bound=storage.Engine)

ROW: serde.Serde[Row] = serde.Vec(Value)
"""The encoding of a table row, a `Vec<Value>`."""

INDEX: serde.Serde[list[Value]] = serde.Vec(Value)
"""The encoding of an index entry, a `BTreeSet<Value>` of primary keys. This
has the same encoding as a sorted `Vec<Value>`."""


class Key(encoding.Key):
    """SQL engine keys, using the Keycode order-preserving encoding. For
    simplicity, table and column names are used directly as identifiers,
    instead of e.g. numeric IDs. It is not possible to change table/column
    names, so this is fine, if somewhat inefficient."""

    Table: ClassVar[type["KeyTable"]]
    Index: ClassVar[type["KeyIndex"]]
    Row: ClassVar[type["KeyRow"]]
    Statistics: ClassVar[type["KeyStatistics"]]
    View: ClassVar[type["KeyView"]]
    User: ClassVar[type["KeyUser"]]
    Membership: ClassVar[type["KeyMembership"]]
    Grant: ClassVar[type["KeyGrant"]]
    Owner: ClassVar[type["KeyOwner"]]
    Checks: ClassVar[type["KeyChecks"]]
    Policy: ClassVar[type["KeyPolicy"]]
    MaterializedView: ClassVar[type["KeyMaterializedView"]]
    MaterializedViewRow: ClassVar[type["KeyMaterializedViewRow"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case KeyTable(name):
                serializer.serialize_newtype_variant(0)
                serializer.serialize_str(name)
            case KeyIndex(table, column, value):
                serializer.serialize_tuple_variant(1)
                serializer.serialize_str(table)
                serializer.serialize_str(column)
                value.serialize(serializer)
            case KeyRow(table, id):
                serializer.serialize_tuple_variant(2)
                serializer.serialize_str(table)
                id.serialize(serializer)
            case KeyStatistics(table):
                serializer.serialize_newtype_variant(3)
                serializer.serialize_str(table)
            case KeyView(name):
                serializer.serialize_newtype_variant(4)
                serializer.serialize_str(name)
            case KeyUser(name):
                serializer.serialize_newtype_variant(5)
                serializer.serialize_str(name)
            case KeyMembership(member, user):
                serializer.serialize_tuple_variant(6)
                serializer.serialize_str(member)
                serializer.serialize_str(user)
            case KeyGrant(object, grantee):
                serializer.serialize_tuple_variant(7)
                serializer.serialize_str(object)
                serializer.serialize_str(grantee)
            case KeyOwner(object):
                serializer.serialize_newtype_variant(8)
                serializer.serialize_str(object)
            case KeyChecks(table):
                serializer.serialize_newtype_variant(9)
                serializer.serialize_str(table)
            case KeyPolicy(table, name):
                serializer.serialize_tuple_variant(10)
                serializer.serialize_str(table)
                serializer.serialize_str(name)
            case KeyMaterializedView(name):
                serializer.serialize_newtype_variant(11)
                serializer.serialize_str(name)
            case KeyMaterializedViewRow(name, position):
                serializer.serialize_tuple_variant(12)
                serializer.serialize_str(name)
                serializer.serialize_u64(position)
            case _:
                panic(f"unknown key {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Key":
        match deserializer.deserialize_enum():
            case 0:
                return KeyTable(deserializer.deserialize_str())
            case 1:
                return KeyIndex(
                    deserializer.deserialize_str(),
                    deserializer.deserialize_str(),
                    Value.deserialize(deserializer),
                )
            case 2:
                return KeyRow(deserializer.deserialize_str(), Value.deserialize(deserializer))
            case 3:
                return KeyStatistics(deserializer.deserialize_str())
            case 4:
                return KeyView(deserializer.deserialize_str())
            case 5:
                return KeyUser(deserializer.deserialize_str())
            case 6:
                return KeyMembership(deserializer.deserialize_str(), deserializer.deserialize_str())
            case 7:
                return KeyGrant(deserializer.deserialize_str(), deserializer.deserialize_str())
            case 8:
                return KeyOwner(deserializer.deserialize_str())
            case 9:
                return KeyChecks(deserializer.deserialize_str())
            case 10:
                return KeyPolicy(deserializer.deserialize_str(), deserializer.deserialize_str())
            case 11:
                return KeyMaterializedView(deserializer.deserialize_str())
            case 12:
                name = deserializer.deserialize_str()
                return KeyMaterializedViewRow(name, deserializer.deserialize_u64())
            case index:
                raise deserializer.invalid_variant(index, 13)


@dataclass(frozen=True)
class KeyTable(Key):
    """A table schema, keyed by table name. The value is a `sql.types.Table`.
    This is `Key::Table` in Rust: use it as `Key.Table`."""

    name: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Table", [self.name], pretty)


@dataclass(frozen=True)
class KeyIndex(Key):
    """A column index entry, keyed by table name, column name, and index
    value. The value is a `BTreeSet` of `sql.types.Value` primary key values.
    This is `Key::Index` in Rust: use it as `Key.Index`."""

    table: str
    column: str
    value: Value

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Index", [self.table, self.column, self.value], pretty)


@dataclass(frozen=True)
class KeyRow(Key):
    """A table row, keyed by table name and primary key value. The value is a
    `sql.types.Row`. This is `Key::Row` in Rust: use it as `Key.Row`."""

    table: str
    id: Value

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Row", [self.table, self.id], pretty)


@dataclass(frozen=True)
class KeyStatistics(Key):
    """A table's column statistics, keyed by table name. The value is a
    `sql.types.statistics.TableStatistics`. This is `Key::Statistics` in
    Rust: use it as `Key.Statistics`."""

    table: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Statistics", [self.table], pretty)


@dataclass(frozen=True)
class KeyView(Key):
    """A view definition, by view name. This is `Key::View` in Rust: use it
    as `Key.View`."""

    name: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("View", [self.name], pretty)


@dataclass(frozen=True)
class KeyUser(Key):
    """A user, by name. This is `Key::User` in Rust: use it as `Key.User`."""

    name: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("User", [self.name], pretty)


@dataclass(frozen=True)
class KeyMembership(Key):
    """A membership, by member and user. This is `Key::Membership` in Rust:
    use it as `Key.Membership`."""

    member: str
    user: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Membership", [self.member, self.user], pretty)


@dataclass(frozen=True)
class KeyGrant(Key):
    """The privileges granted on an object to a user. This is `Key::Grant` in
    Rust: use it as `Key.Grant`."""

    object: str
    grantee: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Grant", [self.object, self.grantee], pretty)


@dataclass(frozen=True)
class KeyOwner(Key):
    """The owner of an object, if it isn't admin. This is `Key::Owner` in
    Rust: use it as `Key.Owner`."""

    object: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Owner", [self.object], pretty)


@dataclass(frozen=True)
class KeyChecks(Key):
    """A table's CHECK constraints, by table name. This is `Key::Checks` in
    Rust: use it as `Key.Checks`."""

    table: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Checks", [self.table], pretty)


@dataclass(frozen=True)
class KeyPolicy(Key):
    """A policy, by table and policy name. This is `Key::Policy` in Rust: use
    it as `Key.Policy`."""

    table: str
    name: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Policy", [self.table, self.name], pretty)


@dataclass(frozen=True)
class KeyMaterializedView(Key):
    """A materialized view definition, by name. This is
    `Key::MaterializedView` in Rust: use it as `Key.MaterializedView`."""

    name: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("MaterializedView", [self.name], pretty)


@dataclass(frozen=True)
class KeyMaterializedViewRow(Key):
    """A materialized view's stored row, by name and position. This is
    `Key::MaterializedViewRow` in Rust: use it as `Key.MaterializedViewRow`."""

    name: str
    position: int

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("MaterializedViewRow", [self.name, self.position], pretty)


Key.Table = KeyTable
Key.Index = KeyIndex
Key.Row = KeyRow
Key.Statistics = KeyStatistics
Key.View = KeyView
Key.User = KeyUser
Key.Membership = KeyMembership
Key.Grant = KeyGrant
Key.Owner = KeyOwner
Key.Checks = KeyChecks
Key.Policy = KeyPolicy
Key.MaterializedView = KeyMaterializedView
Key.MaterializedViewRow = KeyMaterializedViewRow


class KeyPrefix(encoding.Key):
    """Key prefixes, allowing prefix scans of specific parts of the keyspace.
    These must match the keys -- in particular, the enum variant indexes must
    match, since it's part of the encoded key."""

    Table: ClassVar[type["KeyPrefixTable"]]
    Index: ClassVar[type["KeyPrefixIndex"]]
    Row: ClassVar[type["KeyPrefixRow"]]
    Statistics: ClassVar[type["KeyPrefixStatistics"]]
    View: ClassVar[type["KeyPrefixView"]]
    User: ClassVar[type["KeyPrefixUser"]]
    Membership: ClassVar[type["KeyPrefixMembership"]]
    Grant: ClassVar[type["KeyPrefixGrant"]]
    Owner: ClassVar[type["KeyPrefixOwner"]]
    Checks: ClassVar[type["KeyPrefixChecks"]]
    Policy: ClassVar[type["KeyPrefixPolicy"]]
    MaterializedView: ClassVar[type["KeyPrefixMaterializedView"]]
    MaterializedViewRow: ClassVar[type["KeyPrefixMaterializedViewRow"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case KeyPrefixTable():
                serializer.serialize_unit_variant(0)
            case KeyPrefixIndex(table, column):
                serializer.serialize_tuple_variant(1)
                serializer.serialize_str(table)
                serializer.serialize_str(column)
            case KeyPrefixRow(table):
                serializer.serialize_newtype_variant(2)
                serializer.serialize_str(table)
            case KeyPrefixStatistics():
                serializer.serialize_unit_variant(3)
            case KeyPrefixView():
                serializer.serialize_unit_variant(4)
            case KeyPrefixUser():
                serializer.serialize_unit_variant(5)
            case KeyPrefixMembership():
                serializer.serialize_unit_variant(6)
            case KeyPrefixGrant():
                serializer.serialize_unit_variant(7)
            case KeyPrefixOwner():
                serializer.serialize_unit_variant(8)
            case KeyPrefixChecks():
                serializer.serialize_unit_variant(9)
            case KeyPrefixPolicy(table):
                serializer.serialize_newtype_variant(10)
                serializer.serialize_str(table)
            case KeyPrefixMaterializedView():
                serializer.serialize_unit_variant(11)
            case KeyPrefixMaterializedViewRow(name):
                serializer.serialize_newtype_variant(12)
                serializer.serialize_str(name)
            case _:
                panic(f"unknown key prefix {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "KeyPrefix":
        match deserializer.deserialize_enum():
            case 0:
                return KeyPrefixTable()
            case 1:
                return KeyPrefixIndex(deserializer.deserialize_str(), deserializer.deserialize_str())
            case 2:
                return KeyPrefixRow(deserializer.deserialize_str())
            case 3:
                return KeyPrefixStatistics()
            case 4:
                return KeyPrefixView()
            case 5:
                return KeyPrefixUser()
            case 6:
                return KeyPrefixMembership()
            case 7:
                return KeyPrefixGrant()
            case 8:
                return KeyPrefixOwner()
            case 9:
                return KeyPrefixChecks()
            case 10:
                return KeyPrefixPolicy(deserializer.deserialize_str())
            case 11:
                return KeyPrefixMaterializedView()
            case 12:
                return KeyPrefixMaterializedViewRow(deserializer.deserialize_str())
            case index:
                raise deserializer.invalid_variant(index, 13)


@dataclass(frozen=True)
class KeyPrefixTable(KeyPrefix):
    """All table schemas."""


@dataclass(frozen=True)
class KeyPrefixIndex(KeyPrefix):
    """All column index entries, keyed by table and column name."""

    table: str
    column: str


@dataclass(frozen=True)
class KeyPrefixRow(KeyPrefix):
    """All table rows, keyed by table name."""

    table: str


@dataclass(frozen=True)
class KeyPrefixStatistics(KeyPrefix):
    """All table statistics."""


@dataclass(frozen=True)
class KeyPrefixView(KeyPrefix):
    """All view definitions."""


@dataclass(frozen=True)
class KeyPrefixUser(KeyPrefix):
    """All users."""


@dataclass(frozen=True)
class KeyPrefixMembership(KeyPrefix):
    """All memberships."""


@dataclass(frozen=True)
class KeyPrefixGrant(KeyPrefix):
    """All grants."""


@dataclass(frozen=True)
class KeyPrefixOwner(KeyPrefix):
    """All owners."""


@dataclass(frozen=True)
class KeyPrefixChecks(KeyPrefix):
    """All CHECK constraints."""


@dataclass(frozen=True)
class KeyPrefixPolicy(KeyPrefix):
    """All policies of a table."""

    table: str


@dataclass(frozen=True)
class KeyPrefixMaterializedView(KeyPrefix):
    """All materialized view definitions."""


@dataclass(frozen=True)
class KeyPrefixMaterializedViewRow(KeyPrefix):
    """All stored rows of a materialized view."""

    name: str


KeyPrefix.Table = KeyPrefixTable
KeyPrefix.Index = KeyPrefixIndex
KeyPrefix.Row = KeyPrefixRow
KeyPrefix.Statistics = KeyPrefixStatistics
KeyPrefix.View = KeyPrefixView
KeyPrefix.User = KeyPrefixUser
KeyPrefix.Membership = KeyPrefixMembership
KeyPrefix.Grant = KeyPrefixGrant
KeyPrefix.Owner = KeyPrefixOwner
KeyPrefix.Checks = KeyPrefixChecks
KeyPrefix.Policy = KeyPrefixPolicy
KeyPrefix.MaterializedView = KeyPrefixMaterializedView
KeyPrefix.MaterializedViewRow = KeyPrefixMaterializedViewRow


class Local(sql_engine.Engine["Transaction[E]"], Generic[E]):
    """A SQL engine using local storage. This provides the main SQL storage
    logic. The Raft SQL engine dispatches to this for node-local SQL storage,
    executing the same writes across each nodes' instance of `Local`."""

    def __init__(self, engine: E) -> None:
        """Creates a new local SQL engine using the given storage engine."""
        super().__init__()
        self.mvcc: mvcc.MVCC[E] = mvcc.MVCC(engine)
        """The local MVCC storage engine."""
        self.change_log: mvcc.Mutex[ChangeLog] = mvcc.Mutex(ChangeLog())
        """The change log of committed changes, shared by the engine's
        transactions."""

    def resume(self, state: mvcc.TransactionState) -> "Transaction[E]":
        """Resumes a transaction from the given state. This is usually kept
        within `mvcc.Transaction`, but the Raft-based engine can't retain the
        MVCC transaction across requests since it may be executed on different
        leader nodes, so it instead keeps the state client-side in the
        session."""
        return Transaction(self.mvcc.resume(state), self.change_log)

    def get_unversioned(self, key: bytes) -> bytes | None:
        """Gets an unversioned key, or None if it doesn't exist."""
        return self.mvcc.get_unversioned(key)

    def set_unversioned(self, key: bytes, value: bytes) -> None:
        """Sets an unversioned key."""
        self.mvcc.set_unversioned(key, value)

    def begin(self) -> "Transaction[E]":
        return Transaction(self.mvcc.begin(), self.change_log)

    def begin_read_only(self) -> "Transaction[E]":
        return Transaction(self.mvcc.begin_read_only(), self.change_log)

    def begin_as_of(self, version: mvcc.Version) -> "Transaction[E]":
        return Transaction(self.mvcc.begin_as_of(version), self.change_log)

    def changes(self, since: mvcc.Version) -> list[Change]:
        with self.change_log.lock() as change_log:
            return change_log.since(since)


class Transaction(sql_engine.Transaction, Generic[E]):
    """A SQL transaction, wrapping an MVCC transaction."""

    def __init__(self, txn: mvcc.Transaction[E], change_log: mvcc.Mutex[ChangeLog]) -> None:
        """Creates a new SQL transaction using the given MVCC transaction and
        the engine's change log."""
        self.txn = txn
        self.change_log = change_log

    def get_index(self, table: str, column: str, value: Value) -> BTreeSet[Value]:
        """Fetches the matching primary keys for the given secondary index
        value, or an empty set if there is none."""
        assert self.has_index(table, column), f"no index on {table}.{column}"
        encoded = self.txn.get(Key.Index(table, column, value).encode())
        if encoded is None:
            return BTreeSet()
        return BTreeSet(bincode.deserialize(INDEX, encoded))

    def get_row(self, table: str, id: Value) -> Row | None:
        """Fetches a single row by primary key, or None if it doesn't exist."""
        encoded = self.txn.get(Key.Row(table, id).encode())
        if encoded is None:
            return None
        return bincode.deserialize(ROW, encoded)

    def record(
        self, table: str, kind: ChangeKind, key: Value | None = None, data: str | None = None
    ) -> None:
        """Records a change made by the transaction in the change log."""
        change = Change(self.txn.state().version, table, kind, key, data)
        with self.change_log.lock() as change_log:
            change_log.record(change)

    def delete_privileges(self, object: str) -> None:
        """Deletes the owner of a dropped table or view and the privileges
        granted on it, if any."""
        key = Key.Owner(object).encode()
        if self.txn.get(key) is not None:
            self.txn.delete(key)
        for grant in self.list_grants():
            if grant.object == object:
                self.txn.delete(Key.Grant(object, grant.grantee).encode())

    def has_index(self, table: str, column: str) -> bool:
        """Returns true if a secondary index exists for the given column."""
        schema = self.must_get_table(table)
        return next((c.index for c in schema.columns if c.name == column), False)

    def set_index(self, table: str, column: str, value: Value, ids: BTreeSet[Value]) -> None:
        """Stores a secondary index entry for the given column value,
        replacing the existing entry if any."""
        assert self.has_index(table, column), f"no index on {table}.{column}"
        key = Key.Index(table, column, value).encode()
        if ids.is_empty():
            self.txn.delete(key)
        else:
            self.txn.set(key, bincode.serialize(list(ids), INDEX))

    def table_references(self, table: str) -> list[tuple[Table, list[int]]]:
        """Returns all tables referencing a table, as (table, column index)
        pairs. This includes any references from the table itself."""
        result: list[tuple[Table, list[int]]] = []
        for t in self.list_tables():
            references = [i for i, c in enumerate(t.columns) if c.references == table]
            if references:
                result.append((t, references))
        return result

    def state(self) -> mvcc.TransactionState:
        return self.txn.state()

    def commit(self) -> None:
        # Read-only transactions have the version of the next read-write
        # transaction, so they must not commit its changes. Hold the change
        # log lock across the commit, to keep the log in commit order.
        state = self.txn.state()
        if state.read_only:
            self.txn.commit()
            return
        with self.change_log.lock() as change_log:
            self.txn.commit()
            change_log.commit(state.version)

    def rollback(self) -> None:
        state = self.txn.state()
        if state.read_only:
            self.txn.rollback()
            return
        self.txn.rollback()
        with self.change_log.lock() as change_log:
            change_log.rollback(state.version)

    def delete(self, table: str, ids: list[Value]) -> None:
        schema = self.must_get_table(table)
        indexes = [(i, c) for i, c in enumerate(schema.columns) if c.index]

        # Check for foreign key references to the deleted rows.
        for source, refs in self.table_references(schema.name):
            self_reference = source.name == schema.name
            for i in refs:
                column = source.columns[i]
                if i == source.primary_key:
                    # If the reference is from a primary key column, do a lookup.
                    source_ids = BTreeSet(row[i] for row in self.get(source.name, ids))
                else:
                    # Otherwise (commonly), do a secondary index lookup.
                    # All foreign keys have a secondary index.
                    source_ids = self.lookup_index(source.name, column.name, ids)
                # We can ignore any references between the deleted rows,
                # including a row referencing itself.
                if self_reference:
                    for id in ids:
                        source_ids.remove(id)
                # Error if the delete would violate referential integrity.
                if (source_id := source_ids.first()) is not None:
                    pk = source.columns[source.primary_key].name
                    raise errinput(f"row referenced by {source.name}.{pk}={source_id}")

        for id in ids:
            # Update any secondary index entries.
            if indexes and (row := self.get_row(schema.name, id)) is not None:
                for i, column in indexes:
                    index = self.get_index(schema.name, column.name, row[i])
                    index.remove(id)
                    self.set_index(schema.name, column.name, row[i], index)

            # Delete the row.
            self.txn.delete(Key.Row(schema.name, id).encode())
            self.record(schema.name, ChangeKind.Delete, id)

    def get(self, table: str, ids: list[Value]) -> list[Row]:
        return [row for id in ids if (row := self.get_row(table, id)) is not None]

    def insert(self, table: str, rows: list[Row]) -> None:
        schema = self.must_get_table(table)
        for row in rows:
            # Insert the row.
            schema.validate_row(row, False, self)
            id = row[schema.primary_key]
            self.txn.set(Key.Row(schema.name, id).encode(), bincode.serialize(row, ROW))

            # Update any secondary index entries.
            for i, column in enumerate(schema.columns):
                if not column.index:
                    continue
                index = self.get_index(schema.name, column.name, row[i])
                index.insert(id)
                self.set_index(schema.name, column.name, row[i], index)

            self.record(schema.name, ChangeKind.Insert, id, format_row(schema, row))

    def lookup_index(self, table: str, column: str, values: list[Value]) -> BTreeSet[Value]:
        assert self.has_index(table, column), f"no index on {table}.{column}"
        ids: BTreeSet[Value] = BTreeSet()
        for value in values:
            ids.extend(self.get_index(table, column, value))
        return ids

    def scan(self, table: str, filter: Expression | None) -> Rows:
        def decode(item: tuple[bytes, bytes]) -> Row:
            return bincode.deserialize(ROW, item[1])

        rows = Map(self.txn.scan_prefix(KeyPrefix.Row(table).encode()), decode)
        if filter is None:
            return rows

        def predicate(row: Row) -> Row | None:
            match filter.evaluate(row):
                case Value.Boolean(True):
                    return row
                case Value.Boolean(False) | Value.Null():
                    return None
                case value:
                    raise errinput(f"filter returned {value}, expected boolean")

        return FilterMap(rows, predicate)

    def update(self, table: str, rows: BTreeMap[Value, Row]) -> None:
        schema = self.must_get_table(table)
        for id, row in rows:
            # If the primary key changes, we simply do a delete and insert.
            # This simplifies constraint validation.
            if id != row[schema.primary_key]:
                self.delete(schema.name, [id])
                self.insert(schema.name, [row])
                continue

            # Validate the row, but don't write it yet since we may need to
            # read the existing value to update secondary indexes.
            schema.validate_row(row, True, self)

            # Update indexes, knowing that the primary key has not changed.
            indexes: list[tuple[int, Column]] = [
                (i, c) for i, c in enumerate(schema.columns) if c.index
            ]
            if indexes:
                old = self.get(schema.name, [id]).pop(0)
                for i, column in indexes:
                    # If the value didn't change, we don't have to do anything.
                    if old[i] == row[i]:
                        continue

                    # Remove the old value from the index entry.
                    index = self.get_index(schema.name, column.name, old[i])
                    index.remove(id)
                    self.set_index(schema.name, column.name, old[i], index)

                    # Insert the new value into the index entry.
                    index = self.get_index(schema.name, column.name, row[i])
                    index.insert(id)
                    self.set_index(schema.name, column.name, row[i], index)

            # Update the row.
            self.txn.set(Key.Row(schema.name, id).encode(), bincode.serialize(row, ROW))
            self.record(schema.name, ChangeKind.Update, id, format_row(schema, row))

    def create_table(self, table: Table) -> None:
        table.validate_create(self)
        self.txn.set(Key.Table(table.name).encode(), table.encode())
        self.record(table.name, ChangeKind.CreateTable, data=format_table(table))

    def drop_table(self, table: str, if_exists: bool) -> bool:
        schema = self.get_table(table)
        if schema is None:
            if if_exists:
                return False
            raise errinput(f"table {table} does not exist")

        # Check for foreign key references.
        for source, refs in self.table_references(schema.name):
            if source.name != schema.name:
                column = source.columns[refs[0]].name
                raise errinput(f"table {schema.name} is referenced from {source.name}.{column}")

        # Check for views and materialized views that depend on the table.
        from toydb.sql.execution import matviews, views

        views.check_table_dependencies(self, schema.name)
        matviews.check_table_dependencies(self, schema.name)

        # Delete the table schema entry.
        self.txn.delete(Key.Table(schema.name).encode())

        # Delete the table rows.
        prefix = KeyPrefix.Row(schema.name).encode()
        for key, _ in self.txn.scan_prefix(prefix):
            self.txn.delete(key)

        # Delete any secondary index entries.
        for column in schema.columns:
            if not column.index:
                continue
            prefix = KeyPrefix.Index(schema.name, column.name).encode()
            for key, _ in self.txn.scan_prefix(prefix):
                self.txn.delete(key)

        # Delete the table statistics, if the table was analyzed.
        key = Key.Statistics(schema.name).encode()
        if self.txn.get(key) is not None:
            self.txn.delete(key)

        # Delete the table's owner and the privileges granted on it.
        self.delete_privileges(schema.name)

        # Delete the table's CHECK constraints, if it has any.
        key = Key.Checks(schema.name).encode()
        if self.txn.get(key) is not None:
            self.txn.delete(key)

        # Delete the table's policies, if it has any.
        for key, _ in self.txn.scan_prefix(KeyPrefix.Policy(schema.name).encode()):
            self.txn.delete(key)
        self.record(schema.name, ChangeKind.DropTable)
        return True

    def get_table(self, table: str) -> Table | None:
        encoded = self.txn.get(Key.Table(table).encode())
        if encoded is None:
            return None
        return Table.decode(encoded)

    def list_tables(self) -> list[Table]:
        return [Table.decode(value) for _, value in self.txn.scan_prefix(KeyPrefix.Table().encode())]

    def get_statistics(self, table: str) -> TableStatistics | None:
        encoded = self.txn.get(Key.Statistics(table).encode())
        if encoded is None:
            return None
        return TableStatistics.decode(encoded)

    def set_statistics(self, table: str, statistics: TableStatistics) -> None:
        schema = self.must_get_table(table)
        self.txn.set(Key.Statistics(schema.name).encode(), statistics.encode())
        self.record(schema.name, ChangeKind.Analyze)

    def create_view(self, view: View) -> None:
        if self.get_table(view.name) is not None:
            raise errinput(f"table {view.name} already exists")
        if self.get_view(view.name) is not None:
            raise errinput(f"view {view.name} already exists")
        if self.get_matview(view.name) is not None:
            raise errinput(f"materialized view {view.name} already exists")
        self.txn.set(Key.View(view.name).encode(), view.encode())
        self.record(view.name, ChangeKind.CreateView, data=str(view))

    def drop_view(self, view: str, if_exists: bool) -> bool:
        if self.get_view(view) is None:
            if if_exists:
                return False
            raise errinput(f"view {view} does not exist")
        self.txn.delete(Key.View(view).encode())
        self.delete_privileges(view)
        self.record(view, ChangeKind.DropView)
        return True

    def get_view(self, view: str) -> View | None:
        encoded = self.txn.get(Key.View(view).encode())
        if encoded is None:
            return None
        return View.decode(encoded)

    def list_views(self) -> list[View]:
        return [View.decode(value) for _, value in self.txn.scan_prefix(KeyPrefix.View().encode())]

    def create_user(self, user: User) -> None:
        if self.get_user(user.name) is not None:
            raise errinput(f"user {user.name} already exists")
        self.txn.set(Key.User(user.name).encode(), user.encode())

    def drop_user(self, user: str) -> None:
        self.txn.delete(Key.User(user).encode())
        for member, group in self.list_memberships():
            if user in (member, group):
                self.txn.delete(Key.Membership(member, group).encode())
        for grant in self.list_grants():
            if grant.grantee == user:
                self.txn.delete(Key.Grant(grant.object, user).encode())

    def get_user(self, user: str) -> User | None:
        if user == ADMIN:
            return User.admin()
        encoded = self.txn.get(Key.User(user).encode())
        if encoded is None:
            return None
        return User.decode(encoded)

    def list_users(self) -> list[User]:
        prefix = KeyPrefix.User().encode()
        users = [User.decode(value) for _, value in self.txn.scan_prefix(prefix)]
        return sorted([User.admin(), *users], key=lambda user: user.name)

    def set_membership(self, member: str, user: str, is_member: bool) -> None:
        key = Key.Membership(member, user).encode()
        if is_member:
            self.txn.set(key, b"")
        else:
            self.txn.delete(key)

    def list_memberships(self) -> list[Membership]:
        memberships: list[Membership] = []
        for key, _ in self.txn.scan_prefix(KeyPrefix.Membership().encode()):
            match Key.decode(key):
                case Key.Membership(member, user):
                    memberships.append((member, user))
                case decoded:
                    panic(f"unexpected key {decoded!r}")
        return memberships

    def set_grant(self, object: str, grantee: str, privileges: list[Privilege]) -> None:
        key = Key.Grant(object, grantee).encode()
        if privileges:
            self.txn.set(key, bincode.serialize(sorted(set(privileges)), PRIVILEGES))
        else:
            self.txn.delete(key)

    def list_grants(self) -> list[Grant]:
        grants: list[Grant] = []
        for key, value in self.txn.scan_prefix(KeyPrefix.Grant().encode()):
            match Key.decode(key):
                case Key.Grant(object, grantee):
                    privileges = bincode.deserialize(PRIVILEGES, value)
                    grants.append(Grant(object, grantee, privileges))
                case decoded:
                    panic(f"unexpected key {decoded!r}")
        return grants

    def set_owner(self, object: str, owner: str) -> None:
        key = Key.Owner(object).encode()
        if owner == ADMIN:
            self.txn.delete(key)
        else:
            self.txn.set(key, bincode.serialize(owner, serde.String))

    def get_owner(self, object: str) -> str:
        encoded = self.txn.get(Key.Owner(object).encode())
        if encoded is None:
            return ADMIN
        return bincode.deserialize(serde.String, encoded)

    def set_checks(self, table: str, checks: list[Check]) -> None:
        key = Key.Checks(table).encode()
        if checks:
            self.txn.set(key, bincode.serialize(checks, CHECKS))
        elif self.txn.get(key) is not None:
            self.txn.delete(key)

    def get_checks(self, table: str) -> list[Check]:
        encoded = self.txn.get(Key.Checks(table).encode())
        if encoded is None:
            return []
        return bincode.deserialize(CHECKS, encoded)

    def create_policy(self, policy: Policy) -> None:
        self.txn.set(Key.Policy(policy.table, policy.name).encode(), policy.encode())

    def drop_policy(self, table: str, name: str) -> bool:
        key = Key.Policy(table, name).encode()
        if self.txn.get(key) is None:
            return False
        self.txn.delete(key)
        return True

    def list_policies(self, table: str) -> list[Policy]:
        prefix = KeyPrefix.Policy(table).encode()
        return [Policy.decode(value) for _, value in self.txn.scan_prefix(prefix)]

    def create_matview(self, matview: MaterializedView, rows: list[Row]) -> None:
        self.txn.set(Key.MaterializedView(matview.name).encode(), matview.encode())
        self.set_matview_rows(matview.name, rows)

    def refresh_matview(self, name: str, rows: list[Row]) -> None:
        self.delete_matview_rows(name)
        self.set_matview_rows(name, rows)

    def drop_matview(self, name: str) -> bool:
        key = Key.MaterializedView(name).encode()
        if self.txn.get(key) is None:
            return False
        self.txn.delete(key)
        self.delete_matview_rows(name)
        self.delete_privileges(name)
        return True

    def get_matview(self, name: str) -> MaterializedView | None:
        encoded = self.txn.get(Key.MaterializedView(name).encode())
        if encoded is None:
            return None
        return MaterializedView.decode(encoded)

    def get_matview_rows(self, name: str) -> list[Row]:
        prefix = KeyPrefix.MaterializedViewRow(name).encode()
        return [bincode.deserialize(ROW, value) for _, value in self.txn.scan_prefix(prefix)]

    def list_matviews(self) -> list[MaterializedView]:
        prefix = KeyPrefix.MaterializedView().encode()
        return [MaterializedView.decode(value) for _, value in self.txn.scan_prefix(prefix)]

    def set_matview_rows(self, name: str, rows: list[Row]) -> None:
        """Stores the rows of a materialized view, by position."""
        for position, row in enumerate(rows):
            key = Key.MaterializedViewRow(name, position).encode()
            self.txn.set(key, bincode.serialize(row, ROW))

    def delete_matview_rows(self, name: str) -> None:
        """Deletes the stored rows of a materialized view."""
        for key, _ in self.txn.scan_prefix(KeyPrefix.MaterializedViewRow(name).encode()):
            self.txn.delete(key)
