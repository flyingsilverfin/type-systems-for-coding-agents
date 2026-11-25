"""The backup commands of the golden-script runners (`tests/sql/test_mod.py` and
`tests/e2e/test_tests.py`). The runner keeps backups by name.

    backup <name> [as_of=<v>]                   creates a backup through the prefix's session or client
    restore <name>                              restores it through the prefix's session or client
    backup-list <name>                          lists it as toybackup list does
    backup-verify <name>                        verifies it as toybackup verify does
    backup-diff <a> <b>                         compares two backups as toybackup diff does
    backup-hex <name>                           prints its bytes in hex, 16 per line
    backup-load <name> <hex>...                 stores the given bytes as a backup
    backup-patch <name> offset=<i> hex=<bytes>  overwrites bytes at the offset
    backup-truncate <name> length=<n>           keeps only the first n bytes
"""

from collections.abc import Callable

from tests import goldenscript
from toydb.backup import format
from toydb.error import Error

COMMANDS = {
    "backup",
    "restore",
    "backup-list",
    "backup-verify",
    "backup-diff",
    "backup-hex",
    "backup-load",
    "backup-patch",
    "backup-truncate",
}
"""The names of the backup commands."""


class BackupRunner:
    """Runs backup commands, with backups kept by name."""

    def __init__(
        self,
        create: Callable[[str, int | None], bytes],
        restore: Callable[[str, bytes], None],
        list_backup: Callable[[bytes], list[str]] = format.list_backup,
    ) -> None:
        """Creates a runner that creates and restores backups for a command
        prefix with the given functions, and lists them with list_backup."""
        self.create = create
        self.restore = restore
        self.list_backup = list_backup
        self.backups: dict[str, bytes] = {}

    def run(self, prefix: str, command: goldenscript.Command) -> str:
        """Runs a backup command for a prefix, returning its output."""
        args = command.consume_args()
        name = required(args.next_pos(), "name")
        output = ""
        match command.name:
            case "backup":
                as_of = args.take_key("as_of")
                self.backups[name] = self.create(prefix, int(as_of) if as_of is not None else None)
            case "restore":
                self.restore(prefix, self.get(name))
            case "backup-list":
                output = "".join(f"{line}\n" for line in self.list_backup(self.get(name)))
            case "backup-verify":
                try:
                    output = f"ok: {format.verify(self.get(name))} records\n"
                except Error as error:
                    output = f"Error: {error}\n"
            case "backup-diff":
                other = required(args.next_pos(), "other")
                try:
                    lines = format.diff(self.get(name), self.get(other))
                except Error as error:
                    lines = [f"Error: {error}"]
                output = "".join(f"{line}\n" for line in lines)
            case "backup-hex":
                data = self.get(name)
                for start in range(0, len(data), 16):
                    output += " ".join(f"{byte:02x}" for byte in data[start : start + 16]) + "\n"
            case "backup-load":
                hex = "".join(iter(args.next_pos, None))
                self.backups[name] = bytes.fromhex(hex)
            case "backup-patch":
                offset = int(required(args.take_key("offset"), "offset"))
                patch = bytes.fromhex(required(args.take_key("hex"), "hex"))
                data = bytearray(self.get(name))
                if offset + len(patch) > len(data):
                    raise ValueError(f"patch beyond the end of backup {name}")
                data[offset : offset + len(patch)] = patch
                self.backups[name] = bytes(data)
            case "backup-truncate":
                length = int(required(args.take_key("length"), "length"))
                self.backups[name] = self.get(name)[:length]
            case _:
                raise ValueError(f"unknown backup command {command.name}")
        args.reject_next()
        return output

    def get(self, name: str) -> bytes:
        """Returns a backup by name."""
        if (data := self.backups.get(name)) is None:
            raise ValueError(f"unknown backup {name}")
        return data


def required(value: str | None, name: str) -> str:
    """Returns a required argument."""
    if value is None:
        raise ValueError(f"argument {name} not given")
    return value
