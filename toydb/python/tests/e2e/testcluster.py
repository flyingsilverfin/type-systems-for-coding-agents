"""Runs toyDB test clusters. Mirrors `tests/testcluster.rs`."""

import random
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Self

from toydb import Client
from toydb.error import Error, panic
from toydb.raft import NodeID

TIMEOUT: timedelta = timedelta(seconds=5)
"""Timeout for node readiness."""

SQL_BASE_PORT: int = 19600
"""The base SQL port (+id)."""

RAFT_BASE_PORT: int = 19700
"""The base Raft port (+id)."""

TOYDB_BIN: Path = Path(__file__).resolve().parents[2] / "bin" / "toydb"
"""The toyDB server executable (the Python counterpart of the built binary)."""

type NodePorts = dict[NodeID, tuple[int, int]]
"""Raft and SQL ports on localhost, by node ID."""


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

    def __init__(self, servers: dict[NodeID, "TestServer"], dir: tempfile.TemporaryDirectory[str]) -> None:
        self.servers = servers
        self.dir = dir
        """The temporary directory, deleted when closed."""

    @classmethod
    def run(cls, nodes: int) -> "TestCluster":
        """Runs and returns a test cluster. It keeps running until closed."""
        # Create temporary directory.
        dir = tempfile.TemporaryDirectory(prefix="toydb")

        # Allocate port numbers for nodes.
        ports: NodePorts = {
            id: (RAFT_BASE_PORT + id, SQL_BASE_PORT + id) for id in range(1, nodes + 1)
        }

        cluster = cls({}, dir)
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

    def close(self) -> None:
        """Kills the servers and removes the temporary directory."""
        for server in self.servers.values():
            server.close()
        self.servers.clear()
        self.dir.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class TestServer:
    """A toyDB server."""

    __test__ = False  # not a unittest/pytest test class

    def __init__(self, id: NodeID, child: subprocess.Popen[bytes], sql_port: int) -> None:
        self.id = id
        self.child = child
        self.sql_port = sql_port

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

        _, sql_port = ports[id]
        return cls(id, child, sql_port)

    @staticmethod
    def build_config(id: NodeID, dir: Path, ports: NodePorts) -> str:
        """Generates a config file for the given node."""
        raft_port, sql_port = ports[id]
        cfg = f"id: {id}\n"
        cfg += f"data_dir: {dir}\n"
        cfg += f"listen_raft: localhost:{raft_port}\n"
        cfg += f"listen_sql: localhost:{sql_port}\n"
        cfg += "peers: {"
        if len(ports) > 1:
            cfg += "\n"
        for peer_id, (peer_raft_port, _) in ports.items():
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

    def close(self) -> None:
        """Kills the child process (the Rust `Drop`)."""
        self.child.kill()
        self.child.wait()
