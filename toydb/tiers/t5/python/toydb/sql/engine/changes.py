"""The change log of `SHOW CHANGES`: the changes made by committed
transactions, in commit order.

The local engine records each change as its transaction makes it — rows
inserted, updated and deleted, tables and views created and dropped,
statistics stored — and appends a transaction's changes to the log when it
commits; a rollback discards them. The log is kept in memory, is not written
to storage, and starts empty when the engine is created. In a cluster each
node records the changes as it applies the Raft log, so all nodes have the
same log.
"""

import enum
from dataclasses import dataclass

from toydb import encoding
from toydb.encoding import json, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.sql.types import Label, Row, Table, Value
from toydb.sql.types.formats import to_json
from toydb.storage import mvcc

COLUMNS: list[Label] = [
    Label.Unqualified(name) for name in ("version", "table", "op", "key", "data")
]
"""The SHOW CHANGES result columns."""


class ChangeKind(enum.Enum):
    """The kind of a change."""

    Insert = 0
    Update = 1
    Delete = 2
    CreateTable = 3
    DropTable = 4
    Analyze = 5
    CreateView = 6
    DropView = 7

    def __str__(self) -> str:
        match self:
            case ChangeKind.Insert:
                return "insert"
            case ChangeKind.Update:
                return "update"
            case ChangeKind.Delete:
                return "delete"
            case ChangeKind.CreateTable:
                return "create_table"
            case ChangeKind.DropTable:
                return "drop_table"
            case ChangeKind.Analyze:
                return "analyze"
            case ChangeKind.CreateView:
                return "create_view"
            case ChangeKind.DropView:
                return "drop_view"

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_unit_variant(self.value)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "ChangeKind":
        index = deserializer.deserialize_enum()
        if index >= len(cls):
            raise deserializer.invalid_variant(index, len(cls))
        return cls(index)


@dataclass(frozen=True)
class Change(encoding.Value):
    """A change of the database: the table or view it concerns, its kind,
    and, for row changes, the primary key and the row."""

    version: mvcc.Version
    """The version of the transaction that made the change."""
    table: str
    """The table or view name."""
    kind: ChangeKind
    """The kind of change."""
    key: Value | None = None
    """The primary key, for row changes."""
    data: str | None = None
    """The data that SHOW CHANGES shows."""

    def row(self) -> Row:
        """Returns the change as a SHOW CHANGES row."""
        return [
            Value.Integer(self.version),
            Value.String(self.table),
            Value.String(str(self.kind)),
            self.key if self.key is not None else Value.Null(),
            Value.String(self.data) if self.data is not None else Value.Null(),
        ]

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u64(self.version)
        serializer.serialize_str(self.table)
        self.kind.serialize(serializer)
        serde.Option(Value).serialize(self.key, serializer)
        serde.Option(serde.String).serialize(self.data, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Change":
        deserializer.deserialize_struct()
        return cls(
            version=deserializer.deserialize_u64(),
            table=deserializer.deserialize_str(),
            kind=ChangeKind.deserialize(deserializer),
            key=serde.Option(Value).deserialize(deserializer),
            data=serde.Option(serde.String).deserialize(deserializer),
        )


def format_row(table: Table, row: Row) -> str:
    """Formats a row as a JSON object."""
    return json.encode({column.name: to_json(value) for column, value in zip(table.columns, row)})


def format_table(table: Table) -> str:
    """Formats a table's schema display on one line."""
    first, *columns, last = str(table).split("\n")
    return first + " ".join(column.strip() for column in columns) + last


class ChangeLog:
    """The change log: the committed changes in commit order, and the pending
    changes of open transactions by version."""

    def __init__(self) -> None:
        self.committed: list[Change] = []
        """The committed changes, in commit order."""
        self.pending: dict[mvcc.Version, list[Change]] = {}
        """The changes of open transactions, by version."""

    def record(self, change: Change) -> None:
        """Records a change of an open transaction."""
        self.pending.setdefault(change.version, []).append(change)

    def commit(self, version: mvcc.Version) -> None:
        """Appends the pending changes of a transaction to the log."""
        self.committed.extend(self.pending.pop(version, []))

    def rollback(self, version: mvcc.Version) -> None:
        """Discards the pending changes of a transaction."""
        self.pending.pop(version, None)

    def since(self, version: mvcc.Version) -> list[Change]:
        """Returns the committed changes of transactions with a version above
        the given one, in commit order."""
        return [change for change in self.committed if change.version > version]
