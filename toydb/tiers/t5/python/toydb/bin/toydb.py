"""The toyDB server. Takes configuration from a config file (default
config/toydb.yaml) or corresponding TOYDB_ environment variables. Listens
for SQL clients (default port 9601), PostgreSQL clients (default port 9605)
and Raft connections from other toyDB peers (default port 9701). The Raft log
and SQL database are stored at data/raft and data/sql by default.

Use the toysql command-line client to connect to the server.

Mirrors `src/bin/toydb.rs`.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from toydb import config, raft, simplelog, sql, storage
from toydb.error import Error, errinput
from toydb.server import Server

VERSION = "1.0.0"
"""The toyDB version (the Cargo package version)."""


def main() -> None:
    try:
        Command.parse().run()
    except Error as error:
        print(f"Error: {error}", file=sys.stderr)


@dataclass
class Config:
    """The toyDB server configuration. Can be provided via config file
    (default config/toydb.yaml) or TOYDB_ environment variables."""

    id: raft.NodeID
    """The node ID. Must be unique in the cluster."""
    peers: dict[raft.NodeID, str]
    """The other nodes in the cluster, and their Raft TCP addresses."""
    listen_raft: str
    """The Raft listen address."""
    listen_sql: str
    """The SQL listen address."""
    listen_pg: str
    """Address to listen for PostgreSQL connections on."""
    log_level: str
    """The log level."""
    data_dir: str
    """The path to this node's data directory. The Raft log is stored in
    the file "raft", and the SQL state machine in "sql"."""
    storage_raft: str
    """The Raft storage engine: bitcask or memory."""
    storage_sql: str
    """The SQL storage engine: bitcask or memory."""
    fsync: bool
    """If false, don't fsync Raft log writes to disk. Disabling this
    will yield much better write performance, but may lose data on
    host crashes which compromises Raft safety guarantees."""
    compact_threshold: float
    """The garbage fraction threshold at which to trigger compaction."""
    compact_min_bytes: int
    """The minimum bytes of garbage before triggering compaction."""

    @classmethod
    def load(cls, file: str) -> "Config":
        """Loads the configuration from the given file."""
        try:
            return (
                config.Config.builder()
                .set_default("id", "1")
                .set_default("listen_sql", "localhost:9601")
                .set_default("listen_pg", "localhost:9605")
                .set_default("listen_raft", "localhost:9701")
                .set_default("log_level", "info")
                .set_default("data_dir", "data")
                .set_default("storage_raft", "bitcask")
                .set_default("storage_sql", "bitcask")
                .set_default("fsync", True)
                .set_default("compact_threshold", 0.2)
                .set_default("compact_min_bytes", 1_000_000)
                .add_source(config.File.with_name(file))
                .add_source(config.Environment.with_prefix("TOYDB"))
                .build()
                .try_deserialize(cls)
            )
        except config.ConfigError as error:
            raise errinput(str(error)) from error


@dataclass
class Command:
    """The toyDB server command."""

    config: str
    """The configuration file path."""

    @classmethod
    def parse(cls, args: list[str] | None = None) -> "Command":
        """Parses command-line arguments."""
        parser = argparse.ArgumentParser(prog="toydb", description="Starts a toyDB server.")
        parser.add_argument(
            "-c", "--config", default="config/toydb.yaml", help="The configuration file path."
        )
        parser.add_argument("-V", "--version", action="version", version=f"toydb {VERSION}")
        return cls(config=parser.parse_args(args).config)

    def run(self) -> None:
        """Runs the toyDB server."""
        # Load the configuration.
        cfg = Config.load(self.config)

        # Initialize logging.
        try:
            loglevel = simplelog.LevelFilter.from_str(cfg.log_level)
        except ValueError as error:
            raise errinput(str(error)) from error
        filter_allow = ["toydb"] if loglevel != simplelog.LevelFilter.Debug else []
        simplelog.init(loglevel, filter_allow)

        # Initialize the Raft log storage engine.
        datadir = Path(cfg.data_dir)
        match cfg.storage_raft:
            case "bitcask" | "":
                engine: storage.Engine = storage.BitCask.new_maybe_compact(
                    datadir / "raft", cfg.compact_threshold, cfg.compact_min_bytes
                )
                raft_log = raft.Log(engine)
            case "memory":
                raft_log = raft.Log(storage.Memory())
            case name:
                raise errinput(f"invalid Raft storage engine {name}")
        raft_log.enable_fsync(cfg.fsync)

        # Initialize the SQL storage engine.
        raft_state: raft.State
        match cfg.storage_sql:
            case "bitcask" | "":
                engine = storage.BitCask.new_maybe_compact(
                    datadir / "sql", cfg.compact_threshold, cfg.compact_min_bytes
                )
                raft_state = sql.engine.Raft.new_state(engine)
            case "memory":
                raft_state = sql.engine.Raft.new_state(storage.Memory())
            case name:
                raise errinput(f"invalid SQL storage engine {name}")

        # Start the server.
        server = Server(cfg.id, cfg.peers, raft_log, raft_state)
        server.serve(cfg.listen_raft, cfg.listen_sql, cfg.listen_pg)


if __name__ == "__main__":
    main()
