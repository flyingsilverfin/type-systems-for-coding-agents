"""toydump is a debug tool that prints a toyDB BitCask database in
human-readable form. It can print both the SQL database and the Raft log
(via --raft). It only outputs live BitCask data, not garbage entries.

Mirrors `src/bin/toydump.rs`.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from toydb.encoding import format
from toydb.error import Error
from toydb.ops import Bound
from toydb.storage import BitCask

VERSION = "1.0.0"
"""The toyDB version (the Cargo package version)."""


def main() -> None:
    try:
        Command.parse().run()
    except Error as error:
        print(f"Error: {error}", file=sys.stderr)


@dataclass
class Command:
    """The toydump command."""

    file: str
    """The BitCask file to dump (SQL database unless --raft)."""
    raft: bool
    """The file is a Raft log, not SQL database."""
    raw: bool
    """Also show raw key and value."""

    @classmethod
    def parse(cls, args: list[str] | None = None) -> "Command":
        """Parses command-line arguments."""
        parser = argparse.ArgumentParser(prog="toydump", description="Prints toyDB file contents.")
        parser.add_argument("file", help="The BitCask file to dump (SQL database unless --raft).")
        parser.add_argument(
            "--raft", action="store_true", help="The file is a Raft log, not SQL database."
        )
        parser.add_argument("--raw", action="store_true", help="Also show raw key and value.")
        parser.add_argument("-V", "--version", action="version", version=f"toydump {VERSION}")
        parsed = parser.parse_args(args)
        return cls(file=parsed.file, raft=parsed.raft, raw=parsed.raw)

    def run(self) -> None:
        """Runs the command."""
        engine = BitCask(Path(self.file))
        try:
            formatter: format.Formatter
            if self.raft:
                formatter = format.Raft(format.SQLCommand())
            else:
                formatter = format.MVCC(format.SQL())
            for key, value in engine.scan((Bound.Unbounded(), Bound.Unbounded())):
                string = formatter.key_value(key, value)
                if self.raw:
                    string = f"{string} [{format.Raw().key_value(key, value)}]"
                print(string)
        finally:
            engine.close()


if __name__ == "__main__":
    main()
