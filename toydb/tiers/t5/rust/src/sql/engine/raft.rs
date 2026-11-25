use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Mutex;

use crossbeam::channel::Sender;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

use super::{Catalog, Change, Engine as _, Transaction as _};
use crate::encoding::{self, Value as _, bincode};
use crate::errdata;
use crate::error::Result;
use crate::raft;
use crate::sql::execution::audit::{AuditLog, SessionList};
use crate::sql::types::{
    Check, Expression, MaterializedView, Policy, Privilege, Row, Rows, Table, TableStatistics, User,
    Value, View,
};
use crate::storage::{self, mvcc};

/// A read command, submitted via Raft and executed on the leader. Each command
/// corresponds to a SQL engine method and parameters. Uses Cows to allow
/// borrowed encoding and owned decoding.
#[derive(Debug, Serialize, Deserialize)]
pub enum Read<'a> {
    BeginReadOnly {
        as_of: Option<mvcc::Version>,
    },
    Status,

    Get {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
        ids: Cow<'a, [Value]>,
    },
    LookupIndex {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
        column: Cow<'a, str>,
        values: Cow<'a, [Value]>,
    },
    Scan {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
        filter: Option<Expression>,
    },

    GetTable {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
    },
    ListTables {
        txn: Cow<'a, mvcc::TransactionState>,
    },

    GetTableStatistics {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
    },

    GetView {
        txn: Cow<'a, mvcc::TransactionState>,
        view: Cow<'a, str>,
    },
    ListViews {
        txn: Cow<'a, mvcc::TransactionState>,
    },

    Changes {
        since: mvcc::Version,
    },

    GetUser {
        txn: Cow<'a, mvcc::TransactionState>,
        user: Cow<'a, str>,
    },
    ListUsers {
        txn: Cow<'a, mvcc::TransactionState>,
    },
    ListMemberships {
        txn: Cow<'a, mvcc::TransactionState>,
    },
    ListGrants {
        txn: Cow<'a, mvcc::TransactionState>,
    },
    GetOwner {
        txn: Cow<'a, mvcc::TransactionState>,
        object: Cow<'a, str>,
    },
    GetChecks {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
    },
    ListPolicies {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
    },
    GetMaterializedView {
        txn: Cow<'a, mvcc::TransactionState>,
        view: Cow<'a, str>,
    },
    GetMaterializedViewRows {
        txn: Cow<'a, mvcc::TransactionState>,
        view: Cow<'a, str>,
    },
    ListMaterializedViews {
        txn: Cow<'a, mvcc::TransactionState>,
    },
}

impl encoding::Value for Read<'_> {}

/// A write command, submitted via Raft and executed on all nodes. Each command
/// corresponds to a SQL engine method and parameters. Uses Cows to allow
/// borrowed encoding and owned decoding.
#[derive(Debug, Serialize, Deserialize)]
pub enum Write<'a> {
    Begin,
    Commit(Cow<'a, mvcc::TransactionState>),
    Rollback(Cow<'a, mvcc::TransactionState>),

    Delete { txn: Cow<'a, mvcc::TransactionState>, table: Cow<'a, str>, ids: Cow<'a, [Value]> },
    Insert { txn: Cow<'a, mvcc::TransactionState>, table: Cow<'a, str>, rows: Vec<Row> },
    Update { txn: Cow<'a, mvcc::TransactionState>, table: Cow<'a, str>, rows: BTreeMap<Value, Row> },

    CreateTable { txn: Cow<'a, mvcc::TransactionState>, schema: Table },
    DropTable { txn: Cow<'a, mvcc::TransactionState>, table: Cow<'a, str>, if_exists: bool },

    SetTableStatistics {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
        statistics: TableStatistics,
    },

    CreateView { txn: Cow<'a, mvcc::TransactionState>, view: View },
    DropView {
        txn: Cow<'a, mvcc::TransactionState>,
        view: Cow<'a, str>,
        if_exists: bool,
    },

    CreateUser {
        txn: Cow<'a, mvcc::TransactionState>,
        user: User,
    },
    DropUser {
        txn: Cow<'a, mvcc::TransactionState>,
        user: Cow<'a, str>,
    },
    SetMembership {
        txn: Cow<'a, mvcc::TransactionState>,
        member: Cow<'a, str>,
        user: Cow<'a, str>,
        is_member: bool,
    },
    SetPrivileges {
        txn: Cow<'a, mvcc::TransactionState>,
        object: Cow<'a, str>,
        grantee: Cow<'a, str>,
        privileges: BTreeSet<Privilege>,
    },
    SetOwner {
        txn: Cow<'a, mvcc::TransactionState>,
        object: Cow<'a, str>,
        owner: Cow<'a, str>,
    },
    SetChecks {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
        checks: Vec<Check>,
    },
    CreatePolicy {
        txn: Cow<'a, mvcc::TransactionState>,
        policy: Policy,
    },
    DropPolicy {
        txn: Cow<'a, mvcc::TransactionState>,
        table: Cow<'a, str>,
        policy: Cow<'a, str>,
    },
    CreateMaterializedView {
        txn: Cow<'a, mvcc::TransactionState>,
        view: MaterializedView,
        rows: Vec<Row>,
    },
    RefreshMaterializedView {
        txn: Cow<'a, mvcc::TransactionState>,
        view: Cow<'a, str>,
        rows: Vec<Row>,
    },
    DropMaterializedView {
        txn: Cow<'a, mvcc::TransactionState>,
        view: Cow<'a, str>,
    },
}

impl encoding::Value for Write<'_> {}

/// Raft SQL engine status.
#[derive(Serialize, Deserialize)]
pub struct Status {
    pub raft: raft::Status,
    pub mvcc: mvcc::Status,
}

/// A Raft-based SQL engine. This dispatches to the `Local` engine for local
/// storage and processing on each node, but sends read and write commands
/// through Raft for distributed consensus.
///
/// The `Raft` engine itself is simply a Raft client which sends `raft::Request`
/// to the local Raft node for processing. These requests are applied to the
/// Raft SQL engine's `State` state machine running below Raft on each node,
/// which executes the commands on a `Local` SQL engine using a
/// `storage::Engine` for local storage.
///
/// For more details on how SQL statements flow through the engine, see the
/// `sql` module documentation.
pub struct Raft {
    /// Sends requests to the local Raft node, along with a response channel.
    tx: Sender<(raft::Request, Sender<Result<raft::Response>>)>,
    /// The audit log of the server's sessions.
    audit: Mutex<AuditLog>,
    /// The server's open sessions.
    sessions: Mutex<SessionList>,
}

impl Raft {
    /// The unversioned key used to store the applied index. Just uses a string
    /// for simplicity.
    pub const APPLIED_INDEX_KEY: &'static [u8] = b"applied_index";

    /// Creates a new Raft-based SQL engine, with a channel to send requests to
    /// the local Raft node.
    pub fn new(tx: Sender<(raft::Request, Sender<Result<raft::Response>>)>) -> Self {
        Self { tx, audit: Mutex::new(AuditLog::new()), sessions: Mutex::new(SessionList::new()) }
    }

    /// Creates the Raft-managed state machine for the Raft engine. Receives
    /// commands from the Raft engine and executes them on a `Local` engine.
    pub fn new_state<E: storage::Engine>(engine: E) -> Result<State<E>> {
        State::new(engine)
    }

    /// Executes a request against the Raft cluster, waiting for the response.
    fn request(&self, request: raft::Request) -> Result<raft::Response> {
        let (response_tx, response_rx) = crossbeam::channel::bounded(1);
        self.tx.send((request, response_tx))?;
        response_rx.recv()?
    }

    /// Writes through Raft, deserializing the response into the return type.
    fn write<V: DeserializeOwned>(&self, write: Write) -> Result<V> {
        match self.request(raft::Request::Write(write.encode()))? {
            raft::Response::Write(response) => bincode::deserialize(&response),
            response => errdata!("unexpected Raft write response {response:?}"),
        }
    }

    /// Reads from Raft, deserializing the response into the return type.
    fn read<V: DeserializeOwned>(&self, read: Read) -> Result<V> {
        match self.request(raft::Request::Read(read.encode()))? {
            raft::Response::Read(response) => bincode::deserialize(&response),
            response => errdata!("unexpected Raft read response {response:?}"),
        }
    }

    /// Raft SQL engine status.
    pub fn status(&self) -> Result<Status> {
        let raft = match self.request(raft::Request::Status)? {
            raft::Response::Status(status) => status,
            response => return errdata!("unexpected Raft status response {response:?}"),
        };
        let mvcc = self.read(Read::Status)?;
        Ok(Status { raft, mvcc })
    }
}

impl<'a> super::Engine<'a> for Raft {
    type Transaction = Transaction<'a>;

    fn begin(&'a self) -> Result<Self::Transaction> {
        Transaction::begin(self, false, None)
    }

    fn begin_read_only(&'a self) -> Result<Self::Transaction> {
        Transaction::begin(self, true, None)
    }

    fn begin_as_of(&'a self, version: mvcc::Version) -> Result<Self::Transaction> {
        Transaction::begin(self, true, Some(version))
    }

    fn changes(&'a self, since: mvcc::Version) -> Result<Vec<(mvcc::Version, Change)>> {
        self.read(Read::Changes { since })
    }

    fn audit(&'a self) -> &'a Mutex<AuditLog> {
        &self.audit
    }

    fn sessions(&'a self) -> &'a Mutex<SessionList> {
        &self.sessions
    }
}

/// A Raft SQL engine transaction.
///
/// This keeps track of the transaction state in memory. An `mvcc::Transaction`
/// normally manages this, but since `mvcc::Transaction` runs below Raft, it
/// can't maintain this state between individual requests (which could execute
/// on different leaders). Instead, it uses `mvcc::Transaction::resume` to
/// resume the transaction from the provided transaction state for each request.
pub struct Transaction<'a> {
    /// The Raft SQL engine client, used to communicate with Raft.
    raft: &'a Raft,
    /// The MVCC transaction state.
    state: mvcc::TransactionState,
}

impl<'a> Transaction<'a> {
    /// Starts a transaction in the given mode.
    fn begin(raft: &'a Raft, read_only: bool, as_of: Option<mvcc::Version>) -> Result<Self> {
        assert!(as_of.is_none() || read_only, "can't use as_of without read_only");
        // Read-only transactions don't allocate a new MVCC version, so they
        // don't write anything -- they just grab the current transaction state.
        // Submit them as reads to avoid a replication roundtrip.
        let state = if read_only || as_of.is_some() {
            raft.read(Read::BeginReadOnly { as_of })?
        } else {
            raft.write(Write::Begin)?
        };
        Ok(Self { raft, state })
    }
}

impl super::Transaction for Transaction<'_> {
    fn state(&self) -> &mvcc::TransactionState {
        &self.state
    }

    fn commit(self) -> Result<()> {
        if self.state.read_only {
            return Ok(()); // noop
        }
        self.raft.write(Write::Commit(self.state.into()))
    }

    fn rollback(self) -> Result<()> {
        if self.state.read_only {
            return Ok(()); // noop
        }
        self.raft.write(Write::Rollback(self.state.into()))
    }

    fn delete(&self, table: &str, ids: &[Value]) -> Result<()> {
        self.raft.write(Write::Delete {
            txn: (&self.state).into(),
            table: table.into(),
            ids: ids.into(),
        })
    }

    fn get(&self, table: &str, ids: &[Value]) -> Result<Vec<Row>> {
        self.raft.read(Read::Get {
            txn: (&self.state).into(),
            table: table.into(),
            ids: ids.into(),
        })
    }

    fn insert(&self, table: &str, rows: Vec<Row>) -> Result<()> {
        self.raft.write(Write::Insert { txn: (&self.state).into(), table: table.into(), rows })
    }

    fn lookup_index(&self, table: &str, column: &str, values: &[Value]) -> Result<BTreeSet<Value>> {
        self.raft.read(Read::LookupIndex {
            txn: (&self.state).into(),
            table: table.into(),
            column: column.into(),
            values: values.into(),
        })
    }

    fn scan(&self, table: &str, filter: Option<Expression>) -> Result<Rows> {
        let scan: Vec<Row> = self.raft.read(Read::Scan {
            txn: (&self.state).into(),
            table: table.into(),
            filter,
        })?;
        Ok(Box::new(scan.into_iter().map(Ok)))
    }

    fn update(&self, table: &str, rows: BTreeMap<Value, Row>) -> Result<()> {
        self.raft.write(Write::Update { txn: (&self.state).into(), table: table.into(), rows })
    }
}

impl Catalog for Transaction<'_> {
    fn create_table(&self, schema: Table) -> Result<()> {
        self.raft.write(Write::CreateTable { txn: (&self.state).into(), schema })
    }

    fn drop_table(&self, table: &str, if_exists: bool) -> Result<bool> {
        self.raft.write(Write::DropTable {
            txn: (&self.state).into(),
            table: table.into(),
            if_exists,
        })
    }

    fn get_table(&self, table: &str) -> Result<Option<Table>> {
        self.raft.read(Read::GetTable { txn: (&self.state).into(), table: table.into() })
    }

    fn list_tables(&self) -> Result<Vec<Table>> {
        self.raft.read(Read::ListTables { txn: (&self.state).into() })
    }

    fn set_table_statistics(&self, table: &str, statistics: TableStatistics) -> Result<()> {
        self.raft.write(Write::SetTableStatistics {
            txn: (&self.state).into(),
            table: table.into(),
            statistics,
        })
    }

    fn get_table_statistics(&self, table: &str) -> Result<Option<TableStatistics>> {
        self.raft.read(Read::GetTableStatistics { txn: (&self.state).into(), table: table.into() })
    }

    fn create_view(&self, view: View) -> Result<()> {
        self.raft.write(Write::CreateView { txn: (&self.state).into(), view })
    }

    fn drop_view(&self, view: &str, if_exists: bool) -> Result<bool> {
        self.raft.write(Write::DropView { txn: (&self.state).into(), view: view.into(), if_exists })
    }

    fn get_view(&self, view: &str) -> Result<Option<View>> {
        self.raft.read(Read::GetView { txn: (&self.state).into(), view: view.into() })
    }

    fn list_views(&self) -> Result<Vec<View>> {
        self.raft.read(Read::ListViews { txn: (&self.state).into() })
    }

    fn create_user(&self, user: User) -> Result<()> {
        self.raft.write(Write::CreateUser { txn: (&self.state).into(), user })
    }

    fn drop_user(&self, user: &str) -> Result<()> {
        self.raft.write(Write::DropUser { txn: (&self.state).into(), user: user.into() })
    }

    fn get_user(&self, user: &str) -> Result<Option<User>> {
        self.raft.read(Read::GetUser { txn: (&self.state).into(), user: user.into() })
    }

    fn list_users(&self) -> Result<Vec<User>> {
        self.raft.read(Read::ListUsers { txn: (&self.state).into() })
    }

    fn set_membership(&self, member: &str, user: &str, is_member: bool) -> Result<()> {
        self.raft.write(Write::SetMembership {
            txn: (&self.state).into(),
            member: member.into(),
            user: user.into(),
            is_member,
        })
    }

    fn list_memberships(&self) -> Result<Vec<(String, String)>> {
        self.raft.read(Read::ListMemberships { txn: (&self.state).into() })
    }

    fn set_privileges(
        &self,
        object: &str,
        grantee: &str,
        privileges: BTreeSet<Privilege>,
    ) -> Result<()> {
        self.raft.write(Write::SetPrivileges {
            txn: (&self.state).into(),
            object: object.into(),
            grantee: grantee.into(),
            privileges,
        })
    }

    fn list_grants(&self) -> Result<Vec<(String, String, BTreeSet<Privilege>)>> {
        self.raft.read(Read::ListGrants { txn: (&self.state).into() })
    }

    fn set_owner(&self, object: &str, owner: &str) -> Result<()> {
        self.raft.write(Write::SetOwner {
            txn: (&self.state).into(),
            object: object.into(),
            owner: owner.into(),
        })
    }

    fn get_owner(&self, object: &str) -> Result<String> {
        self.raft.read(Read::GetOwner { txn: (&self.state).into(), object: object.into() })
    }

    fn set_checks(&self, table: &str, checks: Vec<Check>) -> Result<()> {
        self.raft.write(Write::SetChecks { txn: (&self.state).into(), table: table.into(), checks })
    }

    fn get_checks(&self, table: &str) -> Result<Vec<Check>> {
        self.raft.read(Read::GetChecks { txn: (&self.state).into(), table: table.into() })
    }

    fn create_policy(&self, policy: Policy) -> Result<()> {
        self.raft.write(Write::CreatePolicy { txn: (&self.state).into(), policy })
    }

    fn drop_policy(&self, table: &str, policy: &str) -> Result<bool> {
        self.raft.write(Write::DropPolicy {
            txn: (&self.state).into(),
            table: table.into(),
            policy: policy.into(),
        })
    }

    fn list_policies(&self, table: &str) -> Result<Vec<Policy>> {
        self.raft.read(Read::ListPolicies { txn: (&self.state).into(), table: table.into() })
    }

    fn create_materialized_view(&self, view: MaterializedView, rows: Vec<Row>) -> Result<()> {
        self.raft.write(Write::CreateMaterializedView { txn: (&self.state).into(), view, rows })
    }

    fn refresh_materialized_view(&self, view: &str, rows: Vec<Row>) -> Result<()> {
        self.raft.write(Write::RefreshMaterializedView {
            txn: (&self.state).into(),
            view: view.into(),
            rows,
        })
    }

    fn drop_materialized_view(&self, view: &str) -> Result<bool> {
        self.raft
            .write(Write::DropMaterializedView { txn: (&self.state).into(), view: view.into() })
    }

    fn get_materialized_view(&self, view: &str) -> Result<Option<MaterializedView>> {
        self.raft.read(Read::GetMaterializedView { txn: (&self.state).into(), view: view.into() })
    }

    fn get_materialized_view_rows(&self, view: &str) -> Result<Vec<Row>> {
        self.raft
            .read(Read::GetMaterializedViewRows { txn: (&self.state).into(), view: view.into() })
    }

    fn list_materialized_views(&self) -> Result<Vec<MaterializedView>> {
        self.raft.read(Read::ListMaterializedViews { txn: (&self.state).into() })
    }
}

/// The state machine for the Raft SQL engine. Receives commands via Raft and
/// dispatches to a `Local` SQL engine which does the actual work, using a
/// `storage::Engine` for storage.
///
/// For simplicity, we don't attempt to stream large requests or responses,
/// instead just delivering them as one large chunk. This means that e.g. a full
/// table scan will pull the entire table into memory, serialize it, and send it
/// across the network as one message, but that's fine for toyDB.
pub struct State<E: storage::Engine + 'static> {
    /// The local SQL engine, used for actual storage.
    local: super::Local<E>,
    /// The last applied index. This tells Raft which command to apply next.
    applied_index: raft::Index,
}

impl<E: storage::Engine> State<E> {
    /// Creates a new Raft state maching using the given storage engine for
    /// local storage.
    pub fn new(engine: E) -> Result<Self> {
        let local = super::Local::new(engine);
        let applied_index = local
            .get_unversioned(Raft::APPLIED_INDEX_KEY)?
            .map(|b| bincode::deserialize(&b))
            .transpose()?
            .unwrap_or_default();
        Ok(State { local, applied_index })
    }

    /// Executes a write command. This is executed on all nodes, but the
    /// response is returned from the Raft leader.
    ///
    /// The response is encoded using Bincode. The caller will know what
    /// response type to expect for each command and deserialize into it.
    fn write(&self, command: Write) -> Result<Vec<u8>> {
        Ok(match command {
            Write::Begin => self.local.begin()?.state().encode(),
            Write::Commit(txn) => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.commit()?)
            }
            Write::Rollback(txn) => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.rollback()?)
            }

            Write::Delete { txn, table, ids } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.delete(&table, &ids)?)
            }
            Write::Insert { txn, table, rows } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.insert(&table, rows)?)
            }
            Write::Update { txn, table, rows } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.update(&table, rows)?)
            }

            Write::CreateTable { txn, schema } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.create_table(schema)?)
            }
            Write::DropTable { txn, table, if_exists } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.drop_table(&table, if_exists)?,
            ),

            Write::SetTableStatistics { txn, table, statistics } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.set_table_statistics(&table, statistics)?,
            ),

            Write::CreateView { txn, view } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.create_view(view)?)
            }
            Write::DropView { txn, view, if_exists } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.drop_view(&view, if_exists)?,
            ),

            Write::CreateUser { txn, user } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.create_user(user)?)
            }
            Write::DropUser { txn, user } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.drop_user(&user)?)
            }
            Write::SetMembership { txn, member, user, is_member } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.set_membership(&member, &user, is_member)?,
            ),
            Write::SetPrivileges { txn, object, grantee, privileges } => bincode::serialize(
                &self
                    .local
                    .resume(txn.into_owned())?
                    .set_privileges(&object, &grantee, privileges)?,
            ),
            Write::SetOwner { txn, object, owner } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.set_owner(&object, &owner)?,
            ),
            Write::SetChecks { txn, table, checks } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.set_checks(&table, checks)?,
            ),
            Write::CreatePolicy { txn, policy } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.create_policy(policy)?)
            }
            Write::DropPolicy { txn, table, policy } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.drop_policy(&table, &policy)?,
            ),
            Write::CreateMaterializedView { txn, view, rows } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.create_materialized_view(view, rows)?,
            ),
            Write::RefreshMaterializedView { txn, view, rows } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.refresh_materialized_view(&view, rows)?,
            ),
            Write::DropMaterializedView { txn, view } => bincode::serialize(
                &self.local.resume(txn.into_owned())?.drop_materialized_view(&view)?,
            ),
        })
    }
}

impl<E: storage::Engine> raft::State for State<E> {
    fn get_applied_index(&self) -> raft::Index {
        self.applied_index
    }

    fn apply(&mut self, entry: raft::Entry) -> Result<Vec<u8>> {
        assert_eq!(entry.index, self.applied_index + 1, "entry index not after applied index");

        let result = match &entry.command {
            Some(command) => match self.write(Write::decode(command)?) {
                // Panic on non-deterministic apply failures, to prevent node
                // state divergence. See `raft::State` docs for details.
                Err(e) if !e.is_deterministic() => panic!("non-deterministic apply failure: {e}"),
                result => result,
            },
            // Raft submits noop commands on leader changes. Ignore them, but
            // record the applied index below.
            None => Ok(Vec::new()),
        };

        // Persist the applied index. We don't have to flush, because it's ok to
        // lose a tail of the state machine writes (e.g. if the machine
        // crashes). Raft will replay the log from the last known applied index.
        self.applied_index = entry.index;
        self.local.set_unversioned(Raft::APPLIED_INDEX_KEY, bincode::serialize(&entry.index))?;
        result
    }

    fn read(&self, command: Vec<u8>) -> Result<Vec<u8>> {
        Ok(match Read::decode(&command)? {
            Read::BeginReadOnly { as_of } => {
                let txn = match as_of {
                    Some(version) => self.local.begin_as_of(version)?,
                    None => self.local.begin_read_only()?,
                };
                txn.state().encode()
            }
            Read::Status => self.local.mvcc.status()?.encode(),

            Read::Get { txn, table, ids } => {
                self.local.resume(txn.into_owned())?.get(&table, &ids)?.encode()
            }
            Read::LookupIndex { txn, table, column, values } => self
                .local
                .resume(txn.into_owned())?
                .lookup_index(&table, &column, &values)?
                .encode(),
            Read::Scan { txn, table, filter } => {
                // For simplicity, buffer the entire scan. See `State` comment.
                self.local
                    .resume(txn.into_owned())?
                    .scan(&table, filter)?
                    .collect::<Result<Vec<Row>>>()?
                    .encode()
            }

            Read::GetTable { txn, table } => {
                self.local.resume(txn.into_owned())?.get_table(&table)?.encode()
            }
            Read::ListTables { txn } => {
                self.local.resume(txn.into_owned())?.list_tables()?.encode()
            }

            Read::GetTableStatistics { txn, table } => {
                self.local.resume(txn.into_owned())?.get_table_statistics(&table)?.encode()
            }

            Read::GetView { txn, view } => {
                self.local.resume(txn.into_owned())?.get_view(&view)?.encode()
            }
            Read::ListViews { txn } => self.local.resume(txn.into_owned())?.list_views()?.encode(),

            Read::Changes { since } => self.local.changes(since)?.encode(),

            Read::GetUser { txn, user } => {
                self.local.resume(txn.into_owned())?.get_user(&user)?.encode()
            }
            Read::ListUsers { txn } => self.local.resume(txn.into_owned())?.list_users()?.encode(),
            Read::ListMemberships { txn } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.list_memberships()?)
            }
            Read::ListGrants { txn } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.list_grants()?)
            }
            Read::GetOwner { txn, object } => {
                bincode::serialize(&self.local.resume(txn.into_owned())?.get_owner(&object)?)
            }
            Read::GetChecks { txn, table } => {
                self.local.resume(txn.into_owned())?.get_checks(&table)?.encode()
            }
            Read::ListPolicies { txn, table } => {
                self.local.resume(txn.into_owned())?.list_policies(&table)?.encode()
            }
            Read::GetMaterializedView { txn, view } => {
                self.local.resume(txn.into_owned())?.get_materialized_view(&view)?.encode()
            }
            Read::GetMaterializedViewRows { txn, view } => {
                self.local.resume(txn.into_owned())?.get_materialized_view_rows(&view)?.encode()
            }
            Read::ListMaterializedViews { txn } => {
                self.local.resume(txn.into_owned())?.list_materialized_views()?.encode()
            }
        })
    }
}
