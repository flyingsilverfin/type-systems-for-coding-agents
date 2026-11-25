use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::slice;
use std::sync::{Arc, Mutex};

use itertools::Itertools as _;
use serde::{Deserialize, Serialize};

use super::{Catalog, Change, ChangeLog};
use crate::encoding::{self, Key as _, Value as _, bincode};
use crate::error::Result;
use crate::sql::execution::audit::{AuditLog, SessionList};
use crate::sql::execution::{json_line, matviews, views};
use crate::sql::types::{
    ADMIN, Check, Expression, MaterializedView, Policy, Privilege, Row, Rows, Table,
    TableStatistics, User, Value, View,
};
use crate::storage::{self, mvcc};
use crate::{errdata, errinput};

/// SQL engine keys, using the Keycode order-preserving encoding. For
/// simplicity, table and column names are used directly as identifiers, instead
/// of e.g. numeric IDs. It is not possible to change table/column names, so
/// this is fine, if somewhat inefficient.
///
/// Uses Cow to allow encoding borrowed values but decoding owned values.
#[derive(Debug, Deserialize, Serialize)]
pub enum Key<'a> {
    /// A table schema, keyed by table name. The value is a `sql::types::Table`.
    Table(Cow<'a, str>),
    /// A column index entry, keyed by table name, column name, and index value.
    /// The value is a `BTreeSet` of `sql::types::Value` primary key values.
    Index(Cow<'a, str>, Cow<'a, str>, Cow<'a, Value>),
    /// A table row, keyed by table name and primary key value. The value is a
    /// `sql::types::Row`.
    Row(Cow<'a, str>, Cow<'a, Value>),
    /// A table's column statistics, keyed by table name. The value is a
    /// `sql::types::TableStatistics`. Only exists if the table was analyzed.
    Statistics(Cow<'a, str>),
    /// A view definition, by view name.
    View(Cow<'a, str>),
    /// A user, by name.
    User(Cow<'a, str>),
    /// A membership, by member and user.
    Membership(Cow<'a, str>, Cow<'a, str>),
    /// The privileges granted on an object to a user.
    Grant(Cow<'a, str>, Cow<'a, str>),
    /// The owner of an object, if it isn't admin.
    Owner(Cow<'a, str>),
    /// A table's CHECK constraints, by table name.
    Checks(Cow<'a, str>),
    /// A policy, by table and policy name.
    Policy(Cow<'a, str>, Cow<'a, str>),
    /// A materialized view definition, by name.
    MaterializedView(Cow<'a, str>),
    /// A materialized view's stored row, by name and position.
    MaterializedViewRow(Cow<'a, str>, u64),
}

impl<'a> encoding::Key<'a> for Key<'a> {}

/// Key prefixes, allowing prefix scans of specific parts of the keyspace. These
/// must match the keys -- in particular, the enum variant indexes must match,
/// since it's part of the encoded key.
#[derive(Deserialize, Serialize)]
enum KeyPrefix<'a> {
    /// All table schemas.
    Table,
    /// All column index entries, keyed by table and column name.
    Index(Cow<'a, str>, Cow<'a, str>),
    /// All table rows, keyed by table name.
    Row(Cow<'a, str>),
    /// All table statistics.
    Statistics,
    /// All view definitions.
    View,
    /// All users.
    User,
    /// All memberships.
    Membership,
    /// All grants.
    Grant,
    /// All owners.
    Owner,
    /// All CHECK constraints.
    Checks,
    /// All policies of a table.
    Policy(Cow<'a, str>),
    /// All materialized view definitions.
    MaterializedView,
    /// All stored rows of a materialized view.
    MaterializedViewRow(Cow<'a, str>),
}

impl<'a> encoding::Key<'a> for KeyPrefix<'a> {}

/// A SQL engine using local storage. This provides the main SQL storage logic.
/// The Raft SQL engine dispatches to this for node-local SQL storage, executing
/// the same writes across each nodes' instance of `Local`.
pub struct Local<E: storage::Engine + 'static> {
    /// The local MVCC storage engine.
    pub mvcc: mvcc::MVCC<E>,
    /// The change log of committed changes, shared by the engine's transactions.
    changes: Arc<Mutex<ChangeLog>>,
    /// The audit log of the server's sessions.
    audit: Mutex<AuditLog>,
    /// The server's open sessions.
    sessions: Mutex<SessionList>,
}

impl<E: storage::Engine> Local<E> {
    /// Creates a new local SQL engine using the given storage engine.
    pub fn new(engine: E) -> Self {
        Self {
            mvcc: mvcc::MVCC::new(engine),
            changes: Arc::new(Mutex::new(ChangeLog::new())),
            audit: Mutex::new(AuditLog::new()),
            sessions: Mutex::new(SessionList::new()),
        }
    }

    /// Resumes a transaction from the given state. This is usually kept within
    /// `mvcc::Transaction`, but the Raft-based engine can't retain the MVCC
    /// transaction across requests since it may be executed on different leader
    /// nodes, so it instead keeps the state client-side in the session.
    pub fn resume(&self, state: mvcc::TransactionState) -> Result<Transaction<E>> {
        Ok(Transaction::new(self.mvcc.resume(state)?, self.changes.clone()))
    }

    /// Gets an unversioned key, or None if it doesn't exist.
    pub fn get_unversioned(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        self.mvcc.get_unversioned(key)
    }

    /// Sets an unversioned key.
    pub fn set_unversioned(&self, key: &[u8], value: Vec<u8>) -> Result<()> {
        self.mvcc.set_unversioned(key, value)
    }
}

impl<E: storage::Engine> super::Engine<'_> for Local<E> {
    type Transaction = Transaction<E>;

    fn begin(&self) -> Result<Self::Transaction> {
        Ok(Self::Transaction::new(self.mvcc.begin()?, self.changes.clone()))
    }

    fn begin_read_only(&self) -> Result<Self::Transaction> {
        Ok(Self::Transaction::new(self.mvcc.begin_read_only()?, self.changes.clone()))
    }

    fn begin_as_of(&self, version: mvcc::Version) -> Result<Self::Transaction> {
        Ok(Self::Transaction::new(self.mvcc.begin_as_of(version)?, self.changes.clone()))
    }

    fn changes(&self, since: mvcc::Version) -> Result<Vec<(mvcc::Version, Change)>> {
        Ok(self.changes.lock()?.since(since))
    }

    fn audit(&self) -> &Mutex<AuditLog> {
        &self.audit
    }

    fn sessions(&self) -> &Mutex<SessionList> {
        &self.sessions
    }
}

/// A SQL transaction, wrapping an MVCC transaction.
pub struct Transaction<E: storage::Engine + 'static> {
    txn: mvcc::Transaction<E>,
    /// The engine's change log, which records the transaction's changes.
    changes: Arc<Mutex<ChangeLog>>,
}

impl<E: storage::Engine> Transaction<E> {
    /// Creates a new SQL transaction using the given MVCC transaction and the
    /// engine's change log.
    fn new(txn: mvcc::Transaction<E>, changes: Arc<Mutex<ChangeLog>>) -> Self {
        Self { txn, changes }
    }

    /// Records a change made by the transaction in the change log.
    fn record(&self, change: Change) -> Result<()> {
        self.changes.lock()?.record(self.txn.version(), change);
        Ok(())
    }

    /// Returns the transaction's internal state.
    pub fn state(&self) -> &mvcc::TransactionState {
        self.txn.state()
    }

    /// Fetches the matching primary keys for the given secondary index value,
    /// or an empty set if there is none.
    fn get_index(&self, table: &str, column: &str, value: &Value) -> Result<BTreeSet<Value>> {
        debug_assert!(self.has_index(table, column)?, "no index on {table}.{column}");
        Ok(self
            .txn
            .get(&Key::Index(table.into(), column.into(), value.into()).encode())?
            .map(|v| BTreeSet::decode(&v))
            .transpose()?
            .unwrap_or_default())
    }

    /// Fetches a single row by primary key, or None if it doesn't exist.
    fn get_row(&self, table: &str, id: &Value) -> Result<Option<Row>> {
        self.txn
            .get(&Key::Row(table.into(), id.into()).encode())?
            .map(|v| Row::decode(&v))
            .transpose()
    }

    /// Returns true if a secondary index exists for the given column.
    fn has_index(&self, table: &str, column: &str) -> Result<bool> {
        let table = self.must_get_table(table)?;
        Ok(table.columns.iter().find(|c| c.name == column).map(|c| c.index).unwrap_or(false))
    }

    /// Stores a secondary index entry for the given column value, replacing the
    /// existing entry if any.
    fn set_index(
        &self,
        table: &str,
        column: &str,
        value: &Value,
        ids: BTreeSet<Value>,
    ) -> Result<()> {
        debug_assert!(self.has_index(table, column)?, "no index on {table}.{column}");
        let key = Key::Index(table.into(), column.into(), value.into()).encode();
        if ids.is_empty() {
            self.txn.delete(&key)?;
        } else {
            self.txn.set(&key, ids.encode())?;
        }
        Ok(())
    }

    /// Deletes the owner of an object and the privileges granted on it.
    fn delete_owner_and_grants(&self, object: &str) -> Result<()> {
        let key = Key::Owner(object.into()).encode();
        if self.txn.get(&key)?.is_some() {
            self.txn.delete(&key)?;
        }
        self.delete_keys(KeyPrefix::Grant, |key| matches!(key, Key::Grant(o, _) if o == object))
    }

    /// Deletes the keys under a prefix for which the predicate is true.
    fn delete_keys(&self, prefix: KeyPrefix, predicate: impl Fn(&Key) -> bool) -> Result<()> {
        let mut keys = self.txn.scan_prefix(&prefix.encode()).map_ok(|(key, _)| key);
        while let Some(key) = keys.next().transpose()? {
            if predicate(&Key::decode(&key)?) {
                self.txn.delete(&key)?;
            }
        }
        Ok(())
    }

    /// Stores the rows of a materialized view, by position.
    fn set_materialized_view_rows(&self, view: &str, rows: Vec<Row>) -> Result<()> {
        for (position, row) in rows.into_iter().enumerate() {
            let key = Key::MaterializedViewRow(view.into(), position as u64).encode();
            self.txn.set(&key, row.encode())?;
        }
        Ok(())
    }

    /// Returns all tables referencing a table, as (table, column index) pairs.
    /// This includes any references from the table itself.
    fn table_references(&self, table: &str) -> Result<Vec<(Table, Vec<usize>)>> {
        Ok(self
            .list_tables()?
            .into_iter()
            .map(|t| {
                let references = t
                    .columns
                    .iter()
                    .enumerate()
                    .filter(|(_, c)| c.references.as_deref() == Some(table))
                    .map(|(i, _)| i)
                    .collect_vec();
                (t, references)
            })
            .filter(|(_, references)| !references.is_empty())
            .collect())
    }
}

impl<E: storage::Engine> super::Transaction for Transaction<E> {
    fn state(&self) -> &mvcc::TransactionState {
        self.txn.state()
    }

    fn commit(self) -> Result<()> {
        // Read-only transactions have the version of the next read-write
        // transaction, so they must not commit its changes. Hold the change
        // log lock across the commit, to keep the log in commit order.
        if self.txn.read_only() {
            return self.txn.commit();
        }
        let version = self.txn.version();
        let mut changes = self.changes.lock()?;
        self.txn.commit()?;
        changes.commit(version);
        Ok(())
    }

    fn rollback(self) -> Result<()> {
        if self.txn.read_only() {
            return self.txn.rollback();
        }
        let version = self.txn.version();
        self.txn.rollback()?;
        self.changes.lock()?.rollback(version);
        Ok(())
    }

    fn delete(&self, table: &str, ids: &[Value]) -> Result<()> {
        let table = self.must_get_table(table)?;
        let indexes = table.columns.iter().enumerate().filter(|(_, c)| c.index).collect_vec();

        // Check for foreign key references to the deleted rows.
        for (source, refs) in self.table_references(&table.name)? {
            let self_reference = source.name == table.name;
            for i in refs {
                let column = &source.columns[i];
                let mut source_ids = if i == source.primary_key {
                    // If the reference is from a primary key column, do a lookup.
                    self.get(&source.name, ids)?
                        .into_iter()
                        .map(|row| row.into_iter().nth(i).expect("short row"))
                        .collect()
                } else {
                    // Otherwise (commonly), do a secondary index lookup.
                    // All foreign keys have a secondary index.
                    self.lookup_index(&source.name, &column.name, ids)?
                };
                // We can ignore any references between the deleted rows,
                // including a row referencing itself.
                if self_reference {
                    for id in ids {
                        source_ids.remove(id);
                    }
                }
                // Error if the delete would violate referential integrity.
                if let Some(source_id) = source_ids.first() {
                    let table = source.name;
                    let column = &source.columns[source.primary_key].name;
                    return errinput!("row referenced by {table}.{column}={source_id}");
                }
            }
        }

        for id in ids {
            // Update any secondary index entries.
            if !indexes.is_empty()
                && let Some(row) = self.get_row(&table.name, id)?
            {
                for (i, column) in indexes.iter().copied() {
                    let mut ids = self.get_index(&table.name, &column.name, &row[i])?;
                    ids.remove(id);
                    self.set_index(&table.name, &column.name, &row[i], ids)?;
                }
            }

            // Delete the row.
            self.txn.delete(&Key::Row((&table.name).into(), id.into()).encode())?;
            self.record(Change::Delete { table: table.name.clone(), id: id.clone() })?;
        }
        Ok(())
    }

    fn get(&self, table: &str, ids: &[Value]) -> Result<Vec<Row>> {
        ids.iter().filter_map(|id| self.get_row(table, id).transpose()).collect()
    }

    fn insert(&self, table: &str, rows: Vec<Row>) -> Result<()> {
        let table = self.must_get_table(table)?;
        for row in rows {
            // Insert the row.
            table.validate_row(&row, false, self)?;
            let id = &row[table.primary_key];
            self.txn.set(&Key::Row((&table.name).into(), id.into()).encode(), row.encode())?;

            // Update any secondary index entries.
            for (i, column) in table.columns.iter().enumerate().filter(|(_, c)| c.index) {
                let mut ids = self.get_index(&table.name, &column.name, &row[i])?;
                ids.insert(id.clone());
                self.set_index(&table.name, &column.name, &row[i], ids)?;
            }

            let row = json_line(&table, &row);
            self.record(Change::Insert { table: table.name.clone(), id: id.clone(), row })?;
        }
        Ok(())
    }

    fn lookup_index(&self, table: &str, column: &str, values: &[Value]) -> Result<BTreeSet<Value>> {
        debug_assert!(self.has_index(table, column)?, "no index on {table}.{column}");
        values.iter().map(|v| self.get_index(table, column, v)).flatten_ok().collect()
    }

    fn scan(&self, table: &str, filter: Option<Expression>) -> Result<Rows> {
        // TODO: this could be simpler if process_results() implemented Clone.
        let rows = self
            .txn
            .scan_prefix(&KeyPrefix::Row(table.into()).encode())
            .map(|result| result.and_then(|(_, value)| Row::decode(&value)));
        let Some(filter) = filter else {
            return Ok(Box::new(rows));
        };
        let rows = rows.filter_map(move |result| {
            result
                .and_then(|row| match filter.evaluate(Some(&row))? {
                    Value::Boolean(true) => Ok(Some(row)),
                    Value::Boolean(false) | Value::Null => Ok(None),
                    value => errinput!("filter returned {value}, expected boolean"),
                })
                .transpose()
        });
        Ok(Box::new(rows))
    }

    fn update(&self, table: &str, rows: BTreeMap<Value, Row>) -> Result<()> {
        let table = self.must_get_table(table)?;
        for (id, row) in rows {
            // If the primary key changes, we simply do a delete and insert.
            // This simplifies constraint validation.
            if id != row[table.primary_key] {
                self.delete(&table.name, &[id])?;
                self.insert(&table.name, vec![row])?;
                continue;
            }

            // Validate the row, but don't write it yet since we may need to
            // read the existing value to update secondary indexes.
            table.validate_row(&row, true, self)?;

            // Update indexes, knowing that the primary key has not changed.
            let indexes = table.columns.iter().enumerate().filter(|(_, c)| c.index).collect_vec();
            if !indexes.is_empty() {
                let old = self.get(&table.name, slice::from_ref(&id))?.remove(0);
                for (i, column) in indexes {
                    // If the value didn't change, we don't have to do anything.
                    if old[i] == row[i] {
                        continue;
                    }

                    // Remove the old value from the index entry.
                    let mut ids = self.get_index(&table.name, &column.name, &old[i])?;
                    ids.remove(&id);
                    self.set_index(&table.name, &column.name, &old[i], ids)?;

                    // Insert the new value into the index entry.
                    let mut ids = self.get_index(&table.name, &column.name, &row[i])?;
                    ids.insert(id.clone());
                    self.set_index(&table.name, &column.name, &row[i], ids)?;
                }
            }

            // Update the row.
            self.txn.set(&Key::Row((&table.name).into(), (&id).into()).encode(), row.encode())?;
            let row = json_line(&table, &row);
            self.record(Change::Update { table: table.name.clone(), id, row })?;
        }
        Ok(())
    }
}

impl<E: storage::Engine> Catalog for Transaction<E> {
    fn create_table(&self, table: Table) -> Result<()> {
        table.validate_create(self)?;
        self.txn.set(&Key::Table((&table.name).into()).encode(), table.encode())?;
        self.record(Change::CreateTable(table))
    }

    fn drop_table(&self, table: &str, if_exists: bool) -> Result<bool> {
        let Some(table) = self.get_table(table)? else {
            if if_exists {
                return Ok(false);
            }
            return errinput!("table {table} does not exist");
        };

        // Check for foreign key references.
        if let Some((source, refs)) =
            self.table_references(&table.name)?.iter().find(|(t, _)| t.name != table.name)
        {
            return errinput!(
                "table {} is referenced from {}.{}",
                table.name,
                source.name,
                source.columns[refs[0]].name
            );
        }

        // Check for views and materialized views that depend on the table.
        views::check_table_dependencies(self, &table.name)?;
        matviews::check_table_dependencies(self, &table.name)?;

        // Delete the table schema entry.
        self.txn.delete(&Key::Table((&table.name).into()).encode())?;

        // Delete the table rows.
        let prefix = &KeyPrefix::Row((&table.name).into()).encode();
        let mut keys = self.txn.scan_prefix(prefix).map_ok(|(key, _)| key);
        while let Some(key) = keys.next().transpose()? {
            self.txn.delete(&key)?;
        }

        // Delete any secondary index entries.
        for column in table.columns.iter().filter(|c| c.index) {
            let prefix = &KeyPrefix::Index((&table.name).into(), (&column.name).into()).encode();
            let mut keys = self.txn.scan_prefix(prefix).map_ok(|(key, _)| key);
            while let Some(key) = keys.next().transpose()? {
                self.txn.delete(&key)?;
            }
        }

        // Delete the column statistics, if the table was analyzed.
        let key = Key::Statistics((&table.name).into()).encode();
        if self.txn.get(&key)?.is_some() {
            self.txn.delete(&key)?;
        }

        // Delete the table's owner and the privileges granted on it, its
        // CHECK constraints and its policies.
        self.delete_owner_and_grants(&table.name)?;
        self.set_checks(&table.name, Vec::new())?;
        self.delete_keys(KeyPrefix::Policy((&table.name).into()), |_| true)?;
        self.record(Change::DropTable(table.name))?;
        Ok(true)
    }

    fn get_table(&self, table: &str) -> Result<Option<Table>> {
        self.txn.get(&Key::Table(table.into()).encode())?.map(|v| Table::decode(&v)).transpose()
    }

    fn list_tables(&self) -> Result<Vec<Table>> {
        self.txn
            .scan_prefix(&KeyPrefix::Table.encode())
            .map(|r| r.and_then(|(_, v)| Table::decode(&v)))
            .collect()
    }

    fn set_table_statistics(&self, table: &str, statistics: TableStatistics) -> Result<()> {
        let table = self.must_get_table(table)?;
        self.txn.set(&Key::Statistics((&table.name).into()).encode(), statistics.encode())?;
        self.record(Change::Analyze(table.name))
    }

    fn get_table_statistics(&self, table: &str) -> Result<Option<TableStatistics>> {
        self.txn
            .get(&Key::Statistics(table.into()).encode())?
            .map(|v| TableStatistics::decode(&v))
            .transpose()
    }

    fn create_view(&self, view: View) -> Result<()> {
        if self.get_table(&view.name)?.is_some() {
            return errinput!("table {} already exists", view.name);
        }
        if self.get_view(&view.name)?.is_some() {
            return errinput!("view {} already exists", view.name);
        }
        if self.get_materialized_view(&view.name)?.is_some() {
            return errinput!("materialized view {} already exists", view.name);
        }
        self.txn.set(&Key::View((&view.name).into()).encode(), view.encode())?;
        self.record(Change::CreateView(view))
    }

    fn drop_view(&self, view: &str, if_exists: bool) -> Result<bool> {
        if self.get_view(view)?.is_none() {
            if if_exists {
                return Ok(false);
            }
            return errinput!("view {view} does not exist");
        }
        self.txn.delete(&Key::View(view.into()).encode())?;
        self.delete_owner_and_grants(view)?;
        self.record(Change::DropView(view.to_string()))?;
        Ok(true)
    }

    fn get_view(&self, view: &str) -> Result<Option<View>> {
        self.txn.get(&Key::View(view.into()).encode())?.map(|v| View::decode(&v)).transpose()
    }

    fn list_views(&self) -> Result<Vec<View>> {
        self.txn
            .scan_prefix(&KeyPrefix::View.encode())
            .map(|r| r.and_then(|(_, v)| View::decode(&v)))
            .collect()
    }

    fn create_user(&self, user: User) -> Result<()> {
        if user.name == ADMIN || self.get_user(&user.name)?.is_some() {
            return errinput!("user {} already exists", user.name);
        }
        self.txn.set(&Key::User((&user.name).into()).encode(), user.encode())
    }

    fn drop_user(&self, user: &str) -> Result<()> {
        if self.get_user(user)?.is_none() {
            return errinput!("user {user} does not exist");
        }
        self.txn.delete(&Key::User(user.into()).encode())?;

        // Delete its memberships, both as a member and of its members, and
        // the privileges granted to it.
        self.delete_keys(KeyPrefix::Membership, |key| {
            matches!(key, Key::Membership(member, group) if member == user || group == user)
        })?;
        self.delete_keys(KeyPrefix::Grant, |key| matches!(key, Key::Grant(_, g) if g == user))
    }

    fn get_user(&self, user: &str) -> Result<Option<User>> {
        self.txn.get(&Key::User(user.into()).encode())?.map(|v| User::decode(&v)).transpose()
    }

    fn list_users(&self) -> Result<Vec<User>> {
        self.txn
            .scan_prefix(&KeyPrefix::User.encode())
            .map(|r| r.and_then(|(_, v)| User::decode(&v)))
            .collect()
    }

    fn set_membership(&self, member: &str, user: &str, is_member: bool) -> Result<()> {
        let key = Key::Membership(member.into(), user.into()).encode();
        match (is_member, self.txn.get(&key)?.is_some()) {
            (true, false) => self.txn.set(&key, Vec::new()),
            (false, true) => self.txn.delete(&key),
            (true, true) | (false, false) => Ok(()),
        }
    }

    fn list_memberships(&self) -> Result<Vec<(String, String)>> {
        self.txn
            .scan_prefix(&KeyPrefix::Membership.encode())
            .map(|r| match Key::decode(&r?.0)? {
                Key::Membership(member, user) => Ok((member.into_owned(), user.into_owned())),
                key => errdata!("invalid membership key {key:?}"),
            })
            .collect()
    }

    fn set_privileges(
        &self,
        object: &str,
        grantee: &str,
        privileges: BTreeSet<Privilege>,
    ) -> Result<()> {
        let key = Key::Grant(object.into(), grantee.into()).encode();
        if !privileges.is_empty() {
            self.txn.set(&key, privileges.encode())
        } else if self.txn.get(&key)?.is_some() {
            self.txn.delete(&key)
        } else {
            Ok(())
        }
    }

    fn list_grants(&self) -> Result<Vec<(String, String, BTreeSet<Privilege>)>> {
        let mut grants = Vec::new();
        let mut scan = self.txn.scan_prefix(&KeyPrefix::Grant.encode());
        while let Some((key, value)) = scan.next().transpose()? {
            let Key::Grant(object, grantee) = Key::decode(&key)? else {
                return errdata!("invalid grant key {key:?}");
            };
            let privileges = BTreeSet::decode(&value)?;
            grants.push((object.into_owned(), grantee.into_owned(), privileges));
        }
        Ok(grants)
    }

    fn set_owner(&self, object: &str, owner: &str) -> Result<()> {
        let key = Key::Owner(object.into()).encode();
        if owner != ADMIN {
            self.txn.set(&key, bincode::serialize(&owner))
        } else if self.txn.get(&key)?.is_some() {
            self.txn.delete(&key)
        } else {
            Ok(())
        }
    }

    fn get_owner(&self, object: &str) -> Result<String> {
        match self.txn.get(&Key::Owner(object.into()).encode())? {
            Some(owner) => bincode::deserialize(&owner),
            None => Ok(ADMIN.to_string()),
        }
    }

    fn set_checks(&self, table: &str, checks: Vec<Check>) -> Result<()> {
        let key = Key::Checks(table.into()).encode();
        if !checks.is_empty() {
            self.txn.set(&key, checks.encode())
        } else if self.txn.get(&key)?.is_some() {
            self.txn.delete(&key)
        } else {
            Ok(())
        }
    }

    fn get_checks(&self, table: &str) -> Result<Vec<Check>> {
        self.txn
            .get(&Key::Checks(table.into()).encode())?
            .map(|v| Vec::decode(&v))
            .transpose()
            .map(Option::unwrap_or_default)
    }

    fn create_policy(&self, policy: Policy) -> Result<()> {
        let key = Key::Policy((&policy.table).into(), (&policy.name).into()).encode();
        self.txn.set(&key, policy.encode())
    }

    fn drop_policy(&self, table: &str, policy: &str) -> Result<bool> {
        let key = Key::Policy(table.into(), policy.into()).encode();
        if self.txn.get(&key)?.is_none() {
            return Ok(false);
        }
        self.txn.delete(&key)?;
        Ok(true)
    }

    fn list_policies(&self, table: &str) -> Result<Vec<Policy>> {
        self.txn
            .scan_prefix(&KeyPrefix::Policy(table.into()).encode())
            .map(|r| r.and_then(|(_, v)| Policy::decode(&v)))
            .collect()
    }

    fn create_materialized_view(&self, view: MaterializedView, rows: Vec<Row>) -> Result<()> {
        self.txn.set(&Key::MaterializedView((&view.name).into()).encode(), view.encode())?;
        self.set_materialized_view_rows(&view.name, rows)
    }

    fn refresh_materialized_view(&self, view: &str, rows: Vec<Row>) -> Result<()> {
        self.delete_keys(KeyPrefix::MaterializedViewRow(view.into()), |_| true)?;
        self.set_materialized_view_rows(view, rows)
    }

    fn drop_materialized_view(&self, view: &str) -> Result<bool> {
        let key = Key::MaterializedView(view.into()).encode();
        if self.txn.get(&key)?.is_none() {
            return Ok(false);
        }
        self.txn.delete(&key)?;
        self.delete_keys(KeyPrefix::MaterializedViewRow(view.into()), |_| true)?;
        self.delete_owner_and_grants(view)?;
        Ok(true)
    }

    fn get_materialized_view(&self, view: &str) -> Result<Option<MaterializedView>> {
        self.txn
            .get(&Key::MaterializedView(view.into()).encode())?
            .map(|v| MaterializedView::decode(&v))
            .transpose()
    }

    fn get_materialized_view_rows(&self, view: &str) -> Result<Vec<Row>> {
        self.txn
            .scan_prefix(&KeyPrefix::MaterializedViewRow(view.into()).encode())
            .map(|r| r.and_then(|(_, v)| Row::decode(&v)))
            .collect()
    }

    fn list_materialized_views(&self) -> Result<Vec<MaterializedView>> {
        self.txn
            .scan_prefix(&KeyPrefix::MaterializedView.encode())
            .map(|r| r.and_then(|(_, v)| MaterializedView::decode(&v)))
            .collect()
    }
}
