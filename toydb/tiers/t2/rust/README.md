# toyDB

A distributed SQL database, built for education: a SQL engine with ACID
transactions on top of a pluggable key/value storage engine, replicated across
a cluster of nodes with Raft consensus, with a server, a client library, and
command-line tools.

* **Encoding** (`src/encoding/`): order-preserving key encoding (`keycode.rs`),
  value encoding (`bincode.rs`), and formatters that print raw keys and values
  in readable form (`format.rs`).

* **Storage** (`src/storage/`): the key/value storage engine interface
  (`engine.rs`) with an in-memory (`memory.rs`) and a log-structured on-disk
  (`bitcask.rs`) implementation, and MVCC transactions with snapshot isolation
  on top of any engine (`mvcc.rs`).

* **Raft** (`src/raft/`): Raft consensus for replicating a state machine
  across nodes: the node and its leader, follower and candidate roles
  (`node.rs`), the replicated log (`log.rs`), the messages exchanged between
  nodes and with clients (`message.rs`), and the state machine interface
  (`state.rs`). The module documentation in `mod.rs` describes the protocol.

* **SQL** (`src/sql/`): a SQL implementation with typed tables, primary and
  secondary indexes, foreign keys, joins, aggregates, and transactions:

  * `types/`: values, expressions, and table schemas.
  * `parser/`: the lexer, the parser, and the abstract syntax tree.
  * `planner/`: the planner, the plan nodes, and the plan optimizers.
  * `execution/`: plan executors (`executor.rs`, `join.rs`, `aggregator.rs`)
    and the session that runs SQL statements (`session.rs`).
  * `engine/`: the SQL storage interface (`engine.rs`), its implementation on
    top of MVCC storage (`local.rs`), and the Raft-replicated engine
    (`raft.rs`): the SQL state machine applied on each node, and the client
    side that submits reads and writes through Raft.

* **Server** (`src/server.rs`): a toyDB node. It runs the Raft node, exchanges
  Raft messages with its peers over TCP, and serves SQL client sessions over
  TCP.

* **Client** (`src/client.rs`): the client library, used by the tools and the
  end-to-end tests.

* **Binaries** (`src/bin/`): `toydb` (the server, configured by
  `config/toydb.yaml` or `TOYDB_*` environment variables), `toysql` (an
  interactive SQL shell), `toydump` (dumps the contents of a storage file),
  and `workload` (a benchmark tool).

* **Errors** (`src/error.rs`): the error type used throughout.

The module documentation at the top of `src/sql/mod.rs` walks through the
life of a SQL statement, and `docs/architecture/` is a guided tour of the
whole codebase.

## Layout

```
Cargo.toml, Cargo.lock      package manifest and locked dependencies
check.sh                    builds and runs every test
src/lib.rs                  crate root
src/error.rs                errors
src/encoding/               key/value encodings and formatters
src/storage/                storage engines and MVCC
src/storage/testscripts/    storage golden scripts
src/raft/                   Raft consensus
src/raft/testscripts/       Raft golden scripts (log/, node/)
src/sql/                    the SQL layer
src/sql/mod.rs              SQL golden script runner (in its test module)
src/sql/testscripts/        SQL golden scripts
src/sql/testscripts/task/   SQL golden scripts for the task at hand
src/server.rs               the server
src/client.rs               the client library
src/bin/                    toydb, toysql, toydump, workload
tests/tests.rs              end-to-end golden script runner
tests/testcluster.rs        test clusters for the end-to-end tests
tests/scripts/              end-to-end golden scripts
tests/scripts/task/         end-to-end golden scripts for the task at hand
config/toydb.yaml           default server configuration
cluster/                    a local 5-node cluster (run.sh, per-node configs)
docs/                       architecture guide, SQL reference and examples
```

Unit tests and golden script runners live in the `#[cfg(test)]` module at the
end of the source file they test; the end-to-end tests are in `tests/`.

## Running

A local five-node cluster (ports 9601-9605 for SQL, 9701-9705 for Raft, data
under `cluster/*/data/`) is started with `./cluster/run.sh`, and a SQL shell
connects to node 1 with `cargo run --bin toysql` (`-H`/`-p` select another
host or port). A single node runs with `cargo run --bin toydb` (configuration
in `config/toydb.yaml`).

## Tests

Run everything with:

```sh
./check.sh
```

It builds the project, runs the unit tests and all golden scripts (including
the end-to-end cluster scripts), prints a short summary, and exits non-zero if
anything fails. Pass `-v` for the full test output.

The tests can also be run directly:

```sh
cargo test                             # everything
cargo test sql::tests                  # all SQL golden scripts
cargo test sql::tests::queries::limit  # a single golden script
cargo test raft::node                  # the tests of one module
cargo test --test tests                # all end-to-end golden scripts
cargo test --test tests errors         # a single end-to-end golden script
```

### Golden scripts

Most tests are golden scripts: plain text files containing commands and their
expected output, separated by `---`. For SQL scripts, the commands are SQL
statements:

```
> CREATE TABLE test (id INT PRIMARY KEY, value STRING)
> INSERT INTO test VALUES (1, 'a'), (2, 'b'), (3, 'c')
---
ok

[plan]> SELECT * FROM test LIMIT 2
---
Limit: 2
└─ Scan: test
1, 'a'
2, 'b'
```

A test runs the commands of a script and fails if the output differs from the
script. Lines starting with `>` are passed to the runner verbatim, other
lines are parsed as `command arg key=value`, `[tag]` prefixes change how the
result is shown (e.g. `[plan]` also prints the query plan), `!` in front of a
command expects it to fail, a `name:` prefix selects e.g. a session or a client,
and `#` starts a comment. See the runner for the commands and tags it
supports.

| Scripts | Runner | Runs |
|---|---|---|
| `src/sql/testscripts/{optimizers,queries,schema,transactions,writes,task}/` | `test_goldenscript` in `src/sql/mod.rs` | SQL statements against a SQL engine |
| `src/sql/testscripts/expressions/` | `test_goldenscript_expr` in `src/sql/mod.rs` | SQL expressions |
| `src/storage/testscripts/mvcc/` | `src/storage/mvcc.rs` | MVCC transactions |
| `src/storage/testscripts/engine/` | `src/storage/memory.rs` and `src/storage/bitcask.rs` | storage engine operations, once per engine |
| `src/storage/testscripts/{memory,bitcask}/` | `src/storage/memory.rs`, `src/storage/bitcask.rs` | engine-specific operations |
| `src/raft/testscripts/log/` | `src/raft/log.rs` | Raft log operations |
| `src/raft/testscripts/node/` | `src/raft/node.rs` | simulated Raft clusters (in memory, deterministic) |
| `tests/scripts/` (including `task/`) | `test_goldenscript` in `tests/tests.rs` | SQL clients against a real server cluster |

Every file in a script directory is a script, and its file name must be a
snake_case identifier (e.g. `group_by`, without an extension).

The expected output is not written by hand. To add or change a script, write
the commands and regenerate the output, then review the result:

```sh
touch src/sql/mod.rs && UPDATE_GOLDENFILES=1 cargo test sql::tests
touch tests/tests.rs && UPDATE_GOLDENFILES=1 cargo test --test tests
```

Scripts are discovered at compile time, so the source file containing the
runner must be recompiled (e.g. by `touch`ing it) when a script is added or
removed. `check.sh` does this for the SQL and end-to-end scripts.

### End-to-end scripts

The scripts in `tests/scripts/` start a real cluster of `toydb` server
processes (built from this project) on localhost and talk to it through the
client library:

```
cluster nodes=3
---
ok

c1:> CREATE TABLE test (id INT PRIMARY KEY, value STRING)
c1:> INSERT INTO test VALUES (1, 'a')
c2:> SELECT * FROM test
---
c2: 1, 'a'
```

`cluster nodes=N` starts the cluster (once per script). Every other command
goes to the client named by its prefix (`c1:`, `c2:`, ...; no prefix is a
client too), which is connected to a random node on first use: `> SQL` runs a
statement (`[result]` shows the result of non-query statements, `[header]`
the column names), `status` shows the server status, `tables` lists the
tables, `table NAME` shows a table schema (`raw=true` for the debug form), and
`close` closes the client. The clusters are stopped and their data removed at
the end of each script. Test clusters take their ports (from 19601 for SQL and
19701 for Raft, shifted by a multiple of 200 when those are in use) through
lock files in `/tmp`, so test runs in different copies of the project can run
at the same time.

### Task scripts

Two directories are for golden scripts that belong to the task at hand. Both
start out empty, and scripts placed there are part of `./check.sh` and
`cargo test`:

* `src/sql/testscripts/task/`: SQL golden scripts, run by the ordinary SQL
  runner (`test_goldenscript` in `src/sql/mod.rs`) as
  `sql::tests::task::<name>`.
* `tests/scripts/task/`: end-to-end golden scripts, run against a real
  cluster by the end-to-end runner (`test_goldenscript` in `tests/tests.rs`)
  as `task::<name>` (`cargo test --test tests task::`).
