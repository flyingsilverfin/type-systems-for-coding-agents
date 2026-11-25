"""A simple stdout/stderr logger, standing in for the `log` and `simplelog`
crates as used by the toyDB server (`src/bin/toydb.rs`).

Has no Rust module counterpart. Configures Python `logging` to format records
like `simplelog::SimpleLogger` with its default config:

    12:34:56 [INFO] Listening on 127.0.0.1:9601 (SQL) and 127.0.0.1:9701 (Raft)
    12:34:56 [DEBUG] (3) toydb::raft::node: Stepping Envelope { ... }

Error records go to stderr, all others to stdout. Python logger names
(`toydb.raft.node`) are shown as Rust module paths (`toydb::raft::node`).
"""

import enum
import logging
import sys
import threading
import time
from typing import TextIO

TRACE: int = 5
"""The log level for trace records, below `logging.DEBUG`."""


class LevelFilter(enum.IntEnum):
    """A log level filter, like `log::LevelFilter`. Values are Python logging
    levels; a record is logged if its level is at least the filter value."""

    Off = logging.CRITICAL + 10
    Error = logging.ERROR
    Warn = logging.WARNING
    Info = logging.INFO
    Debug = logging.DEBUG
    Trace = TRACE

    @classmethod
    def from_str(cls, string: str) -> "LevelFilter":
        """Parses a level filter, case-insensitively (like `str::parse`)."""
        for level in cls:
            if level.name.lower() == string.lower():
                return level
        raise ValueError("attempted to convert a string that doesn't match an existing log level")


_LEVEL_NAMES = {
    logging.CRITICAL: "ERROR",
    logging.ERROR: "ERROR",
    logging.WARNING: "WARN",
    logging.INFO: "INFO",
    logging.DEBUG: "DEBUG",
    TRACE: "TRACE",
}


class Formatter(logging.Formatter):
    """Formats records like simplelog's default config: the UTC time and level
    always, the thread ID and target (module path) for debug and trace
    records, and the source location for trace records."""

    def __init__(self) -> None:
        super().__init__()
        self._thread_ids: dict[int, int] = {}
        self._lock = threading.Lock()

    def thread_id(self, ident: int | None) -> int:
        """Numbers threads sequentially in order of first use, like Rust's
        `ThreadId` (the main thread is 1)."""
        if ident is None:
            return 0
        with self._lock:
            if not self._thread_ids:
                self._thread_ids[threading.main_thread().ident or 0] = 1
            return self._thread_ids.setdefault(ident, len(self._thread_ids) + 1)

    def format(self, record: logging.LogRecord) -> str:
        level = _LEVEL_NAMES.get(record.levelno, record.levelname)
        output = f"{time.strftime('%H:%M:%S', time.gmtime(record.created))} [{level}] "
        if record.levelno <= logging.DEBUG:
            output += f"({self.thread_id(record.thread)}) "
            output += f"{record.name.replace('.', '::')}: "
        if record.levelno <= TRACE:
            output += f"[{record.pathname}:{record.lineno}] "
        output += record.getMessage()
        if record.exc_info:
            output += "\n" + self.formatException(record.exc_info)
        return output


class _StreamHandler(logging.Handler):
    """Writes error records to stderr and others to stdout. The streams are
    looked up when writing, so they can be redirected."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream: TextIO = sys.stderr if record.levelno >= logging.ERROR else sys.stdout
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


class _AllowFilter(logging.Filter):
    """Only allows records whose target starts with one of the given
    prefixes, like `ConfigBuilder::add_filter_allow_str`."""

    def __init__(self, allow: list[str]) -> None:
        super().__init__()
        self.allow = allow

    def filter(self, record: logging.LogRecord) -> bool:
        return not self.allow or any(record.name.startswith(a) for a in self.allow)


def init(level: LevelFilter, filter_allow: list[str] | None = None) -> None:
    """Initializes the global logger at the given level, optionally only
    allowing records from loggers with the given name prefixes."""
    handler = _StreamHandler()
    handler.setFormatter(Formatter())
    handler.addFilter(_AllowFilter(filter_allow or []))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.value)
