"""Runs toyDB test clusters. Mirrors `tests/testcluster.rs`."""

import fcntl
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Self

from toydb import Client
from toydb.pgwire.client import Client as PgClient
from toydb.error import Error, panic
from toydb.raft import NodeID

TIMEOUT: timedelta = timedelta(seconds=30)
"""Timeout for node readiness. Generous, so that clusters can still elect a
leader on a heavily loaded machine (e.g. with concurrent test runs)."""

SQL_BASE_PORT: int = 19600
"""The base SQL port (+id)."""

RAFT_BASE_PORT: int = 19700
"""The base Raft port (+id)."""

PG_BASE_PORT: int = 29600
"""The base PostgreSQL port (+id)."""

PORT_SLOTS: int = 32
"""The number of port slots. Each running test cluster holds a slot, so that
concurrent test runs (e.g. in different copies of the project) don't use the
same ports. Slot n uses the base ports + n * PORT_SLOT_STRIDE (so at most 99
nodes per cluster), slot 0 being the base ports themselves."""

PORT_SLOT_STRIDE: int = 200
"""The port offset between consecutive port slots."""

PORT_SLOT_LOCK_DIR: Path = Path("/tmp")
"""Where the port slot lock files are kept. This is a fixed directory (falling
back to the temporary directory) so that all test runs on the machine see the
same locks, also the Rust tests, which use the same lock files."""

TOYDB_BIN: Path = Path(__file__).resolve().parents[2] / "bin" / "toydb"
"""The toyDB server executable (the Python counterpart of the built binary)."""

type NodePorts = dict[NodeID, tuple[int, int, int]]
"""Raft, SQL and PostgreSQL ports on localhost, by node ID."""


class TestCluster:
    """Runs a toyDB cluster using the server executable in a temporary
    directory. The cluster is killed and removed when closed (the Rust `Drop`),
    e.g. when used as a context manager.

    This runs the cluster as child processes using the server executable
    instead of spawning in-memory threads for a couple of reasons: it avoids
    having to gracefully shut down the server (which is complicated by e.g.
    `socket.accept()` not being interruptable), and it tests the entire server
    (and eventually the toySQL client) end-to-end.
    """

    __test__ = False  # not a unittest/pytest test class

    def __init__(
        self,
        servers: dict[NodeID, "TestServer"],
        dir: tempfile.TemporaryDirectory[str],
        slot: "PortSlot",
    ) -> None:
        self.servers = servers
        self.dir = dir
        """The temporary directory, deleted when closed."""
        self.slot = slot
        """The port slot, released when closed."""

    @classmethod
    def run(cls, nodes: int) -> "TestCluster":
        """Runs and returns a test cluster. It keeps running until closed."""
        # Create temporary directory.
        dir = tempfile.TemporaryDirectory(prefix="toydb")

        # Allocate port numbers for nodes, from a port slot not used by any
        # other test cluster.
        try:
            slot = PortSlot.acquire(nodes)
        except BaseException:
            dir.cleanup()
            raise
        ports: NodePorts = {
            id: (slot.raft_base_port + id, slot.sql_base_port + id, slot.pg_base_port + id)
            for id in range(1, nodes + 1)
        }

        cluster = cls({}, dir, slot)
        try:
            # Start nodes.
            for id in range(1, nodes + 1):
                node_dir = Path(dir.name) / f"toydb{id}"
                cluster.servers[id] = TestServer.run(id, node_dir, ports)

            # Wait for the nodes to be ready, by fetching the server status.
            started = time.monotonic()
            for server in cluster.servers.values():
                while True:
                    try:
                        with server.connect() as client:
                            client.status()
                        break
                    except Error:
                        server.assert_alive()
                        if time.monotonic() - started >= TIMEOUT.total_seconds():
                            raise
                        time.sleep(0.2)
        except BaseException:
            cluster.close()
            raise

        return cluster

    def connect(self) -> Client:
        """Connects to a random cluster node using a Python client. Testing
        with toysql is too annoying, since we have to deal with readline,
        PTYs, echoing, multiline editing, etc."""
        id = random.randint(1, len(self.servers))
        return self.servers[id].connect()

    def connect_pg(self) -> PgClient:
        """Connects to the PostgreSQL port of a random cluster node."""
        id = random.randint(1, len(self.servers))
        return self.servers[id].connect_pg()

    def close(self) -> None:
        """Kills the servers, removes the temporary directory, and releases
        the port slot."""
        for server in self.servers.values():
            server.close()
        self.servers.clear()
        self.dir.cleanup()
        self.slot.release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class PortSlot:
    """A port slot: a range of ports held exclusively by one test cluster, via
    an advisory lock (`flock`) on a lock file. The lock is released when the
    slot is released or the process exits."""

    def __init__(self, index: int, lock: int) -> None:
        self.index = index
        self.lock: int | None = lock
        """The locked lock file descriptor."""
        self.sql_base_port = SQL_BASE_PORT + index * PORT_SLOT_STRIDE
        self.raft_base_port = RAFT_BASE_PORT + index * PORT_SLOT_STRIDE
        self.pg_base_port = PG_BASE_PORT + index * PORT_SLOT_STRIDE

    @classmethod
    def acquire(cls, nodes: int) -> "PortSlot":
        """Acquires the first free port slot whose ports for the given number
        of nodes are not in use, waiting for one if necessary."""
        started = time.monotonic()
        while True:
            for index in range(PORT_SLOTS):
                if (lock := cls.try_lock(index)) is None:
                    continue
                slot = cls(index, lock)
                if slot.available(nodes):
                    return slot
                slot.release()  # e.g. a leftover server from a killed test run
            if time.monotonic() - started >= 60:
                raise Error.IO(f"no free test cluster port slot (of {PORT_SLOTS})")
            time.sleep(0.2)

    @staticmethod
    def try_lock(index: int) -> int | None:
        """Locks the lock file of a slot, or returns None if it is locked."""
        name = f"toydb-testcluster-port-slot-{index}.lock"
        # A lock file created by another user can only be opened read-only,
        # which is enough for locking.
        try:
            fd = os.open(PORT_SLOT_LOCK_DIR / name, os.O_RDWR | os.O_CREAT, 0o666)
        except OSError:
            try:
                fd = os.open(PORT_SLOT_LOCK_DIR / name, os.O_RDONLY)
            except OSError:
                fd = os.open(Path(tempfile.gettempdir()) / name, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        return fd

    def available(self, nodes: int) -> bool:
        """Checks that no one is listening on the slot's ports, for every
        address localhost resolves to."""
        for id in range(1, nodes + 1):
            for port in (self.raft_base_port + id, self.sql_base_port + id, self.pg_base_port + id):
                try:
                    addrs = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
                except OSError:
                    return False
                for family, kind, proto, _, sockaddr in addrs:
                    with socket.socket(family, kind, proto) as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        try:
                            sock.bind(sockaddr)
                        except OSError:
                            return False
        return True

    def release(self) -> None:
        """Releases the slot."""
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


class TestServer:
    """A toyDB server."""

    __test__ = False  # not a unittest/pytest test class

    def __init__(
        self, id: NodeID, child: subprocess.Popen[bytes], sql_port: int, pg_port: int
    ) -> None:
        self.id = id
        self.child = child
        self.sql_port = sql_port
        self.pg_port = pg_port

    @classmethod
    def run(cls, id: NodeID, dir: Path, ports: NodePorts) -> "TestServer":
        """Runs a toyDB server."""
        # Build and write the configuration file.
        configfile = dir / "toydb.yaml"
        dir.mkdir(parents=True, exist_ok=True)
        configfile.write_text(cls.build_config(id, dir, ports))

        # Spawn process. Discard output.
        child = subprocess.Popen(
            [sys.executable, str(TOYDB_BIN), "-c", str(configfile)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        _, sql_port, pg_port = ports[id]
        return cls(id, child, sql_port, pg_port)

    @staticmethod
    def build_config(id: NodeID, dir: Path, ports: NodePorts) -> str:
        """Generates a config file for the given node."""
        raft_port, sql_port, pg_port = ports[id]
        cfg = f"id: {id}\n"
        cfg += f"data_dir: {dir}\n"
        cfg += f"listen_raft: localhost:{raft_port}\n"
        cfg += f"listen_sql: localhost:{sql_port}\n"
        cfg += f"listen_pg: localhost:{pg_port}\n"
        cfg += "peers: {"
        if len(ports) > 1:
            cfg += "\n"
        for peer_id, (peer_raft_port, _, _) in ports.items():
            if peer_id != id:
                cfg += f"  '{peer_id}': localhost:{peer_raft_port},"
        cfg += "}\n"
        return cfg

    def assert_alive(self) -> None:
        """Asserts that the server is still running."""
        if (status := self.child.poll()) is not None:
            panic(f"node {self.id} exited with exit status: {status}")

    def connect(self) -> Client:
        """Connects to the server using a regular client."""
        return Client.connect(("localhost", self.sql_port))

    def connect_pg(self) -> PgClient:
        """Connects to the server's PostgreSQL port."""
        return PgClient.connect(("localhost", self.pg_port))

    def close(self) -> None:
        """Kills the child process (the Rust `Drop`)."""
        self.child.kill()
        self.child.wait()
