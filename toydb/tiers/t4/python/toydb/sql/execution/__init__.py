"""Executes statements and plans.

Mirrors `src/sql/execution/mod.rs`.
"""

from toydb.sql.execution.executor import ExecutionResult, Executor
from toydb.sql.execution.session import Session, StatementResult

__all__ = ["ExecutionResult", "Executor", "Session", "StatementResult"]
