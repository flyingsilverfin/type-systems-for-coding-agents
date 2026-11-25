"""toybackup: creates, restores, lists, verifies and compares backups. `create`
(optionally `--as-of` a version) and `restore` connect to a server (with
`--user` and `--password` for a superuser); `list`, `verify` and `diff` read
files.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from toydb.backup import format
from toydb.client import Client
from toydb.error import Error

VERSION = "1.0.0"
"""The toyDB version (the Cargo package version)."""


def main() -> None:
    try:
        status = Command.parse().run()
    except Error as error:
        print(f"Error: {error}", file=sys.stderr)
        status = 1
    sys.exit(status)


@dataclass
class Command:
    """The toybackup command."""

    command: str
    """The subcommand: create, restore, list, verify or diff."""
    files: list[str]
    """The backup files."""
    host: str = "localhost"
    """Host to connect to."""
    port: int = 9601
    """Port number to connect to."""
    user: str | None = None
    """User to log in as."""
    password: str | None = None
    """Password to log in with."""
    as_of: int | None = None
    """The version to create the backup as of."""

    @classmethod
    def parse(cls, args: list[str] | None = None) -> "Command":
        """Parses command-line arguments."""
        parser = argparse.ArgumentParser(
            prog="toybackup", description="Creates, restores and inspects toyDB backups."
        )
        parser.add_argument("-V", "--version", action="version", version=f"toybackup {VERSION}")
        commands = parser.add_subparsers(dest="command", required=True)

        def server_command(name: str, help: str) -> argparse.ArgumentParser:
            command = commands.add_parser(name, help=help)
            command.add_argument("-H", "--host", default="localhost", help="Host to connect to.")
            command.add_argument(
                "-p", "--port", type=_port, default=9601, help="Port number to connect to."
            )
            command.add_argument("--user", help="User to log in as.")
            command.add_argument("--password", help="Password to log in with.")
            command.add_argument("file", help="The backup file.")
            return command

        create = server_command("create", "Creates a backup of the server's database.")
        create.add_argument(
            "--as-of", type=int, dest="as_of", help="The version to create the backup as of."
        )
        server_command("restore", "Restores a backup into the server's database.")
        commands.add_parser("list", help="Lists a backup's records.").add_argument(
            "file", help="The backup file."
        )
        commands.add_parser("verify", help="Checks a backup.").add_argument(
            "file", help="The backup file."
        )
        diff = commands.add_parser("diff", help="Compares two backups.")
        diff.add_argument("file", help="The first backup file.")
        diff.add_argument("other", help="The second backup file.")

        parsed = parser.parse_args(args)
        if getattr(parsed, "password", None) is not None and parsed.user is None:
            parser.error("--password requires --user")
        files = [parsed.file] + ([parsed.other] if parsed.command == "diff" else [])
        if parsed.command in ("create", "restore"):
            return cls(
                command=parsed.command,
                files=files,
                host=parsed.host,
                port=parsed.port,
                user=parsed.user,
                password=parsed.password,
                as_of=getattr(parsed, "as_of", None),
            )
        return cls(command=parsed.command, files=files)

    def run(self) -> int:
        """Runs the command, returning the exit status."""
        match self.command:
            case "create":
                with self.connect() as client:
                    data = client.backup(self.as_of)
                write(self.files[0], data)
            case "restore":
                data = read(self.files[0])
                with self.connect() as client:
                    client.restore(data)
            case "list":
                lines = format.list_backup(read(self.files[0]))
                print("\n".join(lines))
                return 1 if lines[-1].startswith("Error: ") else 0
            case "verify":
                data = read(self.files[0])
                try:
                    print(f"ok: {format.verify(data)} records")
                except Error as error:
                    print(f"Error: {error}")
                    return 1
            case "diff":
                a, b = read(self.files[0]), read(self.files[1])
                try:
                    lines = format.diff(a, b)
                except Error as error:
                    print(f"Error: {error}")
                    return 1
                for line in lines:
                    print(line)
            case command:
                raise ValueError(f"unknown command {command}")
        return 0

    def connect(self) -> Client:
        """Connects to the server, logging in if a user is given."""
        client = Client.connect((self.host, self.port))
        if self.user is not None:
            try:
                client.authenticate(self.user, self.password)
            except Error:
                client.close()
                raise
        return client


def read(file: str) -> bytes:
    """Reads a backup file."""
    try:
        return Path(file).read_bytes()
    except OSError as error:
        raise Error.from_exception(error) from error


def write(file: str, data: bytes) -> None:
    """Writes a backup file."""
    try:
        Path(file).write_bytes(data)
    except OSError as error:
        raise Error.from_exception(error) from error


def _port(value: str) -> int:
    """Parses a port number (a Rust u16)."""
    port = int(value)
    if not 0 <= port <= 0xFFFF:
        raise ValueError(f"{value} is not in 0..=65535")
    return port


if __name__ == "__main__":
    main()
