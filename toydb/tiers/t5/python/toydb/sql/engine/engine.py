"""The SQL engine interfaces.

Mirrors `src/sql/engine/engine.rs`.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Generic, TypeVar

from toydb.error import errinput
from toydb.sql.engine.changes import Change
from toydb.sql.types import Expression, Row, Rows, Table, Value
from toydb.sql.types.check import Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.policy import Policy
from toydb.sql.types.statistics import TableStatistics
from toydb.sql.types.user import Grant, Membership, Privilege, User
from toydb.sql.types.view import View
from toydb.storage import mvcc
from toydb.storage.btree import BTreeMap, BTreeSet

if TYPE_CHECKING:
    from toydb.sql.execution import Session
    from toydb.sql.execution.audit import AuditLog, SessionList


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

    @abstractmethod
    def create_user(self, user: User) -> None:
        """Creates a user."""

    @abstractmethod
    def drop_user(self, user: str) -> None:
        """Drops a user, its memberships and privileges."""

    @abstractmethod
    def get_user(self, user: str) -> User | None:
        """Fetches a user, or None if it doesn't exist."""

    @abstractmethod
    def list_users(self) -> list[User]:
        """Returns all users."""

    @abstractmethod
    def set_membership(self, member: str, user: str, is_member: bool) -> None:
        """Sets or removes a membership."""

    @abstractmethod
    def list_memberships(self) -> list[Membership]:
        """Returns all memberships."""

    @abstractmethod
    def set_grant(self, object: str, grantee: str, privileges: list[Privilege]) -> None:
        """Sets the privileges granted to a user on an object."""

    @abstractmethod
    def list_grants(self) -> list[Grant]:
        """Returns all grants."""

    @abstractmethod
    def set_owner(self, object: str, owner: str) -> None:
        """Sets the owner of an object."""

    @abstractmethod
    def get_owner(self, object: str) -> str:
        """Fetches the owner of an object (admin if none is stored)."""

    @abstractmethod
    def set_checks(self, table: str, checks: list[Check]) -> None:
        """Sets the CHECK constraints of a table."""

    @abstractmethod
    def get_checks(self, table: str) -> list[Check]:
        """Fetches the CHECK constraints of a table."""

    @abstractmethod
    def create_policy(self, policy: Policy) -> None:
        """Creates a policy."""

    @abstractmethod
    def drop_policy(self, table: str, name: str) -> bool:
        """Drops a policy. Returns true if it existed."""

    @abstractmethod
    def list_policies(self, table: str) -> list[Policy]:
        """Returns the policies of a table, by name."""

    @abstractmethod
    def create_matview(self, matview: MaterializedView, rows: list[Row]) -> None:
        """Creates a materialized view with its rows."""

    @abstractmethod
    def refresh_matview(self, name: str, rows: list[Row]) -> None:
        """Replaces the rows of a materialized view."""

    @abstractmethod
    def drop_matview(self, name: str) -> bool:
        """Drops a materialized view. Returns true if it existed."""

    @abstractmethod
    def get_matview(self, name: str) -> MaterializedView | None:
        """Fetches a materialized view definition, or None if it doesn't
        exist."""

    @abstractmethod
    def get_matview_rows(self, name: str) -> list[Row]:
        """Returns the stored rows of a materialized view."""

    @abstractmethod
    def list_matviews(self) -> list[MaterializedView]:
        """Returns all materialized view definitions."""

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

    def __init__(self) -> None:
        # Imported here, since the execution package depends on this one.
        from toydb.sql.execution.audit import AuditLog, SessionList

        self.audit: mvcc.Mutex[AuditLog] = mvcc.Mutex(AuditLog())
        """The audit log of the server's sessions."""
        self.sessions: mvcc.Mutex[SessionList] = mvcc.Mutex(SessionList())
        """The server's open sessions."""

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
