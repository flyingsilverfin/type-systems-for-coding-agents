//! The change log of `SHOW CHANGES`: the changes made by committed
//! transactions, in commit order.
//!
//! The local engine records each change as its transaction makes it — rows
//! inserted, updated and deleted, tables and views created and dropped,
//! statistics stored — and appends a transaction's changes to the log when it
//! commits; a rollback discards them. The log is kept in memory, is not written
//! to storage, and starts empty when the engine is created. In a cluster each
//! node records the changes as it applies the Raft log, so all nodes have the
//! same log.

use std::collections::HashMap;

use itertools::Itertools as _;
use serde::{Deserialize, Serialize};

use crate::encoding;
use crate::sql::execution::StatementResult;
use crate::sql::types::{Label, Row, Table, Value, View};
use crate::storage::mvcc::Version;

/// A change of the database: the table or view it concerns, its kind, and, for
/// row changes, the primary key and the row.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Change {
    /// A row inserted, with its primary key and the row as a JSON object.
    Insert { table: String, id: Value, row: String },
    /// A row updated, with its primary key and the new row as a JSON object.
    Update { table: String, id: Value, row: String },
    /// A row deleted from a table, with its primary key.
    Delete { table: String, id: Value },
    /// A table created, with its schema.
    CreateTable(Table),
    /// A table dropped.
    DropTable(String),
    /// A table's statistics stored by ANALYZE.
    Analyze(String),
    /// A view created, with its definition.
    CreateView(View),
    /// A view dropped.
    DropView(String),
}

impl encoding::Value for Change {}

impl Change {
    /// Returns the SHOW CHANGES row of the change, made at the given version.
    fn into_row(self, version: Version) -> Row {
        let string = |s: &str| Value::String(s.to_string());
        let (table, op, key, data) = match self {
            Self::Insert { table, id, row } => (table, "insert", id, string(&row)),
            Self::Update { table, id, row } => (table, "update", id, string(&row)),
            Self::Delete { table, id } => (table, "delete", id, Value::Null),
            Self::CreateTable(schema) => {
                let data = string(&schema_line(&schema));
                (schema.name, "create_table", Value::Null, data)
            }
            Self::DropTable(table) => (table, "drop_table", Value::Null, Value::Null),
            Self::Analyze(table) => (table, "analyze", Value::Null, Value::Null),
            Self::CreateView(view) => {
                let data = string(&view.to_string());
                (view.name, "create_view", Value::Null, data)
            }
            Self::DropView(view) => (view, "drop_view", Value::Null, Value::Null),
        };
        vec![Value::Integer(version as i64), Value::String(table), string(op), key, data]
    }
}

/// Formats a table's schema display on one line.
fn schema_line(table: &Table) -> String {
    let schema = table.to_string();
    let lines = schema.lines().map(str::trim).collect_vec();
    let [open, columns @ .., close] = lines.as_slice() else {
        panic!("schema display has no column lines: {schema}");
    };
    format!("{open}{}{close}", columns.join(" "))
}

/// The change log: the committed changes in commit order, and the pending
/// changes of open transactions by version.
#[derive(Debug, Default)]
pub struct ChangeLog {
    /// The committed changes, in commit order, with their transaction's version.
    committed: Vec<(Version, Change)>,
    /// The changes of open transactions, by transaction version.
    pending: HashMap<Version, Vec<Change>>,
}

impl ChangeLog {
    /// Creates an empty change log.
    pub fn new() -> Self {
        Self::default()
    }

    /// Records a change of an open transaction.
    pub fn record(&mut self, version: Version, change: Change) {
        self.pending.entry(version).or_default().push(change);
    }

    /// Appends the pending changes of a transaction to the log.
    pub fn commit(&mut self, version: Version) {
        let changes = self.pending.remove(&version).unwrap_or_default();
        self.committed.extend(changes.into_iter().map(|change| (version, change)));
    }

    /// Discards the pending changes of a transaction.
    pub fn rollback(&mut self, version: Version) {
        self.pending.remove(&version);
    }

    /// Returns the committed changes of transactions with a version above the
    /// given one, in commit order.
    pub fn since(&self, version: Version) -> Vec<(Version, Change)> {
        self.committed.iter().filter(|(v, _)| *v > version).cloned().collect()
    }
}

/// Returns the SHOW CHANGES result of the given changes.
pub fn result(changes: Vec<(Version, Change)>) -> StatementResult {
    let columns = ["version", "table", "op", "key", "data"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    let rows = changes.into_iter().map(|(version, change)| change.into_row(version)).collect();
    StatementResult::Select { columns, rows }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::Result;
    use crate::sql::engine::{Engine as _, Local, Transaction as _};
    use crate::sql::parser::Parser;
    use crate::sql::planner::Plan;
    use crate::sql::types::{Column, DataType};
    use crate::storage::Memory;

    /// A row change of table t, with an integer key.
    fn insert(id: i64) -> Change {
        let row = format!("{{\"id\":{id}}}");
        Change::Insert { table: "t".into(), id: Value::Integer(id), row }
    }

    /// Executes the given statements in a new local engine, each in its own
    /// session.
    fn setup(statements: &[&str]) -> Local<Memory> {
        let engine = Local::new(Memory::new());
        for statement in statements {
            engine.session().execute(statement).expect("statement failed");
        }
        engine
    }

    /// Returns the version, table and op columns of the engine's SHOW CHANGES
    /// rows since the given version.
    fn ops(engine: &Local<Memory>, since: Version) -> Vec<String> {
        let StatementResult::Select { rows, .. } = result(engine.changes(since).unwrap()) else {
            panic!("expected select result");
        };
        rows.iter().map(|row| row[..3].iter().join(", ")).collect()
    }

    #[test]
    fn log_commit_order() {
        let mut log = ChangeLog::new();
        log.record(1, insert(1));
        log.record(2, insert(2));
        log.record(1, insert(3));
        log.commit(2);
        log.commit(1);
        assert_eq!(log.since(0), vec![(2, insert(2)), (1, insert(1)), (1, insert(3))]);
        assert_eq!(log.since(1), vec![(2, insert(2))]);
        assert_eq!(log.since(2), Vec::new());
    }

    #[test]
    fn log_rollback() {
        let mut log = ChangeLog::new();
        log.record(1, insert(1));
        log.record(2, insert(2));
        log.rollback(1);
        log.commit(1); // nothing pending anymore
        log.commit(2);
        assert_eq!(log.since(0), vec![(2, insert(2))]);
    }

    #[test]
    fn log_commit_without_changes() {
        let mut log = ChangeLog::new();
        log.commit(1);
        log.rollback(2);
        assert_eq!(log.since(0), Vec::new());
    }

    #[test]
    fn rows() {
        let table = Table {
            name: "t".into(),
            primary_key: 0,
            columns: vec![
                Column {
                    name: "id".into(),
                    datatype: DataType::String,
                    nullable: false,
                    default: None,
                    unique: true,
                    index: false,
                    references: None,
                },
                Column {
                    name: "n".into(),
                    datatype: DataType::Integer,
                    nullable: true,
                    default: Some(Value::Integer(1)),
                    unique: false,
                    index: true,
                    references: Some("t".into()),
                },
            ],
        };
        let view = View {
            name: "v".into(),
            columns: vec!["a".into()],
            query: "SELECT 1 AS a".into(),
            dependencies: Vec::new(),
        };
        let row = |table: &str, op: &str, key: Value, data: Option<&str>| {
            vec![
                Value::Integer(3),
                Value::String(table.into()),
                Value::String(op.into()),
                key,
                data.map_or(Value::Null, |data| Value::String(data.into())),
            ]
        };
        let key = || Value::String("a".into());
        let json = r#"{"id":"a","n":1}"#;
        let schema =
            "CREATE TABLE t (id STRING PRIMARY KEY, n INTEGER DEFAULT 1 INDEX REFERENCES t)";
        let changes = vec![
            Change::Insert { table: "t".into(), id: key(), row: json.into() },
            Change::Update { table: "t".into(), id: key(), row: json.into() },
            Change::Delete { table: "t".into(), id: key() },
            Change::CreateTable(table),
            Change::DropTable("t".into()),
            Change::Analyze("t".into()),
            Change::CreateView(view),
            Change::DropView("v".into()),
        ];
        let StatementResult::Select { columns, rows } =
            result(changes.into_iter().map(|change| (3, change)).collect())
        else {
            panic!("expected select result");
        };
        assert_eq!(
            columns.iter().map(|label| label.to_string()).collect_vec(),
            vec!["version", "table", "op", "key", "data"]
        );
        assert_eq!(
            rows,
            vec![
                row("t", "insert", key(), Some(json)),
                row("t", "update", key(), Some(json)),
                row("t", "delete", key(), None),
                row("t", "create_table", Value::Null, Some(schema)),
                row("t", "drop_table", Value::Null, None),
                row("t", "analyze", Value::Null, None),
                row("v", "create_view", Value::Null, Some("CREATE VIEW v (a) AS SELECT 1 AS a")),
                row("v", "drop_view", Value::Null, None),
            ]
        );
    }

    #[test]
    fn engine_records_committed_changes() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, v STRING INDEX)",
            "INSERT INTO t VALUES (1, 'a'), (2, 'b')",
            "UPDATE t SET v = 'c' WHERE id = 2",
            "UPDATE t SET id = 3 WHERE id = 1",
            "DELETE FROM t WHERE id = 2",
            "ANALYZE",
            "CREATE VIEW w AS SELECT v FROM t",
            "DROP VIEW w",
            "DROP TABLE t",
        ]);
        assert_eq!(
            ops(&engine, 0),
            vec![
                "1, 't', 'create_table'",
                "2, 't', 'insert'",
                "2, 't', 'insert'",
                "3, 't', 'update'",
                "4, 't', 'delete'",
                "4, 't', 'insert'",
                "5, 't', 'delete'",
                "6, 't', 'analyze'",
                "7, 'w', 'create_view'",
                "8, 'w', 'drop_view'",
                "9, 't', 'drop_table'",
            ]
        );
        assert_eq!(ops(&engine, 8), vec!["9, 't', 'drop_table'"]);

        let changes = engine.changes(1)?;
        assert_eq!(
            changes[0],
            (
                2,
                Change::Insert {
                    table: "t".into(),
                    id: Value::Integer(1),
                    row: r#"{"id":1,"v":"a"}"#.into()
                }
            )
        );
        assert_eq!(
            changes[2],
            (
                3,
                Change::Update {
                    table: "t".into(),
                    id: Value::Integer(2),
                    row: r#"{"id":2,"v":"c"}"#.into()
                }
            )
        );
        Ok(())
    }

    #[test]
    fn engine_discards_rollback() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);

        let txn = engine.begin()?;
        txn.insert("t", vec![vec![Value::Integer(1)]])?;
        txn.rollback()?;

        let txn = engine.begin()?;
        txn.insert("t", vec![vec![Value::Integer(2)]])?;
        assert_eq!(ops(&engine, 1), Vec::<String>::new()); // not committed yet
        txn.commit()?;

        assert_eq!(ops(&engine, 1), vec!["3, 't', 'insert'"]);
        Ok(())
    }

    #[test]
    fn engine_read_only_commit() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);

        // A read-only transaction shares the version of the next read-write
        // transaction, but its commit doesn't commit that transaction's
        // changes.
        let read_only = engine.begin_read_only()?;
        let txn = engine.begin()?;
        assert_eq!(read_only.state().version, txn.state().version);
        txn.insert("t", vec![vec![Value::Integer(1)]])?;
        read_only.commit()?;
        assert_eq!(ops(&engine, 0), vec!["1, 't', 'create_table'"]);

        txn.commit()?;
        assert_eq!(ops(&engine, 1), vec!["2, 't', 'insert'"]);
        Ok(())
    }

    #[test]
    fn engine_resumed_transaction() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);

        // Changes are recorded by version, so a transaction can be resumed
        // between its writes and its commit (as the Raft engine does).
        let state = engine.begin()?.state().clone();
        engine.resume(state.clone())?.insert("t", vec![vec![Value::Integer(1)]])?;
        engine.resume(state.clone())?.delete("t", &[Value::Integer(1)])?;
        engine.resume(state)?.commit()?;
        assert_eq!(ops(&engine, 1), vec!["2, 't', 'insert'", "2, 't', 'delete'"]);
        Ok(())
    }

    #[test]
    fn engine_unchanged() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "DROP TABLE IF EXISTS u",
            "DROP VIEW IF EXISTS v",
            "UPDATE t SET id = 2 WHERE id = 1",
            "SELECT * FROM t",
        ]);
        // A failed statement in an implicit transaction.
        let mut session = engine.session();
        assert!(session.execute("INSERT INTO t VALUES (1), (1)").is_err());
        // A statement planned and executed without committing.
        let txn = engine.begin()?;
        let statement = Parser::parse("INSERT INTO t VALUES (3)")?;
        Plan::build(statement, &txn)?.optimize()?.execute(&txn)?;
        drop(txn);

        assert_eq!(ops(&engine, 0), vec!["1, 't', 'create_table'"]);
        Ok(())
    }
}
