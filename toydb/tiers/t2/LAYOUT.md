# Tier 2 layout

Tier 2 is the full toydb codebase: tier 1 (encoding, storage, the SQL layer on
the local MVCC engine) plus `raft/`, `server.rs`, `client.rs`, `bin/`
(`toydb`, `toysql`, `toydump`, `workload`), `sql/engine/raft.rs`, and the
end-to-end tests that run real server clusters (`tests/`).

```
tiers/t2/
  rust/       Rust workspace (copy of ../../rust without .git, warm target/)
  python/     Python workspace (self-contained, from ../../python)
  grade.py    grader for both workspaces
  LAYOUT.md   this file
```

## Workspaces

| | Rust (`rust/`) | Python (`python/`) |
|---|---|---|
| Application code | `src/**/*.rs` (outside `#[cfg(test)] mod`) | `toydb/**/*.py` (+ launchers `bin/{toydb,toysql,toydump,workload}`) |
| Unit tests | `#[cfg(test)] mod tests` at the end of each source file | `tests/**/test_*.py`, mirroring `toydb/` |
| End-to-end tests | `tests/tests.rs` (runner), `tests/testcluster.rs` (clusters of the built `toydb` binary) | `tests/e2e/test_tests.py`, `tests/e2e/testcluster.py` (clusters of `bin/toydb`) |
| Golden script framework | `goldenscript` crate (dev-dependency) | `tests/goldenscript.py` (+ `tests/test_goldenscript.py`, `tests/goldenscript_testdata/`) |
| SQL golden scripts (93) | `src/sql/testscripts/{expressions,optimizers,queries,schema,transactions,writes}/` | `tests/sql/testscripts/<same>/` |
| Storage golden scripts (33 files, 37 runs) | `src/storage/testscripts/{bitcask,engine,memory,mvcc}/` | `tests/storage/testscripts/<same>/` |
| Raft golden scripts (68) | `src/raft/testscripts/{log,node}/` | `tests/raft/testscripts/<same>/` |
| Cluster (end-to-end) golden scripts (5) | `tests/scripts/` | `tests/e2e/scripts/` |
| Script runners | test modules of `src/sql/mod.rs`, `src/storage/{memory,bitcask,mvcc}.rs`, `src/raft/{log,node}.rs`; `tests/tests.rs` | `tests/sql/test_mod.py`, `tests/storage/test_*.py`, `tests/raft/test_{log,node}.py`, `tests/e2e/test_tests.py` |
| Script registry | `test_each_path!` lines in the runners (compile time) | `REGISTRY` in `run_goldenscripts.py` (run time) |
| **SQL task script dir** | `src/sql/testscripts/task/` | `tests/sql/testscripts/task/` |
| **Cluster task script dir** | `tests/scripts/task/` | `tests/e2e/scripts/task/` |
| Run everything | `./check.sh` (`cargo build --all-targets`, `cargo test`) | `./check.sh` (compile/import check, `run_goldenscripts.py`, `unittest discover`) |
| Server config | `config/toydb.yaml`, `cluster/` (5-node `run.sh`) | `config/toydb.yaml` |
| Docs | `README.md`, `docs/` (upstream architecture guide, SQL reference) | `README.md` |
| Baseline | 203 golden runs + 93 unit tests | 203 golden runs + 122 unit tests |

The 199 golden script files (93 SQL + 33 storage + 68 Raft + 5 cluster) are
byte-identical in both workspaces and to upstream; the 4
`storage/testscripts/engine` scripts run once per engine (hence 203 runs).

The Rust sources are byte-identical to upstream (`../../rust`, commit 2d7cd32)
except:

* `README.md` (rewritten like tier 1's), `check.sh` (new), and the two
  (empty) task directories;
* `src/sql/mod.rs`: one added line,
  `test_each_path! { in "src/sql/testscripts/task" as task => test_goldenscript }`
  (as in tier 1);
* `tests/testcluster.rs`: per-run port allocation (port slots, see
  "Concurrency") and the node readiness timeout raised from 5 s to 30 s.
  Neither is visible to scripts (no output contains ports).

The Python workspace is `../../python` without `PORT_REPORT.md` and
`__pycache__`, plus `LICENSE`, `.gitignore`, `README.md`, `check.sh`,
`config/toydb.yaml` (upstream's, one comment line changed), the golden
scripts, and the tier-1 changes to the test files (scripts read from
`tests/**/testscripts`, the goldenscript crate's own scripts kept in
`tests/goldenscript_testdata`, `UPDATE_GOLDENFILES=1` support in
`tests/goldenscript.py`, the SQL task dir in `tests/sql/test_mod.py`). For
tier 2 additionally: `tests/raft/test_{log,node}.py` read
`tests/raft/testscripts`; `tests/e2e/test_tests.py` reads `tests/e2e/scripts`
including subdirectories; `tests/e2e/testcluster.py` gets the same port slots
and 30 s timeout as the Rust one; `run_goldenscripts.py` is tier 1's with the
Raft, cluster and cluster-task directories registered (and registered
subdirectories of registered directories, i.e. `tests/e2e/scripts/task`, run
along with them). No file refers to anything outside the workspace.
`toydb/` is unchanged from the port.

`check.sh` prints `build: ok`, `golden scripts: P/T passed`,
`unit tests: P/T passed` and `check: OK` or `check: FAILED`, and exits non-zero
on any failure (`-v` prints the full test output). The golden count includes
the Raft, cluster and task scripts. The Rust one exports
`PATH="$HOME/.cargo/bin:$PATH"` and `CARGO_TARGET_DIR="$PWD/target"`, and
counts every test of the `tests/tests.rs` target as a golden script run (the
others by name, as in tier 1 plus `raft::*::tests::scripts::`). `rust/target/`
is warm (1.9 GB).

Warm `check.sh` wall time, measured on this machine (10 cores, background load
average ~10 from other jobs): Rust 17-20 s, Python 23-25 s; at load ~24 both
take up to ~35 s. Where the time goes: Rust recompiles the crate (`check.sh`
touches the runners, about 6-10 s) and then runs the 5 cluster scripts one at
a time (7 s); Python runs the cluster scripts twice, once through
`run_goldenscripts.py` and again as the `tests.e2e.test_tests` unit test (as
tier 1 does for all golden scripts), about 10 s each. A first build of a Rust
copy at a new path takes about 40-60 s (the crate is recompiled, the
dependencies are not).

## Task script directories

Both workspaces have two (initially empty) directories for task-specific
golden scripts:

* SQL scripts, run by the ordinary SQL runner (`test_goldenscript`, the one
  that runs `queries/`, `schema/`, ...), exactly as in tier 1:
  * Rust: `rust/src/sql/testscripts/task/`, registered in `src/sql/mod.rs` by
    `test_each_path! { in "src/sql/testscripts/task" as task => test_goldenscript }`.
    Tests are named `sql::tests::task::<name>`.
  * Python: `python/tests/sql/testscripts/task/`, registered in `REGISTRY`
    in `run_goldenscripts.py` and in the directory list of
    `tests/sql/test_mod.py`.
* Cluster (end-to-end) scripts in the format of upstream `tests/scripts/`
  (`cluster nodes=N`, client prefixes `c1:`, `status`, `table`, `tables`,
  `close`, `> SQL`), run by the end-to-end runner against a real cluster of
  server processes through the client library:
  * Rust: `rust/tests/scripts/task/`. No registration needed: upstream's
    `test_each_path! { in "tests/scripts" => test_goldenscript }` in
    `tests/tests.rs` recurses into subdirectories (test_each_file 0.3.7 turns
    each into a module), so the scripts are tests `task::<name>` of the
    `tests` target (`cargo test --test tests task::`). An empty directory
    yields an empty module and no warning.
  * Python: `python/tests/e2e/scripts/task/`, registered in `REGISTRY` in
    `run_goldenscripts.py`; the `tests.e2e.test_tests` unit test walks
    `tests/e2e/scripts` recursively.

Both empty and non-empty task dirs work in both languages (verified). Script
file names must be snake_case identifiers without extension (the Rust macro
turns them into test function names). Notes:

* The directories may be empty, but in Rust `src/sql/testscripts/task` must
  exist (the macro reads it at compile time). `check.sh` and `grade.py`
  create both if missing; an empty directory is lost by tools that don't track
  empty directories (e.g. git), so copy workspaces with `cp -a`/`cp -r`, or
  run `check.sh` first.
* Rust discovers scripts at compile time, so `src/sql/mod.rs` and
  `tests/tests.rs` must be recompiled after adding or removing a script.
  `check.sh` and `grade.py` `touch` both; a bare `cargo test` does not notice
  a new script. Python discovers scripts at run time.
* `UPDATE_GOLDENFILES=1` rewrites the expected output in both
  (`cargo test sql::tests` / `cargo test --test tests`;
  `python3 run_goldenscripts.py tests/sql` / `tests/e2e`). `grade.py` unsets
  it.

## grade.py

```
python3 grade.py <rust|python> <workspace> <scripts_dir> [--json out.json] [--log grade.log] [--timeout SECS]
```

Standard library only; `<workspace>` may be a copy of `rust/` or `python/`
anywhere on disk. `<scripts_dir>` contains a `sql/` and/or a `cluster/`
subdirectory (anything else, other than dotfiles, is a usage error); script
names must be snake_case identifiers and unique across both. An empty
`<scripts_dir>` (or empty subdirectories) is allowed and grades just the
regression suite. It

1. snapshots the given scripts and moves both of the workspace's task dirs
   aside, so scripts the agent left there are neither graded nor counted;
2. builds (Rust: `cargo test --no-run` with the target dir in the workspace,
   after touching `src/sql/mod.rs` and `tests/tests.rs`; Python: compile and
   import every module);
3. runs the `sql/` scripts from the SQL task dir with the SQL runner and the
   `cluster/` scripts from the cluster task dir with the end-to-end runner,
   and the full regression suite (all other golden scripts, including the
   Raft and cluster scripts, plus the unit tests). Rust runs everything in one
   `cargo test --no-fail-fast`; Python runs the regression suite with empty
   task dirs, then `run_goldenscripts.py` on the two task dirs;
4. restores both task dirs, also on failure, Ctrl-C or SIGTERM (the running
   command's process group, including any test cluster servers, is killed).

Output (stdout, and the same data as JSON with `--json`): the tier-1 fields
`build_ok`, per-script `pass`/`fail` keyed by name (`scripts`,
`scripts_passed`/`scripts_total`), `regression` (`passed`/`total`, split into
`golden_*` and `unit_*`, plus the names of `failed` tests; Rust cluster
scripts appear as `tests::<name>`), `tests_completed` (false on a crash or
timeout, in Rust also if a test target did not report its result), `times`
(`build_s`, `test_s`, `total_s`), `ok`, and `log`; plus `script_kinds`
(`sql` or `cluster` per script). The log (default `<json>.log`, else
`./grade.log`) holds the complete tool output including the diff of each
failing script. Exit status: 0 if the build is ok, every given script passes
and nothing regressed; 1 otherwise; 2 on usage errors.

A script that fails to build in Rust makes all given scripts `fail` with
`build_ok: false`. Regression totals with a clean workspace: Rust 296 (203
golden + 93 unit), Python 325 (203 golden + 122 unit). A grade takes about
20-45 s (Rust, warm) and 21-47 s (Python) depending on machine load.

Verified: `check.sh` passes on both clean workspaces and on `cp -a` copies
elsewhere; `grade.py` with an empty scripts dir reports 296/296 and 325/325;
a trivial `sql/` and a trivial `cluster/` script pass in both languages; a
deliberately wrong one of each fails (with the diff in the log) without
affecting the regression totals; scripts left in the task dirs are restored
and not graded; a broken message in `error.rs`/`error.py` shows up as
regressions; a syntax error gives `build_ok: false`.

## Concurrency

Upstream's test clusters use fixed ports (SQL 19600+id, Raft 19700+id, on
`localhost`), serialized only within one test process. Two runs in different
workspace copies at the same time therefore clash: a node that cannot bind
its port exits, and the other run's nodes answer instead. Demonstrated with
two concurrent runs of the upstream Python port's cluster scripts: 7 of 10
script runs failed ("table test already exists", EOF on the connection, ...).
The Rust tests have the same fixed ports.

Fixed in both workspaces by **port slots** in `testcluster.{rs,py}`: a
cluster takes the first of 32 slots it can lock with `flock` on
`/tmp/toydb-testcluster-port-slot-<n>.lock` (falling back to a read-only open,
then to the temporary directory) and whose ports are not in use on any
address `localhost` resolves to (so leftover servers from killed runs are
skipped). Slot n uses ports 19600/19700 + 200n + id (slot 0 is upstream's
ports; at most 99 nodes per cluster; all ports stay below the ephemeral range,
at most 25999). The lock is held until the cluster is dropped or closed, and
is released by the kernel if the process dies. Rust and Python use the same
lock files, so runs of both languages cooperate. Scripts see no difference.

The readiness timeout (all nodes answering a status request) was raised from
5 s to 30 s in both, because Python 5-node clusters on a heavily loaded
machine sometimes took longer to elect a leader than 5 s (one unit-test
failure, `operation aborted`, in the first concurrency run below; none
after the change).

Verified: 4 grades + 2 `check.sh` runs (2 Rust grades, 2 Python grades, one
`check.sh` each, all in different `cp -a` copies) and then 5 grades +
2 `check.sh` concurrently at load average ~24: every result correct and
identical to the sequential results; a listener squatting on [::1]:19601
makes clusters use slot 1 instead.

Other shared state: none that clashes. Each Rust copy has its own target dir
(the cluster tests build the `toydb` binary into it with escargot); Cargo
only shares `~/.cargo` (read-only, locked briefly). Test data lives in fresh
temporary directories, and Python runs with `PYTHONDONTWRITEBYTECODE=1`.
Two grades (or a grade and `check.sh`) of the **same** workspace must not
run at the same time: they swap the same task dirs (and in Rust share the
target dir lock).

## Line counts

Measured as in tier 1. Non-blank, non-comment lines. Rust: `//`, `///` and
`//!` lines are excluded, and "test" is everything from a file's
`#[cfg(test)] mod` to its end, plus all of `tests/*.rs`. Python: `#` comments
and docstrings are excluded; the count including docstrings is given in
parentheses.

| | Rust | Python |
|---|---|---|
| Application code | 8534 (`src/`, outside test modules) | 11269 (14581) in `toydb/` + 24 (28) `bin/` launchers; of the former, 1212 are stand-ins for Rust std/crates (tier 1's 751 in `fmt.py`, `iter.py`, `ops.py`, `encoding/serde.py`, `storage/btree.py`, plus `config.py`, `net.py`, `simplelog.py`) |
| Test code | 2593: 2303 test modules in `src/` + 290 `tests/*.rs` (upstream: 227) | 8858 (9620) in `tests/` + 136 (167) `run_goldenscripts.py` |
| - of which end-to-end | 290 (`tests/tests.rs`, `tests/testcluster.rs`) | 306 (390) `tests/e2e/` (port: 225) |
| - of which golden script framework | 0 (the `goldenscript` crate) | 1251: `tests/goldenscript.py` 761 + `tests/test_goldenscript.py` 490 |
| Golden scripts | 199 files, 17629 lines | the same 199 files |
| `check.sh` | 57 lines | 71 lines |

The Python application code is larger mainly because serde/bincode derives,
`Debug` formatting and enum variant classes are written out by hand, and the
test code because of generated unit tests without a Rust counterpart and the
ported goldenscript framework (as in tier 1).
