use itertools::Itertools as _;
use log::error;
use serde::{Deserialize, Serialize};

use super::digests::Digests;
use super::prepared::PreparedStatements;
use super::{analyze, export, import, lint, views};
use crate::error::{Error, Result};
use crate::sql::engine::{Engine, Raft, Status, Transaction as _, changes};
use crate::sql::execution::ExecutionResult;
use crate::sql::parser::{self, Parser, ast, format};
use crate::sql::planner::{Plan, describe, estimate};
use crate::sql::types::{Label, Row, Rows, Value};
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
}

impl<'a, E: Engine<'a>> Session<'a, E> {
    /// Creates a new session using the given SQL engine.
    pub fn new(engine: &'a E) -> Self {
        Self { engine, txn: None, digests: Digests::new(), prepared: PreparedStatements::new() }
    }

    /// Executes a client statement.
    pub fn execute(&mut self, statement: &str) -> Result<StatementResult> {
        // Statements that fail to parse aren't recorded in the statement
        // statistics, nor is SHOW STATEMENTS itself.
        let statement = Parser::parse(statement)?;
        let digest = match statement {
            ast::Statement::ShowStatements => None,
            ref statement => Some(parser::digest(statement)),
        };
        let result = self.execute_statement(statement);
        if let Some(digest) = digest {
            self.digests.record(digest, result.as_ref().ok().map(|(_, rows)| *rows));
        }
        result.map(|(result, _)| result)
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

        // Transaction control and the statements that aren't planned are
        // handled here, other statements are handled by the SQL executor.
        Ok(match statement {
            // BEGIN: starts a new transaction and returns its state.
            ast::Statement::Begin { read_only, as_of } => {
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
                let Some(txn) = self.txn.take() else {
                    return errinput!("not in a transaction");
                };
                let version = txn.state().version;
                txn.commit()?;
                (StatementResult::Commit { version }, 0)
            }

            // ROLLBACK: rolls back the currently open transaction, if any.
            ast::Statement::Rollback => {
                let Some(txn) = self.txn.take() else {
                    return errinput!("not in a transaction");
                };
                let version = txn.state().version;
                txn.rollback()?;
                (StatementResult::Rollback { version }, 0)
            }

            // EXPLAIN: returns the given SQL query's plan. Only planned
            // statements can be explained.
            ast::Statement::Explain(statement) => {
                let statement = match *statement {
                    // Transaction statements are passed on to the planner, as
                    // they always have been.
                    statement @ (ast::Statement::Begin { .. }
                    | ast::Statement::Commit
                    | ast::Statement::Rollback) => statement,
                    statement => self.planned_statement("EXPLAIN", statement)?,
                };
                let plan = self.with_txn(true, |txn| Plan::build(statement, txn)?.optimize())?;
                (StatementResult::Explain(plan), 0)
            }

            // SHOW STATEMENTS: returns the session's statement statistics. It
            // doesn't use a transaction.
            ast::Statement::ShowStatements => (self.digests.to_result(), 0),

            // ANALYZE: computes and stores column statistics.
            ast::Statement::Analyze { table } => {
                let tables = self.with_txn(false, |txn| analyze::analyze(txn, table.as_deref()))?;
                (StatementResult::Analyze { tables }, 0)
            }

            // SHOW STATISTICS: returns a table's column statistics.
            ast::Statement::ShowStatistics { table } => {
                let result = self.with_txn(true, |txn| analyze::show_statistics(txn, &table))?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // EXPORT: exports a table or the database as text lines.
            ast::Statement::Export { table, format } => {
                let export =
                    self.with_txn(true, |txn| export::export(txn, table.as_deref(), format))?;
                (StatementResult::Export { format, lines: export.lines }, export.rows)
            }

            // IMPORT: inserts rows from text lines. The result is that of an
            // INSERT.
            ast::Statement::Import { table, format, lines } => {
                let count =
                    self.with_txn(false, |txn| import::import(txn, &table, format, &lines))?;
                (StatementResult::Insert { count }, count)
            }

            // FORMAT: returns the canonical text of the statement.
            ast::Statement::Format(statement) => {
                (StatementResult::Format { lines: format::lines(&statement) }, 0)
            }

            // CREATE VIEW: creates a view.
            ast::Statement::CreateView { name, columns, query } => {
                self.with_txn(false, |txn| views::create_view(txn, &name, columns, *query))?;
                (StatementResult::CreateView { name }, 0)
            }

            // DROP VIEW: drops a view.
            ast::Statement::DropView { name, if_exists } => {
                let existed = self.with_txn(false, |txn| views::drop_view(txn, &name, if_exists))?;
                (StatementResult::DropView { name, existed }, 0)
            }

            // SHOW VIEWS: lists the views.
            ast::Statement::ShowViews => {
                let result = self.with_txn(true, |txn| views::show_views(txn))?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // DESCRIBE: describes the columns of a table or view.
            ast::Statement::Describe(ast::Describe::Name(name)) => {
                let columns = self.with_txn(true, |txn| describe::describe_table(txn, &name))?;
                let result = describe::result(columns);
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // DESCRIBE: describes the result columns of a planned statement,
            // from its plan before optimization.
            ast::Statement::Describe(ast::Describe::Statement(statement)) => {
                let statement = self.planned_statement("DESCRIBE", *statement)?;
                let plan = self.with_txn(true, |txn| Plan::build(statement, txn))?;
                let result = describe::result(describe::describe(&plan));
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // ESTIMATE: estimates the rows of each line of a planned
            // statement's plan, which is planned and optimized as by EXPLAIN.
            ast::Statement::Estimate(statement) => {
                let statement = self.planned_statement("ESTIMATE", *statement)?;
                let (plan, estimates) = self.with_txn(true, |txn| {
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
                self.prepared.prepare(name.clone(), types, *statement)?;
                (StatementResult::Prepare { name }, 0)
            }

            // EXECUTE: executes a prepared statement with the given arguments,
            // in the transaction that the statement would use.
            ast::Statement::Execute { name, arguments } => {
                let (statement, arguments) =
                    self.prepared.execute::<E::Transaction>(&name, arguments)?;
                let read_only = matches!(statement, ast::Statement::Select { .. });
                // The transaction borrows the session, so take the prepared
                // statements out of it while planning.
                let mut prepared = std::mem::take(&mut self.prepared);
                let result = self.with_txn(read_only, |txn| {
                    prepared.plan(&name, statement, arguments, txn)?.execute(txn)?.try_into()
                });
                self.prepared = prepared;
                let result = result?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // DEALLOCATE: removes a prepared statement.
            ast::Statement::Deallocate { name } => {
                self.prepared.deallocate(&name)?;
                (StatementResult::Deallocate { name }, 0)
            }

            // SHOW PREPARED: lists the session's prepared statements. It
            // doesn't use a transaction.
            ast::Statement::ShowPrepared => {
                let result = self.prepared.show();
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // LINT: returns warnings about the statement and the statements it
            // contains, without planning or executing them.
            ast::Statement::Lint(statement) => {
                let warnings = self.with_txn(true, |txn| lint::lint(&statement, txn))?;
                let result = lint::result(warnings);
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // SHOW CHANGES: lists the committed changes from the engine's
            // change log. It doesn't use a transaction.
            ast::Statement::ShowChanges { since } => {
                let result = changes::result(self.engine.changes(since.unwrap_or(0))?);
                let rows = Self::result_rows(&result);
                (result, rows)
            }

            // Other statements (SELECT etc.) are handled by the SQL executor.
            statement => {
                let read_only = matches!(statement, ast::Statement::Select { .. });
                let result = self.with_txn(read_only, |txn| {
                    Plan::build(statement, txn)?.optimize()?.execute(txn)?.try_into()
                })?;
                let rows = Self::result_rows(&result);
                (result, rows)
            }
        })
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
        // Use the current explicit transaction, if there is one.
        if let Some(ref mut txn) = self.txn {
            return f(txn);
        }
        // Otherwise, use an implicit transaction. Doing this session-side
        // results in additional Raft roundtrips to begin and complete the
        // transaction -- we could avoid this if the Raft SQL state machine
        // supported implicit transactions, but we keep it simple.
        let mut txn = match read_only {
            true => self.engine.begin_read_only()?,
            false => self.engine.begin()?,
        };
        let result = f(&mut txn);
        match result {
            Ok(_) => txn.commit()?,
            Err(_) => txn.rollback()?,
        }
        result
    }
}

/// Returns the leading keywords of a statement that has no plan of its own, for
/// errors about statements that require a planned statement, or None if the
/// statement has a plan.
pub(super) fn unplanned_keyword(statement: &ast::Statement) -> Option<&'static str> {
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
        Explain(_) => Some("EXPLAIN"),
        ShowStatements | ShowStatistics { .. } | ShowViews | ShowPrepared | ShowChanges { .. } => {
            Some("SHOW")
        }
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
