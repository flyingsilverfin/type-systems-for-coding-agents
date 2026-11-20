# toyDB

A simple SQL database, built for education. This project contains its
single-node core: a SQL engine with ACID transactions on top of a pluggable
key/value storage engine.

* **Encoding** (`src/encoding/`): order-preserving key encoding (`keycode.rs`),
  value encoding (`bincode.rs`), and formatters that print raw keys and values
  in readable form (`format.rs`).

* **Storage** (`src/storage/`): the key/value storage engine interface
  (`engine.rs`) with an in-memory (`memory.rs`) and a log-structured on-disk
  (`bitcask.rs`) implementation, and MVCC transactions with snapshot isolation
  on top of any engine (`mvcc.rs`).

* **SQL** (`src/sql/`): a SQL implementation with typed tables, primary and
  secondary indexes, foreign keys, joins, aggregates, and transactions:

  * `types/`: values, expressions, and table schemas.
  * `parser/`: the lexer, the parser, and the abstract syntax tree.
  * `planner/`: the planner, the plan nodes, and the plan optimizers.
  * `execution/`: plan executors (`executor.rs`, `join.rs`, `aggregator.rs`)
    and the session that runs SQL statements (`session.rs`).
  * `engine/`: the SQL storage interface (`engine.rs`) and its implementation
    on top of MVCC storage (`local.rs`).

* **Errors** (`src/error.rs`): the error type used throughout.

The module documentation at the top of `src/sql/mod.rs` walks through the
life of a SQL statement.

## Layout

```
Cargo.toml, Cargo.lock      package manifest and locked dependencies
check.sh                    builds and runs every test
src/lib.rs                  crate root
src/error.rs                errors
src/encoding/               key/value encodings and formatters
src/storage/                storage engines and MVCC
src/storage/testscripts/    storage golden scripts
src/sql/                    the SQL layer
src/sql/mod.rs              SQL golden script runner (in its test module)
src/sql/testscripts/        SQL golden scripts
src/sql/testscripts/task/   SQL golden scripts for the task at hand
```

Unit tests and golden script runners live in the `#[cfg(test)]` module at the
end of the source file they test.

## Tests

Run everything with:

```sh
./check.sh
```

It builds the project, runs the unit tests and all golden scripts, prints a
short summary, and exits non-zero if anything fails. Pass `-v` for the full
test output.

The tests can also be run directly:

```sh
cargo test                             # everything
cargo test sql::tests                  # all SQL golden scripts
cargo test sql::tests::queries::limit  # a single golden script
cargo test storage::mvcc               # the tests of one module
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
command expects it to fail, and `#` starts a comment. See the runner for the
commands and tags it supports.

| Scripts | Runner | Runs |
|---|---|---|
| `src/sql/testscripts/{optimizers,queries,schema,transactions,writes,task}/` | `test_goldenscript` in `src/sql/mod.rs` | SQL statements against a SQL engine |
| `src/sql/testscripts/expressions/` | `test_goldenscript_expr` in `src/sql/mod.rs` | SQL expressions |
| `src/storage/testscripts/mvcc/` | `src/storage/mvcc.rs` | MVCC transactions |
| `src/storage/testscripts/engine/` | `src/storage/memory.rs` and `src/storage/bitcask.rs` | storage engine operations, once per engine |
| `src/storage/testscripts/{memory,bitcask}/` | `src/storage/memory.rs`, `src/storage/bitcask.rs` | engine-specific operations |

Every file in a script directory is a script, and its file name must be a
snake_case identifier (e.g. `group_by`, without an extension).

The expected output is not written by hand. To add or change a script, write
the commands and regenerate the output, then review the result:

```sh
touch src/sql/mod.rs && UPDATE_GOLDENFILES=1 cargo test sql::tests
```

Scripts are discovered at compile time, so the source file containing the
runner must be recompiled (e.g. by `touch`ing it) when a script is added or
removed. `check.sh` does this for the SQL scripts.

### Task scripts

`src/sql/testscripts/task/` is for SQL golden scripts that belong to the task
at hand. It starts out empty. Scripts placed there are run by the ordinary SQL
runner (`test_goldenscript`), as `sql::tests::task::<name>`, and are part of
`./check.sh` and `cargo test`.
