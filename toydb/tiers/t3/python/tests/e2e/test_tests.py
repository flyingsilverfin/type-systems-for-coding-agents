"""A basic set of end-to-end tests as Goldenscripts under tests/e2e/scripts/. These
spin up actual clusters using the server executable and run operations against
them from multiple clients.

There are more comprehensive tests elsewhere in the codebase, see the various
tests/*/testscripts scripts.

Mirrors `tests/tests.rs`. Run the scripts with
`python3 run_goldenscripts.py tests/e2e/scripts`.
"""

import io
import threading
import unittest
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from tests import goldenscript
from tests.e2e.testcluster import TestCluster
from tests.goldenscript import CommandParser, Context, arg
from toydb import Client, StatementResult
from toydb.fmt import debug
from toydb.sql.execution import json

SCRIPTS = Path(__file__).resolve().parent / "scripts"
"""The end-to-end goldenscripts. Subdirectories (e.g. `task/`) hold more
scripts."""

MUTEX = threading.Lock()
"""Serializes the tests: the test clusters use the same ports, and we don't
want to run a bunch of them concurrently anyway."""


def test_goldenscript(path: Path) -> None:
    """Runs a goldenscript in tests/e2e/scripts."""
    with MUTEX:
        runner = Runner()
        try:
            goldenscript.run(runner, path)
        finally:
            runner.close()


class Command(ABC):
    """Commands accepted by the end-to-end Goldenscript runner."""


@dataclass
class Close(Command):
    """Closes the client selected by the command prefix."""


@dataclass
class Cluster(Command):
    """Starts a test cluster."""

    nodes: int = arg(key=True)
    """The number of nodes to start."""


@dataclass
class Status(Command):
    """Fetches status from the client selected by the command prefix."""


@dataclass
class Table(Command):
    """Displays a table schema."""

    name: str
    """The table name."""
    raw: bool = arg(key=True, optional=True)
    """Whether to use the raw debug representation."""


@dataclass
class Tables(Command):
    """Lists all table schemas."""


@goldenscript.command(other=True)
@dataclass
class Statement(Command):
    """Executes any other command as a SQL statement."""

    _0: goldenscript.Command


class Runner(goldenscript.Runner[Command]):
    """Runs end-to-end goldenscript tests. See run() for available commands."""

    PARSER: ClassVar[CommandParser[Command]] = CommandParser.derive(Command)

    def __init__(self) -> None:
        self.cluster: TestCluster | None = None
        self.clients: dict[str, Client] = {}

    def close(self) -> None:
        """Closes the clients and the cluster (the Rust `Drop`)."""
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        if self.cluster is not None:
            self.cluster.close()
            self.cluster = None

    def get_client(self, prefix: str | None) -> Client:
        """Fetches a client for the given prefix, or creates a new one."""
        name = self.client_name(prefix)
        if name not in self.clients:
            if self.cluster is None:
                raise ValueError("no cluster")
            self.clients[name] = self.cluster.connect()
        return self.clients[name]

    @staticmethod
    def client_name(prefix: str | None) -> str:
        """Returns a client name for a prefix."""
        return prefix or ""

    def parse_command(self, command: goldenscript.Command) -> Command:
        return self.PARSER.try_from(command)

    def run(self, command: Command, context: Context) -> str:
        """Runs a goldenscript command."""
        output = io.StringIO()

        # Handle simple, non-SQL commands.
        match command:
            case Close():
                name = self.client_name(context.prefix)
                if (client := self.clients.pop(name, None)) is None:
                    raise ValueError("no client to close")
                client.close()
                return output.getvalue()

            case Cluster(nodes):
                if self.cluster is not None:
                    raise ValueError("cluster already exists")
                self.cluster = TestCluster.run(nodes)
                return output.getvalue()

            case Status():
                status = self.get_client(context.prefix).status()
                output.write(debug(status, pretty=True))
                return output.getvalue()

            case Table(name, raw):
                table = self.get_client(context.prefix).get_table(name)
                output.write(debug(table, pretty=True) if raw else str(table))
                return output.getvalue()

            case Tables():
                for table_name in self.get_client(context.prefix).list_tables():
                    output.write(f"{table_name}\n")
                return output.getvalue()

            case Statement(statement):
                pass

            case _:
                raise TypeError(f"unknown command {command!r}")

        # Otherwise, interpret the entire command as a SQL statement.
        if statement.args:
            raise ValueError("statements should be given as a command with no args")
        client = self.get_client(context.prefix)
        input = statement.name
        tags = set(context.tags)

        # Execute the command and display the result if requested.
        # SELECT and EXPLAIN results and EXPORT lines are always output. With
        # [json], output the result's JSON form instead.
        result = client.execute(input)

        if "json" in tags:
            tags.remove("json")
            tags.discard("header")
            tags.discard("result")
            output.write(f"{json.encode_result(result)}\n")
        else:
            match result:
                case StatementResult.Select(columns, rows):
                    if "header" in tags:
                        tags.remove("header")
                        output.write(", ".join(str(column) for column in columns) + "\n")
                    for row in rows:
                        output.write(", ".join(str(value) for value in row) + "\n")
                case StatementResult.Explain(root):
                    output.write(f"{root}\n")
                case StatementResult.Export(lines=lines):
                    for line in lines:
                        output.write(f"{line}\n")
                case _ if "result" in tags:
                    tags.remove("result")
                    output.write(f"{debug(result)}\n")
                case _:
                    pass

        if tags:
            raise ValueError(f"invalid tag {sorted(tags)[0]}")

        return output.getvalue()


def scripts(directory: Path) -> list[Path]:
    """Returns the goldenscripts in a directory and its subdirectories, sorted
    by path."""
    found: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.is_dir():
            found.extend(scripts(path))
        elif path.is_file() and not path.name.startswith("."):
            found.append(path)
    return found


class Goldenscripts(unittest.TestCase):
    """Runs the end-to-end goldenscripts in tests/e2e/scripts (including
    subdirectories such as task/) as unit tests."""

    def test_goldenscripts(self) -> None:
        for path in scripts(SCRIPTS):
            with self.subTest(path=path):
                test_goldenscript(path)


if __name__ == "__main__":
    unittest.main()
