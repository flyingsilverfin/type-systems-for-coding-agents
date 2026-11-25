//! A PostgreSQL connection: startup, simple and extended query.
//!
//! * Startup accepts only protocol 3.0, refuses SSL and GSS encryption, asks a
//!   user with a password for it with the MD5 method, and then sends
//!   ParameterStatus, BackendKeyData and ReadyForQuery.
//! * A Query message is split into statements at the SQL lexer's `;` tokens;
//!   each is executed as if sent alone, and the first error ends the query.
//! * Results: rows as RowDescription, DataRows and `SELECT <n>`; the lines of
//!   EXPLAIN (column `QUERY PLAN`), FORMAT and EXPORT as rows of one text
//!   column; `INSERT 0 <n>`, `UPDATE <n>`, `DELETE <n>`; every other result,
//!   also one added later, as its statement's name in upper case with spaces
//!   (`CREATE TABLE`, `SET ROLE`).
//! * A result column is named by its header without table qualifier. An
//!   unnamed column is named as PostgreSQL names it, from the select-list item:
//!   a function or aggregate call by the function's name, an expression atom
//!   that begins with a keyword and is not a literal by that keyword in lower
//!   case, every other expression `?column?`. Constructs added later are named
//!   by what they are. In a simple query, a column's type is that of its first
//!   non-NULL value (text if none).
//! * Extended query: Parse (one statement, typed parameters `$n`), Bind
//!   (parameter values, result formats), Describe (as DESCRIBE describes the
//!   statement, its parameters replaced by NULL for a statement, bound for a
//!   portal), Execute (with a row limit), Close, Sync. After an error, messages
//!   up to Sync are skipped; Sync closes the portals.
//! * COPY (a command of the connection, recognized by its first word):
//!   `COPY <table> [(<columns>)] TO STDOUT` sends the rows as EXPORT reads
//!   them, and `... FROM STDIN` inserts the rows the client sends as IMPORT
//!   inserts them, in PostgreSQL's text format (tab-separated, `\N` for NULL,
//!   backslash escapes). A field is read by its column's data type as a text
//!   parameter of that type is read; a value of a data type added later as
//!   IMPORT reads it.
//! * Errors carry a SQLSTATE code by kind: invalid input 42000, permission
//!   denied 42501, read-only transaction 25006, serialization failure 40001,
//!   operation aborted 40003, invalid data XX000, io error 58030.

use std::collections::HashMap;
use std::io::{BufReader, Read, Write};

use itertools::Itertools as _;

use super::message::{self, BackendMessage, Field, FrontendMessage, Target, TransactionStatus};
use super::types::{self, Format, Type};
use super::{PgError, md5_response};
use crate::errinput;
use crate::error::{Error, Result};
use crate::sql::engine::{Engine, Transaction as _};
use crate::sql::execution::{
    ImportRow, Session, StatementResult, bind_parameters, unplanned_keyword, users,
};
use crate::sql::parser::{Keyword, Lexer, Parser, Token, ast};
use crate::sql::types::{DataType, Label, Row, Table, Value};

/// Errors are sent with the SQLSTATE code of their kind.
impl From<Error> for PgError {
    fn from(error: Error) -> Self {
        let code = match &error {
            Error::InvalidInput(_) => "42000",
            Error::PermissionDenied(_) => "42501",
            Error::ReadOnly => "25006",
            Error::Serialization => "40001",
            Error::Abort => "40003",
            Error::InvalidData(_) => "XX000",
            Error::IO(_) => "58030",
        };
        Self::new(code, error.to_string())
    }
}

/// Serves a PostgreSQL connection on a stream, until the connection is closed.
pub fn serve<'a, E: Engine<'a>, S: Read + Write>(engine: &'a E, stream: S) -> Result<()> {
    let mut stream = Stream::new(stream);
    let Some(session) = startup(engine, &mut stream)? else {
        return Ok(());
    };
    let connection = Connection {
        session,
        stream,
        statements: HashMap::new(),
        portals: HashMap::new(),
        failed: false,
    };
    connection.run()
}

/// Runs the startup of a connection, returning its session if it succeeds.
fn startup<'a, E: Engine<'a>, S: Read + Write>(
    engine: &'a E,
    stream: &mut Stream<S>,
) -> Result<Option<Session<'a, E>>> {
    let parameters = loop {
        match stream.receive_startup()? {
            // Encryption isn't supported, so the client continues without it.
            Some(FrontendMessage::SslRequest | FrontendMessage::GssEncRequest) => {
                stream.output.push(b'N');
                stream.flush()?;
            }
            Some(FrontendMessage::Startup { version, parameters }) => {
                if version != message::PROTOCOL_VERSION {
                    let (major, minor) = (version >> 16, version & 0xffff);
                    let message = format!("unsupported frontend protocol {major}.{minor}");
                    stream.fatal(PgError::new("0A000", message))?;
                    return Ok(None);
                }
                break parameters;
            }
            // A cancel request, or the end of the stream.
            Some(_) | None => return Ok(None),
        }
    };
    let parameter = |name: &str| {
        parameters.iter().find(|(key, _)| key == name).map(|(_, value)| value.as_str())
    };

    let name = parameter("user").unwrap_or_default().to_string();
    if name.is_empty() {
        stream.fatal(PgError::new("28000", "no user name given"))?;
        return Ok(None);
    }
    let txn = engine.begin_read_only()?;
    let user = users::get_user(&txn, &name);
    txn.commit()?;
    let Some(user) = user? else {
        stream.fatal(PgError::new("28000", format!("user {name} does not exist")))?;
        return Ok(None);
    };

    if let Some(hash) = &user.password {
        let salt = rand::random();
        stream.send(BackendMessage::AuthenticationMd5Password { salt });
        stream.flush()?;
        match stream.receive()? {
            Some(FrontendMessage::Password(response)) if response == md5_response(hash, salt) => {}
            Some(_) => {
                let message = format!("password authentication failed for user {name}");
                stream.fatal(PgError::new("28P01", message))?;
                return Ok(None);
            }
            None => return Ok(None),
        }
    }

    let mut session = engine.session();
    session.login_authenticated(&name)?;
    stream.send(BackendMessage::AuthenticationOk);
    let superuser = if user.superuser { "on" } else { "off" };
    let parameters = [
        ("application_name", parameter("application_name").unwrap_or_default()),
        ("client_encoding", "UTF8"),
        ("DateStyle", "ISO"),
        ("integer_datetimes", "on"),
        ("is_superuser", superuser),
        ("server_encoding", "UTF8"),
        ("server_version", "14.0"),
        ("session_authorization", name.as_str()),
        ("standard_conforming_strings", "on"),
    ];
    for (name, value) in parameters {
        stream.send(BackendMessage::ParameterStatus { name: name.into(), value: value.into() });
    }
    let (process, secret) = (session.number() as i32, rand::random());
    stream.send(BackendMessage::BackendKeyData { process, secret });
    stream.send(BackendMessage::ReadyForQuery(TransactionStatus::Idle));
    stream.flush()?;
    Ok(Some(session))
}

/// A PostgreSQL connection and its session, prepared statements and portals.
struct Connection<'a, E: Engine<'a>, S: Read + Write> {
    /// The session. It is dropped before the stream (fields drop in order).
    session: Session<'a, E>,
    /// The client stream.
    stream: Stream<S>,
    /// The prepared statements, by name ('' for the unnamed one).
    statements: HashMap<String, Statement>,
    /// The portals, by name ('' for the unnamed one).
    portals: HashMap<String, Portal>,
    /// Whether a message of the extended protocol failed since the last Sync.
    failed: bool,
}

/// A statement prepared by Parse, and its parameter OIDs.
struct Statement {
    statement: Option<ast::Statement>,
    types: Vec<u32>,
}

/// A portal created by Bind: a bound statement, its result formats and state.
struct Portal {
    statement: Option<ast::Statement>,
    results: Vec<Format>,
    state: PortalState,
}

/// The execution state of a portal.
enum PortalState {
    /// Not executed yet.
    New,
    /// Executed: the rows not sent yet, and the command tag.
    Executed { rows: Vec<Row>, tag: String },
    /// Completed: every row was sent, with the tag of further executions.
    Completed { tag: String },
}

impl<'a, E: Engine<'a>, S: Read + Write> Connection<'a, E, S> {
    /// Processes messages until the connection is closed.
    fn run(mut self) -> Result<()> {
        while let Some(message) = self.stream.receive()? {
            // After an error in the extended protocol, messages are skipped
            // until Sync.
            if self.failed && !matches!(message, FrontendMessage::Sync | FrontendMessage::Terminate)
            {
                continue;
            }
            let flush = matches!(
                message,
                FrontendMessage::Query(_) | FrontendMessage::Sync | FrontendMessage::Flush
            );
            let result = match message {
                FrontendMessage::Query(query) => {
                    if let Err(error) = self.query(&query) {
                        self.stream.send_error(error);
                    }
                    self.send_ready();
                    Ok(())
                }
                FrontendMessage::Parse { name, query, types } => self.parse(name, &query, types),
                FrontendMessage::Bind { portal, statement, formats, values, results } => {
                    self.bind(portal, &statement, formats, values, results)
                }
                FrontendMessage::Describe { target, name } => self.describe(target, &name),
                FrontendMessage::Execute { portal, max_rows } => self.execute(&portal, max_rows),
                FrontendMessage::Close { target, name } => {
                    match target {
                        Target::Statement => drop(self.statements.remove(&name)),
                        Target::Portal => drop(self.portals.remove(&name)),
                    }
                    self.stream.send(BackendMessage::CloseComplete);
                    Ok(())
                }
                FrontendMessage::Sync => {
                    self.failed = false;
                    self.portals.clear();
                    self.send_ready();
                    Ok(())
                }
                FrontendMessage::Flush => Ok(()),
                FrontendMessage::Terminate => return Ok(()),
                // Messages of the startup and of COPY FROM STDIN.
                FrontendMessage::Password(_)
                | FrontendMessage::CopyData(_)
                | FrontendMessage::CopyDone
                | FrontendMessage::CopyFail(_)
                | FrontendMessage::SslRequest
                | FrontendMessage::GssEncRequest
                | FrontendMessage::CancelRequest { .. }
                | FrontendMessage::Startup { .. } => Ok(()),
            };
            if let Err(error) = result {
                self.stream.send_error(error);
                self.failed = true;
            }
            if flush {
                self.stream.flush()?;
            }
        }
        Ok(())
    }

    /// Sends ReadyForQuery with the session's transaction status.
    fn send_ready(&mut self) {
        let status = match self.session.in_transaction() {
            true => TransactionStatus::Transaction,
            false => TransactionStatus::Idle,
        };
        self.stream.send(BackendMessage::ReadyForQuery(status));
    }

    /// Executes the statements of a simple query, sending their results.
    fn query(&mut self, query: &str) -> std::result::Result<(), PgError> {
        let statements = split_statements(query)?;
        if statements.is_empty() {
            self.stream.send(BackendMessage::EmptyQueryResponse);
        }
        for text in statements {
            if let Some(copy) = CopyCommand::parse(text)? {
                self.copy(copy)?;
                continue;
            }
            let statement = Parser::parse(text)?;
            let result = self.session.execute_parsed(statement.clone())?;
            let tag = command_tag(&result);
            if let Some((names, rows)) = self.result_rows(&statement, result) {
                let fields = names
                    .into_iter()
                    .enumerate()
                    .map(|(i, name)| {
                        let datatype = rows.iter().find_map(|row| row[i].datatype());
                        field(name, types::of_datatype(datatype), Format::Text)
                    })
                    .collect();
                self.stream.send(BackendMessage::RowDescription(fields));
                self.stream.send_rows(rows, &[]);
            }
            self.stream.send(BackendMessage::CommandComplete(tag));
        }
        Ok(())
    }

    /// Prepares a statement (Parse).
    fn parse(
        &mut self,
        name: String,
        query: &str,
        oids: Vec<u32>,
    ) -> std::result::Result<(), PgError> {
        if !name.is_empty() && self.statements.contains_key(&name) {
            let message = format!("prepared statement {name} already exists");
            return Err(PgError::new("42P05", message));
        }
        let statement = match split_statements(query)?.as_slice() {
            [] => None,
            [text] => Some(Parser::parse(text)?),
            _ => {
                let message = "cannot insert multiple commands into a prepared statement";
                return Err(PgError::new("42601", message));
            }
        };
        let highest = statement.as_ref().and_then(|s| s.parameters().last().copied());
        let count = oids.len().max(highest.unwrap_or(0) as usize);
        let mut types = Vec::with_capacity(count);
        for i in 0..count {
            let oid = oids.get(i).copied().unwrap_or(0);
            if oid == 0 {
                let n = i + 1;
                let message = format!("could not determine data type of parameter ${n}");
                return Err(PgError::new("42P18", message));
            }
            types::parameter_datatype(oid)?;
            types.push(oid);
        }
        if count > 0
            && let Some(keyword) = statement.as_ref().and_then(unplanned_keyword)
        {
            return Err(errinput!("PREPARE is not supported for {keyword}"));
        }
        self.statements.insert(name, Statement { statement, types });
        self.stream.send(BackendMessage::ParseComplete);
        Ok(())
    }

    /// Binds parameter values to a prepared statement, creating a portal (Bind).
    fn bind(
        &mut self,
        portal: String,
        name: &str,
        formats: Vec<i16>,
        values: Vec<Option<Vec<u8>>>,
        results: Vec<i16>,
    ) -> std::result::Result<(), PgError> {
        let statement = self.statements.get(name).ok_or_else(|| no_statement(name))?;
        if formats.len() > 1 && formats.len() != values.len() {
            let (k, m) = (formats.len(), values.len());
            let message = format!("bind message has {k} parameter formats but {m} parameters");
            return Err(PgError::new("08P01", message));
        }
        if values.len() != statement.types.len() {
            let (k, m) = (values.len(), statement.types.len());
            let message = format!(
                "bind message supplies {k} parameters, but prepared statement requires {m}"
            );
            return Err(PgError::new("08P01", message));
        }
        let formats: Vec<Format> = formats.into_iter().map(Format::from_code).try_collect()?;
        let mut parameters = Vec::with_capacity(values.len());
        for (i, (value, oid)) in values.iter().zip(&statement.types).enumerate() {
            parameters.push(match value {
                Some(data) => types::decode_parameter(*oid, format_of(&formats, i), data, i + 1)?,
                None => Value::Null,
            });
        }
        let results = results.into_iter().map(Format::from_code).try_collect()?;
        let statement = statement.statement.clone().map(|s| bind_parameters(s, &parameters));
        self.portals.insert(portal, Portal { statement, results, state: PortalState::New });
        self.stream.send(BackendMessage::BindComplete);
        Ok(())
    }

    /// Describes a prepared statement or a portal (Describe).
    fn describe(&mut self, target: Target, name: &str) -> std::result::Result<(), PgError> {
        match target {
            Target::Statement => {
                let statement = self.statements.get(name).ok_or_else(|| no_statement(name))?;
                let types = statement.types.clone();
                let nulls = vec![Value::Null; types.len()];
                let statement = statement.statement.clone().map(|s| bind_parameters(s, &nulls));
                self.stream.send(BackendMessage::ParameterDescription(types));
                self.send_description(statement, &[])
            }
            Target::Portal => {
                let portal = self.portals.get(name).ok_or_else(|| no_portal(name))?;
                let (statement, results) = (portal.statement.clone(), portal.results.clone());
                self.send_description(statement, &results)
            }
        }
    }

    /// Sends the RowDescription of a statement's result columns, or NoData if it
    /// has none.
    fn send_description(
        &mut self,
        statement: Option<ast::Statement>,
        formats: &[Format],
    ) -> std::result::Result<(), PgError> {
        let Some(statement) = statement else {
            self.stream.send(BackendMessage::NoData);
            return Ok(());
        };
        let columns = self.session.describe_columns(statement.clone())?;
        if columns.is_empty() {
            self.stream.send(BackendMessage::NoData);
            return Ok(());
        }
        check_result_formats(formats, columns.len())?;
        let labels = columns.iter().map(|(label, _)| label.clone()).collect_vec();
        let fields = self
            .column_names(&statement, &labels)
            .into_iter()
            .zip(columns)
            .enumerate()
            .map(|(i, (name, (_, datatype)))| {
                field(name, types::of_datatype(datatype), format_of(formats, i))
            })
            .collect();
        self.stream.send(BackendMessage::RowDescription(fields));
        Ok(())
    }

    /// Executes a portal (Execute).
    fn execute(&mut self, name: &str, max_rows: i32) -> std::result::Result<(), PgError> {
        let portal = self.portals.get(name).ok_or_else(|| no_portal(name))?;
        if let PortalState::New = portal.state {
            let Some(statement) = portal.statement.clone() else {
                self.stream.send(BackendMessage::EmptyQueryResponse);
                return Ok(());
            };
            let results = portal.results.clone();
            let result = self.session.execute_parsed(statement.clone())?;
            let tag = command_tag(&result);
            let rows = match self.result_rows(&statement, result) {
                Some((names, rows)) => {
                    check_result_formats(&results, names.len())?;
                    rows
                }
                None => Vec::new(),
            };
            let portal = self.portals.get_mut(name).expect("portal exists");
            portal.state = PortalState::Executed { rows, tag };
        }

        let portal = self.portals.get_mut(name).expect("portal exists");
        let state = match &mut portal.state {
            PortalState::Executed { rows, tag } => {
                let count = match usize::try_from(max_rows) {
                    Ok(max_rows) if max_rows > 0 => max_rows.min(rows.len()),
                    _ => rows.len(),
                };
                self.stream.send_rows(rows.drain(..count), &portal.results);
                if !rows.is_empty() {
                    self.stream.send(BackendMessage::PortalSuspended);
                    return Ok(());
                }
                self.stream.send(BackendMessage::CommandComplete(tag.clone()));
                PortalState::Completed { tag: zero_count(tag) }
            }
            PortalState::Completed { tag } => {
                self.stream.send(BackendMessage::CommandComplete(tag.clone()));
                return Ok(());
            }
            PortalState::New => unreachable!("portal was executed"),
        };
        portal.state = state;
        Ok(())
    }

    /// Executes a COPY command.
    fn copy(&mut self, copy: CopyCommand) -> std::result::Result<(), PgError> {
        let stream = &mut self.stream;
        let count = match copy.direction {
            CopyDirection::ToStdout => self.session.copy_to::<PgError>(&copy.table, |table, rows| {
                let columns = copy_columns(table, &copy.columns)?;
                stream.send(BackendMessage::CopyOutResponse { columns: columns.len() });
                for row in &rows {
                    stream.send(BackendMessage::CopyData(copy_line(row, &columns)));
                }
                stream.send(BackendMessage::CopyDone);
                Ok(rows.len() as u64)
            })?,
            CopyDirection::FromStdin => self.session.copy_from::<PgError>(&copy.table, |table| {
                let columns = copy_columns(table, &copy.columns)?;
                stream.send(BackendMessage::CopyInResponse { columns: columns.len() });
                stream.flush()?;
                let data = stream.receive_copy_data()?;
                let Ok(data) = String::from_utf8(data) else {
                    return Err(types::invalid_encoding());
                };
                Ok(copy_rows(table, &columns, &data)?)
            })?,
        };
        self.stream.send(BackendMessage::CommandComplete(format!("COPY {count}")));
        Ok(())
    }

    /// Returns the column names and rows of a statement result, if it has rows.
    fn result_rows(
        &self,
        statement: &ast::Statement,
        result: StatementResult,
    ) -> Option<(Vec<String>, Vec<Row>)> {
        let lines = |name: &str, lines: Vec<String>| {
            let rows = lines.into_iter().map(|line| vec![Value::String(line)]).collect();
            Some((vec![name.to_string()], rows))
        };
        match result {
            StatementResult::Select { columns, rows } => {
                Some((self.column_names(statement, &columns), rows))
            }
            StatementResult::Explain(plan) => {
                lines("QUERY PLAN", plan.to_string().lines().map(String::from).collect())
            }
            StatementResult::Format { lines: format } => lines("format", format),
            StatementResult::Export { lines: export, .. } => lines("export", export),
            // Other results have no rows.
            _ => None,
        }
    }

    /// Returns the names of a statement's result columns with the given labels.
    fn column_names(&self, statement: &ast::Statement, labels: &[Label]) -> Vec<String> {
        let query = match statement {
            ast::Statement::Execute { name, .. } => self.session.prepared_statement(name),
            statement => Some(statement),
        };
        let items = match query {
            Some(ast::Statement::Select { select, .. }) => select_items(select, labels.len()),
            _ => Vec::new(),
        };
        labels
            .iter()
            .enumerate()
            .map(|(i, label)| column_name(label, items.get(i).copied().flatten()))
            .collect()
    }
}

/// A client stream, with the messages queued to send.
struct Stream<S: Read + Write> {
    reader: BufReader<S>,
    output: Vec<u8>,
    /// Whether the connection was closed by a fatal error.
    closed: bool,
}

impl<S: Read + Write> Stream<S> {
    fn new(stream: S) -> Self {
        Self { reader: BufReader::new(stream), output: Vec::new(), closed: false }
    }

    /// Receives a message, or None if the connection is closed.
    fn receive(&mut self) -> Result<Option<FrontendMessage>> {
        Ok(self.receive_with_type()?.map(|(_, message)| message))
    }

    /// Receives a message with its type byte, or None if the connection is
    /// closed.
    fn receive_with_type(&mut self) -> Result<Option<(u8, FrontendMessage)>> {
        if self.closed {
            return Ok(None);
        }
        let Some((kind, body)) = message::read(&mut self.reader)? else {
            return Ok(None);
        };
        Ok(self.decoded(FrontendMessage::decode(kind, &body))?.map(|message| (kind, message)))
    }

    /// Receives a startup-phase message, or None if the connection is closed.
    fn receive_startup(&mut self) -> Result<Option<FrontendMessage>> {
        let Some(body) = message::read_startup(&mut self.reader)? else {
            return Ok(None);
        };
        self.decoded(FrontendMessage::decode_startup(&body))
    }

    /// Returns a decoded message, or None after sending its decoding error.
    fn decoded(
        &mut self,
        message: std::result::Result<FrontendMessage, PgError>,
    ) -> Result<Option<FrontendMessage>> {
        match message {
            Ok(message) => Ok(Some(message)),
            Err(error) => {
                self.fatal(error)?;
                Ok(None)
            }
        }
    }

    /// Receives the data of COPY FROM STDIN, until CopyDone.
    fn receive_copy_data(&mut self) -> std::result::Result<Vec<u8>, PgError> {
        let mut data = Vec::new();
        loop {
            match self.receive_with_type()? {
                Some((_, FrontendMessage::CopyData(chunk))) => data.extend(chunk),
                Some((_, FrontendMessage::CopyDone)) => return Ok(data),
                Some((_, FrontendMessage::CopyFail(message))) => {
                    let message = format!("COPY from stdin failed: {message}");
                    return Err(PgError::new("57014", message));
                }
                Some((_, FrontendMessage::Flush | FrontendMessage::Sync)) => {}
                Some((kind, _)) => {
                    let kind = kind as char;
                    let message = format!("unexpected message type {kind} during COPY from stdin");
                    return Err(PgError::new("08P01", message));
                }
                None => return Err(PgError::new("08P01", "connection closed during COPY")),
            }
        }
    }

    /// Queues a message.
    fn send(&mut self, message: BackendMessage) {
        message.encode(&mut self.output);
    }

    /// Queues an ErrorResponse.
    fn send_error(&mut self, error: PgError) {
        let (severity, code) = ("ERROR".to_string(), error.code.to_string());
        self.send(BackendMessage::ErrorResponse { severity, code, message: error.message });
    }

    /// Queues DataRows of rows, with values in the given formats.
    fn send_rows(&mut self, rows: impl IntoIterator<Item = Row>, formats: &[Format]) {
        for row in rows {
            let values = row
                .iter()
                .enumerate()
                .map(|(i, value)| types::encode(value, format_of(formats, i)))
                .collect();
            self.send(BackendMessage::DataRow(values));
        }
    }

    /// Sends a fatal error, which closes the connection.
    fn fatal(&mut self, error: PgError) -> Result<()> {
        let (severity, code) = ("FATAL".to_string(), error.code.to_string());
        self.send(BackendMessage::ErrorResponse { severity, code, message: error.message });
        self.closed = true;
        self.flush()
    }

    /// Sends the queued messages.
    fn flush(&mut self) -> Result<()> {
        let stream = self.reader.get_mut();
        stream.write_all(&self.output)?;
        stream.flush()?;
        self.output.clear();
        Ok(())
    }
}

/// A COPY command: a table, its columns, and the direction.
#[derive(Debug, PartialEq)]
struct CopyCommand {
    table: String,
    columns: Vec<String>,
    direction: CopyDirection,
}

/// The direction of a COPY command.
#[derive(Debug, PartialEq)]
enum CopyDirection {
    ToStdout,
    FromStdin,
}

impl CopyCommand {
    /// Parses a COPY command, or returns None if the statement isn't one.
    fn parse(statement: &str) -> std::result::Result<Option<Self>, PgError> {
        let tokens: Vec<Token> = Lexer::new(statement).try_collect()?;
        let mut tokens = tokens.into_iter();
        match tokens.next() {
            Some(Token::Ident(word)) if word == "copy" => {}
            _ => return Ok(None),
        }
        let syntax_error = || PgError::new("42601", "syntax error in COPY command");
        let Some(Token::Ident(table)) = tokens.next() else {
            return Err(syntax_error());
        };
        let mut columns = Vec::new();
        let mut next = tokens.next();
        if next == Some(Token::OpenParen) {
            loop {
                let Some(Token::Ident(column)) = tokens.next() else {
                    return Err(syntax_error());
                };
                columns.push(column);
                match tokens.next() {
                    Some(Token::Comma) => {}
                    Some(Token::CloseParen) => break,
                    _ => return Err(syntax_error()),
                }
            }
            next = tokens.next();
        }
        let direction = match (next, tokens.next()) {
            (Some(Token::Keyword(Keyword::To)), Some(Token::Ident(target)))
                if target == "stdout" =>
            {
                CopyDirection::ToStdout
            }
            (Some(Token::Keyword(Keyword::From)), Some(Token::Ident(source)))
                if source == "stdin" =>
            {
                CopyDirection::FromStdin
            }
            _ => return Err(syntax_error()),
        };
        if tokens.next().is_some() {
            return Err(syntax_error());
        }
        Ok(Some(Self { table, columns, direction }))
    }
}

/// Splits SQL text into statements at the lexer's `;` tokens.
fn split_statements(text: &str) -> Result<Vec<&str>> {
    let mut lexer = Lexer::new(text);
    let mut statements = Vec::new();
    let (mut start, mut empty) = (0, true);
    while let Some(token) = lexer.next().transpose()? {
        if token != Token::Semicolon {
            empty = false;
            continue;
        }
        let end = lexer.position();
        if !empty {
            statements.push(&text[start..end - 1]);
        }
        (start, empty) = (end, true);
    }
    if !empty {
        statements.push(&text[start..]);
    }
    Ok(statements)
}

/// Returns the command tag of a statement result.
fn command_tag(result: &StatementResult) -> String {
    use StatementResult::*;
    match result {
        Select { rows, .. } => format!("SELECT {}", rows.len()),
        Insert { count } => format!("INSERT 0 {count}"),
        Update { count } => format!("UPDATE {count}"),
        Delete { count } => format!("DELETE {count}"),
        Begin(_) => "BEGIN".to_string(),
        Commit { .. } => "COMMIT".to_string(),
        Rollback { .. } => "ROLLBACK".to_string(),
        Explain(_) => "EXPLAIN".to_string(),
        CreateTable { .. } => "CREATE TABLE".to_string(),
        DropTable { .. } => "DROP TABLE".to_string(),
        Analyze { .. } => "ANALYZE".to_string(),
        Export { .. } => "EXPORT".to_string(),
        Format { .. } => "FORMAT".to_string(),
        CreateView { .. } => "CREATE VIEW".to_string(),
        DropView { .. } => "DROP VIEW".to_string(),
        Prepare { .. } => "PREPARE".to_string(),
        Deallocate { .. } => "DEALLOCATE".to_string(),
        CreateUser { .. } => "CREATE USER".to_string(),
        DropUser { .. } => "DROP USER".to_string(),
        Grant { .. } => "GRANT".to_string(),
        Revoke { .. } => "REVOKE".to_string(),
        SetRole { .. } => "SET ROLE".to_string(),
        ResetRole { .. } => "RESET ROLE".to_string(),
        CreatePolicy { .. } => "CREATE POLICY".to_string(),
        DropPolicy { .. } => "DROP POLICY".to_string(),
        CreateMaterializedView { .. } => "CREATE MATERIALIZED VIEW".to_string(),
        RefreshMaterializedView { .. } => "REFRESH MATERIALIZED VIEW".to_string(),
        DropMaterializedView { .. } => "DROP MATERIALIZED VIEW".to_string(),
    }
}

/// Returns a command tag with its row count, if it has one, replaced by 0.
fn zero_count(tag: &str) -> String {
    match tag.rsplit_once(' ') {
        Some((name, count)) if count.parse::<u64>().is_ok() => format!("{name} 0"),
        _ => tag.to_string(),
    }
}

/// Returns the select-list item of each of a query's result columns.
fn select_items(
    select: &[(ast::Expression, Option<String>)],
    columns: usize,
) -> Vec<Option<&ast::Expression>> {
    let stars = select.iter().filter(|(expr, _)| *expr == ast::Expression::All).count();
    let star_columns = match stars {
        0 => 0,
        stars => columns.saturating_sub(select.len() - stars) / stars,
    };
    select
        .iter()
        .flat_map(|(expr, _)| match expr {
            ast::Expression::All => vec![None; star_columns],
            expr => vec![Some(expr)],
        })
        .collect()
}

/// Returns the name of a result column: its header, or the name PostgreSQL
/// gives an unnamed expression.
fn column_name(label: &Label, item: Option<&ast::Expression>) -> String {
    match (label, item) {
        (Label::None, Some(expr)) => expression_name(expr),
        (Label::None, None) => "?column?".to_string(),
        (label, _) => label.as_header().to_string(),
    }
}

/// Returns the name PostgreSQL gives an unnamed expression.
fn expression_name(expr: &ast::Expression) -> String {
    match expr {
        ast::Expression::Column(_, name) => name.clone(),
        ast::Expression::Function(name, _) => name.to_lowercase(),
        ast::Expression::All
        | ast::Expression::Literal(_)
        | ast::Expression::Operator(_)
        | ast::Expression::Parameter(_) => "?column?".to_string(),
    }
}

/// Returns a RowDescription field.
fn field(name: String, pgtype: Type, format: Format) -> Field {
    let (type_oid, size) = (pgtype.oid, pgtype.size);
    Field { name, table_oid: 0, column: 0, type_oid, size, modifier: -1, format }
}

/// Returns the format of the i-th item from the format codes of a message.
fn format_of(formats: &[Format], i: usize) -> Format {
    match formats {
        [] => Format::Text,
        [format] => *format,
        formats => formats[i],
    }
}

/// Checks that result format codes fit the number of result columns.
fn check_result_formats(formats: &[Format], columns: usize) -> std::result::Result<(), PgError> {
    if formats.len() > 1 && formats.len() != columns {
        let k = formats.len();
        let message =
            format!("bind message has {k} result formats but query has {columns} columns");
        return Err(PgError::new("08P01", message));
    }
    Ok(())
}

/// Returns the error for a missing prepared statement.
fn no_statement(name: &str) -> PgError {
    match name {
        "" => PgError::new("26000", "unnamed prepared statement does not exist"),
        name => PgError::new("26000", format!("prepared statement {name} does not exist")),
    }
}

/// Returns the error for a missing portal.
fn no_portal(name: &str) -> PgError {
    match name {
        "" => PgError::new("34000", "unnamed portal does not exist"),
        name => PgError::new("34000", format!("portal {name} does not exist")),
    }
}

/// Returns the indexes of the columns of a COPY command.
fn copy_columns(table: &Table, names: &[String]) -> Result<Vec<usize>> {
    if names.is_empty() {
        return Ok((0..table.columns.len()).collect());
    }
    let mut columns = Vec::with_capacity(names.len());
    for name in names {
        let Some(index) = table.columns.iter().position(|column| column.name == *name) else {
            return errinput!("unknown column {name} in table {}", table.name);
        };
        if columns.contains(&index) {
            return errinput!("column {name} given multiple times");
        }
        columns.push(index);
    }
    Ok(columns)
}

/// Returns the line of a row for COPY TO STDOUT.
fn copy_line(row: &Row, columns: &[usize]) -> Vec<u8> {
    let fields = columns.iter().map(|&index| match types::encode_text(&row[index]) {
        Some(text) => copy_escape(&text),
        None => "\\N".to_string(),
    });
    format!("{}\n", fields.collect_vec().join("\t")).into_bytes()
}

/// Escapes a field for COPY TO STDOUT.
fn copy_escape(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '\\' => escaped.push_str("\\\\"),
            '\t' => escaped.push_str("\\t"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            c => escaped.push(c),
        }
    }
    escaped
}

/// Undoes the escapes of a field of COPY FROM STDIN.
fn copy_unescape(field: &str) -> String {
    let mut text = String::with_capacity(field.len());
    let mut chars = field.chars();
    while let Some(c) = chars.next() {
        text.push(match c {
            '\\' => match chars.next() {
                Some('t') => '\t',
                Some('n') => '\n',
                Some('r') => '\r',
                Some(c) => c,
                None => '\\',
            },
            c => c,
        });
    }
    text
}

/// Reads the rows of COPY FROM STDIN data, as the values of the given columns.
fn copy_rows(table: &Table, columns: &[usize], data: &str) -> Result<Vec<ImportRow>> {
    let mut rows = Vec::new();
    for (i, line) in data.split_terminator('\n').enumerate() {
        let number = i + 1;
        let fields = line.split('\t').collect_vec();
        if fields.len() != columns.len() {
            let (expected, found) = (columns.len(), fields.len());
            return errinput!(
                "wrong number of fields on line {number}: expected {expected}, found {found}"
            );
        }
        let mut values = Vec::with_capacity(columns.len());
        for (field, &index) in fields.into_iter().zip(columns) {
            let column = &table.columns[index];
            values.push(match field {
                "\\N" => Value::Null,
                field => match read_field(&copy_unescape(field), column.datatype) {
                    Some(value) => value,
                    None => {
                        let (datatype, name) = (column.datatype, &column.name);
                        let message = format!("invalid value for {datatype} column {name}");
                        return errinput!("{message} on line {number}");
                    }
                },
            });
        }
        rows.push((columns.to_vec(), values));
    }
    Ok(rows)
}

/// Reads a field of COPY FROM STDIN as a value of its column's data type.
fn read_field(text: &str, datatype: DataType) -> Option<Value> {
    let pgtype = match datatype {
        DataType::Boolean => types::BOOL,
        DataType::Integer => types::INT8,
        DataType::Float => types::FLOAT8,
        DataType::String => types::TEXT,
    };
    types::decode_text(pgtype, text).ok()
}

#[cfg(test)]
mod tests {
    use std::os::unix::net::UnixStream;

    use test_case::test_case;

    use super::*;
    use crate::pgwire::client::{Client, CopyIn};
    use crate::sql::engine::Local;
    use crate::storage::Memory;

    #[test_case("" => Ok(vec![]); "empty")]
    #[test_case(" ; ;" => Ok(vec![]); "empty statements")]
    #[test_case("SELECT 1" => Ok(vec!["SELECT 1"]); "one")]
    #[test_case("SELECT 1; ; SELECT 'é;'; " => Ok(vec!["SELECT 1", " SELECT 'é;'"]); "two")]
    #[test_case("SELECT \"a;b\";" => Ok(vec!["SELECT \"a;b\""]); "quoted identifier")]
    #[test_case("SELECT 1; SELECT 'x" => Err("invalid input: unexpected end of string literal".into()); "lexer error")]
    fn split(text: &str) -> std::result::Result<Vec<&str>, String> {
        split_statements(text).map_err(|error| error.to_string())
    }

    /// Returns a COPY command.
    fn copy_command(table: &str, columns: &[&str], direction: CopyDirection) -> CopyCommand {
        let columns = columns.iter().map(|column| column.to_string()).collect();
        CopyCommand { table: table.to_string(), columns, direction }
    }

    #[test_case("SELECT 1" => Ok(None); "not copy")]
    #[test_case("\"COPY\" t TO STDOUT" => Ok(None); "quoted copy")]
    #[test_case("copy T TO stdout" => Ok(Some(copy_command("t", &[], CopyDirection::ToStdout))); "to")]
    #[test_case("COPY t (a, \"B\") FROM STDIN" => Ok(Some(copy_command("t", &["a", "B"], CopyDirection::FromStdin))); "from")]
    #[test_case("COPY t TO STDIN" => Err(()); "to stdin")]
    #[test_case("COPY t FROM STDOUT" => Err(()); "from stdout")]
    #[test_case("COPY t () TO STDOUT" => Err(()); "no columns")]
    #[test_case("COPY t (a TO STDOUT" => Err(()); "unclosed columns")]
    #[test_case("COPY select TO STDOUT" => Err(()); "keyword table")]
    #[test_case("COPY t TO STDOUT x" => Err(()); "trailing token")]
    fn copy(statement: &str) -> std::result::Result<Option<CopyCommand>, ()> {
        CopyCommand::parse(statement).map_err(|error| {
            assert_eq!(error, PgError::new("42601", "syntax error in COPY command"));
        })
    }

    #[test]
    fn tags() {
        let select = StatementResult::Select { columns: vec![Label::None], rows: vec![vec![]] };
        assert_eq!(command_tag(&select), "SELECT 1");
        assert_eq!(command_tag(&StatementResult::Insert { count: 2 }), "INSERT 0 2");
        let drop = StatementResult::DropTable { name: "t".into(), existed: false };
        assert_eq!(command_tag(&drop), "DROP TABLE");
        assert_eq!(command_tag(&StatementResult::SetRole { user: "u".into() }), "SET ROLE");
        assert_eq!(zero_count("SELECT 3"), "SELECT 0");
        assert_eq!(zero_count("INSERT 0 2"), "INSERT 0 0");
        assert_eq!(zero_count("CREATE TABLE"), "CREATE TABLE");
    }

    #[test_case("SELECT a, t.b, c AS x FROM t" => vec!["a", "b", "x"]; "headers")]
    #[test_case("SELECT 1, 'a', NULL, $1, -a, a + 1 FROM t" => vec!["?column?"; 6]; "unnamed")]
    #[test_case("SELECT COUNT(*), Sqrt(a), (max(a)) FROM t" => vec!["count", "sqrt", "max"]; "functions")]
    #[test_case("SELECT 1, *, 2 FROM t" => vec!["?column?", "a", "b", "?column?"]; "star")]
    fn names(query: &str) -> Vec<String> {
        // A two-column table t (a, b); unnamed expressions have no label.
        let ast::Statement::Select { select, .. } = Parser::parse(query).unwrap() else {
            panic!("expected select");
        };
        let labels: Vec<Label> = select
            .iter()
            .flat_map(|(expr, alias)| match (expr, alias) {
                (ast::Expression::All, _) => vec![
                    Label::Qualified("t".into(), "a".into()),
                    Label::Qualified("t".into(), "b".into()),
                ],
                (_, Some(alias)) => vec![Label::Unqualified(alias.clone())],
                (ast::Expression::Column(table, column), None) => {
                    let table = table.clone().unwrap_or("t".into());
                    vec![Label::Qualified(table, column.clone())]
                }
                (_, None) => vec![Label::None],
            })
            .collect();
        let items = select_items(&select, labels.len());
        labels.iter().zip(items).map(|(label, item)| column_name(label, item)).collect()
    }

    #[test]
    fn codes() {
        let code = |error: Error| PgError::from(error).code;
        assert_eq!(code(Error::InvalidInput("x".into())), "42000");
        assert_eq!(code(Error::PermissionDenied("x".into())), "42501");
        assert_eq!(code(Error::ReadOnly), "25006");
        assert_eq!(code(Error::Serialization), "40001");
        assert_eq!(code(Error::Abort), "40003");
        assert_eq!(code(Error::InvalidData("x".into())), "XX000");
        assert_eq!(code(Error::IO("x".into())), "58030");
        let error = PgError::from(Error::InvalidInput("x".into()));
        assert_eq!(error.message, "invalid input: x");
    }

    #[test]
    fn copy_format() {
        let row = vec![
            Value::Integer(1),
            Value::Null,
            Value::String("a\tb\nc\r\\".into()),
            Value::Float(f64::NAN),
            Value::Boolean(true),
        ];
        let line = copy_line(&row, &[0, 1, 2, 3, 4]);
        assert_eq!(String::from_utf8(line).unwrap(), "1\t\\N\ta\\tb\\nc\\r\\\\\tNaN\tt\n");
        assert_eq!(copy_unescape("a\\tb\\nc\\r\\\\\\x\\"), "a\tb\nc\r\\x\\");
        assert_eq!(copy_escape(&copy_unescape("x\\ty")), "x\\ty");
    }

    #[test_case("t", DataType::Boolean => Some(Value::Boolean(true)); "boolean")]
    #[test_case("yes", DataType::Boolean => None; "boolean invalid")]
    #[test_case("+7", DataType::Integer => Some(Value::Integer(7)); "integer")]
    #[test_case("inf", DataType::Float => Some(Value::Float(f64::INFINITY)); "float")]
    #[test_case("-1e3", DataType::Float => Some(Value::Float(-1000.0)); "float exponent")]
    #[test_case("", DataType::String => Some(Value::String(String::new())); "string")]
    fn copy_field(text: &str, datatype: DataType) -> Option<Value> {
        read_field(text, datatype)
    }

    /// Creates a local engine with the given statements executed, and runs a
    /// closure with a client of a connection to it over an in-memory stream.
    fn with_connection(statements: &[&str], f: impl FnOnce(&mut Client<UnixStream>)) {
        let engine = Local::new(Memory::new());
        let mut session = engine.session();
        for statement in statements {
            session.execute(statement).expect("statement failed");
        }
        let (client, server) = UnixStream::pair().expect("stream pair failed");
        std::thread::scope(|s| {
            s.spawn(|| serve(&engine, server).expect("connection failed"));
            // Dropping the client closes the connection.
            f(&mut Client::new(client));
        });
    }

    /// Starts a connection as a user, and returns the displayed messages.
    fn startup(client: &mut Client<UnixStream>, user: &str, password: Option<&str>) -> String {
        let parameters = vec![("user".to_string(), user.to_string())];
        let messages = client.startup(message::PROTOCOL_VERSION, parameters, password).unwrap();
        client.display(&messages).join("\n")
    }

    #[test]
    fn startup_password() {
        let statements = ["CREATE USER alice PASSWORD 'pw'", "CREATE USER bob"];
        with_connection(&statements, |client| {
            let output = startup(client, "alice", Some("pw"));
            assert!(output.starts_with("AuthenticationMD5Password\nAuthenticationOk\n"));
            assert!(output.contains(" is_superuser=off "));
            assert!(output.ends_with("BackendKeyData\nReadyForQuery idle"));
        });
        with_connection(&statements, |client| {
            let output = startup(client, "alice", Some("wrong"));
            assert_eq!(
                output,
                "AuthenticationMD5Password\n\
                 ErrorResponse FATAL 28P01 password authentication failed for user alice"
            );
            assert!(client.receive().unwrap().is_none());
        });
        with_connection(&statements, |client| {
            let output = startup(client, "bob", Some("ignored"));
            assert!(output.starts_with("AuthenticationOk\n"));
        });
        // Another message in place of the password fails too.
        with_connection(&statements, |client| {
            let parameters = vec![("user".to_string(), "alice".to_string())];
            let version = message::PROTOCOL_VERSION;
            client.send(&FrontendMessage::Startup { version, parameters });
            client.flush().unwrap();
            let message = client.receive().unwrap();
            assert!(matches!(message, Some(BackendMessage::AuthenticationMd5Password { .. })));
            client.send(&FrontendMessage::Query("SELECT 1".into()));
            client.flush().unwrap();
            let messages = [client.receive().unwrap().unwrap()];
            assert_eq!(
                client.display(&messages),
                vec!["ErrorResponse FATAL 28P01 password authentication failed for user alice"]
            );
            assert!(client.receive().unwrap().is_none());
        });
    }

    #[test]
    fn query_results() {
        with_connection(&["CREATE TABLE t (id INT PRIMARY KEY, v STRING)"], |client| {
            startup(client, "admin", None);
            let query = "INSERT INTO t VALUES (1, 'a'), (2, NULL); SELECT v, id * 2 FROM t; BEGIN";
            let messages = client.query(query, CopyIn::Data(None)).unwrap();
            assert_eq!(
                client.display(&messages),
                vec![
                    "CommandComplete INSERT 0 2",
                    "RowDescription v text(25,-1), ?column? int8(20,8)",
                    "DataRow 'a', '2'",
                    "DataRow NULL, '4'",
                    "CommandComplete SELECT 2",
                    "CommandComplete BEGIN",
                    "ReadyForQuery transaction",
                ]
            );
        });
    }

    #[test]
    fn extended_query() {
        let statements = ["CREATE TABLE t (id INT PRIMARY KEY)", "INSERT INTO t VALUES (1), (2)"];
        with_connection(&statements, |client| {
            startup(client, "admin", None);
            let query = "SELECT id FROM t WHERE id >= $1".to_string();
            client.send(&FrontendMessage::Parse { name: String::new(), query, types: vec![20] });
            client.send(&FrontendMessage::Bind {
                portal: String::new(),
                statement: String::new(),
                formats: vec![1],
                values: vec![Some(1i64.to_be_bytes().to_vec())],
                results: vec![],
            });
            client.send(&FrontendMessage::Execute { portal: String::new(), max_rows: 1 });
            client.send(&FrontendMessage::Execute { portal: String::new(), max_rows: 1 });
            let messages = client.sync().unwrap();
            assert_eq!(
                client.display(&messages),
                vec![
                    "ParseComplete",
                    "BindComplete",
                    "DataRow '1'",
                    "PortalSuspended",
                    "DataRow '2'",
                    "CommandComplete SELECT 2",
                    "ReadyForQuery idle",
                ]
            );
        });
    }

    #[test]
    fn copy_in_out() {
        with_connection(&["CREATE TABLE t (id INT PRIMARY KEY, v STRING)"], |client| {
            startup(client, "admin", None);
            let data = CopyIn::Data(Some(b"1\ta\\tb\n2\t\\N\n".to_vec()));
            let messages = client.query("COPY t FROM STDIN", data).unwrap();
            assert_eq!(
                client.display(&messages),
                vec!["CopyInResponse 2 columns", "CommandComplete COPY 2", "ReadyForQuery idle"]
            );
            let messages = client.query("COPY t (v) TO STDOUT", CopyIn::Data(None)).unwrap();
            assert_eq!(
                client.display(&messages),
                vec![
                    "CopyOutResponse 1 columns",
                    "CopyData 'a\\\\tb\\n'",
                    "CopyData '\\\\N\\n'",
                    "CopyDone",
                    "CommandComplete COPY 2",
                    "ReadyForQuery idle",
                ]
            );
        });
    }

    #[test]
    fn protocol_errors() {
        let statements = ["CREATE TABLE t (id INT PRIMARY KEY)", "INSERT INTO t VALUES (1), (2)"];
        with_connection(&statements, |client| {
            startup(client, "admin", None);
            // After an error, messages up to Sync are skipped, also a Query;
            // COPY messages outside of COPY are ignored.
            client.send(&FrontendMessage::CopyDone);
            client.send(&FrontendMessage::Bind {
                portal: String::new(),
                statement: "nosuch".into(),
                formats: Vec::new(),
                values: Vec::new(),
                results: Vec::new(),
            });
            client.send(&FrontendMessage::Query("SELECT 1".into()));
            client.send(&FrontendMessage::Flush);
            let messages = client.sync().unwrap();
            assert_eq!(
                client.display(&messages),
                vec![
                    "ErrorResponse ERROR 26000 prepared statement nosuch does not exist",
                    "ReadyForQuery idle",
                ]
            );

            // Text that isn't UTF-8.
            let invalid = "ErrorResponse ERROR 22021 invalid byte sequence for encoding UTF8";
            let query = "SELECT $1".to_string();
            client.send(&FrontendMessage::Parse { name: String::new(), query, types: vec![25] });
            client.send(&FrontendMessage::Bind {
                portal: String::new(),
                statement: String::new(),
                formats: Vec::new(),
                values: vec![Some(vec![0xff])],
                results: Vec::new(),
            });
            let messages = client.sync().unwrap();
            assert_eq!(client.display(&messages)[1..], [invalid, "ReadyForQuery idle"]);
            let data = CopyIn::Data(Some(vec![0xff, b'\n']));
            let messages = client.query("COPY t (id) FROM STDIN", data).unwrap();
            assert_eq!(
                client.display(&messages),
                vec!["CopyInResponse 1 columns", invalid, "ReadyForQuery idle"]
            );

            // Another message during COPY FROM STDIN.
            client.send(&FrontendMessage::Query("COPY t (id) FROM STDIN".into()));
            client.flush().unwrap();
            let message = client.receive().unwrap();
            assert_eq!(message, Some(BackendMessage::CopyInResponse { columns: 1 }));
            let query = "SELECT 1".to_string();
            client.send(&FrontendMessage::Parse { name: String::new(), query, types: Vec::new() });
            client.flush().unwrap();
            let messages = client.receive_until_ready().unwrap();
            assert_eq!(
                client.display(&messages),
                vec![
                    "ErrorResponse ERROR 08P01 unexpected message type P during COPY from stdin",
                    "ReadyForQuery idle",
                ]
            );
            let messages = client.query("SELECT count(*) FROM t", CopyIn::Data(None)).unwrap();
            assert_eq!(client.display(&messages)[1], "DataRow '2'");
        });
    }
}
