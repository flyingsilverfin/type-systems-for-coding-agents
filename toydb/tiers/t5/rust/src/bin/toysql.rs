//! toySQL is a command-line client for toyDB. It connects to a toyDB node
//! (default localhost:9601) and executes SQL statements against it via an
//! interactive shell interface. Command history is stored in .toysql.history.
//! With --pg, it connects to the node's PostgreSQL port (default 9605) and uses
//! the PostgreSQL protocol.

#![warn(clippy::all)]

use std::net::TcpStream;
use std::path::PathBuf;

use clap::Parser as _;
use itertools::Itertools as _;
use rustyline::error::ReadlineError;
use rustyline::history::DefaultHistory;
use rustyline::validate::{ValidationContext, ValidationResult, Validator};
use rustyline::{Editor, Modifiers};
use rustyline_derive::{Completer, Helper, Highlighter, Hinter};

use toydb::Client;
use toydb::errinput;
use toydb::error::Result;
use toydb::pgwire;
use toydb::pgwire::client::CopyIn;
use toydb::pgwire::message::{BackendMessage, Field, PROTOCOL_VERSION, TransactionStatus};
use toydb::pgwire::types::{self, Type};
use toydb::sql::execution::StatementResult;
use toydb::sql::json;
use toydb::sql::parser::{Lexer, Token};
use toydb::sql::types::Value;

fn main() {
    if let Err(error) = Command::parse().run() {
        eprintln!("Error: {error}");
    }
}

/// The toySQL command.
#[derive(clap::Parser)]
#[command(about = "A toyDB client.", version, propagate_version = true)]
struct Command {
    /// A SQL statement to execute, then exit.
    #[arg()]
    statement: Option<String>,
    /// Host to connect to.
    #[arg(short = 'H', long, default_value = "localhost")]
    host: String,
    /// Port number to connect to (default 9601, or 9605 with --pg).
    #[arg(short = 'p', long)]
    port: Option<u16>,
    /// User to log in as.
    #[arg(long)]
    user: Option<String>,
    /// Password to log in with.
    #[arg(long, requires = "user")]
    password: Option<String>,
    /// Use the PostgreSQL protocol, on the server's PostgreSQL port.
    #[arg(long)]
    pg: bool,
}

impl Command {
    /// Runs the command.
    fn run(self) -> Result<()> {
        let connection = match self.pg {
            false => {
                let mut client = Client::connect((self.host.as_str(), self.port.unwrap_or(9601)))?;
                if let Some(user) = &self.user {
                    client.login(user, self.password.as_deref())?;
                }
                Connection::Toydb(client)
            }
            true => {
                let addr = (self.host.as_str(), self.port.unwrap_or(9605));
                let mut client = pgwire::Client::connect(addr)?;
                let user = self.user.as_deref().unwrap_or("admin").to_string();
                let parameters = vec![
                    ("user".to_string(), user),
                    ("application_name".to_string(), "toysql".to_string()),
                ];
                let password = self.password.as_deref();
                for message in client.startup(PROTOCOL_VERSION, parameters, password)? {
                    if let BackendMessage::ErrorResponse { message, .. } = message {
                        return errinput!("{message}");
                    }
                }
                if client.is_closed() {
                    return errinput!("connection closed (a password is required)");
                }
                Connection::Postgres { client, in_transaction: false }
            }
        };
        let mut shell = Shell::new(connection)?;
        match self.statement {
            Some(statement) => shell.execute(&statement),
            None => shell.run(),
        }
    }
}

/// The connection of a toySQL shell.
enum Connection {
    /// A toyDB client.
    Toydb(Client),
    /// A PostgreSQL client, and whether its session has an open transaction.
    Postgres { client: pgwire::Client<TcpStream>, in_transaction: bool },
}

/// An interactive toySQL shell.
struct Shell {
    /// The connection.
    connection: Connection,
    /// The Rustyline command editor.
    editor: Editor<InputValidator, DefaultHistory>,
    /// The path to the history file, if any.
    history_path: Option<PathBuf>,
    /// If true, SELECT column headers will be displayed.
    show_headers: bool,
    /// The output format for statement results.
    format: Format,
}

/// Output formats for statement results.
#[derive(Clone, Copy, PartialEq)]
enum Format {
    /// Human-readable text.
    Text,
    /// Each result as a single line of JSON.
    Json,
}

impl Shell {
    /// Creates a new shell on the given connection.
    fn new(connection: Connection) -> Result<Self> {
        // Set up Rustyline. Make sure multiline pastes are handled normally.
        let mut editor = Editor::new()?;
        editor.set_helper(Some(InputValidator));
        editor.bind_sequence(
            rustyline::KeyEvent(rustyline::KeyCode::BracketedPasteStart, Modifiers::NONE),
            rustyline::Cmd::Noop,
        );
        let history_path =
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".toysql.history"));
        Ok(Self { connection, editor, history_path, show_headers: false, format: Format::Text })
    }

    /// Returns the toyDB client, or errors for a PostgreSQL connection.
    fn client(&mut self) -> Result<&mut Client> {
        match &mut self.connection {
            Connection::Toydb(client) => Ok(client),
            Connection::Postgres { .. } => {
                errinput!("not available over the PostgreSQL protocol")
            }
        }
    }

    /// Executes a SQL statement or ! command.
    fn execute(&mut self, input: &str) -> Result<()> {
        if input.starts_with('!') {
            self.execute_command(input)
        } else if !input.is_empty() {
            self.execute_sql(input)
        } else {
            Ok(())
        }
    }

    /// Executes a toySQL ! command (e.g. !help)
    fn execute_command(&mut self, input: &str) -> Result<()> {
        let mut input = input.split_ascii_whitespace();
        let Some(command) = input.next() else {
            return errinput!("expected command");
        };
        let args = input.collect_vec();

        match (command, args.as_slice()) {
            // Toggles column headers.
            ("!headers", []) => {
                self.show_headers = !self.show_headers;
                match self.show_headers {
                    true => println!("Headers enabled"),
                    false => println!("Headers disabled"),
                }
            }
            ("!headers", _) => return errinput!("!headers takes no arguments"),

            // Sets the output format.
            ("!format", ["text"]) => {
                self.format = Format::Text;
                println!("Output format text");
            }
            ("!format", ["json"]) => {
                self.format = Format::Json;
                println!("Output format json");
            }
            ("!format", _) => return errinput!("!format takes 1 argument: text or json"),

            // Displays help.
            ("!help", []) => println!(
                r#"
Enter a SQL statement terminated by a semicolon (;) to execute it, or Ctrl-D to
exit. The following commands are also available:

    !format FORMAT     Sets the output format: text (default) or json
    !headers           Toggles column headers
    !help              This help message
    !status            Display server status
    !table NAME        Display a table schema
    !tables            List tables
"#
            ),
            ("!help", _) => return errinput!("!help takes no arguments"),

            // Displays server status.
            ("!status", []) => {
                let status = self.client()?.status()?;
                println!(
                    r#"
Server:       n{server} with Raft leader n{leader} in term {term} for {nodes} nodes
Raft log:     {committed} committed, {applied} applied, {raft_size} MB, {raft_garbage}% garbage ({raft_storage} engine)
Replication:  {raft_match}
SQL storage:  {sql_keys} keys, {sql_size} MB logical, {nodes}x {sql_disk_size} MB disk, {sql_garbage}% garbage ({sql_storage} engine)
Transactions: {active_txns} active, {versions} total
"#,
                    server = status.server,
                    leader = status.raft.leader,
                    term = status.raft.term,
                    nodes = status.raft.match_index.len(),
                    committed = status.raft.commit_index,
                    applied = status.raft.applied_index,
                    raft_size =
                        format_args!("{:.3}", status.raft.storage.size as f64 / 1_000_000.0),
                    raft_garbage =
                        format_args!("{:.0}", status.raft.storage.garbage_disk_percent()),
                    raft_storage = status.raft.storage.name,
                    raft_match =
                        status.raft.match_index.iter().map(|(n, m)| format!("n{n}:{m}")).join(" "),
                    sql_keys = status.mvcc.storage.keys,
                    sql_size = format_args!("{:.3}", status.mvcc.storage.size as f64 / 1_000_000.0),
                    sql_disk_size =
                        format_args!("{:.3}", status.mvcc.storage.disk_size as f64 / 1_000_000.0),
                    sql_garbage = format_args!("{:.0}", status.mvcc.storage.garbage_disk_percent()),
                    sql_storage = status.mvcc.storage.name,
                    active_txns = status.mvcc.active_txns,
                    versions = status.mvcc.versions,
                )
            }
            ("!status", _) => return errinput!("!status takes no arguments"),

            ("!table", [name]) => println!("{}", self.client()?.get_table(name)?),
            ("!table", _) => return errinput!("!table takes 1 argument"),

            ("!tables", []) => self.client()?.list_tables()?.iter().for_each(|t| println!("{t}")),
            ("!tables", _) => return errinput!("!tables takes no arguments"),

            (command, _) => return errinput!("unknown command {command}"),
        }
        Ok(())
    }

    /// Executes a SQL statement and displays the results.
    fn execute_sql(&mut self, statement: &str) -> Result<()> {
        use StatementResult::*;
        if let Connection::Postgres { .. } = self.connection {
            return self.execute_pg(statement);
        }
        let result = self.client()?.execute(statement)?;
        if self.format == crate::Format::Json {
            println!("{}", json::result(&result));
            return Ok(());
        }
        match result {
            Begin(state) => match state.read_only {
                true => println!("Began read-only transaction at version {}", state.version),
                false => println!("Began transaction {}", state.version),
            },
            Commit { version } => println!("Committed transaction {version}"),
            Rollback { version } => println!("Rolled back transaction {version}"),
            Insert { count } => println!("Inserted {count} rows"),
            Delete { count } => println!("Deleted {count} rows"),
            Update { count } => println!("Updated {count} rows"),
            CreateTable { name } => println!("Created table {name}"),
            DropTable { name, existed } => match existed {
                true => println!("Dropped table {name}"),
                false => println!("Table {name} does not exist"),
            },
            Explain(plan) => println!("{plan}"),
            Select { columns, rows } => {
                if self.show_headers {
                    println!("{}", columns.iter().map(|c| c.as_header()).join(", "));
                }
                for row in rows {
                    println!("{}", row.iter().join(", "));
                }
            }
            Analyze { tables } => match tables.is_empty() {
                true => println!("No tables analyzed"),
                false => println!("Analyzed tables {}", tables.join(", ")),
            },
            Export { lines, .. } | Format { lines } => {
                lines.iter().for_each(|line| println!("{line}"))
            }
            CreateView { name } => println!("Created view {name}"),
            DropView { name, existed } => match existed {
                true => println!("Dropped view {name}"),
                false => println!("View {name} does not exist"),
            },
            Prepare { name } => println!("Prepared statement {name}"),
            Deallocate { name } => println!("Deallocated statement {name}"),
            CreateUser { name } => println!("Created user {name}"),
            DropUser { name, existed } => match existed {
                true => println!("Dropped user {name}"),
                false => println!("User {name} does not exist"),
            },
            Grant { .. } => println!("Granted"),
            Revoke { .. } => println!("Revoked"),
            SetRole { user } => println!("Role set to {user}"),
            ResetRole { user } => println!("Role reset to {user}"),
            CreatePolicy { table, name } => println!("Created policy {name} on {table}"),
            DropPolicy { table, name, existed } => match existed {
                true => println!("Dropped policy {name} on {table}"),
                false => println!("Policy {name} on {table} does not exist"),
            },
            CreateMaterializedView { name, count } => {
                println!("Created materialized view {name} with {count} rows")
            }
            RefreshMaterializedView { name, count } => {
                println!("Refreshed materialized view {name} with {count} rows")
            }
            DropMaterializedView { name, existed } => match existed {
                true => println!("Dropped materialized view {name}"),
                false => println!("Materialized view {name} does not exist"),
            },
        }
        Ok(())
    }

    /// Executes SQL statements as a PostgreSQL simple query and displays their
    /// results.
    fn execute_pg(&mut self, statement: &str) -> Result<()> {
        if self.format == Format::Json {
            return errinput!("JSON output is not available over the PostgreSQL protocol");
        }
        let Connection::Postgres { client, in_transaction } = &mut self.connection else {
            return errinput!("not a PostgreSQL connection");
        };
        let copy = CopyIn::Fail("toysql doesn't support COPY FROM STDIN".to_string());
        let mut columns = Vec::new();
        let mut rows = Vec::new();
        for message in client.query(statement, copy)? {
            match message {
                BackendMessage::RowDescription(fields) => columns = fields,
                BackendMessage::DataRow(values) => rows.push(values),
                BackendMessage::CopyData(data) => print!("{}", String::from_utf8_lossy(&data)),
                BackendMessage::CommandComplete(tag) => {
                    Self::print_pg_result(&tag, &columns, &rows, self.show_headers);
                    (columns, rows) = (Vec::new(), Vec::new());
                }
                BackendMessage::ErrorResponse { message, .. } => eprintln!("Error: {message}"),
                BackendMessage::ReadyForQuery(status) => {
                    *in_transaction = status == TransactionStatus::Transaction;
                }
                _ => {}
            }
        }
        if client.is_closed() {
            return errinput!("connection closed");
        }
        Ok(())
    }

    /// Displays the result of a statement executed over the PostgreSQL
    /// protocol, from its command tag and its rows.
    fn print_pg_result(
        tag: &str,
        columns: &[Field],
        rows: &[Vec<Option<Vec<u8>>>],
        show_headers: bool,
    ) {
        let (command, count) = tag.rsplit_once(' ').unwrap_or((tag, ""));
        let values = rows.iter().map(|row| {
            row.iter().zip(columns).map(|(value, column)| {
                let Some(value) = value else {
                    return Value::Null;
                };
                let text = String::from_utf8_lossy(value);
                Type::from_oid(column.type_oid)
                    .and_then(|pgtype| types::decode_text(pgtype, &text).ok())
                    .unwrap_or_else(|| Value::String(text.into_owned()))
            })
        });
        match command {
            "EXPLAIN" | "FORMAT" | "EXPORT" => {
                values.flatten().for_each(|line| match line {
                    Value::String(line) => println!("{line}"),
                    line => println!("{line}"),
                });
            }
            "SELECT" => {
                if show_headers {
                    println!("{}", columns.iter().map(|column| &column.name).join(", "));
                }
                values.for_each(|row| println!("{}", row.into_iter().join(", ")));
            }
            "INSERT 0" => println!("Inserted {count} rows"),
            "UPDATE" => println!("Updated {count} rows"),
            "DELETE" => println!("Deleted {count} rows"),
            _ => println!("{tag}"),
        }
    }

    /// Prompts the user for input. Returns None if the shell should close.
    fn prompt(&mut self) -> rustyline::Result<String> {
        let prompt = match &self.connection {
            Connection::Toydb(client) => match client.txn() {
                Some(txn) if txn.read_only => format!("toydb@{}> ", txn.version),
                Some(txn) => format!("toydb:{}> ", txn.version),
                None => "toydb> ".to_string(),
            },
            Connection::Postgres { in_transaction: true, .. } => "toydb*> ".to_string(),
            Connection::Postgres { in_transaction: false, .. } => "toydb> ".to_string(),
        };
        self.editor.readline(&prompt)
    }

    /// Runs the interactive shell.
    fn run(&mut self) -> Result<()> {
        // Load the history file, if any.
        if let Some(history_path) = &self.history_path {
            match self.editor.load_history(history_path) {
                Ok(()) => {}
                Err(ReadlineError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }

        // Print welcome message.
        match &mut self.connection {
            Connection::Toydb(client) => {
                let server = client.status()?.server;
                println!("Connected to toyDB node n{server}. Enter !help for instructions.");
            }
            Connection::Postgres { .. } => println!(
                "Connected to toyDB with the PostgreSQL protocol. Enter !help for instructions."
            ),
        }

        // Prompt for commands and execute them.
        loop {
            let input = match self.prompt() {
                Ok(input) => input.trim().to_string(),
                Err(ReadlineError::Interrupted) => continue,
                Err(ReadlineError::Eof) => break,
                Err(error) => return Err(error.into()),
            };
            self.editor.add_history_entry(&input)?;
            if let Err(error) = self.execute(&input) {
                eprintln!("Error: {error}");
            };
        }

        // Save the history file.
        if let Some(history_path) = &self.history_path {
            self.editor.save_history(history_path)?;
        }
        Ok(())
    }
}

/// A Rustyline helper for multiline editing. After a new line is entered, it
/// determines whether the input makes up a complete SQL statement that should
/// be submitted to the server (i.e. it's terminated by ;), or wait for further
/// input.
#[derive(Completer, Helper, Highlighter, Hinter)]
struct InputValidator;

impl Validator for InputValidator {
    fn validate(&self, ctx: &mut ValidationContext) -> rustyline::Result<ValidationResult> {
        let input = ctx.input();
        // Empty lines and ! commands are ready.
        if input.is_empty() || input.starts_with('!') || input == ";" {
            return Ok(ValidationResult::Valid(None));
        }
        // For SQL statements, just look for any semicolon or lexer error, and
        // rely on the server for further validation and error handling.
        if Lexer::new(input).any(|r| matches!(r, Ok(Token::Semicolon) | Err(_))) {
            return Ok(ValidationResult::Valid(None));
        }
        // Otherwise, wait for more input.
        Ok(ValidationResult::Incomplete)
    }

    fn validate_while_typing(&self) -> bool {
        false // only check after completed lines
    }
}
