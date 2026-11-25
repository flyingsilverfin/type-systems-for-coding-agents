//! toybackup: creates, restores, lists, verifies and compares backups.
//! `create` (optionally `--as-of` a version) and `restore` connect to a server
//! (with `--user` and `--password` for a superuser); `list`, `verify` and
//! `diff` read files.

#![warn(clippy::all)]

use std::path::PathBuf;

use clap::Parser as _;

use toydb::Client;
use toydb::backup::format;
use toydb::error::Result;

fn main() {
    match Command::parse().run() {
        Ok(true) => {}
        Ok(false) => std::process::exit(1),
        Err(error) => {
            eprintln!("Error: {error}");
            std::process::exit(1);
        }
    }
}

/// The toybackup command.
#[derive(clap::Parser)]
#[command(about = "Manages toyDB backups.", version, propagate_version = true)]
struct Command {
    #[command(subcommand)]
    subcommand: Subcommand,
}

/// A toybackup subcommand.
#[derive(clap::Subcommand)]
enum Subcommand {
    /// Writes a backup of the server's database to a file.
    Create {
        #[command(flatten)]
        server: Server,
        /// Create the backup as of this version.
        #[arg(long)]
        as_of: Option<u64>,
        /// The backup file.
        file: PathBuf,
    },
    /// Restores a backup file into the server's database.
    Restore {
        #[command(flatten)]
        server: Server,
        /// The backup file.
        file: PathBuf,
    },
    /// Lists the records of a backup file.
    List {
        /// The backup file.
        file: PathBuf,
    },
    /// Verifies a backup file.
    Verify {
        /// The backup file.
        file: PathBuf,
    },
    /// Compares two backup files.
    Diff {
        /// The first backup file.
        a: PathBuf,
        /// The second backup file.
        b: PathBuf,
    },
}

/// The server to connect to.
#[derive(clap::Args)]
struct Server {
    /// Host to connect to.
    #[arg(short = 'H', long, default_value = "localhost")]
    host: String,
    /// Port number to connect to.
    #[arg(short = 'p', long, default_value = "9601")]
    port: u16,
    /// User to log in as.
    #[arg(long)]
    user: Option<String>,
    /// Password to log in with.
    #[arg(long, requires = "user")]
    password: Option<String>,
}

impl Server {
    /// Connects to the server, logging in if a user is given.
    fn connect(&self) -> Result<Client> {
        let mut client = Client::connect((self.host.as_str(), self.port))?;
        if let Some(user) = &self.user {
            client.login(user, self.password.as_deref())?;
        }
        Ok(client)
    }
}

impl Command {
    /// Runs the command. Returns false if a backup file is invalid, after
    /// printing its error.
    fn run(self) -> Result<bool> {
        match self.subcommand {
            Subcommand::Create { server, as_of, file } => {
                let data = server.connect()?.backup(as_of)?;
                std::fs::write(file, data)?;
            }
            Subcommand::Restore { server, file } => {
                let data = std::fs::read(file)?;
                server.connect()?.restore(&data)?;
            }
            Subcommand::List { file } => {
                let lines = format::list(&std::fs::read(file)?);
                for line in &lines {
                    println!("{line}");
                }
                return Ok(!lines.last().is_some_and(|line| line.starts_with("Error: ")));
            }
            Subcommand::Verify { file } => match format::read(&std::fs::read(file)?) {
                Ok(records) => println!("ok: {} records", records.len()),
                Err(error) => {
                    println!("Error: {error}");
                    return Ok(false);
                }
            },
            Subcommand::Diff { a, b } => {
                match format::diff(&std::fs::read(a)?, &std::fs::read(b)?) {
                    Ok(lines) => lines.iter().for_each(|line| println!("{line}")),
                    Err(error) => {
                        println!("Error: {error}");
                        return Ok(false);
                    }
                }
            }
        }
        Ok(true)
    }
}
