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
from toydb.sql.engine.engine import Catalog
from toydb.sql.types import Column, Expression, Row, Rows, Table, Value
from toydb.sql.types.statistics import TableStatistics
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
            case index:
                raise deserializer.invalid_variant(index, 4)


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


Key.Table = KeyTable
Key.Index = KeyIndex
Key.Row = KeyRow
Key.Statistics = KeyStatistics


class KeyPrefix(encoding.Key):
    """Key prefixes, allowing prefix scans of specific parts of the keyspace.
    These must match the keys -- in particular, the enum variant indexes must
    match, since it's part of the encoded key."""

    Table: ClassVar[type["KeyPrefixTable"]]
    Index: ClassVar[type["KeyPrefixIndex"]]
    Row: ClassVar[type["KeyPrefixRow"]]

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
            case index:
                raise deserializer.invalid_variant(index, 3)


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


KeyPrefix.Table = KeyPrefixTable
KeyPrefix.Index = KeyPrefixIndex
KeyPrefix.Row = KeyPrefixRow


class Local(sql_engine.Engine["Transaction[E]"], Generic[E]):
    """A SQL engine using local storage. This provides the main SQL storage
    logic. The Raft SQL engine dispatches to this for node-local SQL storage,
    executing the same writes across each nodes' instance of `Local`."""

    def __init__(self, engine: E) -> None:
        """Creates a new local SQL engine using the given storage engine."""
        self.mvcc: mvcc.MVCC[E] = mvcc.MVCC(engine)
        """The local MVCC storage engine."""

    def resume(self, state: mvcc.TransactionState) -> "Transaction[E]":
        """Resumes a transaction from the given state. This is usually kept
        within `mvcc.Transaction`, but the Raft-based engine can't retain the
        MVCC transaction across requests since it may be executed on different
        leader nodes, so it instead keeps the state client-side in the
        session."""
        return Transaction(self.mvcc.resume(state))

    def get_unversioned(self, key: bytes) -> bytes | None:
        """Gets an unversioned key, or None if it doesn't exist."""
        return self.mvcc.get_unversioned(key)

    def set_unversioned(self, key: bytes, value: bytes) -> None:
        """Sets an unversioned key."""
        self.mvcc.set_unversioned(key, value)

    def begin(self) -> "Transaction[E]":
        return Transaction(self.mvcc.begin())

    def begin_read_only(self) -> "Transaction[E]":
        return Transaction(self.mvcc.begin_read_only())

    def begin_as_of(self, version: mvcc.Version) -> "Transaction[E]":
        return Transaction(self.mvcc.begin_as_of(version))


class Transaction(sql_engine.Transaction, Generic[E]):
    """A SQL transaction, wrapping an MVCC transaction."""

    def __init__(self, txn: mvcc.Transaction[E]) -> None:
        """Creates a new SQL transaction using the given MVCC transaction."""
        self.txn = txn

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
        self.txn.commit()

    def rollback(self) -> None:
        self.txn.rollback()

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

    def create_table(self, table: Table) -> None:
        if self.get_table(table.name) is not None:
            raise errinput(f"table {table.name} already exists")
        table.validate(self)
        self.txn.set(Key.Table(table.name).encode(), table.encode())

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
