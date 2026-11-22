# Tier 4 layout

Tier 4 is tier 3 (the full toydb plus the four tier-3 extension layers: JSON
output, statement digests, column statistics, export/import) plus seven more
**extension layers** that consume the core types: an SQL formatter, views,
result description, row estimates, prepared statements, statement lint and a
change log. They are specified, language-neutrally, in `EXT4_SPEC.md`;
`EXT4_DOCS.md` fixes their documentation text (identical in both
implementations), and `EXT4_AMENDMENTS.md` records what the reconciliation of
the two implementations changed, including the documentation-parity pass over
all eleven extensions (section 9).

```
tiers/t4/
  rust/                 Rust workspace (tier 3's + the tier-4 extensions, warm target/)
  python/               Python workspace (tier 3's + the tier-4 extensions)
  ext4_scripts/         the 45 tier-4 extension golden scripts, by extension (<ext>/sql/, <ext>/cluster/),
                        as installed in both workspaces' ext4/ directories (38 SQL, 7 cluster)
  EXT4_SPEC.md          tier-4 extension specification
  EXT4_DOCS.md          documentation text of the tier-4 modules (both implementations)
  EXT4_AMENDMENTS.md    reconciliation record (per-site table, script, spec, implementation and doc changes)
  EXT4_DESIGN_NOTES.md  experimenter-only design notes (per-task extension sites)
  grade.py              grader for both workspaces
  LAYOUT.md             this file
```

The tier-3 extension specification (`../t3/EXT_SPEC.md`, `../t3/EXT_AMENDMENTS.md`)
still describes the four tier-3 extensions.

## Workspaces

| | Rust (`rust/`) | Python (`python/`) |
|---|---|---|
| Application code | `src/**/*.rs` (outside `#[cfg(test)] mod`) | `toydb/**/*.py` (+ launchers `bin/{toydb,toysql,toydump,workload}`) |
| Unit tests | `#[cfg(test)] mod tests` at the end of each source file | `tests/**/test_*.py`, mirroring `toydb/` |
| End-to-end tests | `tests/tests.rs` (runner), `tests/testcluster.rs` (clusters of the built `toydb` binary) | `tests/e2e/test_tests.py`, `tests/e2e/testcluster.py` (clusters of `bin/toydb`) |
| Golden script framework | `goldenscript` crate (dev-dependency) | `tests/goldenscript.py` (+ `tests/test_goldenscript.py`, `tests/goldenscript_testdata/`) |
| SQL golden scripts (93 + 28 + 38) | `src/sql/testscripts/{expressions,optimizers,queries,schema,transactions,writes}/` + `ext/` + **`ext4/`** | `tests/sql/testscripts/<same>/` |
| Storage golden scripts (33 files, 37 runs) | `src/storage/testscripts/{bitcask,engine,memory,mvcc}/` | `tests/storage/testscripts/<same>/` |
| Raft golden scripts (68) | `src/raft/testscripts/{log,node}/` | `tests/raft/testscripts/<same>/` |
| Cluster (end-to-end) golden scripts (5 + 4 + 7) | `tests/scripts/` + `tests/scripts/ext/` + **`tests/scripts/ext4/`** | `tests/e2e/scripts/` + `tests/e2e/scripts/ext/` + **`tests/e2e/scripts/ext4/`** |
| Script runners | test modules of `src/sql/mod.rs`, `src/storage/{memory,bitcask,mvcc}.rs`, `src/raft/{log,node}.rs`; `tests/tests.rs` | `tests/sql/test_mod.py`, `tests/storage/test_*.py`, `tests/raft/test_{log,node}.py`, `tests/e2e/test_tests.py` |
| Script registry | `test_each_path!` lines in the runners (compile time; `ext4` added in `src/sql/mod.rs`, `tests/scripts` recurses) | `REGISTRY` in `run_goldenscripts.py` (run time; two `ext4` entries added) and the directory list of `tests/sql/test_mod.py` |
| **SQL task script dir** | `src/sql/testscripts/task/` | `tests/sql/testscripts/task/` |
| **Cluster task script dir** | `tests/scripts/task/` | `tests/e2e/scripts/task/` |
| Run everything | `./check.sh` (`cargo build --all-targets`, `cargo test`) | `./check.sh` (compile/import check, `run_goldenscripts.py`, `unittest discover`) |
| Server config | `config/toydb.yaml`, `cluster/` (5-node `run.sh`) | `config/toydb.yaml` |
| Docs | `README.md` (tier-3 "Extensions" section + the tier-4 section of EXT4_DOCS §8), `docs/` (upstream architecture guide, SQL reference) | `README.md` (the same sections) |
| Baseline | 280 golden runs + 530 unit tests | 280 golden runs + 271 unit tests |

The 276 golden script files (231 of tier 3 + 45 tier-4 extension scripts) are
byte-identical in both workspaces; the 231 are byte-identical to tier 3, the 45
to `ext4_scripts/`. 280 runs = tier 3's 235 + 38 SQL + 7 cluster tier-4
scripts.

The two READMEs have the same content except for language-specific paths and
commands: the tier-3 "Extensions" sections, which tier 3 had written
independently per language, were merged into one text (EXT4_AMENDMENTS §9),
followed by EXT4_DOCS §8 word for word; the layout tables and the runner table
list the `ext4/` directories.

### Where the tier-4 extensions live

New modules (EXT4_DOCS §0) and the existing files they extend:

| Extension | Rust | Python |
|---|---|---|
| Shared: keywords, AST, session | `sql/parser/lexer.rs` (12 keywords, `Token::Parameter`), `sql/parser/{parser,ast}.rs` (12 statements, `Expression::Parameter`), `sql/execution/session.rs` (dispatch, EXPLAIN rejection, `StatementResult::{Format,CreateView,DropView,Prepare,Deallocate}`), `sql/json.rs` (their JSON), `bin/toysql.rs` | `sql/parser/{lexer,parser,ast}.py`, `sql/execution/session.py` (same; hand-written serde of the new results), `sql/execution/json.py`, `bin/toysql.py` |
| format | `sql/parser/format.rs` (shares `precedence`/`is_value` with `sql/parser/digest.rs`) | `sql/parser/format.py` (`precedence`/`format_is`, imported by `sql/execution/digest.py`) |
| views | `sql/types/view.rs`, `sql/execution/views.rs`; catalog methods in `sql/engine/engine.rs`, `Key::View` in `sql/engine/local.rs`, Raft `Write::{CreateView,DropView}` / `Read::{GetView,ListViews}` in `sql/engine/raft.rs`; `Node::View` in `sql/planner/plan.rs`, views in FROM in `sql/planner/planner.rs`; views in `EXPORT AS SQL` (`sql/execution/export.rs`); key display in `encoding/format.rs` | `sql/types/view.py`, `sql/execution/views.py`; `sql/engine/{engine,local,raft}.py` (`KeyView`, the 4 commands with serde); `sql/planner/{plan,planner}.py`; `sql/execution/export.py`; `encoding/format.py` |
| describe | `sql/planner/describe.rs` | `sql/planner/describe.py` |
| estimate | `sql/planner/estimate.rs` | `sql/planner/estimate.py` |
| prepared | `sql/execution/prepared.rs`; parameters in the lexer, parser, AST and planner | `sql/execution/prepared.py`; same |
| lint | `sql/execution/lint.rs` | `sql/execution/lint.py` |
| changes | `sql/engine/changes.rs`; the change log in `sql/engine/local.rs` (recorded by the transaction methods, `Engine::changes` in `engine.rs`), Raft `Read::Changes`; `export::json_line` for row data | `sql/engine/changes.py`; `sql/engine/{engine,local,raft}.py` |

Rust dependencies are unchanged from tier 3 (`Cargo.{toml,lock}` identical).
`check.sh` (both), `tests/testcluster.rs`, `tests/e2e/testcluster.py` and the
task directories are unchanged from tier 3. The runners changed only to print
FORMAT lines like EXPORT lines (`src/sql/mod.rs`, `tests/tests.rs`,
`tests/sql/test_mod.py`, `tests/e2e/test_tests.py`) and to register `ext4/`.
No file refers to anything outside the workspace.

`check.sh` (both identical to tier 3's) prints `build: ok`,
`golden scripts: P/T passed`, `unit tests: P/T passed` and `check: OK` or
`check: FAILED`, and exits non-zero on any failure (`-v` prints the full test
output). The golden count includes the tier-3 and tier-4 extension, Raft,
cluster and task scripts. `rust/target/` is warm (about 2.3 GB).

Warm `check.sh` wall time, measured on this machine (10 cores, background load
average 11-13 from other jobs, briefly 27): **Rust 48-52 s, Python 67-73 s**
(tier 3: 22-25 s and 35-37 s at load ~10). Where the time goes: Rust recompiles
the crate (`check.sh` touches the runners) and runs the 16 cluster scripts one
at a time; Python runs the 16 cluster scripts twice, once through
`run_goldenscripts.py` and again as the `tests.e2e.test_tests` unit test. A
first build of a Rust copy at a new path takes about 45-60 s (the crate is
recompiled, the dependencies are not).

## Task script directories

Unchanged from tier 3: both workspaces have two (initially empty) directories
for task-specific golden scripts:

* SQL scripts, run by the ordinary SQL runner (the one that runs `queries/`,
  `ext/`, `ext4/`, ...):
  * Rust: `rust/src/sql/testscripts/task/`, registered in `src/sql/mod.rs` by
    `test_each_path! { in "src/sql/testscripts/task" as task => test_goldenscript }`.
    Tests are named `sql::tests::task::<name>`.
  * Python: `python/tests/sql/testscripts/task/`, registered in `REGISTRY`
    in `run_goldenscripts.py` and in the directory list of
    `tests/sql/test_mod.py`.
* Cluster (end-to-end) scripts in the format of upstream `tests/scripts/`
  (`cluster nodes=N`, client prefixes `c1:`, `status`, `table`, `tables`,
  `close`, `> SQL`, and the `[json]` tag), run against a real cluster of
  server processes through the client library:
  * Rust: `rust/tests/scripts/task/`. No registration needed:
    `test_each_path! { in "tests/scripts" => test_goldenscript }` in
    `tests/tests.rs` recurses into subdirectories (`ext/`, `ext4/` and `task/`
    become modules), so the scripts are tests `task::<name>` of the `tests`
    target.
  * Python: `python/tests/e2e/scripts/task/`, registered in `REGISTRY` in
    `run_goldenscripts.py`; the `tests.e2e.test_tests` unit test walks
    `tests/e2e/scripts` recursively.

Both runners accept every tier-3 and tier-4 extension statement and the
`[json]` tag in task scripts too. Script file names must be snake_case
identifiers without extension. Notes (unchanged from tiers 2 and 3):

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
  ("failed searching for potential workspace").

## grade.py

Tier 3's `grade.py` with the docstring adapted (tier 4, `ext/` and `ext4/`);
same CLI and JSON:

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
   tier-3 and tier-4 extension, Raft and cluster scripts, plus the unit tests).
   Rust runs everything in one `cargo test --no-fail-fast`; Python runs the
   regression suite with empty task dirs, then `run_goldenscripts.py` on the
   two task dirs;
4. restores both task dirs, also on failure, Ctrl-C or SIGTERM (the running
   command's process group, including any test cluster servers, is killed).

Output (stdout, and the same data as JSON with `--json`): `build_ok`,
per-script `pass`/`fail` keyed by name (`scripts`,
`scripts_passed`/`scripts_total`), `script_kinds` (`sql` or `cluster`),
`regression` (`passed`/`total`, split into `golden_*` and `unit_*`, plus the
names of `failed` tests; Rust cluster scripts appear as `tests::<name>`, the
extension ones as `tests::ext::<name>` and `tests::ext4::<name>`, the SQL ones
as `sql::tests::ext::<name>` and `sql::tests::ext4::<name>`),
`tests_completed`, `times` (`build_s`, `test_s`, `total_s`), `ok`, and `log`.
The log (default `<json>.log`, else `./grade.log`) holds the complete tool
output including the diff of each failing script. Exit status: 0 if the build
is ok, every given script passes and nothing regressed; 1 otherwise; 2 on
usage errors.

Regression totals with a clean workspace: **Rust 810 (280 golden + 530
unit), Python 551 (280 golden + 271 unit)**. A grade takes about 44-46 s
(Rust, warm) and 76-78 s (Python) at load 11-13.

Verified: `check.sh` passes on both clean workspaces and on `cp -a` copies
elsewhere; `grade.py` with an empty scripts dir reports 810/810 and 551/551;
on the copies, a trivial `sql/` script (the README example — IMPORT, ANALYZE,
EXPORT, `[json]` SHOW STATISTICS — plus FORMAT, CREATE VIEW, SHOW VIEWS,
DESCRIBE, ESTIMATE, PREPARE, EXECUTE, SHOW PREPARED, LINT, SHOW CHANGES) and a
trivial `cluster/` script (a view, `[json]` FORMAT and SHOW CHANGES across
three nodes), whose expected output was generated by the Python implementation,
pass in both languages; a deliberately wrong copy of each fails with the diff
in the log; the regression totals are unaffected, and the task dirs are
restored.

## Concurrency

Unchanged from tier 3: test clusters take port slots through
`/tmp/toydb-testcluster-port-slot-<n>.lock` (shared by both languages), the
readiness timeout is 30 s, each Rust copy has its own target dir, test data
lives in fresh temporary directories, and Python runs with
`PYTHONDONTWRITEBYTECODE=1`. The tier-4 extensions add no shared state across
sessions or processes (prepared statements live in the session; views and
statistics in the database; the change log in each server's memory). Two
grades (or a grade and `check.sh`) of the **same** workspace must not run at
the same time.

## Line counts

Measured as in tiers 1-3 (the counter reproduces tier 3's numbers exactly).
Non-blank, non-comment lines. Rust: `//`, `///` and `//!` lines are excluded,
and "test" is everything from a file's `#[cfg(test)] mod` to its end, plus all
of `tests/*.rs`. Python: `#` comments and docstrings are excluded; the count
including docstrings is given in parentheses.

| | Rust | Python |
|---|---|---|
| Application code | **12498** (`src/`, outside test modules; tier 3: 10070) | **14930 (18895)** in `toydb/` (tier 3: 12675 (16238)) + 24 (28) `bin/` launchers; of the former, 1212 are stand-ins for Rust std/crates, as in tiers 2 and 3 |
| Test code | 4307: 4005 test modules in `src/` + 302 `tests/*.rs` (tier 3: 2972) | 11797 (12803) in `tests/` + 140 (171) `run_goldenscripts.py` (tier 3: 9794 (10621) + 138 (169)) |
| - of which end-to-end | 302 (`tests/tests.rs`, `tests/testcluster.rs`) | 316 (400) `tests/e2e/` |
| - of which golden script framework | 0 (the `goldenscript` crate) | 1251, unchanged |
| Golden scripts | 276 files, 23987 lines (tier 3: 231 files, 20090 lines) | the same 276 files |
| `check.sh` | 57 lines (unchanged) | 71 lines (unchanged) |

**The Rust application code is 12498 lines, 7502 lines (37.5%) below the
20k target.** The seven tier-4 extensions add 2428 lines of Rust application
code (and 2255 of Python), about a quarter of the roughly 10k per language
that was planned; reaching 20k would take about three times as much extension
code again.

### The tier-4 extensions alone

| | Rust | Python |
|---|---|---|
| Application code added | **2428** = 1729 in 8 new modules + 699 in 21 existing files | **2255 (2657)** = 1512 (1786) in 8 new modules + 743 (871) in 21 existing files |
| Largest changes to existing files | `parser/ast.rs` +134, `execution/session.rs` +125, `parser/parser.rs` +104, `engine/local.rs` +69, `parser/lexer.rs` +54, `planner/planner.rs` +48, `engine/raft.rs` +42, `parser/digest.rs` +38 | `execution/session.py` +183, `parser/ast.py` +146, `parser/parser.py` +90, `engine/raft.py` +89, `engine/local.py` +85, `parser/lexer.py` +36, `planner/plan.py` +35, `planner/planner.py` +34; `execution/digest.py` -9 (`precedence` moved to `format.py`) |
| Test code added | 1335: 1313 in the new modules, 22 in existing ones | 2003 (2182) in `tests/`: 1551 (1699) in 8 new test modules, 452 (483) in 8 existing ones; + 2 `run_goldenscripts.py` |
| Unit tests added | 286 (244 → 530) | 100 (171 → 271) |
| Golden scripts added | 45 files, 3897 lines (`ext4_scripts/`) | the same |

Per extension (new modules only; the changes to existing files are shared and
listed above):

| Extension | Rust app | Rust test | Python app | Python test | Golden scripts (sql + cluster, lines) |
|---|---|---|---|---|---|
| format | 324 `parser/format.rs` | 90 | 321 (368) `parser/format.py` | 210 (215) | 6 + 1, 534 |
| views | 136 = 19 `types/view.rs` + 117 `execution/views.rs` | 185 | 111 (140) = 30 (42) `types/view.py` + 81 (98) `execution/views.py` | 160 (187) | 7 + 1, 763 |
| describe | 264 `planner/describe.rs` | 228 | 256 (294) `planner/describe.py` | 236 (260) | 5 + 1, 570 |
| estimate | 300 `planner/estimate.rs` | 202 | 263 (301) `planner/estimate.py` | 223 (244) | 5 + 1, 484 |
| prepared | 230 `execution/prepared.rs` | 208 | 147 (181) `execution/prepared.py` | 208 (236) | 5 + 1, 716 |
| lint | 395 `execution/lint.rs` | 157 | 319 (375) `execution/lint.py` | 265 (284) | 5 + 1, 423 |
| changes | 80 `engine/changes.rs` | 243 | 95 (127) `engine/changes.py` | 249 (273) | 5 + 1, 407 |
| **total** | **1729** | **1313** | **1512 (1786)** | **1551 (1699)** | **38 + 7, 3897** |

Python test lines are those of the mirrored test modules (`tests/sql/.../test_<module>.py`;
views: `test_view.py` + `test_views.py`). As at tier 3, the two implementations
are of similar size: the Rust tier-4 application code is 8% larger than the
Python code (2428 vs 2255 lines), the new modules 14% (1729 vs 1512); Python's
extra lines are mostly the hand-written serde of the new keys, results and Raft
commands in existing files.
