//! A basic set of end-to-end tests as Goldenscripts under tests/scripts/. These
//! spin up actual clusters using the built binary and run operations against
//! them from multiple clients.
//!
//! There are more comprehensive tests elsewhere in the codebase, see the various
//! src/*/testscript scripts.

#![warn(clippy::all)]

mod testcluster;

use std::collections::HashMap;
use std::error::Error;
use std::fmt::Write as _;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};

use itertools::Itertools as _;
use test_each_file::test_each_path;

use testcluster::TestCluster;
use toydb::pgwire::client::{Client as PgClient, CopyIn};
use toydb::pgwire::message::{FrontendMessage, PROTOCOL_VERSION, Target};
use toydb::sql::json;
use toydb::{Client, StatementResult};

// Run goldenscript tests in tests/scripts.
test_each_path! { in "tests/scripts" => test_goldenscript }

fn test_goldenscript(path: &Path) {
    // We can't run tests concurrently, because the test clusters end up using
    // the same ports. We also don't want to run a bunch of them concurrently.
    // We can't use the #[serial_test] macro either, since it doesn't work with
    // test_each_path. Just use a mutex to serialize them.
    static MUTEX: LazyLock<Mutex<()>> = LazyLock::new(Mutex::default);
    let _guard = MUTEX.lock().ok(); // ignore poisoning

    goldenscript::run(&mut Runner::new(), path).expect("goldenscript failed")
}

/// Runs Raft goldenscript tests. See run() for available commands.
struct Runner {
    cluster: Option<TestCluster>,
    clients: HashMap<String, Client>,
    /// The PostgreSQL connections, by prefix.
    pg_clients: HashMap<String, PgClient<TcpStream>>,
    /// The directory of the backup files, named by their backup names.
    backups: tempfile::TempDir,
}

/// Commands accepted by the end-to-end Goldenscript runner.
#[derive(goldenscript::Command)]
enum Command {
    /// Creates a backup through the client selected by the command prefix.
    Backup {
        /// The backup name.
        name: String,
        /// The version to create the backup as of.
        #[arg(key)]
        as_of: Option<u64>,
    },
    /// Compares two backups with toybackup diff.
    #[command(name = "backup-diff")]
    BackupDiff { a: String, b: String },
    /// Prints a backup's bytes in hex.
    #[command(name = "backup-hex")]
    BackupHex { name: String },
    /// Lists a backup with toybackup list.
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
    /// Verifies a backup with toybackup verify.
    #[command(name = "backup-verify")]
    BackupVerify { name: String },
    /// Closes the client selected by the command prefix.
    Close,
    /// Restores a backup through the client selected by the command prefix.
    Restore { name: String },
    /// Logs in the client selected by the command prefix, connecting it
    /// first if needed.
    Login {
        /// The user name.
        user: String,
        /// The password, if any.
        #[arg(key)]
        password: Option<String>,
    },
    /// Drives the PostgreSQL connection selected by the command prefix, which
    /// startup connects to a random node.
    Pg {
        /// The subcommand and its positional arguments.
        args: Vec<String>,
        /// The subcommand's key=value arguments.
        #[arg(optional)]
        options: Vec<(String, String)>,
    },
    /// Starts a test cluster.
    Cluster {
        /// The number of nodes to start.
        #[arg(key)]
        nodes: u8,
    },
    /// Fetches status from the client selected by the command prefix.
    Status,
    /// Displays a table schema.
    Table {
        /// The table name.
        name: String,
        /// Whether to use the raw debug representation.
        #[arg(key, optional)]
        raw: bool,
    },
    /// Lists all table schemas.
    Tables,
    /// Executes any other command as a SQL statement.
    #[command(other)]
    Statement(goldenscript::Command),
}

impl Runner {
    fn new() -> Self {
        Self {
            cluster: None,
            clients: HashMap::new(),
            pg_clients: HashMap::new(),
            backups: tempfile::TempDir::with_prefix("toydb-backups").expect("tempdir failed"),
        }
    }

    /// Returns the path of a backup file.
    fn backup_path(&self, name: &str) -> PathBuf {
        self.backups.path().join(name)
    }

    /// Reads a backup file.
    fn read_backup(&self, name: &str) -> Result<Vec<u8>, Box<dyn Error>> {
        std::fs::read(self.backup_path(name)).map_err(|_| format!("unknown backup {name}").into())
    }

    /// Runs toybackup with the given subcommand on backup files, and returns
    /// its output.
    fn toybackup(&self, subcommand: &str, names: &[&str]) -> Result<String, Box<dyn Error>> {
        for name in names {
            self.read_backup(name)?;
        }
        let build = escargot::CargoBuild::new().bin("toybackup").run()?;
        let output = build
            .command()
            .arg(subcommand)
            .args(names.iter().map(|name| self.backup_path(name)))
            .output()?;
        Ok(String::from_utf8(output.stdout)?)
    }

    /// Fetches a client for the given prefix, or creates a new one.
    fn get_client(&mut self, prefix: &Option<String>) -> Result<&mut Client, Box<dyn Error>> {
        let name = Self::client_name(prefix);
        if !self.clients.contains_key(name) {
            let Some(cluster) = self.cluster.as_mut() else {
                return Err("no cluster".into());
            };
            let client = cluster.connect()?;
            self.clients.insert(name.to_string(), client);
        }
        Ok(self.clients.get_mut(name).expect("no client"))
    }

    /// Runs a pg command on the prefix's PostgreSQL connection.
    fn pg(
        &mut self,
        prefix: &Option<String>,
        args: &[String],
        options: &[(String, String)],
    ) -> Result<String, Box<dyn Error>> {
        let name = Self::client_name(prefix);
        if args.first().map(String::as_str) == Some("startup") {
            if self.pg_clients.contains_key(name) {
                return Err("connection already open".into());
            }
            let Some(cluster) = self.cluster.as_ref() else {
                return Err("no cluster".into());
            };
            self.pg_clients.insert(name.to_string(), cluster.connect_pg()?);
        }
        let Some(client) = self.pg_clients.get_mut(name) else {
            return Err("no open PostgreSQL connection".into());
        };
        let output = pg_command(client, args, options);
        if client.is_closed() || output.is_err() {
            self.pg_clients.remove(name);
        }
        output
    }

    /// Returns a client name for a prefix.
    fn client_name(prefix: &Option<String>) -> &str {
        prefix.as_deref().unwrap_or_default()
    }
}

/// Runs a pg command on a PostgreSQL connection, and returns its output.
fn pg_command(
    client: &mut PgClient<TcpStream>,
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

impl goldenscript::Runner for Runner {
    type Command = Command;

    /// Runs a goldenscript command.
    fn run(
        &mut self,
        command: &Command,
        context: &goldenscript::Context,
    ) -> Result<String, Box<dyn Error>> {
        let mut output = String::new();

        // Handle simple, non-SQL commands.
        let command = match command {
            Command::Backup { name, as_of } => {
                let data = self.get_client(&context.prefix)?.backup(*as_of)?;
                std::fs::write(self.backup_path(name), data)?;
                return Ok(output);
            }

            Command::Restore { name } => {
                let data = self.read_backup(name)?;
                self.get_client(&context.prefix)?.restore(&data)?;
                return Ok(output);
            }

            Command::BackupList { name } => return self.toybackup("list", &[name]),

            Command::BackupVerify { name } => return self.toybackup("verify", &[name]),

            Command::BackupDiff { a, b } => return self.toybackup("diff", &[a, b]),

            Command::BackupHex { name } => {
                for chunk in self.read_backup(name)?.chunks(16) {
                    writeln!(output, "{}", chunk.iter().map(|b| format!("{b:02x}")).join(" "))?;
                }
                return Ok(output);
            }

            Command::BackupLoad { name, hex } => {
                std::fs::write(self.backup_path(name), hex::decode(hex.concat())?)?;
                return Ok(output);
            }

            Command::BackupPatch { name, offset, hex } => {
                let bytes = hex::decode(hex)?;
                let mut data = self.read_backup(name)?;
                let Some(target) = data.get_mut(*offset..*offset + bytes.len()) else {
                    return Err("patch beyond end of backup".into());
                };
                target.copy_from_slice(&bytes);
                std::fs::write(self.backup_path(name), data)?;
                return Ok(output);
            }

            Command::BackupTruncate { name, length } => {
                let mut data = self.read_backup(name)?;
                data.truncate(*length);
                std::fs::write(self.backup_path(name), data)?;
                return Ok(output);
            }

            Command::Close => {
                let name = Self::client_name(&context.prefix);
                if self.clients.remove(name).is_none() {
                    return Err("no client to close".into());
                }
                return Ok(output);
            }

            &Command::Cluster { nodes } => {
                if self.cluster.is_some() {
                    return Err("cluster already exists".into());
                }
                self.cluster = Some(TestCluster::run(nodes)?);
                return Ok(output);
            }

            Command::Login { user, password } => {
                self.get_client(&context.prefix)?.login(user, password.as_deref())?;
                return Ok(output);
            }

            Command::Pg { args, options } => return self.pg(&context.prefix, args, options),

            Command::Status => {
                let status = self.get_client(&context.prefix)?.status()?;
                write!(output, "{status:#?}")?;
                return Ok(output);
            }

            &Command::Table { ref name, raw } => {
                let table = self.get_client(&context.prefix)?.get_table(name)?;
                if raw {
                    write!(output, "{table:#?}")?;
                } else {
                    write!(output, "{table}")?;
                }
                return Ok(output);
            }

            Command::Tables => {
                let tables = self.get_client(&context.prefix)?.list_tables()?;
                for table in tables {
                    writeln!(output, "{table}")?;
                }
                return Ok(output);
            }

            Command::Statement(command) => command,
        };

        // Otherwise, interpret the entire command as a SQL statement.
        if !command.args.is_empty() {
            return Err("statements should be given as a command with no args".into());
        }
        let client = self.get_client(&context.prefix)?;
        let input = &command.name;
        let mut tags = context.tags.clone();

        // Execute the command and display the result if requested.
        // SELECT, EXPLAIN, EXPORT and FORMAT results are always output. With
        // json, the JSON result is output instead.
        let result = client.execute(input)?;

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
                StatementResult::Explain(root) => writeln!(output, "{root}")?,
                StatementResult::Export { lines, .. } | StatementResult::Format { lines } => {
                    for line in lines {
                        writeln!(output, "{line}")?;
                    }
                }
                result if tags.remove("result") => writeln!(output, "{result:?}")?,
                _ => {}
            }
        }

        if let Some(tag) = tags.iter().next() {
            return Err(format!("invalid tag {tag}").into());
        }

        Ok(output)
    }
}
