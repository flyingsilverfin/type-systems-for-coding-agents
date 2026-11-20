"""toyDB: a distributed SQL database, ported from Rust to Python.

Mirrors `src/lib.rs`: the crate's modules are `client`, `encoding`, `error`,
`raft`, `server`, `sql` and `storage`, and `Client`, `Server` and
`StatementResult` are re-exported here.
"""

from toydb import client, encoding, error, raft, server, sql, storage
from toydb.client import Client
from toydb.server import Server
from toydb.sql.execution import StatementResult

__all__ = [
    "Client",
    "Server",
    "StatementResult",
    "client",
    "encoding",
    "error",
    "raft",
    "server",
    "sql",
    "storage",
]
