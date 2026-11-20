# toyDB

A distributed SQL database, built for education: a SQL engine with ACID
transactions on top of a pluggable key/value storage engine, replicated across
a cluster of nodes with Raft consensus, with a server, a client library, and
command-line tools.

* **Encoding** (`toydb/encoding/`): order-preserving key encoding
  (`keycode.py`), value encoding (`bincode.py`, on the data model in
  `serde.py`), formatters that print raw keys and values in readable form
  (`format.py`), and the JSON text and CSV lines used by the extensions
  (`json.py`, `csv.py`).

* **Storage** (`toydb/storage/`): the key/value storage engine interface
  (`engine.py`) with an in-memory (`memory.py`) and a log-structured on-disk
  (`bitcask.py`) implementation, and MVCC transactions with snapshot isolation
  on top of any engine (`mvcc.py`). `btree.py` provides the ordered map and
  set they use.

* **Raft** (`toydb/raft/`): Raft consensus for replicating a state machine
  across nodes: the node and its leader, follower and candidate roles
  (`node.py`), the replicated log (`log.py`), the messages exchanged between
  nodes and with clients (`message.py`), and the state machine interface
  (`state.py`). The module documentation in `__init__.py` describes the
  protocol.

* **SQL** (`toydb/sql/`): a SQL implementation with typed tables, primary and
  secondary indexes, foreign keys, joins, aggregates, and transactions:

  * `types/`: values, expressions, and table schemas, plus column statistics
    (`statistics.py`) and the value formats of the extensions (`formats.py`:
    values as JSON, SQL literals and CSV fields, and values read from text).
  * `parser/`: the lexer, the parser, and the abstract syntax tree.
  * `planner/`: the planner, the plan nodes, and the plan optimizers.
  * `execution/`: plan executors (`executor.py`, `join.py`, `aggregator.py`),
    the session that runs SQL statements (`session.py`), and the extensions
    (see below): `json.py`, `digest.py`, `analyze.py`, `export.py` and
    `importer.py`.
  * `engine/`: the SQL storage interface (`engine.py`), its implementation on
    top of MVCC storage (`local.py`), and the Raft-replicated engine
    (`raft.py`): the SQL state machine applied on each node, and the client
    side that submits reads and writes through Raft.

* **Server** (`toydb/server.py`): a toyDB node. It runs the Raft node,
  exchanges Raft messages with its peers over TCP, and serves SQL client
  sessions over TCP.

* **Client** (`toydb/client.py`): the client library, used by the tools and
  the end-to-end tests.

* **Binaries** (`toydb/bin/`, with launchers in `bin/`): `toydb` (the server,
  configured by `config/toydb.yaml` or `TOYDB_*` environment variables; see
  `toydb/config.py`), `toysql` (an interactive SQL shell), `toydump` (dumps
  the contents of a storage file), and `workload` (a benchmark tool).

* **Errors** (`toydb/error.py`): the error type used throughout.

* **Helpers**: `toydb/fmt.py` (display and debug formatting of values),
  `toydb/iter.py` (clonable iterators), `toydb/ops.py` (range bounds),
  `toydb/net.py` (address resolution, listening and connecting), and
  `toydb/simplelog.py` (the server's log output format).

The module documentation at the top of `toydb/sql/__init__.py` walks through
the life of a SQL statement.

## Extensions

Four extension layers build on the SQL engine's types and are reachable from
SQL or the client:

* **JSON output** (`toydb/sql/execution/json.py`): every statement result has
  a stable, compact JSON form, e.g.
  `{"result":"select","columns":["item.id",null],"rows":[[1,2.5]]}`. `EXPLAIN`
  results contain the plan as a tree of nodes, with column references
  (`{"column":3,"label":"item.qty"}`), constants (`{"constant":5}`) and
  operators (`{"op":"_ > _","args":[...]}`). Values without a native JSON form
  (NaN, infinities) are written as `{"type":"FLOAT","value":"NaN"}`. In
  `toysql`, `!format json` prints results as JSON (`!format text` switches
  back); in golden scripts, the `[json]` tag does.

* **Statement digests** (`toydb/sql/execution/digest.py`): each session keeps
  statistics of the statements it executes, grouped by digest: the canonical
  SQL text with literals replaced by `?`, so that e.g. `select * from T where
  (ID = 1)` and `SELECT * FROM t WHERE id = 2` are both `SELECT * FROM t WHERE
  id = ?`. `SHOW STATEMENTS` lists `digest`, `calls`, `errors` and `rows`, in
  order of first execution. In a cluster, the statistics belong to the client
  connection.

* **Column statistics** (`toydb/sql/execution/analyze.py`,
  `toydb/sql/types/statistics.py`): `ANALYZE [table]` computes per-column
  statistics (row and NULL counts, distinct values, min, max, most frequent
  value, average string length) and stores them in the catalog, under a key
  of their own. They are transactional and replicated like any other write,
  and removed by `DROP TABLE`. `SHOW STATISTICS table` shows them.

* **Export and import** (`toydb/sql/execution/export.py`,
  `toydb/sql/execution/importer.py`): `EXPORT table AS CSV|JSON|SQL` returns a
  table's rows as lines of CSV (with a header), JSON objects, or SQL
  statements that re-create the table; `EXPORT AS SQL` exports the whole
  database, ordered by foreign key references. `IMPORT INTO table FROM
  CSV|JSON 'line', ...` inserts rows from such lines, one string literal per
  line, exactly like an `INSERT` with the CSV header or the JSON member names
  as its column list.

```
> CREATE TABLE item (id INT PRIMARY KEY, name STRING, price FLOAT)
> IMPORT INTO item FROM CSV 'id,name,price', '1,apple,1.5', '2,"b, c",NaN'
> ANALYZE item
> EXPORT item AS JSON
---
{"id":1,"name":"apple","price":1.5}
{"id":2,"name":"b, c","price":{"type":"FLOAT","value":"NaN"}}

[json]> SHOW STATISTICS item
---
{"result":"select","columns":["column","rows","nulls","distinct","min","max","top","top_count","avg_length"],"rows":[...]}
```

`ANALYZE`, `CSV`, `EXPORT`, `IMPORT`, `JSON`, `SHOW`, `SQL`, `STATEMENTS` and
`STATISTICS` are reserved keywords. The extension statements can't be
explained.

## Layout

```
check.sh                     builds and runs every test
run_goldenscripts.py         runs golden scripts
toydb/__init__.py            package root
toydb/error.py               errors
toydb/encoding/              key/value encodings and formatters
toydb/storage/               storage engines and MVCC
tests/storage/testscripts/   storage golden scripts
toydb/raft/                  Raft consensus
tests/raft/testscripts/      Raft golden scripts (log/, node/)
toydb/sql/                   the SQL layer
tests/sql/test_mod.py        SQL golden script runner
tests/sql/testscripts/       SQL golden scripts
tests/sql/testscripts/ext/   SQL golden scripts of the extensions
tests/sql/testscripts/task/  SQL golden scripts for the task at hand
toydb/server.py              the server
toydb/client.py              the client library
toydb/bin/                   toydb, toysql, toydump, workload
bin/                         launchers for the binaries
config/toydb.yaml            default server configuration
tests/e2e/test_tests.py      end-to-end golden script runner
tests/e2e/testcluster.py     test clusters for the end-to-end tests
tests/e2e/scripts/           end-to-end golden scripts
tests/e2e/scripts/ext/       end-to-end golden scripts of the extensions
tests/e2e/scripts/task/      end-to-end golden scripts for the task at hand
tests/goldenscript.py        the golden script framework
```

Unit tests and golden script runners live under `tests/`, which mirrors the
package: the tests for `toydb/x/y.py` are in `tests/x/test_y.py`, and the
end-to-end tests are in `tests/e2e/`. Only the standard library is needed
(Python 3.12 or later).

## Running

A single node runs with `bin/toydb` (configuration in `config/toydb.yaml`,
`-c` selects another file), and a SQL shell connects to it with `bin/toysql`
(`-H`/`-p` select the host and port, default `localhost:9601`). A cluster is
a set of nodes with distinct `id`s, ports and data directories, each listing
the others' Raft addresses under `peers`.

## Tests

Run everything with:

```sh
./check.sh
```

It builds the project (checks that every module compiles and imports), runs
the unit tests and all golden scripts (including the end-to-end cluster
scripts), prints a short summary, and exits non-zero if anything fails. Pass
`-v` for the full test output.

The tests can also be run directly:

```sh
python3 run_goldenscripts.py                                   # all golden scripts
python3 run_goldenscripts.py tests/sql                         # all SQL golden scripts
python3 run_goldenscripts.py tests/sql/testscripts/queries/limit  # a single golden script
python3 run_goldenscripts.py tests/e2e                         # all end-to-end golden scripts
python3 run_goldenscripts.py tests/e2e/scripts/errors          # a single end-to-end golden script
python3 -m unittest discover -s tests -t .                     # all unit tests
python3 -m unittest tests.raft.test_node                       # the tests of one module
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
result is shown (e.g. `[plan]` also prints the query plan, `[json]` prints
the result's JSON form instead of the normal output), `!` in front of a
command expects it to fail, a `name:` prefix selects e.g. a session or a
client, and `#` starts a comment. See the runner for the commands and tags it
supports.

| Scripts | Runner | Runs |
|---|---|---|
| `tests/sql/testscripts/{ext,optimizers,queries,schema,transactions,writes,task}/` | `test_goldenscript` in `tests/sql/test_mod.py` | SQL statements against a SQL engine |
| `tests/sql/testscripts/expressions/` | `test_goldenscript_expr` in `tests/sql/test_mod.py` | SQL expressions |
| `tests/storage/testscripts/mvcc/` | `tests/storage/test_mvcc.py` | MVCC transactions |
| `tests/storage/testscripts/engine/` | `tests/storage/test_memory.py` and `tests/storage/test_bitcask.py` | storage engine operations, once per engine |
| `tests/storage/testscripts/{memory,bitcask}/` | `tests/storage/test_memory.py`, `tests/storage/test_bitcask.py` | engine-specific operations |
| `tests/raft/testscripts/log/` | `tests/raft/test_log.py` | Raft log operations |
| `tests/raft/testscripts/node/` | `tests/raft/test_node.py` | simulated Raft clusters (in memory, deterministic) |
| `tests/e2e/scripts/` (including `ext/` and `task/`) | `test_goldenscript` in `tests/e2e/test_tests.py` | SQL clients against a real server cluster |

Every file in a script directory is a script, and its file name must be a
snake_case identifier (e.g. `group_by`, without an extension).

The expected output is not written by hand. To add or change a script, write
the commands and regenerate the output, then review the result:

```sh
UPDATE_GOLDENFILES=1 python3 run_goldenscripts.py tests/sql
UPDATE_GOLDENFILES=1 python3 run_goldenscripts.py tests/e2e
```

Scripts are discovered at run time from the directories listed in `REGISTRY`
in `run_goldenscripts.py`, so a new script is picked up by the next run.

### End-to-end scripts

The scripts in `tests/e2e/scripts/` start a real cluster of `bin/toydb`
server processes on localhost and talk to it through the client library:

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
the column names, `[json]` the result's JSON form), `status` shows the server
status, `tables` lists the tables, `table NAME` shows a table schema
(`raw=true` for the debug form), and `close` closes the client. The clusters are stopped and their data removed at
the end of each script. Test clusters take their ports (from 19601 for SQL and
19701 for Raft, shifted by a multiple of 200 when those are in use) through
lock files in `/tmp`, so test runs in different copies of the project can run
at the same time.

### Task scripts

Two directories are for golden scripts that belong to the task at hand. Both
start out empty, and scripts placed there are part of `./check.sh` and
`python3 run_goldenscripts.py`:

* `tests/sql/testscripts/task/`: SQL golden scripts, run by the ordinary SQL
  runner (`test_goldenscript` in `tests/sql/test_mod.py`).
* `tests/e2e/scripts/task/`: end-to-end golden scripts, run against a real
  cluster by the end-to-end runner (`test_goldenscript` in
  `tests/e2e/test_tests.py`).
