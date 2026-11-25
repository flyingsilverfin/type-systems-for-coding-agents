//! Implements a SQL execution engine. A SQL statement flows through the engine
//! as follows:
//!
//! 1. The `toySQL` client connects to the server, which creates a new
//!    `sql::execution::Session` in `Server::sql_session`.
//!
//! 2. `toySQL` submits a SQL `SELECT` string, which the server executes via
//!    `Session::execute`.
//!
//! 3. `Session::execute` calls `Parser::parse` to parse the SQL `SELECT` string
//!    into an `ast::Statement::Select` AST (Abstract Syntax Tree). The parser
//!    uses the `Lexer` for initial tokenization.
//!     
//! 4. `Session::execute` obtains a new read-only `sql::engine::Transaction` via
//!    `Session::with_txn`. We'll gloss over the details here.
//!
//! 5. `Session::execute` calls `Plan::build` to construct an execution plan
//!    from the AST via the `Planner`, using the `Transaction`'s
//!    `sql::engine::Catalog` trait to look up table schema information.
//!
//! 6. `Session::execute` calls `Plan::optimize` to optimize the execution plan
//!    via the optimizers in `sql::planner::optimizer`. This e.g. performs
//!    filter pushdown to filter rows during storage scans, uses secondary
//!    indexes where appropriate, and chooses more efficient join types.
//!
//! 7. `Session::execute` calls `Plan::execute` to actually execute the plan,
//!    using the `Transaction` to access the `sql::engine::Engine`.  It uses the
//!    executors in `sql::execution` to recursively execute the
//!    `sql::planner::Node` nodes, which stream and process `sql::types::Row`
//!    vectors via `sql::types::Rows` iterators.
//!
//! 8. At the tip of the execution plan there's typically a `Node::Scan` which
//!    performs full table scans from storage. It is executed by
//!    `sql::execution::source::scan`, which calls `Transaction::scan`.
//!
//! 9. The upper `sql::engine::Raft` engine submits a `Read::Scan` request to
//!    Raft via `Raft::read` and `Raft::execute`. This is submitted through the
//!    crossbeam channel `Raft::tx`, which is routed to the local Raft node in
//!    `Server::raft_route` via `raft::Node::step`.
//!
//! 10. We'll skip Raft details, but see the `raft` module documentation. The
//!     `Read::Scan` request eventually makes its way to the SQL state machine
//!     `sql::engine::raft::State` that's managed by Raft. Since this is a read
//!     request, it is executed only on the leader node, calling `State::read`.
//!
//! 11. `State` wraps the `sql::engine::Local` SQL execution engine that runs
//!     on each node, using local storage. `State::read` calls
//!     `Transaction::scan` using a `Local::Transaction`.
//!
//! 12. The `Local` engine uses a `storage::BitCask` engine for local storage,
//!     with `storage::mvcc` providing transactions. See their documentation
//!     for details.
//!
//! 13. `Transaction::scan` uses `sql::engine::KeyPrefix::Table` to obtain the
//!     key prefix for the scanned table, encoded via `encoding::keycode`. It
//!     scans rows under this prefix by calling `MVCC::scan_prefix`, which in
//!     turn dispatches to `BitCask::scan_prefix`. It returns a row iterator.
//!
//! 14. A row iterator is propagated back up through the stack:
//!     `BitCask` → `MVCC` → `Local` → `State` → `Raft` → `scan` → `Plan::execute`
//!
//! 15. `Plan::execute` collects the results in a `ExecutionResult::Select`,
//!     and returns it to `Session::execute`. It in turns returns it to
//!     `Server::sql_session`, which encodes it and sends it across the wire
//!     to `toySQL`, which displays them to the user.
//!
//! TODO: expand this into a "Life of a SQL statement" document.

pub mod engine;
pub mod execution;
pub mod json;
pub mod parser;
pub mod planner;
pub mod types;

/// SQL tests are implemented as goldenscripts under src/sql/testscripts.
#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::error::Error;
    use std::fmt::Write as _;
    use std::os::unix::net::UnixStream;
    use std::path::Path;
    use std::result::Result;

    use crossbeam::channel::Receiver;
    use itertools::Itertools as _;
    use tempfile::TempDir;
    use test_each_file::test_each_path;

    use super::engine::Catalog as _;
    use super::execution::{Session, StatementResult};
    use super::json;
    use super::parser::Parser;
    use super::planner::{OPTIMIZERS, Plan};
    use crate::backup;
    use crate::encoding::format::{self, Formatter as _};
    use crate::pgwire;
    use crate::pgwire::client::{self as pgclient, CopyIn};
    use crate::pgwire::message::{FrontendMessage, PROTOCOL_VERSION, Target};
    use crate::sql::engine::{Engine, Local};
    use crate::sql::planner::{Planner, Scope};
    use crate::storage::engine::test as testengine;
    use crate::storage::{self, Engine as _};

    // Run goldenscript tests in src/sql/testscripts.
    test_each_path! { in "src/sql/testscripts/expressions" as expressions => test_goldenscript_expr }
    test_each_path! { in "src/sql/testscripts/ext" as ext => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/ext4" as ext4 => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/ext5" as ext5 => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/optimizers" as optimizers => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/queries" as queries => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/schema" as schema => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/task" as task => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/transactions" as transactions => test_goldenscript }
    test_each_path! { in "src/sql/testscripts/writes" as writes => test_goldenscript }

    /// Runs SQL goldenscripts.
    fn test_goldenscript(path: &Path) {
        // The runner's Session can't borrow from an Engine in the same struct,
        // so pass an engine reference. Use both BitCask and Memory engines and
        // mirror operations across them. Emit engine operations to op_rx.
        let (op_tx, op_rx) = crossbeam::channel::unbounded();
        let tempdir = TempDir::with_prefix("toydb").expect("tempdir failed");
        let bitcask =
            storage::BitCask::new(tempdir.path().join("bitcask")).expect("bitcask failed");
        let memory = storage::Memory::new();
        let engine =
            Local::new(testengine::Emit::new(testengine::Mirror::new(bitcask, memory), op_tx));

        // PostgreSQL connections are served by threads of the scope.
        std::thread::scope(|scope| {
            let mut runner = SQLRunner::new(&engine, op_rx, scope);
            goldenscript::run(&mut runner, path).expect("goldenscript failed")
        })
    }

    /// Runs expression goldenscripts.
    fn test_goldenscript_expr(path: &Path) {
        goldenscript::run(&mut ExpressionRunner, path).expect("goldenscript failed")
    }

    /// The SQL test runner.
    struct SQLRunner<'a, 's> {
        engine: &'a TestEngine,
        sessions: HashMap<String, Session<'a, TestEngine>>,
        op_rx: Receiver<testengine::Operation>,
        /// The PostgreSQL connections, by prefix, over in-memory streams to
        /// connections served by threads of the scope.
        pg_clients: HashMap<String, pgclient::Client<UnixStream>>,
        scope: &'s std::thread::Scope<'s, 'a>,
        /// The backups, by name.
        backups: HashMap<String, Vec<u8>>,
    }

    type TestEngine =
        Local<testengine::Emit<testengine::Mirror<storage::BitCask, storage::Memory>>>;

    /// Commands accepted by the SQL Goldenscript runner.
    #[derive(goldenscript::Command)]
    enum SQLCommand {
        /// Creates a backup through the prefix's session.
        Backup {
            /// The backup name.
            name: String,
            /// The version to create the backup as of.
            #[arg(key)]
            as_of: Option<u64>,
        },
        /// Compares two backups as toybackup diff does.
        #[command(name = "backup-diff")]
        BackupDiff { a: String, b: String },
        /// Prints a backup's bytes in hex.
        #[command(name = "backup-hex")]
        BackupHex { name: String },
        /// Lists a backup as toybackup list does.
        #[command(name = "backup-list")]
        BackupList { name: String },
        /// Stores the bytes given in hex as a backup.
        #[command(name = "backup-load")]
        BackupLoad { name: String, hex: Vec<String> },
        /// Overwrites bytes of a backup at an offset.
        #[command(name = "backup-patch")]
        BackupPatch {
            name: String,
            #[arg(key)]
            offset: usize,
            #[arg(key)]
            hex: String,
        },
        /// Keeps only the first bytes of a backup.
        #[command(name = "backup-truncate")]
        BackupTruncate {
            name: String,
            #[arg(key)]
            length: usize,
        },
        /// Verifies a backup as toybackup verify does.
        #[command(name = "backup-verify")]
        BackupVerify { name: String },
        /// Dumps the raw SQL storage engine contents.
        Dump,
        /// Drives the prefix's PostgreSQL connection.
        Pg {
            /// The subcommand and its positional arguments.
            args: Vec<String>,
            /// The subcommand's key=value arguments.
            #[arg(optional)]
            options: Vec<(String, String)>,
        },
        /// Restores a backup through the prefix's session.
        Restore { name: String },
        /// Displays table schemas.
        Schema(
            /// The table names, or empty to display all tables.
            #[arg(optional)]
            Vec<String>,
        ),
        /// Any other command is executed as a raw SQL statement.
        #[command(other)]
        Statement(goldenscript::Command),
    }

    impl<'a, 's> SQLRunner<'a, 's> {
        fn new(
            engine: &'a TestEngine,
            op_rx: Receiver<testengine::Operation>,
            scope: &'s std::thread::Scope<'s, 'a>,
        ) -> Self {
            Self {
                engine,
                sessions: HashMap::new(),
                op_rx,
                pg_clients: HashMap::new(),
                scope,
                backups: HashMap::new(),
            }
        }

        /// Returns a backup by name.
        fn backup(&mut self, name: &str) -> Result<&mut Vec<u8>, Box<dyn Error>> {
            self.backups.get_mut(name).ok_or_else(|| format!("unknown backup {name}").into())
        }

        /// Runs a pg command on the prefix's PostgreSQL connection. startup
        /// connects it, over an in-memory stream to a connection of the engine.
        fn pg(
            &mut self,
            prefix: &str,
            args: &[String],
            options: &[(String, String)],
        ) -> Result<String, Box<dyn Error>> {
            if args.first().map(String::as_str) == Some("startup") {
                if self.pg_clients.contains_key(prefix) {
                    return Err("connection already open".into());
                }
                let (client, server) = UnixStream::pair()?;
                let engine = self.engine;
                self.scope.spawn(move || pgwire::connection::serve(engine, server));
                self.pg_clients.insert(prefix.to_string(), pgclient::Client::new(client));
            }
            let Some(client) = self.pg_clients.get_mut(prefix) else {
                return Err("no open PostgreSQL connection".into());
            };
            let output = pg_command(client, args, options);
            if client.is_closed() || output.is_err() {
                self.pg_clients.remove(prefix);
            }
            output
        }
    }

    /// Runs a pg command on a PostgreSQL connection, and returns its output.
    fn pg_command(
        client: &mut pgclient::Client<UnixStream>,
        args: &[String],
        options: &[(String, String)],
    ) -> Result<String, Box<dyn Error>> {
        let option = |key: &str| {
            options.iter().find(|(name, _)| name == key).map(|(_, value)| value.as_str())
        };
        let list = |key: &str| -> Result<Vec<i64>, Box<dyn Error>> {
            match option(key) {
                None | Some("") => Ok(Vec::new()),
                Some(list) => Ok(list.split(',').map(str::parse).collect::<Result<_, _>>()?),
            }
        };
        let name = |key: &str| option(key).unwrap_or_default().to_string();
        let target = || match (option("statement"), option("portal")) {
            (Some(name), None) => Ok((Target::Statement, name.to_string())),
            (None, Some(name)) => Ok((Target::Portal, name.to_string())),
            _ => Err("expected statement or portal"),
        };
        let sql = || args.get(1).cloned().ok_or("expected SQL");

        let mut output = Vec::new();
        let messages = match args.first().map(String::as_str).unwrap_or_default() {
            "startup" => {
                if args.iter().any(|arg| arg == "ssl") {
                    output.push(format!("SSLResponse {}", client.request_ssl()? as char));
                }
                let user = option("user").unwrap_or("admin").to_string();
                let mut parameters = vec![("user".to_string(), user)];
                if let Some(name) = option("application_name") {
                    parameters.push(("application_name".to_string(), name.to_string()));
                }
                let version = match option("protocol").map(|p| p.split_once('.')) {
                    Some(Some((major, minor))) => {
                        major.parse::<u32>()? << 16 | minor.parse::<u32>()?
                    }
                    Some(None) => return Err("invalid protocol".into()),
                    None => PROTOCOL_VERSION,
                };
                client.startup(version, parameters, option("password"))?
            }
            "query" => client.query(&sql()?, CopyIn::Data(None))?,
            "copy" => {
                let copy = match (option("data"), option("fail")) {
                    (data, None) => CopyIn::Data(data.map(|data| data.as_bytes().to_vec())),
                    (None, Some(message)) => CopyIn::Fail(message.to_string()),
                    (Some(_), Some(_)) => return Err("data and fail are exclusive".into()),
                };
                client.query(&sql()?, copy)?
            }
            "parse" => {
                let types =
                    list("types")?.into_iter().map(u32::try_from).collect::<Result<_, _>>()?;
                client.send(&FrontendMessage::Parse { name: name("name"), query: sql()?, types });
                Vec::new()
            }
            "bind" => {
                let formats: Vec<i16> =
                    list("formats")?.into_iter().map(i16::try_from).collect::<Result<_, _>>()?;
                let results =
                    list("results")?.into_iter().map(i16::try_from).collect::<Result<_, _>>()?;
                let params = option("params").map(|p| p.split(',').collect()).unwrap_or(Vec::new());
                let mut values = Vec::new();
                for param in params {
                    values.push(match param {
                        "NULL" => None,
                        param => match param.strip_prefix("0x") {
                            Some(hex) => Some(hex::decode(hex)?),
                            None => Some(param.as_bytes().to_vec()),
                        },
                    });
                }
                let (portal, statement) = (name("portal"), name("statement"));
                client.send(&FrontendMessage::Bind { portal, statement, formats, values, results });
                Vec::new()
            }
            "describe" => {
                let (target, name) = target()?;
                client.send(&FrontendMessage::Describe { target, name });
                Vec::new()
            }
            "execute" => {
                let max_rows = option("max_rows").map(str::parse).transpose()?.unwrap_or(0);
                client.send(&FrontendMessage::Execute { portal: name("portal"), max_rows });
                Vec::new()
            }
            "close" => {
                let (target, name) = target()?;
                client.send(&FrontendMessage::Close { target, name });
                Vec::new()
            }
            "sync" => client.sync()?,
            "terminate" => {
                client.terminate()?;
                return Ok(String::new());
            }
            command => return Err(format!("unknown pg command {command}").into()),
        };
        output.extend(client.display(&messages));
        if client.is_closed() {
            output.push("Closed".to_string());
        }
        Ok(output.into_iter().map(|line| format!("{line}\n")).collect())
    }

    impl goldenscript::Runner for SQLRunner<'_, '_> {
        type Command = SQLCommand;

        fn run(
            &mut self,
            command: &SQLCommand,
            context: &goldenscript::Context,
        ) -> Result<String, Box<dyn Error>> {
            let mut output = String::new();

            // Obtain a session based on the command prefix ("" if none).
            let prefix = context.prefix.clone().unwrap_or_default();
            let session = self.sessions.entry(prefix).or_insert_with(|| self.engine.session());

            // Handle runner commands.
            let command = match command {
                SQLCommand::Backup { name, as_of } => {
                    let data = session.backup(*as_of)?;
                    self.backups.insert(name.clone(), data);
                    return Ok(output);
                }

                SQLCommand::Restore { name } => {
                    let data = self.backups.get(name).ok_or(format!("unknown backup {name}"))?;
                    session.restore(data)?;
                    return Ok(output);
                }

                SQLCommand::BackupList { name } => {
                    for line in backup::format::list(self.backup(name)?) {
                        writeln!(output, "{line}")?;
                    }
                    return Ok(output);
                }

                SQLCommand::BackupVerify { name } => {
                    match backup::format::read(self.backup(name)?) {
                        Ok(records) => writeln!(output, "ok: {} records", records.len())?,
                        Err(error) => writeln!(output, "Error: {error}")?,
                    }
                    return Ok(output);
                }

                SQLCommand::BackupDiff { a, b } => {
                    let (a, b) = (self.backup(a)?.clone(), self.backup(b)?);
                    match backup::format::diff(&a, b) {
                        Ok(lines) => lines.iter().try_for_each(|l| writeln!(output, "{l}"))?,
                        Err(error) => writeln!(output, "Error: {error}")?,
                    }
                    return Ok(output);
                }

                SQLCommand::BackupHex { name } => {
                    for chunk in self.backup(name)?.chunks(16) {
                        writeln!(output, "{}", chunk.iter().map(|b| format!("{b:02x}")).join(" "))?;
                    }
                    return Ok(output);
                }

                SQLCommand::BackupLoad { name, hex } => {
                    self.backups.insert(name.clone(), hex::decode(hex.concat())?);
                    return Ok(output);
                }

                SQLCommand::BackupPatch { name, offset, hex } => {
                    let bytes = hex::decode(hex)?;
                    let data = self.backup(name)?;
                    let Some(target) = data.get_mut(*offset..*offset + bytes.len()) else {
                        return Err("patch beyond end of backup".into());
                    };
                    target.copy_from_slice(&bytes);
                    return Ok(output);
                }

                SQLCommand::BackupTruncate { name, length } => {
                    self.backup(name)?.truncate(*length);
                    return Ok(output);
                }

                SQLCommand::Dump => {
                    let mut engine = self.engine.mvcc.engine.lock().expect("mutex failed");
                    let mut iter = engine.scan(..);
                    while let Some((key, value)) = iter.next().transpose()? {
                        let fmtkv = format::MVCC::<format::SQL>::key_value(&key, &value);
                        let rawkv = format::Raw::key_value(&key, &value);
                        writeln!(output, "{fmtkv} [{rawkv}]",)?;
                    }
                    return Ok(output);
                }

                SQLCommand::Schema(tables) => {
                    let schemas = if tables.is_empty() {
                        session.with_txn(true, |txn| txn.list_tables())?
                    } else {
                        tables
                            .iter()
                            .map(|t| session.with_txn(true, |txn| txn.must_get_table(t)))
                            .try_collect()?
                    };
                    return Ok(schemas.into_iter().join("\n"));
                }

                SQLCommand::Pg { args, options } => {
                    let prefix = context.prefix.clone().unwrap_or_default();
                    return self.pg(&prefix, args, options);
                }

                SQLCommand::Statement(command) => command,
            };

            // The entire command is the SQL statement. There are no args.
            if !command.args.is_empty() {
                return Err("SQL statements should be given as a command with no args".into());
            }
            let input = &command.name;
            let mut tags = context.tags.clone();

            // Output the plan if requested.
            if tags.remove("plan") {
                let ast = Parser::parse(input)?;
                let plan =
                    session.with_txn(true, |txn| Planner::new(txn).build(ast)?.optimize())?;
                writeln!(output, "{plan}")?;
            }

            // Output plan optimizations if requested.
            if tags.remove("opt") {
                if tags.contains("plan") {
                    return Err("using both plan and opt is redundant".into());
                }
                let ast = Parser::parse(input)?;
                let plan = session.with_txn(true, |txn| Planner::new(txn).build(ast))?;
                let Plan::Select(mut root) = plan else {
                    return Err("can only use opt with SELECT plans".into());
                };
                writeln!(output, "{}", format!("Initial:\n{root}").replace('\n', "\n   "))?;
                for optimizer in OPTIMIZERS.iter() {
                    let prev = root.clone();
                    root = optimizer.optimize(root)?;
                    if root != prev {
                        writeln!(
                            output,
                            "{}",
                            format!("{optimizer:?}:\n{root}").replace('\n', "\n   ")
                        )?;
                    }
                }
            }

            // Execute the statement.
            let result = session.execute(input)?;

            // Output engine ops if requested.
            if tags.remove("ops") {
                while let Ok(op) = self.op_rx.try_recv() {
                    match op {
                        testengine::Operation::Delete { key } => {
                            let fmtkey = format::MVCC::<format::SQL>::key(&key);
                            let rawkey = format::Raw::key(&key);
                            writeln!(output, "delete {fmtkey} [{rawkey}]")?;
                        }
                        testengine::Operation::Flush => writeln!(output, "flush")?,
                        testengine::Operation::Set { key, value } => {
                            let fmtkv = format::MVCC::<format::SQL>::key_value(&key, &value);
                            let rawkv = format::Raw::key_value(&key, &value);
                            writeln!(output, "set {fmtkv} [{rawkv}]")?;
                        }
                    }
                }
            }

            // Output the result if requested. SELECT, EXPORT and FORMAT
            // results are always output. With json, the JSON result is output
            // instead.
            if tags.remove("json") {
                tags.remove("header");
                tags.remove("result");
                writeln!(output, "{}", json::result(&result))?;
            } else {
                match result {
                    StatementResult::Select { columns, rows } => {
                        if tags.remove("header") {
                            writeln!(output, "{}", columns.into_iter().join(", "))?;
                        }
                        for row in rows {
                            writeln!(output, "{}", row.into_iter().join(", "))?;
                        }
                    }
                    StatementResult::Export { lines, .. } | StatementResult::Format { lines } => {
                        for line in lines {
                            writeln!(output, "{line}")?;
                        }
                    }
                    result if tags.remove("result") => writeln!(output, "{result:?}")?,
                    _ => {}
                }
            }

            // Reject unknown tags.
            if let Some(tag) = tags.iter().next() {
                return Err(format!("unknown tag {tag}").into());
            }

            Ok(output)
        }

        /// Drain unprocessed operations after each command.
        fn end_command(
            &mut self,
            _: &SQLCommand,
            _: &goldenscript::Context,
        ) -> Result<String, Box<dyn Error>> {
            while self.op_rx.try_recv().is_ok() {}
            Ok(String::new())
        }
    }

    /// A test runner for expressions. Evaluates expressions to values, and
    /// optionally emits the expression tree.
    struct ExpressionRunner;

    type Catalog<'a> = <Local<storage::Memory> as Engine<'a>>::Transaction;

    impl goldenscript::Runner for ExpressionRunner {
        type Command = goldenscript::Command;

        fn run(
            &mut self,
            command: &goldenscript::Command,
            context: &goldenscript::Context,
        ) -> Result<String, Box<dyn Error>> {
            let mut output = String::new();

            // The entire command is the expression to evaluate. There are no args.
            if command.consume_args().next().is_some() {
                return Err("expressions should be given as a command with no args".into());
            }
            let input = &command.name;
            let mut tags = context.tags.clone();

            // Parse and build the expression.
            let ast = Parser::parse_expr(input)?;
            let expr = Planner::<Catalog>::build_expression(ast, &Scope::new())?;

            // Evaluate the expression.
            let value = expr.evaluate(None)?;
            write!(output, "{value}")?;

            // If requested, convert the expression to conjunctive normal form
            // and dump it. Assert that it produces the same result.
            if tags.remove("cnf") {
                let cnf = expr.clone().into_cnf();
                assert_eq!(value, cnf.evaluate(None)?, "CNF result differs");
                write!(output, " ← {cnf}")?;
            }

            // If requested, debug-dump the parsed expression.
            if tags.remove("expr") {
                write!(output, " ← {:?}", expr)?;
            }
            writeln!(output)?;

            // Reject unknown tags.
            if let Some(tag) = tags.iter().next() {
                return Err(format!("unknown tag {tag}").into());
            }

            Ok(output)
        }
    }
}
