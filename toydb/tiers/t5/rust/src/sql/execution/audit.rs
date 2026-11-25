//! The audit log and the session list of a server, for `SHOW AUDIT` and
//! `SHOW SESSIONS`.
//!
//! Every statement that a session executes, successfully or not, is recorded
//! after it is executed — except statements that fail to parse and SHOW AUDIT —
//! with the session's current user, the statement's class, its name (its
//! leading keywords in lower case joined by `_`, without optional words and
//! operands, as in JSON results) and its accesses as objects
//! (`<name> (<kind>)`), and its error. The class follows from the statement
//! and its accesses, the first that applies:
//!
//! 1. `ROLE`: CREATE USER, DROP USER, GRANT, REVOKE;
//! 2. `DDL`: a `create`, `drop`, `alter` or `policy` access;
//! 3. `WRITE`: an `insert`, `update`, `delete` or `refresh` access;
//! 4. `READ`: a `read` access;
//! 5. `MISC`: every other statement.
//!
//! Statements added later get their class from their accesses by the same
//! rule. Sessions are numbered in the order the server creates them. Both lists
//! are kept in memory, are not written to storage and are not replicated.

use std::collections::BTreeMap;
use std::fmt::Display;

use itertools::Itertools as _;

use super::StatementResult;
use super::privileges::{Access, AccessKind};
use crate::sql::parser::ast::Statement;
use crate::sql::types::{Label, Value};
use crate::storage::mvcc;

/// The audit class of a statement.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Class {
    Role,
    Ddl,
    Write,
    Read,
    Misc,
}

impl Display for Class {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Role => "ROLE",
            Self::Ddl => "DDL",
            Self::Write => "WRITE",
            Self::Read => "READ",
            Self::Misc => "MISC",
        })
    }
}

/// Returns the audit class of a statement, from its kind and its accesses.
pub fn class(statement: &Statement, accesses: &[Access]) -> Class {
    if matches!(
        statement,
        Statement::CreateUser { .. }
            | Statement::DropUser { .. }
            | Statement::Grant(_)
            | Statement::Revoke(_)
    ) {
        return Class::Role;
    }
    accesses.iter().map(|access| access_class(access.kind)).min().unwrap_or(Class::Misc)
}

/// Returns the class that an access of the given kind gives a statement.
fn access_class(kind: AccessKind) -> Class {
    match kind {
        AccessKind::Create | AccessKind::Drop | AccessKind::Alter | AccessKind::Policy => {
            Class::Ddl
        }
        AccessKind::Insert | AccessKind::Update | AccessKind::Delete | AccessKind::Refresh => {
            Class::Write
        }
        AccessKind::Read => Class::Read,
        AccessKind::Analyze | AccessKind::Grant => Class::Misc,
    }
}

/// Returns the name of a statement.
pub fn statement_name(statement: &Statement) -> &'static str {
    match statement {
        Statement::Begin { .. } => "begin",
        Statement::Commit => "commit",
        Statement::Rollback => "rollback",
        Statement::Explain { .. } => "explain",
        Statement::CreateTable { .. } => "create_table",
        Statement::DropTable { .. } => "drop_table",
        Statement::Delete { .. } => "delete",
        Statement::Insert { .. } => "insert",
        Statement::Update { .. } => "update",
        Statement::Select { .. } => "select",
        Statement::ShowStatements => "show_statements",
        Statement::Analyze { .. } => "analyze",
        Statement::ShowStatistics { .. } => "show_statistics",
        Statement::Export { .. } => "export",
        Statement::Import { .. } => "import",
        Statement::Format(_) => "format",
        Statement::CreateView { .. } => "create_view",
        Statement::DropView { .. } => "drop_view",
        Statement::ShowViews => "show_views",
        Statement::Describe(_) => "describe",
        Statement::Estimate(_) => "estimate",
        Statement::Prepare { .. } => "prepare",
        Statement::Execute { .. } => "execute",
        Statement::Deallocate { .. } => "deallocate",
        Statement::ShowPrepared => "show_prepared",
        Statement::Lint(_) => "lint",
        Statement::ShowChanges { .. } => "show_changes",
        Statement::CreateUser { .. } => "create_user",
        Statement::DropUser { .. } => "drop_user",
        Statement::Grant(_) => "grant",
        Statement::Revoke(_) => "revoke",
        Statement::SetRole { .. } => "set_role",
        Statement::ResetRole => "reset_role",
        Statement::ShowUsers => "show_users",
        Statement::ShowGrants => "show_grants",
        Statement::ShowAudit { .. } => "show_audit",
        Statement::ShowSessions => "show_sessions",
        Statement::CreatePolicy { .. } => "create_policy",
        Statement::DropPolicy { .. } => "drop_policy",
        Statement::CreateMaterializedView { .. } => "create_materialized_view",
        Statement::RefreshMaterializedView { .. } => "refresh_materialized_view",
        Statement::DropMaterializedView { .. } => "drop_materialized_view",
    }
}

/// An audit log entry: the user, class, statement name, objects and error of an
/// executed statement.
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub user: String,
    pub class: Class,
    pub statement: &'static str,
    pub objects: Vec<Access>,
    pub error: Option<String>,
}

impl Entry {
    /// Returns the SHOW AUDIT row of the entry, with its sequence number.
    fn to_row(&self, seq: u64) -> Vec<Value> {
        let objects = self
            .objects
            .iter()
            .map(|access| format!("{} ({})", access.name, access.kind))
            .join(", ");
        vec![
            Value::Integer(seq as i64),
            Value::String(self.user.clone()),
            Value::String(self.class.to_string()),
            Value::String(self.statement.to_string()),
            Value::String(objects),
            self.error.clone().map_or(Value::Null, Value::String),
        ]
    }
}

/// The audit log of a server: the entries in the order the statements were
/// executed.
#[derive(Debug, Default)]
pub struct AuditLog {
    /// The entries, the first with sequence number 1.
    entries: Vec<Entry>,
}

impl AuditLog {
    /// Creates an empty audit log.
    pub fn new() -> Self {
        Self::default()
    }

    /// Appends an entry to the log.
    pub fn record(&mut self, entry: Entry) {
        self.entries.push(entry);
    }

    /// Returns the SHOW AUDIT result of the entries with a sequence number
    /// above the given one.
    pub fn result(&self, since: u64) -> StatementResult {
        let rows = (1..)
            .zip(&self.entries)
            .filter(|(seq, _)| *seq > since)
            .map(|(seq, entry)| entry.to_row(seq))
            .collect();
        StatementResult::Select {
            columns: labels(&["seq", "username", "class", "statement", "objects", "error"]),
            rows,
        }
    }
}

/// An open session: its users, its transaction and the number of statements it
/// executed.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SessionInfo {
    pub user: String,
    pub login: String,
    pub txn: Option<mvcc::Version>,
    pub executed: u64,
}

/// The open sessions of a server, by number.
#[derive(Debug, Default)]
pub struct SessionList {
    /// The open sessions, by number.
    sessions: BTreeMap<u64, SessionInfo>,
    /// The number of the last session opened.
    last: u64,
}

impl SessionList {
    /// Creates an empty session list.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds a new session to the list and returns its number.
    pub fn open(&mut self, info: SessionInfo) -> u64 {
        self.last += 1;
        self.sessions.insert(self.last, info);
        self.last
    }

    /// Updates an open session.
    pub fn update(&mut self, number: u64, info: SessionInfo) {
        if let Some(session) = self.sessions.get_mut(&number) {
            *session = info;
        }
    }

    /// Removes a session from the list.
    pub fn close(&mut self, number: u64) {
        self.sessions.remove(&number);
    }

    /// Returns the SHOW SESSIONS result.
    pub fn result(&self) -> StatementResult {
        let rows = self
            .sessions
            .iter()
            .map(|(number, info)| {
                vec![
                    Value::Integer(*number as i64),
                    Value::String(info.user.clone()),
                    Value::String(info.login.clone()),
                    info.txn.map_or(Value::Null, |version| Value::Integer(version as i64)),
                    Value::Integer(info.executed as i64),
                ]
            })
            .collect();
        StatementResult::Select {
            columns: labels(&["session", "username", "login", "txn", "executed"]),
            rows,
        }
    }
}

/// Returns unqualified column labels.
fn labels(names: &[&str]) -> Vec<Label> {
    names.iter().map(|name| Label::Unqualified(name.to_string())).collect()
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::error::Result;
    use crate::sql::engine::{Engine as _, Local};
    use crate::sql::execution::privileges;
    use crate::sql::parser::Parser;
    use crate::storage::Memory;

    /// Returns the rows of a SHOW result, one string each.
    fn rows(result: StatementResult) -> Vec<String> {
        let StatementResult::Select { rows, .. } = result else {
            panic!("expected select result");
        };
        rows.into_iter().map(|row| row.into_iter().join(", ")).collect()
    }

    /// An entry of the given user and statement name, without objects.
    fn entry(user: &str, statement: &'static str) -> Entry {
        Entry { user: user.into(), class: Class::Misc, statement, objects: Vec::new(), error: None }
    }

    #[test_case("CREATE USER bob" => Class::Role; "create user")]
    #[test_case("DROP USER IF EXISTS bob" => Class::Role; "drop user")]
    #[test_case("GRANT SELECT ON t TO alice" => Class::Role; "grant privileges")]
    #[test_case("REVOKE alice FROM admin" => Class::Role; "revoke membership")]
    #[test_case("CREATE VIEW v AS SELECT * FROM t" => Class::Ddl; "create view")]
    #[test_case("DROP TABLE IF EXISTS nosuch" => Class::Ddl; "drop table")]
    #[test_case("CREATE POLICY p ON t USING (TRUE)" => Class::Ddl; "create policy")]
    #[test_case("INSERT INTO t VALUES (1)" => Class::Write; "insert")]
    #[test_case("IMPORT INTO t FROM CSV 'id'" => Class::Write; "import")]
    #[test_case("EXPLAIN DELETE FROM t" => Class::Write; "explain delete")]
    #[test_case("EXPLAIN (ANALYZE) SELECT * FROM t" => Class::Read; "explain analyze select")]
    #[test_case("SELECT * FROM t" => Class::Read; "select")]
    #[test_case("EXPORT AS SQL" => Class::Read; "export")]
    #[test_case("ANALYZE t" => Class::Misc; "analyze")]
    #[test_case("SELECT 1" => Class::Misc; "select constant")]
    #[test_case("BEGIN" => Class::Misc; "begin")]
    #[test_case("EXECUTE p (1)" => Class::Misc; "execute")]
    fn class(statement: &str) -> Class {
        let engine = Local::new(Memory::new());
        engine.session().execute("CREATE TABLE t (id INT PRIMARY KEY)").unwrap();
        let txn = engine.begin_read_only().unwrap();
        let statement = Parser::parse(statement).unwrap();
        let accesses = privileges::accesses(&txn, &statement).unwrap();
        super::class(&statement, &accesses)
    }

    #[test]
    fn class_precedence() {
        let access = |name: &str, kind| Access { name: name.into(), kind };
        let statement = Statement::ShowViews;
        let class = |accesses: &[Access]| super::class(&statement, accesses);
        assert_eq!(class(&[access("t", AccessKind::Alter)]), Class::Ddl);
        assert_eq!(class(&[access("t", AccessKind::Policy)]), Class::Ddl);
        assert_eq!(class(&[access("m", AccessKind::Refresh)]), Class::Write);
        let (read, update) = (access("a", AccessKind::Read), access("b", AccessKind::Update));
        assert_eq!(class(&[read.clone(), update]), Class::Write);
        assert_eq!(class(&[read, access("b", AccessKind::Drop)]), Class::Ddl);
        assert_eq!(class(&[access("t", AccessKind::Grant)]), Class::Misc);
        assert_eq!(class(&[]), Class::Misc);
    }

    #[test_case("SELECT 1" => "select"; "select")]
    #[test_case("CREATE TABLE t (id INT PRIMARY KEY)" => "create_table"; "create table")]
    #[test_case("EXPLAIN SELECT 1" => "explain"; "explain")]
    #[test_case("EXPLAIN (ANALYZE) SELECT 1" => "explain"; "explain analyze")]
    #[test_case("DESCRIBE t" => "describe"; "describe")]
    #[test_case("SHOW STATISTICS t" => "show_statistics"; "show statistics")]
    #[test_case("GRANT alice TO bob" => "grant"; "grant")]
    #[test_case("REVOKE SELECT ON t FROM bob" => "revoke"; "revoke")]
    #[test_case("SET ROLE alice" => "set_role"; "set role")]
    #[test_case("SHOW AUDIT SINCE 1" => "show_audit"; "show audit")]
    #[test_case("SHOW SESSIONS" => "show_sessions"; "show sessions")]
    #[test_case("CREATE POLICY p ON t TO alice USING (TRUE)" => "create_policy"; "create policy")]
    #[test_case("DROP POLICY IF EXISTS p ON t" => "drop_policy"; "drop policy")]
    fn statement_name(statement: &str) -> &'static str {
        super::statement_name(&Parser::parse(statement).unwrap())
    }

    #[test]
    fn log() {
        let mut log = AuditLog::new();
        log.record(Entry {
            user: "alice".into(),
            class: Class::Read,
            statement: "select",
            objects: vec![
                Access { name: "a".into(), kind: AccessKind::Read },
                Access { name: "b".into(), kind: AccessKind::Read },
            ],
            error: Some("invalid input: table b does not exist".into()),
        });
        log.record(entry("admin", "begin"));
        assert_eq!(
            rows(log.result(0)),
            vec![
                "1, 'alice', 'READ', 'select', 'a (read), b (read)', 'invalid input: table b does not exist'",
                "2, 'admin', 'MISC', 'begin', '', NULL",
            ]
        );
        assert_eq!(rows(log.result(1)), vec!["2, 'admin', 'MISC', 'begin', '', NULL"]);
        assert_eq!(rows(log.result(u64::MAX)), Vec::<String>::new());
    }

    #[test]
    fn session_list() {
        let info = |user: &str, txn, executed| SessionInfo {
            user: user.into(),
            login: "admin".into(),
            txn,
            executed,
        };
        let mut sessions = SessionList::new();
        assert_eq!(sessions.open(info("admin", None, 0)), 1);
        assert_eq!(sessions.open(info("admin", None, 0)), 2);
        sessions.update(2, info("alice", Some(3), 2));
        assert_eq!(
            rows(sessions.result()),
            vec!["1, 'admin', 'admin', NULL, 0", "2, 'alice', 'admin', 3, 2"]
        );

        // Numbers aren't reused after a session closes.
        sessions.close(1);
        sessions.update(1, info("bob", None, 0));
        assert_eq!(sessions.open(info("admin", None, 0)), 3);
        assert_eq!(
            rows(sessions.result()),
            vec!["2, 'alice', 'admin', 3, 2", "3, 'admin', 'admin', NULL, 0"]
        );
    }

    /// Sessions record their statements in the engine's audit log and keep
    /// their entries in the engine's session list.
    #[test]
    fn sessions() -> Result<()> {
        let engine = Local::new(Memory::new());
        let mut a = engine.session();
        let mut b = engine.session();
        a.execute("CREATE TABLE t (id INT PRIMARY KEY)")?;
        a.execute("CREATE USER alice")?;
        a.execute("GRANT SELECT ON t TO alice")?;
        a.execute("PREPARE p (INT) AS INSERT INTO t VALUES ($1)")?;
        a.execute("SET ROLE alice")?;
        a.execute("SELECT * FROM t").map(|_| ())?;
        assert!(a.execute("EXECUTE p (1)").is_err());
        assert!(a.execute("SELECT FROM t").is_err());
        assert!(a.execute("SHOW AUDIT").is_err());
        b.execute("BEGIN")?;
        b.execute("PREPARE q (INT) AS DELETE FROM t WHERE id = $1")?;
        b.execute("DESCRIBE EXECUTE q (1)").map(|_| ())?;
        assert!(b.execute("EXECUTE q ('x')").is_err());

        assert_eq!(
            rows(b.execute("SHOW SESSIONS")?),
            vec!["1, 'alice', 'admin', NULL, 7", "2, 'admin', 'admin', 5, 4"]
        );
        assert_eq!(
            rows(b.execute("SHOW AUDIT SINCE 4")?),
            vec![
                "5, 'admin', 'MISC', 'set_role', '', NULL",
                "6, 'alice', 'READ', 'select', 't (read)', NULL",
                "7, 'alice', 'WRITE', 'execute', 't (insert)', 'permission denied: user alice has no INSERT privilege on t'",
                "8, 'admin', 'MISC', 'begin', '', NULL",
                "9, 'admin', 'MISC', 'prepare', '', NULL",
                "10, 'admin', 'WRITE', 'describe', 't (delete)', NULL",
                "11, 'admin', 'MISC', 'execute', '', 'invalid input: invalid datatype STRING for INTEGER parameter $1'",
                "12, 'admin', 'MISC', 'show_sessions', '', NULL",
            ]
        );

        // A dropped session leaves the list.
        drop(a);
        assert_eq!(rows(b.execute("SHOW SESSIONS")?), vec!["2, 'admin', 'admin', 5, 5"]);
        Ok(())
    }
}
