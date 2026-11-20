"""toyDB: a distributed SQL database, ported from Rust to Python.

Mirrors `src/lib.rs`. The crate-level re-export (`StatementResult`) is
resolved lazily, to avoid import cycles between the submodules.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from toydb.sql.execution import StatementResult

__all__ = ["StatementResult"]


def __getattr__(name: str) -> Any:
    match name:
        case "StatementResult":
            from toydb.sql.execution import StatementResult

            return StatementResult
        case _:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
