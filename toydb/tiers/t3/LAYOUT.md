# Tier 3 layout

Tier 3 is tier 2 (the full toydb: SQL engine on the local MVCC engine, Raft,
server, client, binaries, end-to-end cluster tests) plus four **extension
layers** that consume the core types: JSON output, statement digests, column
statistics, and export/import. They are specified, language-neutrally, in
`EXT_SPEC.md` (sections 1-7 are the user-facing behaviour, section 8 the
implementation requirements); `EXT_AMENDMENTS.md` records what the
reconciliation of the two implementations changed.

```
tiers/t3/
  rust/              Rust workspace (tier 2's + the extensions, warm target/)
  python/            Python workspace (tier 2's + the extensions)
  ext_scripts/       the 32 extension golden scripts (sql/ 28, cluster/ 4), as installed in both workspaces
  EXT_SPEC.md        extension specification
  EXT_AMENDMENTS.md  reconciliation record (script, spec and implementation changes)
  EXT_DESIGN_NOTES.md  experimenter-only design notes (per-task extension sites)
  grade.py           grader for both workspaces
  LAYOUT.md          this file
```

## Workspaces

| | Rust (`rust/`) | Python (`python/`) |
|---|---|---|
| Application code | `src/**/*.rs` (outside `#[cfg(test)] mod`) | `toydb/**/*.py` (+ launchers `bin/{toydb,toysql,toydump,workload}`) |
| Unit tests | `#[cfg(test)] mod tests` at the end of each source file | `tests/**/test_*.py`, mirroring `toydb/` |
| End-to-end tests | `tests/tests.rs` (runner), `tests/testcluster.rs` (clusters of the built `toydb` binary) | `tests/e2e/test_tests.py`, `tests/e2e/testcluster.py` (clusters of `bin/toydb`) |
| Golden script framework | `goldenscript` crate (dev-dependency) | `tests/goldenscript.py` (+ `tests/test_goldenscript.py`, `tests/goldenscript_testdata/`) |
| SQL golden scripts (93 + 28) | `src/sql/testscripts/{expressions,optimizers,queries,schema,transactions,writes}/` + **`ext/`** | `tests/sql/testscripts/<same>/` |
| Storage golden scripts (33 files, 37 runs) | `src/storage/testscripts/{bitcask,engine,memory,mvcc}/` | `tests/storage/testscripts/<same>/` |
| Raft golden scripts (68) | `src/raft/testscripts/{log,node}/` | `tests/raft/testscripts/<same>/` |
| Cluster (end-to-end) golden scripts (5 + 4) | `tests/scripts/` + **`tests/scripts/ext/`** | `tests/e2e/scripts/` + **`tests/e2e/scripts/ext/`** |
| Script runners | test modules of `src/sql/mod.rs`, `src/storage/{memory,bitcask,mvcc}.rs`, `src/raft/{log,node}.rs`; `tests/tests.rs` | `tests/sql/test_mod.py`, `tests/storage/test_*.py`, `tests/raft/test_{log,node}.py`, `tests/e2e/test_tests.py` |
| Script registry | `test_each_path!` lines in the runners (compile time) | `REGISTRY` in `run_goldenscripts.py` (run time) |
| **SQL task script dir** | `src/sql/testscripts/task/` | `tests/sql/testscripts/task/` |
| **Cluster task script dir** | `tests/scripts/task/` | `tests/e2e/scripts/task/` |
| Run everything | `./check.sh` (`cargo build --all-targets`, `cargo test`) | `./check.sh` (compile/import check, `run_goldenscripts.py`, `unittest discover`) |
| Server config | `config/toydb.yaml`, `cluster/` (5-node `run.sh`) | `config/toydb.yaml` |
| Docs | `README.md` (with an "Extensions" section), `docs/` (upstream architecture guide, SQL reference) | `README.md` (with an "Extensions" section) |
| Baseline | 235 golden runs + 244 unit tests | 235 golden runs + 171 unit tests |

The 231 golden script files (199 of tier 2 + 32 extension scripts) are
byte-identical in both workspaces; the 199 are byte-identical to tier 2 and
upstream, the 32 to `ext_scripts/`. 235 runs = tier 2's 203 + 28 SQL + 4
cluster extension scripts.

### Where the extensions live

| Extension | Rust | Python |
|---|---|---|
| Shared: keywords, AST, `IS NOT` lowering | `sql/parser/{lexer,parser,ast}.rs` (9 keywords, 5 statements, `Operator::IsNot`, `Statement::desugar`, `format_ident`), `sql/planner/planner.rs` (desugar at the start of `build`) | `sql/parser/{lexer,parser,ast}.py`, `sql/planner/planner.py` (`Planner.desugar`) |
| Shared: session, results | `sql/execution/session.rs` (digest recording, the 5 statements, EXPLAIN rejection, `StatementResult::{Analyze,Export}` appended) | `sql/execution/session.py` (same; hand-written serde variants 10, 11) |
| Shared: value formats | `sql/json.rs::value`, `sql/execution/export.rs::sql_literal`, `sql/execution/import.rs::parse_value` | `sql/types/formats.py` |
| A JSON output | `encoding/json.rs` (writer), `sql/json.rs` (values, results, plans); `[json]` in `sql/mod.rs` and `tests/tests.rs`; `bin/toysql.rs` `!format` | `encoding/json.py` (writer, strict reader), `sql/execution/json.py`; `[json]` in `tests/sql/test_mod.py`, `tests/e2e/test_tests.py`; `bin/toysql.py` |
| B digests | `sql/parser/digest.rs` (canonical text), `sql/execution/digests.rs` (per-session statistics) | `sql/execution/digest.py` (both) |
| C statistics | `sql/types/statistics.rs`, `sql/execution/analyze.rs`; `Key::Statistics` + catalog methods in `sql/engine/{engine,local}.rs`; Raft `Read::GetTableStatistics` / `Write::SetTableStatistics` in `sql/engine/raft.rs`; `encoding/format.rs` | `sql/types/statistics.py`, `sql/execution/analyze.py`; `sql/engine/{engine,local,raft}.py`; `encoding/format.py` |
| D export/import | `encoding/csv.rs`, `sql/execution/{export,import}.rs`; `Table::write_create_table` in `sql/types/schema.rs`; `build_row` in `sql/execution/executor.rs` (shared with INSERT) | `encoding/csv.py`, `sql/execution/{export,importer}.py`; `Table.format` in `sql/types/schema.py`; `Executor.build_row` |

Rust moved `serde_json` from dev-dependencies to dependencies (features
`arbitrary_precision`, `preserve_order`), used only for parsing JSON lines;
JSON output uses the small writer in `encoding/json.rs`. Python uses the
standard `json` module (strict: no `NaN`/`Infinity`, no unpaired surrogates)
with its own compact writer.

The Rust workspace is tier 2's `rust/` plus the extension code and scripts,
`README.md` (extensions documented), and `Cargo.{toml,lock}` (serde_json).
`check.sh`, `tests/testcluster.rs` and the task directories are unchanged from
tier 2. The Python workspace is tier 2's `python/` plus the extension code,
tests and scripts, `README.md`, the `ext` entries in `run_goldenscripts.py`
and `tests/sql/test_mod.py`, and `[json]`/EXPORT output in the two runners;
`check.sh` and `tests/e2e/testcluster.py` are unchanged. Existing unit test
expectations changed only where EXT_SPEC 8.1 allows (Python `test_lexer.py`
keyword count 66 → 75; `test_parser.py` AST dumps of `IS NOT`; the `IS NOT`
case in `test_expression.py`'s plan-building helper). No file refers to
anything outside the workspace.

`check.sh` (both identical to tier 2's) prints `build: ok`,
`golden scripts: P/T passed`, `unit tests: P/T passed` and `check: OK` or
`check: FAILED`, and exits non-zero on any failure (`-v` prints the full test
output). The golden count includes the extension, Raft, cluster and task
scripts. `rust/target/` is warm (about 2 GB).

Warm `check.sh` wall time, measured on this machine (10 cores, background load
average ~10 from other jobs): Rust 22-25 s, Python 35-37 s. Where the time
goes: Rust recompiles the crate (`check.sh` touches the runners) and runs the
9 cluster scripts one at a time; Python runs the 9 cluster scripts twice, once
through `run_goldenscripts.py` and again as the `tests.e2e.test_tests` unit
test. A first build of a Rust copy at a new path takes about 30-60 s (the
crate is recompiled, the dependencies are not).

## Task script directories

As in tier 2, both workspaces have two (initially empty) directories for
task-specific golden scripts:

* SQL scripts, run by the ordinary SQL runner (the one that runs `queries/`,
  `ext/`, ...):
  * Rust: `rust/src/sql/testscripts/task/`, registered in `src/sql/mod.rs` by
    `test_each_path! { in "src/sql/testscripts/task" as task => test_goldenscript }`.
    Tests are named `sql::tests::task::<name>`.
  * Python: `python/tests/sql/testscripts/task/`, registered in `REGISTRY`
    in `run_goldenscripts.py` and in the directory list of
    `tests/sql/test_mod.py`.
* Cluster (end-to-end) scripts in the format of upstream `tests/scripts/`
  (`cluster nodes=N`, client prefixes `c1:`, `status`, `table`, `tables`,
  `close`, `> SQL`, and now the `[json]` tag), run against a real cluster of
  server processes through the client library:
  * Rust: `rust/tests/scripts/task/`. No registration needed:
    `test_each_path! { in "tests/scripts" => test_goldenscript }` in
    `tests/tests.rs` recurses into subdirectories (`ext/` and `task/` become
    modules), so the scripts are tests `task::<name>` of the `tests` target.
  * Python: `python/tests/e2e/scripts/task/`, registered in `REGISTRY` in
    `run_goldenscripts.py`; the `tests.e2e.test_tests` unit test walks
    `tests/e2e/scripts` recursively.

Both runners accept every extension statement and the `[json]` tag in task
scripts too. Script file names must be snake_case identifiers without
extension. Notes (unchanged from tier 2):

* The directories may be empty, but in Rust `src/sql/testscripts/task` must
  exist (the macro reads it at compile time). `check.sh` and `grade.py` create
  both if missing; copy workspaces with `cp -a`/`cp -r` (an empty directory is
  lost by tools that don't track them, e.g. git), or run `check.sh` first.
* Rust discovers scripts at compile time, so `src/sql/mod.rs` and
  `tests/tests.rs` must be recompiled after adding or removing a script.
  `check.sh` and `grade.py` `touch` both; a bare `cargo test` does not notice a
  new script. Python discovers scripts at run time.
* `UPDATE_GOLDENFILES=1` rewrites the expected output in both
  (`cargo test sql::tests` / `cargo test --test tests`;
  `python3 run_goldenscripts.py tests/sql` / `tests/e2e`). `grade.py` unsets
  it.
* Cargo searches parent directories for a workspace manifest: a Rust copy
  placed below a directory that contains another `Cargo.toml` fails to build
  ("failed searching for potential workspace"). The same holds for tier 2.

## grade.py

Tier 2's `grade.py` with the docstring adapted; same CLI and JSON:

```
python3 grade.py <rust|python> <workspace> <scripts_dir> [--json out.json] [--log grade.log] [--timeout SECS]
```

Standard library only; `<workspace>` may be a copy of `rust/` or `python/`
anywhere on disk (see the Cargo note above). `<scripts_dir>` contains a `sql/`
and/or a `cluster/` subdirectory (anything else, other than dotfiles, is a
usage error); script names must be snake_case identifiers and unique across
both. An empty `<scripts_dir>` grades just the regression suite. It

1. snapshots the given scripts and moves both of the workspace's task dirs
   aside, so scripts the agent left there are neither graded nor counted;
2. builds (Rust: `cargo test --no-run` with the target dir in the workspace,
   after touching `src/sql/mod.rs` and `tests/tests.rs`; Python: compile and
   import every module);
3. runs the `sql/` scripts from the SQL task dir with the SQL runner and the
   `cluster/` scripts from the cluster task dir with the end-to-end runner,
   and the full regression suite (all other golden scripts, including the
   extension, Raft and cluster scripts, plus the unit tests). Rust runs
   everything in one `cargo test --no-fail-fast`; Python runs the regression
   suite with empty task dirs, then `run_goldenscripts.py` on the two task
   dirs;
4. restores both task dirs, also on failure, Ctrl-C or SIGTERM (the running
   command's process group, including any test cluster servers, is killed).

Output (stdout, and the same data as JSON with `--json`): `build_ok`,
per-script `pass`/`fail` keyed by name (`scripts`,
`scripts_passed`/`scripts_total`), `script_kinds` (`sql` or `cluster`),
`regression` (`passed`/`total`, split into `golden_*` and `unit_*`, plus the
names of `failed` tests; Rust cluster scripts appear as `tests::<name>`, the
extension ones as `tests::ext::<name>`, the SQL ones as
`sql::tests::ext::<name>`), `tests_completed`, `times` (`build_s`, `test_s`,
`total_s`), `ok`, and `log`. The log (default `<json>.log`, else
`./grade.log`) holds the complete tool output including the diff of each
failing script. Exit status: 0 if the build is ok, every given script passes
and nothing regressed; 1 otherwise; 2 on usage errors.

Regression totals with a clean workspace: **Rust 479 (235 golden + 244
unit), Python 406 (235 golden + 171 unit)**. A grade takes about 24-25 s
(Rust, warm) and 35-39 s (Python) at load ~10.

Verified: `check.sh` passes on both clean workspaces and on `cp -a` copies
elsewhere; `grade.py` with an empty scripts dir reports 479/479 and 406/406;
on the copies, a trivial `sql/` and a trivial `cluster/` script (using
IMPORT, `[json]`, SHOW STATEMENTS, ANALYZE, SHOW STATISTICS) pass in both
languages, a deliberately wrong one of each fails with the diff in the log,
the regression totals are unaffected, and the task dirs are restored.

## Concurrency

Unchanged from tier 2: test clusters take port slots through
`/tmp/toydb-testcluster-port-slot-<n>.lock` (shared by both languages), the
readiness timeout is 30 s, each Rust copy has its own target dir, test data
lives in fresh temporary directories, and Python runs with
`PYTHONDONTWRITEBYTECODE=1`. The extensions add no shared state (statement
digests live in the session, statistics in the database). Two grades (or a
grade and `check.sh`) of the **same** workspace must not run at the same time.
During this reconciliation Rust and Python `check.sh`/`grade.py` runs of
different workspaces ran concurrently many times without interference.

## Line counts

Measured as in tiers 1 and 2. Non-blank, non-comment lines. Rust: `//`, `///`
and `//!` lines are excluded, and "test" is everything from a file's
`#[cfg(test)] mod` to its end, plus all of `tests/*.rs`. Python: `#` comments
and docstrings are excluded; the count including docstrings is given in
parentheses.

| | Rust | Python |
|---|---|---|
| Application code | 10070 (`src/`, outside test modules; tier 2: 8534) | 12675 (16238) in `toydb/` (tier 2: 11269 (14581)) + 24 (28) `bin/` launchers; of the former, 1212 are stand-ins for Rust std/crates, as in tier 2 |
| Test code | 2972: 2670 test modules in `src/` + 302 `tests/*.rs` (tier 2: 2593) | 9794 (10621) in `tests/` + 138 (169) `run_goldenscripts.py` (tier 2: 8858 (9620) + 136 (167)) |
| - of which end-to-end | 302 (`tests/tests.rs`, `tests/testcluster.rs`) | 316 (400) `tests/e2e/` |
| - of which golden script framework | 0 (the `goldenscript` crate) | 1251, unchanged |
| Golden scripts | 231 files, 20090 lines (tier 2: 199 files, 17629 lines) | the same 231 files |
| `check.sh` | 57 lines (unchanged) | 71 lines (unchanged) |

### The extensions alone

| | Rust | Python |
|---|---|---|
| Application code added | **1536** = 1135 in 9 new modules + 401 in 18 existing files | **1406 (1657)** = 1005 (1202) in 9 new modules + 401 (455) in 12 existing files |
| New modules (app) | `encoding/json.rs` 104, `encoding/csv.rs` 48, `sql/json.rs` 250, `sql/parser/digest.rs` 251, `sql/execution/digests.rs` 50, `sql/execution/analyze.rs` 72, `sql/types/statistics.rs` 96, `sql/execution/export.rs` 98, `sql/execution/import.rs` 166 | `encoding/json.py` 45 (68), `encoding/csv.py` 43 (62), `sql/types/formats.py` 65 (86), `sql/types/statistics.py` 106 (130), `sql/execution/json.py` 237 (257), `sql/execution/digest.py` 272 (311), `sql/execution/analyze.py` 59 (68), `sql/execution/export.py` 57 (77), `sql/execution/importer.py` 121 (143) |
| Largest changes to existing files | `parser/ast.rs` +138 (5 statements, `IsNot`, `transform`, `desugar`), `execution/session.rs` +62, `parser/parser.rs` +58, `parser/lexer.rs` +33, `bin/toysql.rs` +27, `engine/raft.rs` +25 | `parser/ast.py` +101, `execution/session.py` +75 (incl. serde of the 2 results), `parser/parser.py` +64, `planner/planner.py` +47, `engine/raft.py` +43 (serde of the 2 commands), `engine/local.py` +24, `bin/toysql.py` +22 |
| Test code added | 379: 354 in the new modules, 13 `sql/mod.rs` (`[json]`, EXPORT lines), 12 `tests/tests.rs` | 936 (1001) in `tests/`: 915 (980) in 10 new test modules, 19 in the two runners, 2 in `test_expression.py`; + 2 `run_goldenscripts.py` |
| Unit tests added | 151 (93 → 244) | 49 (122 → 171) |
| Golden scripts added | 32 files, 2461 lines (`ext_scripts/`) | the same |

Both are well below EXT_SPEC 8.12's estimate (Rust about half of 3,000, Python about 40% of 3,500 lines). The
Python extension code is about as large as the Rust code: its JSON, CSV,
digest and plan writers are as compact as Rust's, and the extra hand-written
serde (2 results, the statistics, 2 Raft commands, the key) is offset by
Rust's longer exhaustive matches (`Statement::desugar`, `Expression::transform`,
the planner's statement list) and type definitions.
