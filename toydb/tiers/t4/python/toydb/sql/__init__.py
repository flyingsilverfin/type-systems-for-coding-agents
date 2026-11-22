"""Implements a SQL execution engine. A SQL statement flows through the engine
as follows:

1. The `toySQL` client connects to the server, which creates a new
   `sql.execution.Session` in `Server.sql_session`.

2. `toySQL` submits a SQL `SELECT` string, which the server executes via
   `Session.execute`.

3. `Session.execute` calls `Parser.parse` to parse the SQL `SELECT` string
   into an `ast.Statement.Select` AST (Abstract Syntax Tree). The parser
   uses the `Lexer` for initial tokenization.

4. `Session.execute` obtains a new read-only `sql.engine.Transaction` via
   `Session.with_txn`. We'll gloss over the details here.

5. `Session.execute` calls `Plan.build` to construct an execution plan
   from the AST via the `Planner`, using the `Transaction`'s
   `sql.engine.Catalog` trait to look up table schema information.

6. `Session.execute` calls `Plan.optimize` to optimize the execution plan
   via the optimizers in `sql.planner.optimizer`. This e.g. performs
   filter pushdown to filter rows during storage scans, uses secondary
   indexes where appropriate, and chooses more efficient join types.

7. `Session.execute` calls `Plan.execute` to actually execute the plan,
   using the `Transaction` to access the `sql.engine.Engine`. It uses the
   executors in `sql.execution` to recursively execute the
   `sql.planner.Node` nodes, which stream and process `sql.types.Row`
   lists via `sql.types.Rows` iterators.

8. At the tip of the execution plan there's typically a `Node.Scan` which
   performs full table scans from storage. It is executed by
   `sql.execution.source.scan`, which calls `Transaction.scan`.

9. The upper `sql.engine.Raft` engine submits a `Read.Scan` request to
   Raft via `Raft.read` and `Raft.execute`. This is submitted through the
   channel `Raft.tx`, which is routed to the local Raft node in
   `Server.raft_route` via `raft.Node.step`.

10. We'll skip Raft details, but see the `raft` module documentation. The
    `Read.Scan` request eventually makes its way to the SQL state machine
    `sql.engine.raft.State` that's managed by Raft. Since this is a read
    request, it is executed only on the leader node, calling `State.read`.

11. `State` wraps the `sql.engine.Local` SQL execution engine that runs
    on each node, using local storage. `State.read` calls
    `Transaction.scan` using a `Local.Transaction`.

12. The `Local` engine uses a `storage.BitCask` engine for local storage,
    with `storage.mvcc` providing transactions. See their documentation
    for details.

13. `Transaction.scan` uses `sql.engine.KeyPrefix.Table` to obtain the
    key prefix for the scanned table, encoded via `encoding.keycode`. It
    scans rows under this prefix by calling `MVCC.scan_prefix`, which in
    turn dispatches to `BitCask.scan_prefix`. It returns a row iterator.

14. A row iterator is propagated back up through the stack:
    `BitCask` → `MVCC` → `Local` → `State` → `Raft` → `scan` → `Plan.execute`

15. `Plan.execute` collects the results in a `ExecutionResult.Select`,
    and returns it to `Session.execute`. It in turns returns it to
    `Server.sql_session`, which encodes it and sends it across the wire
    to `toySQL`, which displays them to the user.

Mirrors `src/sql/mod.rs`. The submodules (`engine`, `execution`, `parser`,
`planner` and `types`) are not imported here: import them explicitly, e.g.
`from toydb.sql.parser import Parser`. The SQL goldenscript runners live in
`tests/sql/test_mod.py`.
"""
