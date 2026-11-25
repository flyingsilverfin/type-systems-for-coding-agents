"""The SQL engine provides SQL data storage and access, as well as session and
transaction management. The `Local` engine provides node-local on-disk
storage, while the `Raft` engine submits commands through Raft consensus
before dispatching to the `Local` engine on each node.

Mirrors `src/sql/engine/mod.rs`. The Raft engine's re-exports (`Raft`,
`Status` and `Write`) are resolved lazily, since `toydb.sql.engine.raft`
depends on the `toydb.raft` package, which in turn formats SQL keys and values
via this package.
"""

import importlib
from typing import TYPE_CHECKING, Any

from toydb.sql.engine.engine import Catalog, Engine, Transaction
from toydb.sql.engine.local import Key, Local

if TYPE_CHECKING:
    from toydb.sql.engine.raft import Raft, Status, Write

__all__ = ["Catalog", "Engine", "Key", "Local", "Raft", "Status", "Transaction", "Write"]


def __getattr__(name: str) -> Any:
    if name in ("Raft", "Status", "Write"):
        raft = importlib.import_module("toydb.sql.engine.raft")
        return getattr(raft, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
