# toyDB

A simple SQL database, built for education. This project contains its
single-node core: a SQL engine with ACID transactions on top of a pluggable
key/value storage engine.

* **Encoding** (`toydb/encoding/`): order-preserving key encoding
  (`keycode.py`), value encoding (`bincode.py`, on the data model in
  `serde.py`), and formatters that print raw keys and values in readable form
  (`format.py`).

* **Storage** (`toydb/storage/`): the key/value storage engine interface
  (`engine.py`) with an in-memory (`memory.py`) and a log-structured on-disk
  (`bitcask.py`) implementation, and MVCC transactions with snapshot isolation
  on top of any engine (`mvcc.py`). `btree.py` provides the ordered map and
  set they use.

* **SQL** (`toydb/sql/`): a SQL implementation with typed tables, primary and
  secondary indexes, foreign keys, joins, aggregates, and transactions:

  * `types/`: values, expressions, and table schemas.
  * `parser/`: the lexer, the parser, and the abstract syntax tree.
  * `planner/`: the planner, the plan nodes, and the plan optimizers.
  * `execution/`: plan executors (`executor.py`, `join.py`, `aggregator.py`)
    and the session that runs SQL statements (`session.py`).
  * `engine/`: the SQL storage interface (`engine.py`) and its implementation
    on top of MVCC storage (`local.py`).

* **Errors** (`toydb/error.py`): the error type used throughout.

* **Helpers**: `toydb/fmt.py` (display and debug formatting of values),
  `toydb/iter.py` (clonable iterators), and `toydb/ops.py` (range bounds).

The module documentation at the top of `toydb/sql/__init__.py` walks through
the life of a SQL statement.

## Layout

```
check.sh                    builds and runs every test
run_goldenscripts.py        runs golden scripts
toydb/__init__.py           package root
toydb/error.py              errors
toydb/encoding/             key/value encodings and formatters
toydb/storage/              storage engines and MVCC
tests/storage/testscripts/  storage golden scripts
toydb/sql/                  the SQL layer
tests/sql/test_mod.py       SQL golden script runner
tests/sql/testscripts/      SQL golden scripts
tests/sql/testscripts/task/ SQL golden scripts for the task at hand
tests/goldenscript.py       the golden script framework
```

Unit tests and golden script runners live under `tests/`, which mirrors the
package: the tests for `toydb/x/y.py` are in `tests/x/test_y.py`. Only the
standard library is needed (Python 3.12 or later).

## Tests

Run everything with:

```sh
./check.sh
```

It builds the project (checks that every module compiles and imports), runs
the unit tests and all golden scripts, prints a short summary, and exits
non-zero if anything fails. Pass `-v` for the full test output.

The tests can also be run directly:

```sh
python3 run_goldenscripts.py                                   # all golden scripts
python3 run_goldenscripts.py tests/sql                         # all SQL golden scripts
python3 run_goldenscripts.py tests/sql/testscripts/queries/limit  # a single golden script
python3 -m unittest discover -s tests -t .                     # all unit tests
python3 -m unittest tests.storage.test_mvcc                    # the tests of one module
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
| `tests/sql/testscripts/{optimizers,queries,schema,transactions,writes,task}/` | `test_goldenscript` in `tests/sql/test_mod.py` | SQL statements against a SQL engine |
| `tests/sql/testscripts/expressions/` | `test_goldenscript_expr` in `tests/sql/test_mod.py` | SQL expressions |
| `tests/storage/testscripts/mvcc/` | `tests/storage/test_mvcc.py` | MVCC transactions |
| `tests/storage/testscripts/engine/` | `tests/storage/test_memory.py` and `tests/storage/test_bitcask.py` | storage engine operations, once per engine |
| `tests/storage/testscripts/{memory,bitcask}/` | `tests/storage/test_memory.py`, `tests/storage/test_bitcask.py` | engine-specific operations |

Every file in a script directory is a script, and its file name must be a
snake_case identifier (e.g. `group_by`, without an extension).

The expected output is not written by hand. To add or change a script, write
the commands and regenerate the output, then review the result:

```sh
UPDATE_GOLDENFILES=1 python3 run_goldenscripts.py tests/sql
```

Scripts are discovered at run time from the directories listed in `REGISTRY`
in `run_goldenscripts.py`, so a new script is picked up by the next run.

### Task scripts

`tests/sql/testscripts/task/` is for SQL golden scripts that belong to the
task at hand. It starts out empty. Scripts placed there are run by the
ordinary SQL runner (`test_goldenscript`), and are part of `./check.sh` and
`python3 run_goldenscripts.py`.
