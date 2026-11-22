"""toySQL is a command-line client for toyDB. It connects to a toyDB node
(default localhost:9601) and executes SQL statements against it via an
interactive shell interface. Command history is stored in .toysql.history.

Mirrors `src/bin/toysql.rs`. Uses the `readline` module (where available)
for line editing and history instead of rustyline (see `Editor`).
"""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from toydb.client import Client
from toydb.error import Error, errinput
from toydb.sql.execution import StatementResult, json
from toydb.sql.parser import Lexer, Token

try:
    import readline as _readline

    readline: ModuleType | None = _readline
except ImportError:  # e.g. on Windows
    readline = None

VERSION = "1.0.0"
"""The toyDB version (the Cargo package version)."""


def main() -> None:
    try:
        Command.parse().run()
    except Error as error:
        print(f"Error: {error}", file=sys.stderr)


@dataclass
class Command:
    """The toySQL command."""

    statement: str | None
    """A SQL statement to execute, then exit."""
    host: str
    """Host to connect to."""
    port: int
    """Port number to connect to."""

    @classmethod
    def parse(cls, args: list[str] | None = None) -> "Command":
        """Parses command-line arguments."""
        parser = argparse.ArgumentParser(prog="toysql", description="A toyDB client.")
        parser.add_argument("statement", nargs="?", help="A SQL statement to execute, then exit.")
        parser.add_argument("-H", "--host", default="localhost", help="Host to connect to.")
        parser.add_argument(
            "-p", "--port", type=_port, default=9601, help="Port number to connect to."
        )
        parser.add_argument("-V", "--version", action="version", version=f"toysql {VERSION}")
        parsed = parser.parse_args(args)
        return cls(statement=parsed.statement, host=parsed.host, port=parsed.port)

    def run(self) -> None:
        """Runs the command."""
        shell = Shell(self.host, self.port)
        try:
            if self.statement is not None:
                shell.execute(self.statement)
            else:
                shell.run()
        finally:
            shell.client.close()


def _port(value: str) -> int:
    """Parses a port number (a Rust u16)."""
    port = int(value)
    if not 0 <= port <= 0xFFFF:
        raise ValueError(f"{value} is not in 0..=65535")
    return port


class Shell:
    """An interactive toySQL shell."""

    def __init__(self, host: str, port: int) -> None:
        """Creates a new shell connected to the given server."""
        self.client = Client.connect((host, port))
        """The toyDB client."""
        self.editor = Editor(InputValidator())
        """The command line editor."""
        home = os.environ.get("HOME")
        self.history_path: Path | None = Path(home) / ".toysql.history" if home else None
        """The path to the history file, if any."""
        self.show_headers = False
        """If true, SELECT column headers will be displayed."""
        self.output_json = False
        """If true, results are displayed in their JSON form (!format json)."""

    def execute(self, input: str) -> None:
        """Executes a SQL statement or ! command."""
        if input.startswith("!"):
            self.execute_command(input)
        elif input:
            self.execute_sql(input)

    def execute_command(self, input: str) -> None:
        """Executes a toySQL ! command (e.g. !help)."""
        words = input.split()
        if not words:
            raise errinput("expected command")
        command, args = words[0], words[1:]

        match (command, args):
            # Toggles column headers.
            case ("!headers", []):
                self.show_headers = not self.show_headers
                print("Headers enabled" if self.show_headers else "Headers disabled")
            case ("!headers", _):
                raise errinput("!headers takes no arguments")

            # Sets the output format.
            case ("!format", ["text"]):
                self.output_json = False
                print("Output format text")
            case ("!format", ["json"]):
                self.output_json = True
                print("Output format json")
            case ("!format", _):
                raise errinput("!format takes 1 argument: text or json")

            # Displays help.
            case ("!help", []):
                print(
                    """
Enter a SQL statement terminated by a semicolon (;) to execute it, or Ctrl-D to
exit. The following commands are also available:

    !format FORMAT     Sets the output format: text (default) or json
    !headers           Toggles column headers
    !help              This help message
    !status            Display server status
    !table NAME        Display a table schema
    !tables            List tables
"""
                )
            case ("!help", _):
                raise errinput("!help takes no arguments")

            # Displays server status.
            case ("!status", []):
                status = self.client.status()
                server = status.server
                leader, term = status.raft.leader, status.raft.term
                nodes = len(status.raft.match_index)
                committed = status.raft.commit_index
                applied = status.raft.applied_index
                raft_size = f"{status.raft.storage.size / 1_000_000:.3f}"
                raft_garbage = f"{status.raft.storage.garbage_disk_percent():.0f}"
                raft_storage = status.raft.storage.name
                raft_match = " ".join(f"n{n}:{m}" for n, m in status.raft.match_index.items())
                sql_keys = status.mvcc.storage.keys
                sql_size = f"{status.mvcc.storage.size / 1_000_000:.3f}"
                sql_disk_size = f"{status.mvcc.storage.disk_size / 1_000_000:.3f}"
                sql_garbage = f"{status.mvcc.storage.garbage_disk_percent():.0f}"
                sql_storage = status.mvcc.storage.name
                active_txns = status.mvcc.active_txns
                versions = status.mvcc.versions
                print(
                    f"""
Server:       n{server} with Raft leader n{leader} in term {term} for {nodes} nodes
Raft log:     {committed} committed, {applied} applied, {raft_size} MB, \
{raft_garbage}% garbage ({raft_storage} engine)
Replication:  {raft_match}
SQL storage:  {sql_keys} keys, {sql_size} MB logical, {nodes}x {sql_disk_size} MB disk, \
{sql_garbage}% garbage ({sql_storage} engine)
Transactions: {active_txns} active, {versions} total
"""
                )
            case ("!status", _):
                raise errinput("!status takes no arguments")

            case ("!table", [name]):
                print(self.client.get_table(name))
            case ("!table", _):
                raise errinput("!table takes 1 argument")

            case ("!tables", []):
                for table in self.client.list_tables():
                    print(table)
            case ("!tables", _):
                raise errinput("!tables takes no arguments")

            case (command, _):
                raise errinput(f"unknown command {command}")

    def execute_sql(self, statement: str) -> None:
        """Executes a SQL statement and displays the results."""
        result = self.client.execute(statement)
        if self.output_json:
            print(json.encode_result(result))
            return
        match result:
            case StatementResult.Begin(state):
                if state.read_only:
                    print(f"Began read-only transaction at version {state.version}")
                else:
                    print(f"Began transaction {state.version}")
            case StatementResult.Commit(version):
                print(f"Committed transaction {version}")
            case StatementResult.Rollback(version):
                print(f"Rolled back transaction {version}")
            case StatementResult.Insert(count):
                print(f"Inserted {count} rows")
            case StatementResult.Delete(count):
                print(f"Deleted {count} rows")
            case StatementResult.Update(count):
                print(f"Updated {count} rows")
            case StatementResult.CreateTable(name):
                print(f"Created table {name}")
            case StatementResult.DropTable(name, existed):
                if existed:
                    print(f"Dropped table {name}")
                else:
                    print(f"Table {name} does not exist")
            case StatementResult.Explain(plan):
                print(plan)
            case StatementResult.Select(columns, rows):
                if self.show_headers:
                    print(", ".join(column.as_header() for column in columns))
                for row in rows:
                    print(", ".join(str(value) for value in row))
            case StatementResult.Analyze(tables):
                if tables:
                    print(f"Analyzed tables {', '.join(tables)}")
                else:
                    print("No tables analyzed")
            case StatementResult.Export(lines=lines) | StatementResult.Format(lines=lines):
                for line in lines:
                    print(line)
            case StatementResult.CreateView(name):
                print(f"Created view {name}")
            case StatementResult.DropView(name, existed):
                if existed:
                    print(f"Dropped view {name}")
                else:
                    print(f"View {name} does not exist")
            case StatementResult.Prepare(name):
                print(f"Prepared statement {name}")
            case StatementResult.Deallocate(name):
                print(f"Deallocated statement {name}")

    def prompt(self) -> str:
        """Prompts the user for input. Raises EOFError if the shell should
        close, or KeyboardInterrupt if the input was interrupted."""
        match self.client.txn():
            case txn if txn is not None and txn.read_only:
                prompt = f"toydb@{txn.version}> "
            case txn if txn is not None:
                prompt = f"toydb:{txn.version}> "
            case _:
                prompt = "toydb> "
        return self.editor.readline(prompt)

    def run(self) -> None:
        """Runs the interactive shell."""
        # Load the history file, if any.
        if self.history_path is not None:
            try:
                self.editor.load_history(self.history_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise Error.from_exception(error) from error

        # Print welcome message.
        server = self.client.status().server
        print(f"Connected to toyDB node n{server}. Enter !help for instructions.")

        # Prompt for commands and execute them.
        while True:
            try:
                input_ = self.prompt().strip()
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                break
            self.editor.add_history_entry(input_)
            try:
                self.execute(input_)
            except Error as error:
                print(f"Error: {error}", file=sys.stderr)

        # Save the history file.
        if self.history_path is not None:
            try:
                self.editor.save_history(self.history_path)
            except OSError as error:
                raise Error.from_exception(error) from error


class Editor:
    """A line editor with history and multiline input, standing in for the
    rustyline `Editor`. Uses the `readline` module for line editing and
    history where available, and `input()` to read lines."""

    def __init__(self, helper: "InputValidator") -> None:
        self.helper = helper
        """Validates input, determining when multiline input is complete."""
        if readline is not None:
            readline.set_auto_history(False)

    def readline(self, prompt: str) -> str:
        """Reads input, prompting for further lines until the helper deems it
        complete. Raises EOFError on end of input, or KeyboardInterrupt if
        interrupted."""
        input_ = input(prompt)
        while not self.helper.validate(input_):
            input_ += "\n" + input()
        return input_

    def add_history_entry(self, input: str) -> None:
        """Adds an input to the history, ignoring empty input and consecutive
        duplicates (like rustyline)."""
        if readline is None or not input:
            return
        length = readline.get_current_history_length()
        if length > 0 and readline.get_history_item(length) == input:
            return
        readline.add_history(input)

    def load_history(self, path: Path) -> None:
        """Loads the history from a file. Raises FileNotFoundError if it
        doesn't exist."""
        if readline is not None:
            readline.read_history_file(path)

    def save_history(self, path: Path) -> None:
        """Saves the history to a file."""
        if readline is not None:
            readline.write_history_file(path)


class InputValidator:
    """A line editor helper for multiline editing. After a new line is
    entered, it determines whether the input makes up a complete SQL statement
    that should be submitted to the server (i.e. it's terminated by ;), or
    wait for further input."""

    def validate(self, input: str) -> bool:
        """Returns True if the input is complete (valid), or False if the
        editor should wait for more input (incomplete). Only called after
        completed lines (rustyline's `validate_while_typing() == false`)."""
        # Empty lines and ! commands are ready.
        if not input or input.startswith("!") or input == ";":
            return True
        # For SQL statements, just look for any semicolon or lexer error, and
        # rely on the server for further validation and error handling.
        try:
            return any(isinstance(token, Token.Semicolon) for token in Lexer(input))
        except Error:
            return True


if __name__ == "__main__":
    main()
