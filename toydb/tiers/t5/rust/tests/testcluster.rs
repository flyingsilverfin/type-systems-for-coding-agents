use std::collections::BTreeMap;
use std::error::Error;
use std::fmt::Write as _;
use std::fs::File;
use std::net::{TcpListener, TcpStream, ToSocketAddrs as _};
use std::path::Path;
use std::time::Duration;

use rand::RngExt as _;

use toydb::Client;
use toydb::pgwire::Client as PgClient;
use toydb::raft::NodeID;

/// Timeout for node readiness. Generous, so that clusters can still elect a
/// leader on a heavily loaded machine (e.g. with concurrent test runs).
const TIMEOUT: Duration = Duration::from_secs(30);

/// The base SQL port (+id).
const SQL_BASE_PORT: u16 = 19600;

/// The base Raft port (+id).
const RAFT_BASE_PORT: u16 = 19700;

/// The base PostgreSQL port (+id). It is above the SQL and Raft ports of all
/// port slots.
const PG_BASE_PORT: u16 = 26000;

/// The number of port slots. Each running test cluster holds a slot, so that
/// concurrent test runs (e.g. in different copies of the project) don't use the
/// same ports. Slot n uses the base ports + n * PORT_SLOT_STRIDE (so at most 99
/// nodes per cluster), slot 0 being the base ports themselves.
const PORT_SLOTS: u16 = 32;

/// The port offset between consecutive port slots.
const PORT_SLOT_STRIDE: u16 = 200;

/// Where the port slot lock files are kept. This is a fixed directory (falling
/// back to the temporary directory) so that all test runs on the machine see
/// the same locks, also the Python tests, which use the same lock files.
const PORT_SLOT_LOCK_DIR: &str = "/tmp";

/// Runs a toyDB cluster using the built binary in a temporary directory. The
/// cluster will be killed and removed when dropped.
///
/// This runs the cluster as child processes using the built binary instead of
/// spawning in-memory threads for a couple of reasons: it avoids having to
/// gracefully shut down the server (which is complicated by e.g.
/// TcpListener::accept() not being interruptable), and it tests the entire
/// server (and eventually the toySQL client) end-to-end.
pub struct TestCluster {
    servers: BTreeMap<NodeID, TestServer>,
    #[allow(dead_code)]
    dir: tempfile::TempDir, // deleted when dropped
    #[allow(dead_code)]
    slot: PortSlot, // released when dropped, after the servers are killed
}

type NodePorts = BTreeMap<NodeID, (u16, u16, u16)>; // raft,sql,pg on localhost

impl TestCluster {
    /// Runs and returns a test cluster. It keeps running until dropped.
    pub fn run(nodes: u8) -> Result<Self, Box<dyn Error>> {
        // Create temporary directory.
        let dir = tempfile::TempDir::with_prefix("toydb")?;

        // Allocate port numbers for nodes, from a port slot not used by any
        // other test cluster.
        let slot = PortSlot::acquire(nodes)?;
        let ports: NodePorts = (1..=nodes)
            .map(|id| {
                let id_port = id as u16;
                let raft_port = slot.raft_base_port + id_port;
                (id, (raft_port, slot.sql_base_port + id_port, slot.pg_base_port + id_port))
            })
            .collect();

        // Start nodes.
        let mut servers = BTreeMap::new();
        for id in 1..=nodes {
            let dir = dir.path().join(format!("toydb{id}"));
            servers.insert(id, TestServer::run(id, &dir, &ports)?);
        }

        // Wait for the nodes to be ready, by fetching the server status.
        let started = std::time::Instant::now();
        for server in servers.values_mut() {
            while let Err(error) = server.connect().and_then(|mut c| Ok(c.status()?)) {
                server.assert_alive();
                if started.elapsed() >= TIMEOUT {
                    return Err(error);
                }
                std::thread::sleep(Duration::from_millis(200));
            }
        }

        Ok(Self { servers, dir, slot })
    }

    /// Connects to a random cluster node using a Rust client. Testing with
    /// toysql is too annoying, since we have to deal with rustyline, PTYs,
    /// echoing, multiline editing, etc.
    pub fn connect(&self) -> Result<Client, Box<dyn Error>> {
        let id = rand::rng().random_range(1..=self.servers.len()) as NodeID;
        self.servers.get(&id).unwrap().connect()
    }

    /// Connects to the PostgreSQL port of a random cluster node.
    pub fn connect_pg(&self) -> Result<PgClient<TcpStream>, Box<dyn Error>> {
        let id = rand::rng().random_range(1..=self.servers.len()) as NodeID;
        Ok(PgClient::connect(("localhost", self.servers.get(&id).unwrap().pg_port))?)
    }
}

/// A port slot: a range of ports held exclusively by one test cluster, via an
/// advisory lock (flock) on a lock file. The lock is released when dropped or
/// when the process exits.
struct PortSlot {
    #[allow(dead_code)]
    lock: File,
    sql_base_port: u16,
    raft_base_port: u16,
    pg_base_port: u16,
}

impl PortSlot {
    /// Acquires the first free port slot whose ports for the given number of
    /// nodes are not in use, waiting for one if necessary.
    fn acquire(nodes: u8) -> Result<Self, Box<dyn Error>> {
        let started = std::time::Instant::now();
        loop {
            for index in 0..PORT_SLOTS {
                let Some(lock) = Self::try_lock(index)? else {
                    continue;
                };
                let slot = Self {
                    lock,
                    sql_base_port: SQL_BASE_PORT + index * PORT_SLOT_STRIDE,
                    raft_base_port: RAFT_BASE_PORT + index * PORT_SLOT_STRIDE,
                    pg_base_port: PG_BASE_PORT + index * PORT_SLOT_STRIDE,
                };
                if slot.available(nodes) {
                    return Ok(slot);
                }
                // E.g. a leftover server from a killed test run. Dropping the
                // slot releases it.
            }
            if started.elapsed() >= Duration::from_secs(60) {
                return Err(format!("no free test cluster port slot (of {PORT_SLOTS})").into());
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }

    /// Locks the lock file of a slot, or returns None if it is locked.
    fn try_lock(index: u16) -> Result<Option<File>, Box<dyn Error>> {
        let name = format!("toydb-testcluster-port-slot-{index}.lock");
        let open = |dir: &Path| {
            let mut options = std::fs::OpenOptions::new();
            options.read(true).write(true).create(true).truncate(false).open(dir.join(&name))
        };
        // A lock file created by another user can only be opened read-only,
        // which is enough for locking.
        let shared = Path::new(PORT_SLOT_LOCK_DIR).join(&name);
        let file = match open(Path::new(PORT_SLOT_LOCK_DIR)).or_else(|_| File::open(&shared)) {
            Ok(file) => file,
            Err(_) => open(&std::env::temp_dir())?,
        };
        match file.try_lock() {
            Ok(()) => Ok(Some(file)),
            Err(std::fs::TryLockError::WouldBlock) => Ok(None),
            Err(std::fs::TryLockError::Error(error)) => Err(error.into()),
        }
    }

    /// Checks that no one is listening on the slot's ports, for every address
    /// localhost resolves to.
    fn available(&self, nodes: u8) -> bool {
        (1..=nodes as u16)
            .flat_map(|id| {
                [self.raft_base_port + id, self.sql_base_port + id, self.pg_base_port + id]
            })
            .all(|port| match ("localhost", port).to_socket_addrs() {
                Ok(mut addrs) => addrs.all(|addr| TcpListener::bind(addr).is_ok()),
                Err(_) => false,
            })
    }
}

/// A toyDB server.
pub struct TestServer {
    id: NodeID,
    child: std::process::Child,
    sql_port: u16,
    pg_port: u16,
}

impl TestServer {
    /// Runs a toyDB server.
    fn run(id: NodeID, dir: &Path, ports: &NodePorts) -> Result<Self, Box<dyn Error>> {
        // Build and write the configuration file.
        let configfile = dir.join("toydb.yaml");
        std::fs::create_dir_all(dir)?;
        std::fs::write(&configfile, Self::build_config(id, dir, ports)?)?;

        // Build the binary.
        //
        // TODO: this may contribute to slow tests, consider building once.
        let build = escargot::CargoBuild::new().bin("toydb").run()?;

        // Spawn process. Discard output.
        let child = build
            .command()
            .args(["-c", &configfile.to_string_lossy()])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()?;

        let (_, sql_port, pg_port) = ports.get(&id).copied().expect("node not in ports");
        Ok(Self { id, child, sql_port, pg_port })
    }

    /// Generates a config file for the given node.
    fn build_config(id: NodeID, dir: &Path, ports: &NodePorts) -> Result<String, Box<dyn Error>> {
        let (raft_port, sql_port, pg_port) = ports.get(&id).expect("node not in ports");
        let mut cfg = String::new();
        writeln!(cfg, "id: {id}")?;
        writeln!(cfg, "data_dir: {}", dir.to_string_lossy())?;
        writeln!(cfg, "listen_raft: localhost:{raft_port}")?;
        writeln!(cfg, "listen_sql: localhost:{sql_port}")?;
        writeln!(cfg, "listen_pg: localhost:{pg_port}")?;
        write!(cfg, "peers: {{")?;
        if ports.len() > 1 {
            writeln!(cfg)?;
        }
        for (peer_id, (peer_raft_port, _, _)) in ports.iter().filter(|(peer, _)| **peer != id) {
            write!(cfg, "  '{peer_id}': localhost:{peer_raft_port},")?;
        }
        writeln!(cfg, "}}")?;
        Ok(cfg)
    }

    /// Asserts that the server is still running.
    fn assert_alive(&mut self) {
        if let Some(status) = self.child.try_wait().expect("failed to check exit status") {
            panic!("node {id} exited with {status}", id = self.id)
        }
    }

    /// Connects to the server using a regular client.
    fn connect(&self) -> Result<Client, Box<dyn Error>> {
        Ok(Client::connect(("localhost", self.sql_port))?)
    }
}

impl Drop for TestServer {
    // Kills the child process when dropped.
    fn drop(&mut self) {
        self.child.kill().expect("failed to kill node");
        self.child.wait().expect("failed to wait for node to terminate");
    }
}
