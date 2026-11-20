"""The SQL engine provides SQL data storage and access, as well as session and
transaction management. The `Local` engine provides node-local on-disk
storage, while the `Raft` engine submits commands through Raft consensus
before dispatching to the `Local` engine on each node.

Mirrors `src/sql/engine/mod.rs`.
"""

from toydb.sql.engine.engine import Catalog, Engine, Transaction
from toydb.sql.engine.local import Key, Local

__all__ = ["Catalog", "Engine", "Key", "Local", "Transaction"]
