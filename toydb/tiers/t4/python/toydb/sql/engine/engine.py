"""The SQL engine interfaces.

Mirrors `src/sql/engine/engine.rs`.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Generic, TypeVar

from toydb.error import errinput
from toydb.sql.engine.changes import Change
from toydb.sql.types import Expression, Row, Rows, Table, Value
from toydb.sql.types.statistics import TableStatistics
from toydb.sql.types.view import View
from toydb.storage import mvcc
from toydb.storage.btree import BTreeMap, BTreeSet

if TYPE_CHECKING:
    from toydb.sql.execution import Session


class Catalog(ABC):
    """The catalog stores table schema information. It must be implemented for
    Transaction, and is thus fully transactional. For simplicity, it only
    supports creating and dropping tables -- there are no ALTER TABLE schema
    changes, nor CREATE INDEX. It also stores the column statistics computed
    by ANALYZE."""

    @abstractmethod
    def create_table(self, table: Table) -> None:
        """Creates a new table. Errors if it already exists."""

    @abstractmethod
    def drop_table(self, table: str, if_exists: bool) -> bool:
        """Drops a table. Errors if it does not exist, unless if_exists is
        true. Returns true if the table existed and was deleted."""

    @abstractmethod
    def get_table(self, table: str) -> Table | None:
        """Fetches a table schema, or None if it doesn't exist."""

    @abstractmethod
    def list_tables(self) -> list[Table]:
        """Returns a list of all table schemas."""

    @abstractmethod
    def get_statistics(self, table: str) -> TableStatistics | None:
        """Fetches a table's column statistics, or None if the table hasn't
        been analyzed."""

    @abstractmethod
    def set_statistics(self, table: str, statistics: TableStatistics) -> None:
        """Stores a table's column statistics, replacing any existing ones.
        Errors if the table does not exist."""

    @abstractmethod
    def create_view(self, view: View) -> None:
        """Creates a view. Errors if a table or view of that name exists."""

    @abstractmethod
    def drop_view(self, view: str, if_exists: bool) -> bool:
        """Drops a view. Errors if it does not exist, unless if_exists is
        true. Returns true if the view existed."""

    @abstractmethod
    def get_view(self, view: str) -> View | None:
        """Fetches a view definition, or None if it doesn't exist."""

    @abstractmethod
    def list_views(self) -> list[View]:
        """Returns a list of all view definitions."""

    def must_get_table(self, table: str) -> Table:
        """Fetches a table schema, or errors if it does not exist."""
        if (schema := self.get_table(table)) is None:
            raise errinput(f"table {table} does not exist")
        return schema


class Transaction(Catalog):
    """A SQL transaction. Executes transactional CRUD operations on table rows.
    Provides snapshot isolation (see `storage.mvcc` module for details).

    All methods operate on row batches rather than single rows to amortize the
    cost. With the Raft engine, each call results in a Raft roundtrip, and we'd
    rather not have to do that for every single row that's modified.
    """

    @abstractmethod
    def state(self) -> mvcc.TransactionState:
        """The transaction's internal MVCC state."""

    @abstractmethod
    def commit(self) -> None:
        """Commits the transaction."""

    @abstractmethod
    def rollback(self) -> None:
        """Rolls back the transaction."""

    @abstractmethod
    def delete(self, table: str, ids: list[Value]) -> None:
        """Deletes table rows by primary key, if they exist."""

    @abstractmethod
    def get(self, table: str, ids: list[Value]) -> list[Row]:
        """Fetches table rows by primary key, if they exist."""

    @abstractmethod
    def insert(self, table: str, rows: list[Row]) -> None:
        """Inserts new table rows."""

    @abstractmethod
    def lookup_index(self, table: str, column: str, values: list[Value]) -> BTreeSet[Value]:
        """Looks up a set of primary keys by index values. BTreeSet for testing."""

    @abstractmethod
    def scan(self, table: str, filter: Expression | None) -> Rows:
        """Scans a table's rows, optionally applying the given filter."""

    @abstractmethod
    def update(self, table: str, rows: BTreeMap[Value, Row]) -> None:
        """Updates table rows by primary key. BTreeMap for testing."""


T = TypeVar("T", bound=Transaction)


class Engine(ABC, Generic[T]):
    """A SQL engine. This provides low-level CRUD (create, read, update,
    delete) operations for table rows, a schema catalog for accessing and
    modifying table schemas, and interactive SQL sessions that execute client
    SQL statements. All engine access is transactional with snapshot isolation.

    The type parameter is the engine's transaction type. This provides both
    row-level CRUD operations and transactional access to the schema catalog.
    """

    @abstractmethod
    def begin(self) -> T:
        """Begins a read-write transaction."""

    @abstractmethod
    def begin_read_only(self) -> T:
        """Begins a read-only transaction."""

    @abstractmethod
    def begin_as_of(self, version: mvcc.Version) -> T:
        """Begins a read-only transaction as of a historical version."""

    @abstractmethod
    def changes(self, since: mvcc.Version) -> list[Change]:
        """Returns the committed changes of transactions with a version above
        the given one, in commit order."""

    def session(self) -> "Session[T]":
        """Creates a client session for executing SQL statements."""
        from toydb.sql.execution import Session

        return Session(self)
