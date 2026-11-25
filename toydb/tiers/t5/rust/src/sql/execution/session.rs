use std::fmt::Display;

use itertools::Itertools as _;
use log::error;
use serde::{Deserialize, Serialize};

use super::audit::{self, SessionInfo};
use super::digests::Digests;
use super::import::ImportRow;
use super::prepared::PreparedStatements;
use super::system::SessionState;
use super::{analyze, export, import, lint, matviews, policies, privileges, profile, users, views};
use crate::backup;
use crate::error::{Error, Result};
use crate::sql::engine::{Catalog, Engine, Raft, Status, Transaction as _, changes};
use crate::sql::execution::ExecutionResult;
use crate::sql::parser::{self, Parser, ast, format};
use crate::sql::planner::{Plan, describe, estimate};
use crate::sql::types::{ADMIN, DataType, Label, Row, Rows, Table, Value};
use crate::storage::mvcc;
use crate::{errdata, errinput};

/// A SQL client session. Parses and executes raw SQL statements and handles
/// transaction control. Keeps statistics about executed statements.
pub struct Session<'a, E: Engine<'a>> {
    /// The SQL engine.
    engine: &'a E,
    /// The current transaction, if any.
    txn: Option<E::Transaction>,
    /// Statement statistics, by statement digest.
    digests: Digests,
    /// Prepared statements, by name.
    prepared: PreparedStatements,
    /// The login user.
    login_user: String,
    /// The current user.
    current_user: String,
    /// Whether the session has logged in or executed a statement.
    started: bool,
    /// The session's number in the engine's session list.
    number: u64,
    /// The number of statements the session executed.
    executed: u64,
}

impl<'a, E: Engine<'a>> Session<'a, E> {
    /// Creates a new session using the given SQL engine.
    pub fn new(engine: &'a E) -> Self {
        let mut session = Self {
            engine,
            txn: None,
            digests: Digests::new(),
            prepared: PreparedStatements::new(),
            login_user: ADMIN.to_string(),
            current_user: ADMIN.to_string(),
            started: false,
            number: 0,
            executed: 0,
        };
        session.number =
            engine.sessions().lock().expect("session list lock failed").open(session.info());
        session
    }

    /// Logs in as a user.
    pub fn login(&mut self, user: &str, password: Option<&str>) -> Result<()> {
        if self.started {
            return errinput!("session already started");
        }
        self.with_txn(true, |txn| users::check_login(txn, user, password))?;
        self.login_authenticated(user)
    }

    /// Logs in as a user that the caller has authenticated.
    pub fn login_authenticated(&mut self, user: &str) -> Result<()> {
        if self.started {
            return errinput!("session already started");
        }
        self.login_user = user.to_string();
        self.current_user = user.to_string();
        self.started = true;
        self.update_session_list()
    }

    /// Returns the session's number in the engine's session list.
    pub fn number(&self) -> u64 {
        self.number
    }

    /// Returns true if the session has an open transaction.
    pub fn in_transaction(&self) -> bool {
        self.txn.is_some()
    }

    /// Returns the statement of a prepared statement, if it exists.
    pub fn prepared_statement(&self, name: &str) -> Option<&ast::Statement> {
        self.prepared.statement(name)
    }

    /// Executes a client statement.
    pub fn execute(&mut self, statement: &str) -> Result<StatementResult> {
        self.started = true;
        // Statements that fail to parse aren't recorded in the statement
        // statistics or the audit log.
        let statement = Parser::parse(statement)?;
        self.execute_parsed(statement)
    }

    /// Executes a parsed client statement.
    pub fn execute_parsed(&mut self, statement: ast::Statement) -> Result<StatementResult> {
        self.started = true;
        // SHOW STATEMENTS and SHOW AUDIT themselves aren't recorded in the
        // statement statistics or the audit log.
        let digest = match statement {
            ast::Statement::ShowStatements => None,
            ref statement => Some(parser::digest(statement)),
        };
        let entry = match statement {
            ast::Statement::ShowAudit { .. } => None,
            ref statement => Some(self.audit_entry(statement)?),
        };
        let result = self.execute_statement(statement);
        if let Some(digest) = digest {
            self.digests.record(digest, result.as_ref().ok().map(|(_, rows)| *rows));
        }
        if let Some(entry) = entry {
            self.record(entry, result.as_ref().err())?;
        }
        self.update_session_list()?;
        result.map(|(result, _)| result)
    }

    /// Executes COPY ... TO STDOUT of the PostgreSQL protocol.
    pub fn copy_to<CopyError: From<Error> + Display>(
        &mut self,
        table: &str,
        send: impl FnOnce(&Table, Vec<Row>) -> std::result::Result<u64, CopyError>,
    ) -> std::result::Result<u64, CopyError> {
        let format = ast::ExportFormat::Csv;
        let statement = ast::Statement::Export { table: Some(table.to_string()), format };
        let entry = self.audit_entry(&statement)?;
        let check = self.check(&statement);
        let user = self.current_user.clone();
        let rows = self.with_txn(true, |txn| {
            check.run(txn)?;
            let table = txn.must_get_table(table)?;
            let rows: Vec<Row> = policies::scan(txn, &table, &user)?.try_collect()?;
            Ok((table, rows))
        });
        let result = match rows {
            Ok((table, rows)) => send(&table, rows),
            Err(error) => Err(error.into()),
        };
        self.record_copy(entry, &result)?;
        result
    }

    /// Executes COPY ... FROM STDIN of the PostgreSQL protocol.
    pub fn copy_from<CopyError: From<Error> + Display>(
        &mut self,
        table: &str,
        receive: impl FnOnce(&Table) -> std::result::Result<Vec<ImportRow>, CopyError>,
    ) -> std::result::Result<u64, CopyError> {
        let (format, lines) = (ast::ImportFormat::Csv, Vec::new());
        let statement = ast::Statement::Import { table: table.to_string(), format, lines };
        let entry = self.audit_entry(&statement)?;
        let check = self.check(&statement);
        let user = self.current_user.clone();
        let result = match self.with_txn(true, |txn| {
            check.run(txn)?;
            txn.must_get_table(table)
        }) {
            Ok(table) => receive(&table).and_then(|rows| {
                Ok(self.with_txn(false, |txn| {
                    check.run(txn)?;
                    import::insert(txn, table, rows, &user)
                })?)
            }),
            Err(error) => Err(error.into()),
        };
        self.record_copy(entry, &result)?;
        result
    }

    /// Creates a backup of the database, as of a version if given.
    pub fn backup(&mut self, as_of: Option<mvcc::Version>) -> Result<Vec<u8>> {
        let user = self.current_user.clone();
        let Some(version) = as_of else {
            return self.with_txn(true, |txn| backup::create(txn, &user));
        };
        if self.txn.is_some() {
            return errinput!("already in a transaction");
        }
        let txn = self.engine.begin_as_of(version)?;
        let result = backup::create(&txn, &user);
        match result {
            Ok(_) => txn.commit()?,
            Err(_) => txn.rollback()?,
        }
        result
    }

    /// Restores a backup into the database.
    pub fn restore(&mut self, data: &[u8]) -> Result<()> {
        let user = self.current_user.clone();
        let session = Self::session_state(self.engine, &self.prepared, &self.digests);
        Self::with_engine_txn(self.engine, &mut self.txn, false, |txn| {
            backup::restore(txn, &user, data, Some(&session))
        })
    }

    /// Records an executed statement in the audit log, with its error.
    fn record(&mut self, entry: audit::Entry, error: Option<&impl Display>) -> Result<()> {
        let error = error.map(|error| error.to_string());
        self.engine.audit().lock()?.record(audit::Entry { error, ..entry });
        self.executed += 1;
        Ok(())
    }

    /// Records a COPY command in the audit log, with its error.
    fn record_copy<T>(
        &mut self,
        entry: audit::Entry,
        result: &std::result::Result<T, impl Display>,
    ) -> Result<()> {
        self.record(audit::Entry { statement: "copy", ..entry }, result.as_ref().err())?;
        self.update_session_list()
    }

    /// Returns the audit log entry of a statement, without its error.
    fn audit_entry(&mut self, statement: &ast::Statement) -> Result<audit::Entry> {
        // EXECUTE has the accesses of the statement it executes, also when
        // explained, described or estimated.
        let executed = match statement {
            ast::Statement::Explain { statement, .. }
            | ast::Statement::Describe(ast::Describe::Statement(statement))
            | ast::Statement::Estimate(statement) => statement.as_ref(),
            statement => statement,
        };
        // Arguments containing parameters can't be bound: the statement fails
        // before it is executed.
        let bound = match executed {
            ast::Statement::Execute { name, arguments } if executed.parameters().is_empty() => self
                .prepared
                .bind::<E::Transaction>(name, arguments.clone())
                .ok()
                .map(|(statement, _)| statement),
            _ => None,
        };
        let audited = bound.as_ref().unwrap_or(statement);
        let accesses = self.with_txn(true, |txn| privileges::accesses(txn, audited))?;
        Ok(audit::Entry {
            user: self.current_user.clone(),
            class: audit::class(audited, &accesses),
            statement: audit::statement_name(statement),
            objects: accesses,
            error: None,
        })
    }

    /// Returns the session's entry in the engine's session list.
    fn info(&self) -> SessionInfo {
        SessionInfo {
            user: self.current_user.clone(),
            login: self.login_user.clone(),
            txn: self.txn.as_ref().map(|txn| txn.state().version),
            executed: self.executed,
        }
    }

    /// Updates the session's entry in the engine's session list.
    fn update_session_list(&self) -> Result<()> {
        self.engine.sessions().lock()?.update(self.number, self.info());
        Ok(())
    }

    /// Executes a parsed statement. Returns the result along with the number
    /// of rows it produced (returned, written, imported or exported), for the
    /// statement statistics.
    fn execute_statement(&mut self, statement: ast::Statement) -> Result<(StatementResult, u64)> {
        // Reject parameters outside of PREPARE, FORMAT and LINT.
        if !matches!(
            statement,
            ast::Statement::Format(_) | ast::Statement::Lint(_) | ast::Statement::Prepare { .. }
        ) {
            if let Some(n) = statement.parameters().first() {
                return errinput!("parameter ${n} can only be used in a prepared statement");
            }
        }

        // Every statement is checked against the current user in its
        // transaction, before it does anything else. EXPLAIN, DESCRIBE,
        // ESTIMATE and EXECUTE check the statement they plan instead.
        let check = self.check(&statement);

        // Transaction control and the statements that aren't planned are
        // handled here, other statements are handled by the SQL executor.
        Ok(match statement {
            // BEGIN: starts a new transaction and returns its state.
            ast::Statement::Begin { read_only, as_of } => {
                self.check_without_txn(&check)?;
                if self.txn.is_some() {
                    return errinput!("already in a transaction");
                }
                let txn = match (read_only, as_of) {
                    (false, None) => self.engine.begin()?,
                    (true, None) => self.engine.begin_read_only()?,
                    (true, Some(as_of)) => self.engine.begin_as_of(as_of)?,
                    (false, Some(_)) => {
                        return errinput!("can't start read-write transaction in a given version");
                    }
                };
                let state = txn.state().clone();
                self.txn = Some(txn);
                (StatementResult::Begin(state), 0)
            }

            // COMMIT: commits the currently open transaction, if any.
            ast::Statement::Commit => {
                self.check_without_txn(&check)?;
                let Some(txn) = self.txn.take() else {
                    return errinput!("not in a transaction");
                };
                let version = txn.state().version;
                txn.commit()?;
                (StatementResult::Commit { version }, 0)
            }

            // ROLLBACK: rolls back the currently open transaction, if any.
            ast::Statement::Rollback => {
                self.check_without_txn(&check)?;
                let Some(txn) = self.txn.take() else {
                    return errinput!("not in a transaction");
                };
                let version = txn.state().version;
                txn.rollback()?;
                (StatementResult::Rollback { version }, 0)
            }

            // EXPLAIN: returns the given SQL query's plan. Only planned
            // statements can be explained.
            ast::Statement::Explain { statement, analyze: false } => {
                let statement = match *statement {
                    // Transaction statements are passed on to the planner, as
                    // they always have been.
                    statement @ (ast::Statement::Begin { .. }
                    | ast::Statement::Commit
                    | ast::Statement::Rollback) => statement,
                    statement => self.planned_statement("EXPLAIN", statement)?,
                };
                let check = self.check(&statement);
                let plan = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    Plan::build(statement, txn)?.optimize()
                })?;
                (StatementResult::Explain(plan), 0)
            }

            // EXPLAIN (ANALYZE): executes a planned statement, in the
            // transaction that the statement would use, and returns its plan
            // with the estimated and the actual rows of every line.
            ast::Statement::Explain { statement, analyze: true } => {
                let statement = self.planned_statement("EXPLAIN", *statement)?;
                let read_only = matches!(statement, ast::Statement::Select { .. });
                let check = self.check(&statement);
                let user = self.current_user.clone();
                let session = Self::session_state(self.engine, &self.prepared, &self.digests);
                let result = Self::with_engine_txn(self.engine, &mut self.txn, read_only, |txn| {
                    check.run(txn)?;
                    let plan = Plan::build(statement, txn)?.optimize()?;
                    let estimates = estimate::estimate(&plan, txn)?;
                    let display = plan.to_string();
                    let (result, rows) = profile::profile(plan, txn, &user, Some(&session))?;
                    Self::set_creator(&result, txn, &user)?;
                    Ok(profile::result(&display, estimates, rows))
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // SHOW STATEMENTS: returns the session's statement statistics. It
            // doesn't use a transaction.
            ast::Statement::ShowStatements => {
                self.check_without_txn(&check)?;
                (self.digests.to_result(), 0)
            }

            // ANALYZE: computes and stores column statistics.
            ast::Statement::Analyze { table } => {
                let tables = self.with_txn(false, |txn| {
                    check.run(txn)?;
                    analyze::analyze(txn, table.as_deref())
                })?;
                (StatementResult::Analyze { tables }, 0)
            }

            // SHOW STATISTICS: returns a table's column statistics.
            ast::Statement::ShowStatistics { table } => {
                let result = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    analyze::show_statistics(txn, &table)
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // EXPORT: exports a table or the database as text lines.
            ast::Statement::Export { table, format } => {
                let user = self.current_user.clone();
                let export = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    export::export(txn, table.as_deref(), format, &user)
                })?;
                (StatementResult::Export { format, lines: export.lines }, export.rows)
            }

            // IMPORT: inserts rows from text lines. The result is that of an
            // INSERT.
            ast::Statement::Import { table, format, lines } => {
                let user = self.current_user.clone();
                let count = self.with_txn(false, |txn| {
                    check.run(txn)?;
                    import::import(txn, &table, format, &lines, &user)
                })?;
                (StatementResult::Insert { count }, count)
            }

            // FORMAT: returns the canonical text of the statement.
            ast::Statement::Format(statement) => {
                self.check_without_txn(&check)?;
                (StatementResult::Format { lines: format::lines(&statement) }, 0)
            }

            // CREATE VIEW: creates a view.
            ast::Statement::CreateView { name, columns, query } => {
                let user = self.current_user.clone();
                self.with_txn(false, |txn| {
                    check.run(txn)?;
                    views::create_view(txn, &name, columns, *query)?;
                    users::set_creator(txn, &name, &user)
                })?;
                (StatementResult::CreateView { name }, 0)
            }

            // DROP VIEW: drops a view.
            ast::Statement::DropView { name, if_exists } => {
                let existed = self.with_txn(false, |txn| {
                    check.run(txn)?;
                    views::drop_view(txn, &name, if_exists)
                })?;
                (StatementResult::DropView { name, existed }, 0)
            }

            // SHOW VIEWS: lists the views.
            ast::Statement::ShowViews => {
                let result = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    views::show_views(txn)
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // DESCRIBE: describes the columns of a table, view or system table.
            ast::Statement::Describe(ast::Describe::Name(name)) => {
                let columns = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    describe::describe_name(txn, &name)
                })?;
                let result = describe::result(columns);
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // DESCRIBE: describes the result columns of a planned statement,
            // from its plan before optimization.
            ast::Statement::Describe(ast::Describe::Statement(statement)) => {
                let statement = self.planned_statement("DESCRIBE", *statement)?;
                let check = self.check(&statement);
                let plan = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    Plan::build(statement, txn)
                })?;
                let result = describe::result(describe::describe(&plan));
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // ESTIMATE: estimates the rows of each line of a planned
            // statement's plan, which is planned and optimized as by EXPLAIN.
            ast::Statement::Estimate(statement) => {
                let statement = self.planned_statement("ESTIMATE", *statement)?;
                let check = self.check(&statement);
                let (plan, estimates) = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    let plan = Plan::build(statement, txn)?.optimize()?;
                    let estimates = estimate::estimate(&plan, txn)?;
                    Ok((plan, estimates))
                })?;
                let result = estimate::result(&plan, estimates);
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // PREPARE: prepares a statement for the session. It doesn't use a
            // transaction.
            ast::Statement::Prepare { name, types, statement } => {
                self.check_without_txn(&check)?;
                self.prepared.prepare(name.clone(), types, *statement)?;
                (StatementResult::Prepare { name }, 0)
            }

            // EXECUTE: executes a prepared statement with the given arguments,
            // in the transaction that the statement would use.
            ast::Statement::Execute { name, arguments } => {
                let (statement, arguments) =
                    self.prepared.execute::<E::Transaction>(&name, arguments)?;
                let read_only = matches!(statement, ast::Statement::Select { .. });
                let check = self.check(&statement);
                let user = self.current_user.clone();
                let (engine, digests) = (self.engine, &self.digests);
                let prepared = &mut self.prepared;
                let result = Self::with_engine_txn(engine, &mut self.txn, read_only, |txn| {
                    check.run(txn)?;
                    let plan = prepared.plan(&name, statement, arguments, txn)?;
                    let session = Self::session_state(engine, prepared, digests);
                    Self::execute_plan(plan, txn, &user, &session)
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // DEALLOCATE: removes a prepared statement.
            ast::Statement::Deallocate { name } => {
                self.check_without_txn(&check)?;
                self.prepared.deallocate(&name)?;
                (StatementResult::Deallocate { name }, 0)
            }

            // SHOW PREPARED: lists the session's prepared statements. It
            // doesn't use a transaction.
            ast::Statement::ShowPrepared => {
                self.check_without_txn(&check)?;
                let result = self.prepared.show();
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // LINT: returns warnings about the statement and the statements it
            // contains, without planning or executing them.
            ast::Statement::Lint(statement) => {
                let warnings = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    lint::lint(&statement, txn)
                })?;
                let result = lint::result(warnings);
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // SHOW CHANGES: lists the committed changes from the engine's
            // change log. It doesn't use a transaction.
            ast::Statement::ShowChanges { since } => {
                self.check_without_txn(&check)?;
                let result = changes::result(self.engine.changes(since.unwrap_or(0))?);
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // SHOW AUDIT: lists the server's audit log. It doesn't use a
            // transaction.
            ast::Statement::ShowAudit { since } => {
                self.check_without_txn(&check)?;
                let result = self.engine.audit().lock()?.result(since.unwrap_or(0));
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // SHOW SESSIONS: lists the server's sessions. It doesn't use a
            // transaction.
            ast::Statement::ShowSessions => {
                self.check_without_txn(&check)?;
                let result = self.engine.sessions().lock()?.result();
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // CREATE USER: creates a user.
            ast::Statement::CreateUser { name, password, superuser } => {
                self.with_txn(false, |txn| {
                    check.run(txn)?;
                    users::create_user(txn, &name, password.as_deref(), superuser)
                })?;
                (StatementResult::CreateUser { name }, 0)
            }

            // DROP USER: drops a user.
            ast::Statement::DropUser { name, if_exists } => {
                let (login, current) = (self.login_user.clone(), self.current_user.clone());
                let existed = self.with_txn(false, |txn| {
                    check.run(txn)?;
                    users::drop_user(txn, &name, if_exists, &login, &current)
                })?;
                (StatementResult::DropUser { name, existed }, 0)
            }

            // GRANT: grants privileges on a table or view, or makes a user a member of
            // another.
            ast::Statement::Grant(ast::Grant::Privileges { privileges, object, user }) => {
                self.with_txn(false, |txn| {
                    check.run(txn)?;
                    users::grant(txn, &privileges, &object, &user)
                })?;
                (StatementResult::Grant { table: Some(object) }, 0)
            }
            ast::Statement::Grant(ast::Grant::Membership { group, user }) => {
                self.with_txn(false, |txn| {
                    check.run(txn)?;
                    users::grant_membership(txn, &group, &user)
                })?;
                (StatementResult::Grant { table: None }, 0)
            }

            // REVOKE: revokes privileges on a table or view, or ends a membership.
            ast::Statement::Revoke(ast::Grant::Privileges { privileges, object, user }) => {
                self.with_txn(false, |txn| {
                    check.run(txn)?;
                    users::revoke(txn, &privileges, &object, &user)
                })?;
                (StatementResult::Revoke { table: Some(object) }, 0)
            }
            ast::Statement::Revoke(ast::Grant::Membership { group, user }) => {
                self.with_txn(false, |txn| {
                    check.run(txn)?;
                    users::revoke_membership(txn, &group, &user)
                })?;
                (StatementResult::Revoke { table: None }, 0)
            }

            // SET ROLE: sets the current user. It doesn't use a transaction.
            ast::Statement::SetRole { user } => {
                self.with_txn(true, |txn| {
                    users::must_get_user(txn, &user)?;
                    check.run(txn)
                })?;
                self.current_user = user.clone();
                (StatementResult::SetRole { user }, 0)
            }

            // RESET ROLE: sets the current user to the login user. It doesn't
            // use a transaction.
            ast::Statement::ResetRole => {
                self.check_without_txn(&check)?;
                self.current_user = self.login_user.clone();
                (StatementResult::ResetRole { user: self.current_user.clone() }, 0)
            }

            // SHOW USERS: lists the users.
            ast::Statement::ShowUsers => {
                let result = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    users::show_users(txn)
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // SHOW GRANTS: lists the owners and privileges of tables and views.
            ast::Statement::ShowGrants => {
                let result = self.with_txn(true, |txn| {
                    check.run(txn)?;
                    users::show_grants(txn)
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // CREATE POLICY: creates a row-level security policy.
            ast::Statement::CreatePolicy { name, table, user, using, with_check } => {
                self.with_txn(false, |txn| {
                    check.run(txn)?;
                    let user = user.as_deref();
                    policies::create_policy(txn, &name, &table, user, &using, with_check.as_ref())
                })?;
                (StatementResult::CreatePolicy { table, name }, 0)
            }

            // DROP POLICY: drops a row-level security policy.
            ast::Statement::DropPolicy { name, table, if_exists } => {
                let existed = self.with_txn(false, |txn| {
                    check.run(txn)?;
                    policies::drop_policy(txn, &name, &table, if_exists)
                })?;
                (StatementResult::DropPolicy { table, name, existed }, 0)
            }

            // CREATE MATERIALIZED VIEW: creates a materialized view.
            ast::Statement::CreateMaterializedView { name, columns, query } => {
                let user = self.current_user.clone();
                let session = Self::session_state(self.engine, &self.prepared, &self.digests);
                let count = Self::with_engine_txn(self.engine, &mut self.txn, false, |txn| {
                    check.run(txn)?;
                    let count = matviews::create_materialized_view(
                        txn,
                        &name,
                        columns,
                        *query,
                        &user,
                        Some(&session),
                    )?;
                    users::set_creator(txn, &name, &user)?;
                    Ok(count)
                })?;
                (StatementResult::CreateMaterializedView { name, count }, 0)
            }

            // REFRESH MATERIALIZED VIEW: recomputes a materialized view's rows.
            ast::Statement::RefreshMaterializedView { name } => {
                let user = self.current_user.clone();
                let session = Self::session_state(self.engine, &self.prepared, &self.digests);
                let count = Self::with_engine_txn(self.engine, &mut self.txn, false, |txn| {
                    check.run(txn)?;
                    matviews::refresh_materialized_view(txn, &name, &user, Some(&session))
                })?;
                (StatementResult::RefreshMaterializedView { name, count }, 0)
            }

            // DROP MATERIALIZED VIEW: drops a materialized view.
            ast::Statement::DropMaterializedView { name, if_exists } => {
                let existed = self.with_txn(false, |txn| {
                    check.run(txn)?;
                    matviews::drop_materialized_view(txn, &name, if_exists)
                })?;
                (StatementResult::DropMaterializedView { name, existed }, 0)
            }

            // Other statements (SELECT etc.) are handled by the SQL executor.
            statement => {
                let read_only = matches!(statement, ast::Statement::Select { .. });
                let user = self.current_user.clone();
                let session = Self::session_state(self.engine, &self.prepared, &self.digests);
                let result = Self::with_engine_txn(self.engine, &mut self.txn, read_only, |txn| {
                    check.run(txn)?;
                    let plan = Plan::build(statement, txn)?.optimize()?;
                    Self::execute_plan(plan, txn, &user, &session)
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }
        })
    }

    /// Describes the labels and data types of a statement's result columns.
    pub fn describe_columns(
        &mut self,
        statement: ast::Statement,
    ) -> Result<Vec<(Label, Option<DataType>)>> {
        let statement = self.planned_statement("DESCRIBE", statement)?;
        let check = self.check(&statement);
        let plan = self.with_txn(true, |txn| {
            check.run(txn)?;
            Plan::build(statement, txn)
        })?;
        // Only queries have result columns.
        let labels = match &plan {
            Plan::Select(root) => (0..root.columns()).map(|i| root.column_label(i)).collect(),
            _ => Vec::new(),
        };
        let datatypes = describe::describe(&plan).into_iter().map(|column| column.datatype);
        Ok(labels.into_iter().zip(datatypes).collect())
    }

    /// Returns the privilege check of a statement for the session's users.
    fn check(&self, statement: &ast::Statement) -> PrivilegeCheck {
        PrivilegeCheck {
            statement: statement.clone(),
            login: self.login_user.clone(),
            user: self.current_user.clone(),
        }
    }

    /// Runs the privilege check of a statement that doesn't use a transaction.
    fn check_without_txn(&mut self, check: &PrivilegeCheck) -> Result<()> {
        self.with_txn(true, |txn| check.run(txn))
    }

    /// Executes a plan as the given user, who becomes the owner of a table it
    /// creates.
    fn execute_plan(
        plan: Plan,
        txn: &E::Transaction,
        user: &str,
        session: &SessionState,
    ) -> Result<StatementResult> {
        let result = plan.execute(txn, user, Some(session))?;
        Self::set_creator(&result, txn, user)?;
        result.try_into()
    }

    /// Makes the user the owner of a table that an execution result created.
    fn set_creator(result: &ExecutionResult, txn: &E::Transaction, user: &str) -> Result<()> {
        if let ExecutionResult::CreateTable { name } = result {
            users::set_creator(txn, name, user)?;
        }
        Ok(())
    }

    /// Returns the planned statement that EXPLAIN, DESCRIBE or ESTIMATE (the
    /// given word) work on: the statement itself, or for EXECUTE the prepared
    /// statement with its arguments bound. Errors for other statements.
    fn planned_statement(&self, word: &str, statement: ast::Statement) -> Result<ast::Statement> {
        if let ast::Statement::Execute { name, arguments } = statement {
            let (statement, _) = self.prepared.bind::<E::Transaction>(&name, arguments)?;
            return Ok(statement);
        }
        if let Some(keyword) = unplanned_keyword(&statement) {
            return errinput!("{word} is not supported for {keyword}");
        }
        Ok(statement)
    }

    /// Returns the session state that system tables show.
    fn session_state<'s>(
        engine: &'a E,
        prepared: &'s PreparedStatements,
        digests: &'s Digests,
    ) -> SessionState<'s>
    where
        'a: 's,
    {
        SessionState { audit: engine.audit(), sessions: engine.sessions(), prepared, digests }
    }

    /// Returns the number of rows that a statement result returned or wrote,
    /// for the statement statistics.
    fn result_rows(result: &StatementResult) -> u64 {
        match result {
            StatementResult::Select { rows, .. } => rows.len() as u64,
            StatementResult::Delete { count }
            | StatementResult::Insert { count }
            | StatementResult::Update { count } => *count,
            // Other results don't return or write rows.
            _ => 0,
        }
    }

    /// Runs a closure in the session's transaction, if there is one, otherwise
    /// a temporary implicit transaction. If read_only is true, uses a read-only
    /// implicit transaction. Does not automatically retry errors.
    pub fn with_txn<F, T>(&mut self, read_only: bool, f: F) -> Result<T>
    where
        F: FnOnce(&mut E::Transaction) -> Result<T>,
    {
        Self::with_engine_txn(self.engine, &mut self.txn, read_only, f)
    }

    /// Runs a closure as with_txn, given the session's engine and transaction,
    /// so that the closure can borrow the session's other fields.
    fn with_engine_txn<F, T>(
        engine: &'a E,
        txn: &mut Option<E::Transaction>,
        read_only: bool,
        f: F,
    ) -> Result<T>
    where
        F: FnOnce(&mut E::Transaction) -> Result<T>,
    {
        // Use the current explicit transaction, if there is one.
        if let Some(txn) = txn {
            return f(txn);
        }
        // Otherwise, use an implicit transaction. Doing this session-side
        // results in additional Raft roundtrips to begin and complete the
        // transaction -- we could avoid this if the Raft SQL state machine
        // supported implicit transactions, but we keep it simple.
        let mut txn = match read_only {
            true => engine.begin_read_only()?,
            false => engine.begin()?,
        };
        let result = f(&mut txn);
        match result {
            Ok(_) => txn.commit()?,
            Err(_) => txn.rollback()?,
        }
        result
    }
}

/// The privilege check of a statement, for a session's login and current user.
struct PrivilegeCheck {
    statement: ast::Statement,
    login: String,
    user: String,
}

impl PrivilegeCheck {
    /// Runs the check in the given transaction.
    fn run(&self, catalog: &impl Catalog) -> Result<()> {
        privileges::check(catalog, &self.login, &self.user, &self.statement)
    }
}

/// Returns the leading keywords of a statement that has no plan of its own, for
/// errors about statements that require a planned statement, or None if the
/// statement has a plan.
pub fn unplanned_keyword(statement: &ast::Statement) -> Option<&'static str> {
    use ast::Statement::*;
    match statement {
        CreateTable { .. }
        | DropTable { .. }
        | Delete { .. }
        | Insert { .. }
        | Update { .. }
        | Select { .. } => None,
        Begin { .. } => Some("BEGIN"),
        Commit => Some("COMMIT"),
        Rollback => Some("ROLLBACK"),
        Explain { .. } => Some("EXPLAIN"),
        ShowStatements
        | ShowStatistics { .. }
        | ShowViews
        | ShowPrepared
        | ShowChanges { .. }
        | ShowUsers
        | ShowGrants
        | ShowAudit { .. }
        | ShowSessions => Some("SHOW"),
        Analyze { .. } => Some("ANALYZE"),
        Export { .. } => Some("EXPORT"),
        Import { .. } => Some("IMPORT"),
        Format(_) => Some("FORMAT"),
        CreateView { .. } => Some("CREATE VIEW"),
        DropView { .. } => Some("DROP VIEW"),
        Describe(_) => Some("DESCRIBE"),
        Estimate(_) => Some("ESTIMATE"),
        Prepare { .. } => Some("PREPARE"),
        Execute { .. } => Some("EXECUTE"),
        Deallocate { .. } => Some("DEALLOCATE"),
        Lint(_) => Some("LINT"),
        CreateUser { .. } => Some("CREATE USER"),
        DropUser { .. } => Some("DROP USER"),
        Grant(_) => Some("GRANT"),
        Revoke(_) => Some("REVOKE"),
        SetRole { .. } => Some("SET"),
        ResetRole => Some("RESET"),
        CreatePolicy { .. } => Some("CREATE POLICY"),
        DropPolicy { .. } => Some("DROP POLICY"),
        CreateMaterializedView { .. } => Some("CREATE MATERIALIZED"),
        RefreshMaterializedView { .. } => Some("REFRESH"),
        DropMaterializedView { .. } => Some("DROP MATERIALIZED"),
    }
}

impl Session<'_, Raft> {
    /// Returns the Raft SQL engine status.
    pub fn status(&self) -> Result<Status> {
        self.engine.status()
    }
}

/// If the session has an open transaction when dropped, roll it back.
impl<'a, E: Engine<'a>> Drop for Session<'a, E> {
    fn drop(&mut self) {
        if let Ok(mut sessions) = self.engine.sessions().lock() {
            sessions.close(self.number);
        }
        let Some(txn) = self.txn.take() else { return };
        if let Err(error) = txn.rollback() {
            error!("implicit transaction rollback failed: {error}")
        }
    }
}

/// A session statement result, returned over the network to SQL clients.
#[derive(Debug, PartialEq, Serialize, Deserialize)]
pub enum StatementResult {
    Begin(mvcc::TransactionState),
    Commit { version: mvcc::Version },
    Rollback { version: mvcc::Version },
    Explain(Plan),
    CreateTable { name: String },
    DropTable { name: String, existed: bool },
    Delete { count: u64 },
    Insert { count: u64 },
    Update { count: u64 },
    // For simplicity, we buffer and send the entire set of rows as a vector
    // instead of streaming them to the client. Streaming reads haven't been
    // implemented from Raft either, so they're buffered all the way through.
    Select { columns: Vec<Label>, rows: Vec<Row> },
    // New variants must be appended, since the enum is a network protocol.
    Analyze { tables: Vec<String> },
    Export { format: ast::ExportFormat, lines: Vec<String> },
    /// The lines of FORMAT.
    Format { lines: Vec<String> },
    CreateView { name: String },
    DropView { name: String, existed: bool },
    Prepare { name: String },
    Deallocate { name: String },
    CreateUser { name: String },
    DropUser { name: String, existed: bool },
    Grant { table: Option<String> },
    Revoke { table: Option<String> },
    SetRole { user: String },
    ResetRole { user: String },
    CreatePolicy { table: String, name: String },
    DropPolicy { table: String, name: String, existed: bool },
    CreateMaterializedView { name: String, count: u64 },
    RefreshMaterializedView { name: String, count: u64 },
    DropMaterializedView { name: String, existed: bool },
}

/// Converts an execution result into a statement result.
impl TryFrom<ExecutionResult> for StatementResult {
    type Error = Error;

    fn try_from(result: ExecutionResult) -> Result<Self> {
        Ok(match result {
            ExecutionResult::CreateTable { name } => Self::CreateTable { name },
            ExecutionResult::DropTable { name, existed } => Self::DropTable { name, existed },
            ExecutionResult::Delete { count } => Self::Delete { count },
            ExecutionResult::Insert { count } => Self::Insert { count },
            ExecutionResult::Update { count } => Self::Update { count },
            ExecutionResult::Select { rows, columns } => {
                // We buffer the entire set of rows, for simplicity.
                Self::Select { columns, rows: rows.try_collect()? }
            }
        })
    }
}

/// Attempts to convert a SELECT result into a row iterator.
impl TryFrom<StatementResult> for Rows {
    type Error = Error;

    fn try_from(result: StatementResult) -> Result<Self> {
        let StatementResult::Select { rows, .. } = result else {
            return errdata!("expected select result, found {result:?}");
        };
        Ok(Box::new(rows.into_iter().map(Ok)))
    }
}

/// Extracts the first row from a SELECT result.
impl TryFrom<StatementResult> for Row {
    type Error = Error;

    fn try_from(result: StatementResult) -> Result<Self> {
        let mut rows: Rows = result.try_into()?;
        rows.next().transpose()?.ok_or_else(|| errdata!("no rows returned"))
    }
}

/// Extracts the value of the first column in the first row.
impl TryFrom<StatementResult> for Value {
    type Error = Error;

    fn try_from(result: StatementResult) -> Result<Self> {
        let row: Row = result.try_into()?;
        row.into_iter().next().ok_or_else(|| errdata!("no columns returned"))
    }
}

/// Extracts the first boolean value of the first column in the first row.
impl TryFrom<StatementResult> for bool {
    type Error = Error;

    fn try_from(result: StatementResult) -> Result<Self> {
        Value::try_from(result)?.try_into()
    }
}

/// Extracts the first f64 value of the first column in the first row.
impl TryFrom<StatementResult> for f64 {
    type Error = Error;

    fn try_from(result: StatementResult) -> Result<Self> {
        Value::try_from(result)?.try_into()
    }
}

/// Extracts the first i64 value of the first column in the first row.
impl TryFrom<StatementResult> for i64 {
    type Error = Error;

    fn try_from(result: StatementResult) -> Result<Self> {
        Value::try_from(result)?.try_into()
    }
}

/// Extracts the first string value of the first column in the first row.
impl TryFrom<StatementResult> for String {
    type Error = Error;

    fn try_from(result: StatementResult) -> Result<Self> {
        Value::try_from(result)?.try_into()
    }
}
