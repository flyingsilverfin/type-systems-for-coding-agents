"""Query profiles for `EXPLAIN (ANALYZE)`: executes a planned statement, with
its effects, and returns its plan with the estimated and the actual number of
rows of every line of the plan display.

The statement is executed as when it is issued, except that every plan node is
run to completion before its parent reads its rows, and the rows each node
produces are counted. A write's statement line has the count of its result; a
statement line without one (also of a plan added later) has 0; every node,
also one added later, has the rows it produced.
"""

from typing import TYPE_CHECKING

from toydb.sql.engine import Transaction
from toydb.sql.execution.executor import ExecutionResult, Executor
from toydb.sql.planner import Plan
from toydb.sql.planner.estimate import estimate_rows
from toydb.sql.types import Label, Row, Value

if TYPE_CHECKING:
    from toydb.sql.execution.session import Session

COLUMNS: list[Label] = [Label.Unqualified(name) for name in ("node", "estimate", "rows")]
"""The EXPLAIN (ANALYZE) result columns."""


def profile_rows(
    txn: Transaction, plan: Plan, user: str, session: "Session | None" = None
) -> tuple[ExecutionResult, list[Row]]:
    """Executes a plan, returning its execution result and the EXPLAIN
    (ANALYZE) result rows."""
    estimates = estimate_rows(txn, plan)
    result, counts = profile(txn, plan, user, session)
    rows = [[*estimate, Value.Integer(count)] for estimate, count in zip(estimates, counts)]
    return result, rows


def profile(
    txn: Transaction, plan: Plan, user: str, session: "Session | None" = None
) -> tuple[ExecutionResult, list[int]]:
    """Executes a plan, running every node to completion, and returns the
    number of rows of every line of its plan display, in display order."""
    executor = Executor(txn, user, session, profiling=True)
    result = executor.execute(plan)
    nodes = executor.profile
    assert nodes is not None
    match result:
        # A query's plan display has no statement line.
        case ExecutionResult.Select():
            return result, nodes
        case (
            ExecutionResult.Delete(count)
            | ExecutionResult.Insert(count)
            | ExecutionResult.Update(count)
        ):
            return result, [count, *nodes]
        case _:
            return result, [0, *nodes]
