use std::collections::{BTreeMap, BTreeSet};
use std::sync::Mutex;

use super::Change;
use crate::errinput;
use crate::error::Result;
use crate::sql::execution::Session;
use crate::sql::execution::audit::{AuditLog, SessionList};
use crate::sql::types::{
    Check, Expression, MaterializedView, Policy, Privilege, Row, Rows, Table, TableStatistics, User,
    Value, View,
};
use crate::storage::mvcc;

/// A SQL engine. This provides low-level CRUD (create, read, update, delete)
/// operations for table rows, a schema catalog for accessing and modifying
/// table schemas, and interactive SQL sessions that execute client SQL
/// statements. All engine access is transactional with snapshot isolation.
pub trait Engine<'a>: Sized {
    /// The engine's transaction type. This provides both row-level CRUD operations and
    /// transactional access to the schema catalog.
    type Transaction: Transaction + 'a;

    /// Begins a read-write transaction.
    fn begin(&'a self) -> Result<Self::Transaction>;
    /// Begins a read-only transaction.
    fn begin_read_only(&'a self) -> Result<Self::Transaction>;
    /// Begins a read-only transaction as of a historical version.
    fn begin_as_of(&'a self, version: mvcc::Version) -> Result<Self::Transaction>;

    /// Returns the committed changes of transactions with a version above the
    /// given one, in commit order.
    fn changes(&'a self, since: mvcc::Version) -> Result<Vec<(mvcc::Version, Change)>>;

    /// Returns the audit log of the engine's sessions.
    fn audit(&'a self) -> &'a Mutex<AuditLog>;
    /// Returns the engine's open sessions.
    fn sessions(&'a self) -> &'a Mutex<SessionList>;

    /// Creates a client session for executing SQL statements.
    fn session(&'a self) -> Session<'a, Self> {
        Session::new(self)
    }
}

/// A SQL transaction. Executes transactional CRUD operations on table rows.
/// Provides snapshot isolation (see `storage::mvcc` module for details).
///
/// All methods operate on row batches rather than single rows to amortize the
/// cost. With the Raft engine, each call results in a Raft roundtrip, and we'd
/// rather not have to do that for every single row that's modified.
pub trait Transaction: Catalog {
    /// The transaction's internal MVCC state.
    fn state(&self) -> &mvcc::TransactionState;

    /// Commits the transaction.
    fn commit(self) -> Result<()>;
    /// Rolls back the transaction.
    fn rollback(self) -> Result<()>;

    /// Deletes table rows by primary key, if they exist.
    fn delete(&self, table: &str, ids: &[Value]) -> Result<()>;
    /// Fetches table rows by primary key, if they exist.
    fn get(&self, table: &str, ids: &[Value]) -> Result<Vec<Row>>;
    /// Inserts new table rows.
    fn insert(&self, table: &str, rows: Vec<Row>) -> Result<()>;
    /// Looks up a set of primary keys by index values. BTreeSet for testing.
    fn lookup_index(&self, table: &str, column: &str, values: &[Value]) -> Result<BTreeSet<Value>>;
    /// Scans a table's rows, optionally applying the given filter.
    fn scan(&self, table: &str, filter: Option<Expression>) -> Result<Rows>;
    /// Updates table rows by primary key. BTreeMap for testing.
    fn update(&self, table: &str, rows: BTreeMap<Value, Row>) -> Result<()>;
}

/// The catalog stores table schema information and column statistics. It must
/// be implemented for Transaction, and is thus fully transactional. For
/// simplicity, it only supports creating and dropping tables -- there are no
/// ALTER TABLE schema changes, nor CREATE INDEX.
pub trait Catalog {
    /// Creates a new table. Errors if it already exists.
    fn create_table(&self, table: Table) -> Result<()>;
    /// Drops a table. Errors if it does not exist, unless if_exists is true.
    /// Returns true if the table existed and was deleted.
    fn drop_table(&self, table: &str, if_exists: bool) -> Result<bool>;
    /// Fetches a table schema, or None if it doesn't exist.
    fn get_table(&self, table: &str) -> Result<Option<Table>>;
    /// Returns a list of all table schemas.
    fn list_tables(&self) -> Result<Vec<Table>>;

    /// Stores a table's column statistics, replacing any existing ones. They
    /// are removed when the table is dropped.
    fn set_table_statistics(&self, table: &str, statistics: TableStatistics) -> Result<()>;
    /// Fetches a table's column statistics, or None if the table hasn't been
    /// analyzed.
    fn get_table_statistics(&self, table: &str) -> Result<Option<TableStatistics>>;

    /// Creates a view. Errors if a table or view of that name exists.
    fn create_view(&self, view: View) -> Result<()>;
    /// Drops a view. Errors if it does not exist, unless if_exists is true.
    /// Returns true if the view existed.
    fn drop_view(&self, view: &str, if_exists: bool) -> Result<bool>;
    /// Fetches a view definition, or None if it doesn't exist.
    fn get_view(&self, view: &str) -> Result<Option<View>>;
    /// Returns a list of all view definitions.
    fn list_views(&self) -> Result<Vec<View>>;

    /// Creates a user.
    fn create_user(&self, user: User) -> Result<()>;
    /// Drops a user, its memberships and privileges.
    fn drop_user(&self, user: &str) -> Result<()>;
    /// Fetches a user, or None if it doesn't exist.
    fn get_user(&self, user: &str) -> Result<Option<User>>;
    /// Returns all users.
    fn list_users(&self) -> Result<Vec<User>>;
    /// Sets or removes a membership.
    fn set_membership(&self, member: &str, user: &str, is_member: bool) -> Result<()>;
    /// Returns all memberships.
    fn list_memberships(&self) -> Result<Vec<(String, String)>>;
    /// Sets the privileges granted to a user on an object.
    fn set_privileges(
        &self,
        object: &str,
        grantee: &str,
        privileges: BTreeSet<Privilege>,
    ) -> Result<()>;
    /// Returns all grants.
    fn list_grants(&self) -> Result<Vec<(String, String, BTreeSet<Privilege>)>>;
    /// Sets the owner of an object.
    fn set_owner(&self, object: &str, owner: &str) -> Result<()>;
    /// Fetches the owner of an object (admin if none is stored).
    fn get_owner(&self, object: &str) -> Result<String>;

    /// Sets the CHECK constraints of a table.
    fn set_checks(&self, table: &str, checks: Vec<Check>) -> Result<()>;
    /// Fetches the CHECK constraints of a table.
    fn get_checks(&self, table: &str) -> Result<Vec<Check>>;

    /// Creates a policy.
    fn create_policy(&self, policy: Policy) -> Result<()>;
    /// Drops a policy. Returns true if it existed.
    fn drop_policy(&self, table: &str, policy: &str) -> Result<bool>;
    /// Returns the policies of a table, by name.
    fn list_policies(&self, table: &str) -> Result<Vec<Policy>>;

    /// Creates a materialized view with its rows.
    fn create_materialized_view(&self, view: MaterializedView, rows: Vec<Row>) -> Result<()>;
    /// Replaces the rows of a materialized view.
    fn refresh_materialized_view(&self, view: &str, rows: Vec<Row>) -> Result<()>;
    /// Drops a materialized view. Returns true if it existed.
    fn drop_materialized_view(&self, view: &str) -> Result<bool>;
    /// Fetches a materialized view definition, or None if it doesn't exist.
    fn get_materialized_view(&self, view: &str) -> Result<Option<MaterializedView>>;
    /// Returns the stored rows of a materialized view.
    fn get_materialized_view_rows(&self, view: &str) -> Result<Vec<Row>>;
    /// Returns all materialized view definitions.
    fn list_materialized_views(&self) -> Result<Vec<MaterializedView>>;

    /// Fetches a table schema, or errors if it does not exist.
    fn must_get_table(&self, table: &str) -> Result<Table> {
        self.get_table(table)?.ok_or_else(|| errinput!("table {table} does not exist"))
    }
}
