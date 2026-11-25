# Tier 5 layout

Tier 5 is tier 4 (the full toydb plus the eleven tier-3 and tier-4 extension
layers: JSON output, statement digests, column statistics, export/import, an
SQL formatter, views, result description, row estimates, prepared statements,
statement lint, a change log) plus nine more **extension layers**: users and
privileges, an audit log, CHECK constraints, row-level security policies, a
PostgreSQL wire protocol, backups, system tables, materialized views and query
profiles. They are specified, language-neutrally, in `EXT5_SPEC.md`;
`EXT5_DOCS.md` fixes their documentation text (identical in both
implementations), and `EXT5_AMENDMENTS.md` records what the reconciliation of
the two implementations changed, including the documentation-parity pass over
all twenty extensions (section 11).

```
tiers/t5/
  rust/                 Rust workspace (tier 4's + the tier-5 extensions, warm target/)
  python/               Python workspace (tier 4's + the tier-5 extensions)
  ext5_scripts/         the 66 tier-5 extension golden scripts, by extension (<ext>/sql/, <ext>/cluster/),
                        as installed in both workspaces' ext5/ directories (57 SQL, 9 cluster)
  EXT5_SPEC.md          tier-5 extension specification
  EXT5_DOCS.md          documentation text of the tier-5 modules (both implementations)
  EXT5_AMENDMENTS.md    reconciliation record (per extension; doc parity; per-site table)
  EXT5_DESIGN_NOTES.md  experimenter-only design notes (per-task extension sites)
  grade.py              grader for both workspaces
  PRERUN_AUDIT.md       pre-run fairness audit of the tasks' hidden_t5 scripts (one amendment)
  prerun_audit_backup/  the hidden_t5 scripts as they were before that audit's amendment
  LAYOUT.md             this file
```

The tier-3 and tier-4 extension specifications (`../t3/EXT_SPEC.md`,
`../t4/EXT4_SPEC.md` and their amendments) still describe the eleven earlier
extensions.

## Workspaces

| | Rust (`rust/`) | Python (`python/`) |
|---|---|---|
| Application code | `src/**/*.rs` (outside `#[cfg(test)] mod`) | `toydb/**/*.py` (+ launchers `bin/{toydb,toysql,toydump,workload,toybackup}`) |
| Unit tests | `#[cfg(test)] mod tests` at the end of each source file | `tests/**/test_*.py`, mirroring `toydb/` |
| End-to-end tests | `tests/tests.rs` (runner), `tests/testcluster.rs` (clusters of the built `toydb` binary) | `tests/e2e/test_tests.py`, `tests/e2e/testcluster.py` (clusters of `bin/toydb`) |
| Golden script framework | `goldenscript` crate (dev-dependency) | `tests/goldenscript.py` (+ `tests/test_goldenscript.py`, `tests/goldenscript_testdata/`) |
| SQL golden scripts (93 + 28 + 38 + 57) | `src/sql/testscripts/{expressions,optimizers,queries,schema,transactions,writes}/` + `ext/` + `ext4/` + **`ext5/`** | `tests/sql/testscripts/<same>/` |
| Storage golden scripts (33 files, 37 runs) | `src/storage/testscripts/{bitcask,engine,memory,mvcc}/` | `tests/storage/testscripts/<same>/` |
| Raft golden scripts (68) | `src/raft/testscripts/{log,node}/` | `tests/raft/testscripts/<same>/` |
| Cluster (end-to-end) golden scripts (5 + 4 + 7 + 9) | `tests/scripts/` + `tests/scripts/ext/` + `tests/scripts/ext4/` + **`tests/scripts/ext5/`** | `tests/e2e/scripts/` + `tests/e2e/scripts/ext/` + `tests/e2e/scripts/ext4/` + **`tests/e2e/scripts/ext5/`** |
| Script runners | test modules of `src/sql/mod.rs`, `src/storage/{memory,bitcask,mvcc}.rs`, `src/raft/{log,node}.rs`; `tests/tests.rs` | `tests/sql/test_mod.py`, `tests/storage/test_*.py`, `tests/raft/test_{log,node}.py`, `tests/e2e/test_tests.py`; the `pg` and backup commands in `tests/pgwire/runner.py`, `tests/backup/runner.py` |
| Script registry | `test_each_path!` lines in the runners (compile time; `ext5` added in `src/sql/mod.rs`, `tests/scripts` recurses) | `REGISTRY` in `run_goldenscripts.py` (run time; two `ext5` entries added) and the directory list of `tests/sql/test_mod.py` |
| **SQL task script dir** | `src/sql/testscripts/task/` | `tests/sql/testscripts/task/` |
| **Cluster task script dir** | `tests/scripts/task/` | `tests/e2e/scripts/task/` |
| Run everything | `./check.sh` (`cargo build --all-targets`, `cargo test`) | `./check.sh` (compile/import check, `run_goldenscripts.py`, `unittest discover`) |
| Server config | `config/toydb.yaml` (now with `listen_pg`), `cluster/` (5-node `run.sh`; PostgreSQL on 9801-9805) | `config/toydb.yaml` (with `listen_pg`) |
| Docs | `README.md` (tier-3, tier-4 and tier-5 extension sections), `docs/` (upstream architecture guide, SQL reference) | `README.md` (the same sections) |
| Baseline | 346 golden runs + 835 unit tests | 346 golden runs + 469 unit tests |

The 342 golden script files (276 of tier 4 + 66 tier-5 extension scripts) are
byte-identical in both workspaces; the 276 are byte-identical to tier 4, the 66
to `ext5_scripts/`. 346 runs = tier 4's 280 + 57 SQL + 9 cluster tier-5
scripts.

The two READMEs have the same content except for language-specific paths and
commands: tier 4's README, followed by EXT5_DOCS §9 word for word (the three
extension sections are byte-identical between the languages); the layout
blocks list the `ext5/` directories, `pgwire/`, `backup/` and `toybackup`, and
the end-to-end section names the runner's tier-5 commands (EXT5_AMENDMENTS 11).

### Where the tier-5 extensions live

New modules (EXT5_DOCS §0) and the existing files they extend (paths below
`src/` and `toydb/`):

| Extension | Rust | Python |
|---|---|---|
| Shared: keywords, AST, session | `sql/parser/lexer.rs` (19 keywords, 106 in all, `Keyword::ALL`), `sql/parser/{parser,ast}.rs` (the new statements, CHECK constraints, `EXPLAIN (ANALYZE)`), `sql/parser/{format,digest}.rs`, `sql/execution/session.rs` (dispatch, privilege check, audit recording, login and current user), `sql/execution/lint.rs`, `sql/json.rs`, `error.rs` (`PermissionDenied`), `bin/toysql.rs` | `sql/parser/{lexer,parser,ast,format}.py`, `sql/execution/{session,digest,lint,json}.py`, `error.py`, `bin/toysql.py` |
| users | `sql/types/user.rs`, `sql/execution/privileges.rs`, `sql/execution/users.rs`; catalog methods in `sql/engine/engine.rs`, `Key::{User,Membership,Grant,Owner}` in `sql/engine/local.rs`, Raft commands in `sql/engine/raft.rs`, key display in `encoding/format.rs`; `Request::Authenticate` in `server.rs`/`client.rs`; ownership through `sql/execution/prepared.rs` | `sql/types/user.py`, `sql/execution/privileges.py`, `sql/execution/users.py`; `sql/engine/{engine,local,raft}.py` (keys and commands with serde), `encoding/format.py`, `server.py`, `client.py` |
| audit | `sql/execution/audit.rs`; the audit log and session list of `sql/engine/{engine,local,raft}.rs` (one per server, shared by its SQL and PostgreSQL connections, `server.rs`) | `sql/execution/audit.py`; same files |
| checks | `sql/types/check.rs`, `sql/execution/checks.rs`; `Key::Checks`, catalog methods, Raft commands; CREATE TABLE in `sql/planner/{plan,planner}.rs`, enforcement in `sql/execution/executor.rs`; `EXPORT AS SQL` (`sql/execution/export.rs`) | `sql/types/check.py`, `sql/execution/checks.py`; `sql/engine/*.py`, `sql/planner/{plan,planner}.py`, `sql/execution/{executor,export}.py` |
| policies | `sql/types/policy.rs`, `sql/execution/policies.rs`; `Key::Policy`, catalog methods, Raft commands; row filtering and WITH CHECK in `sql/execution/executor.rs` | `sql/types/policy.py`, `sql/execution/policies.py`; `sql/engine/*.py`, `sql/execution/executor.py` |
| pgwire | `pgwire/{mod,message,types,connection,client}.rs`, `encoding/md5.rs`; `listen_pg` in `bin/toydb.rs` and `server.rs`; `toysql --pg` (`bin/toysql.rs`); the `pg` runner commands in `sql/mod.rs` (tests) and `tests/tests.rs` | `pgwire/{__init__,message,types,connection,client}.py` (MD5 from `hashlib`); `bin/toydb.py`, `server.py`, `bin/toysql.py`; `tests/pgwire/runner.py` |
| backup | `backup/{mod,format}.rs`, `bin/toybackup.rs`; `Request::{Backup,Restore}` in `server.rs`/`client.rs`, `Session::{backup,restore}` | `backup/{__init__,format}.py`, `bin/toybackup.py` (+ launcher `bin/toybackup`); `server.py`, `client.py`, `sql/execution/session.py`; `tests/backup/runner.py` |
| catalog | `sql/execution/system.rs`; `Node::SystemTable` in `sql/planner/plan.rs`, system tables in FROM in `sql/planner/planner.rs`, `sql/planner/{describe,estimate}.rs`, `sql/execution/executor.rs`; `DataType::ALL` (`sql/types/value.rs`) | `sql/execution/system.py`; `sql/planner/{plan,planner,describe,estimate}.py`, `sql/execution/executor.py` |
| matviews | `sql/types/matview.rs`, `sql/execution/matviews.rs`; `Key::{MaterializedView,MaterializedViewRow}`, catalog methods, Raft commands; `Node::MaterializedView`; prepared statements, views, EXPORT, backup records | `sql/types/matview.py`, `sql/execution/matviews.py`; same files |
| profile | `sql/execution/profile.rs`; the profiling mode of `sql/execution/executor.rs` (`execute_node`/`node_rows`) | `sql/execution/profile.py`; `sql/execution/executor.py` |

Rust dependencies are unchanged from tier 4 (`Cargo.{toml,lock}` identical;
MD5 is `encoding/md5.rs`). Python uses only the standard library. `check.sh`
(both), `tests/testcluster.rs`'s and `tests/e2e/testcluster.py`'s slot logic and
the task directories are unchanged from tier 4; the test clusters also give
each node a PostgreSQL port (below), and the runners register `ext5/` and gained
the `login`, `pg`, `backup`, `restore` and `backup-*` commands. No file refers
to anything outside the workspace.

`check.sh` (both identical to tier 4's) prints `build: ok`,
`golden scripts: P/T passed`, `unit tests: P/T passed` and `check: OK` or
`check: FAILED`, and exits non-zero on any failure (`-v` prints the full test
output). The golden count includes the tier-3, tier-4 and tier-5 extension,
Raft, cluster and task scripts. `rust/target/` is warm (about 3.3 GB).

Warm `check.sh` wall time, measured on this machine (10 cores, background load
average 4-22 from other jobs): **Rust 49-85 s** (49 s at load 4-10, 67 s at
12-15, 85 s at 10-14 just after a doc-comment edit), **Python 99-147 s** (99 s
at load ~10, 104 s at 6-13, 147 s at 14-22) (tier 4: 48-52 s and 67-73 s at
load 11-13). As at tier 4, Rust recompiles the crate (`check.sh` touches the
runners) and runs the 25 cluster scripts one at a time; Python runs the 25
cluster scripts twice, once through `run_goldenscripts.py` and again as the
`tests.e2e.test_tests` unit test. A first build of a `cp -a` Rust copy at a
new path took 24 s.

## Task script directories

Unchanged from tiers 2-4: both workspaces have two (initially empty)
directories for task-specific golden scripts:

* SQL scripts, run by the ordinary SQL runner (the one that runs `queries/`,
  `ext/`, `ext4/`, `ext5/`, ...):
  * Rust: `rust/src/sql/testscripts/task/`, registered in `src/sql/mod.rs` by
    `test_each_path! { in "src/sql/testscripts/task" as task => test_goldenscript }`.
    Tests are named `sql::tests::task::<name>`.
  * Python: `python/tests/sql/testscripts/task/`, registered in `REGISTRY`
    in `run_goldenscripts.py` and in the directory list of
    `tests/sql/test_mod.py`.
* Cluster (end-to-end) scripts in the format of upstream `tests/scripts/`
  (`cluster nodes=N`, client prefixes `c1:`, `status`, `table`, `tables`,
  `close`, `> SQL`, the `[json]` tag, and the tier-5 `login`, `pg`, `backup`,
  `restore` and `backup-*` commands), run against a real cluster of server
  processes through the client library:
  * Rust: `rust/tests/scripts/task/`. No registration needed:
    `test_each_path! { in "tests/scripts" => test_goldenscript }` in
    `tests/tests.rs` recurses into subdirectories (`ext/`, `ext4/`, `ext5/` and
    `task/` become modules), so the scripts are tests `task::<name>` of the
    `tests` target.
  * Python: `python/tests/e2e/scripts/task/`, registered in `REGISTRY` in
    `run_goldenscripts.py`; the `tests.e2e.test_tests` unit test walks
    `tests/e2e/scripts` recursively.

Both runners accept every tier-3, tier-4 and tier-5 extension statement and
command in task scripts too. Script file names must be snake_case identifiers
without extension. Notes (unchanged from tiers 2-4):

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

Tier 4's `grade.py` with the docstring adapted (tier 5, `ext/`, `ext4/` and
`ext5/`); same CLI and JSON:

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
   tier-3, tier-4 and tier-5 extension, Raft and cluster scripts, plus the
   unit tests). Rust runs everything in one `cargo test --no-fail-fast`;
   Python runs the regression suite with empty task dirs, then
   `run_goldenscripts.py` on the two task dirs;
4. restores both task dirs, also on failure, Ctrl-C or SIGTERM (the running
   command's process group, including any test cluster servers, is killed).

Output (stdout, and the same data as JSON with `--json`): `build_ok`,
per-script `pass`/`fail` keyed by name (`scripts`,
`scripts_passed`/`scripts_total`), `script_kinds` (`sql` or `cluster`),
`regression` (`passed`/`total`, split into `golden_*` and `unit_*`, plus the
names of `failed` tests; Rust cluster scripts appear as `tests::<name>`, the
extension ones as `tests::ext::<name>`, `tests::ext4::<name>` and
`tests::ext5::<name>`, the SQL ones as `sql::tests::ext5::<name>` etc.),
`tests_completed`, `times` (`build_s`, `test_s`, `total_s`), `ok`, and `log`.
The log (default `<json>.log`, else `./grade.log`) holds the complete tool
output including the diff of each failing script. Exit status: 0 if the build
is ok, every given script passes and nothing regressed; 1 otherwise; 2 on
usage errors.

Regression totals with a clean workspace: **Rust 1181 (346 golden + 835
unit), Python 815 (346 golden + 469 unit)**. A grade with an empty scripts dir
took 52 s (Rust, warm) and 103 s (Python) at load 4-12; with four task scripts
on a `cp -a` copy, 91 s and 98 s.

Verified: `check.sh` passes on both clean workspaces and on `cp -a` copies
elsewhere; `grade.py` with an empty scripts dir reports 1181/1181 and 815/815;
on the copies, a trivial `sql/` script (CHECK, CREATE USER, GRANT, CREATE
POLICY, CREATE MATERIALIZED VIEW, `toydb_tables`, EXPLAIN (ANALYZE), SHOW
USERS, SHOW GRANTS, SET ROLE, RESET ROLE, REFRESH, `[json]` `toydb_policies`)
and a trivial `cluster/` script (`login`, `backup`, `backup-list`, a `pg`
startup and query as a non-admin user, `restore`),
whose expected output was generated by the Python implementation, pass in both
languages; a deliberately wrong copy of each fails with the diff in the log;
the regression totals are unaffected, and the task dirs are restored.

## Concurrency

As in tiers 3 and 4: test clusters take port slots through
`/tmp/toydb-testcluster-port-slot-<n>.lock` (shared by both languages), the
readiness timeout is 30 s, each Rust copy has its own target dir, test data
lives in fresh temporary directories, and Python runs with
`PYTHONDONTWRITEBYTECODE=1`. New in tier 5: each test cluster node also gets a
PostgreSQL port in its slot (base 26000 in Rust, 29600 in Python, + slot ×
200 + node id, above every slot's SQL and Raft ports), and the backup commands
of the runners write their files to fresh temporary directories. The audit log
and session list are in each server's memory; users, grants, policies, CHECK
constraints and materialized views live in the database. Two grades (or a grade
and `check.sh`) of the **same** workspace must not run at the same time.

## Line counts

Measured as in tiers 1-4 (the counter reproduces tier 4's numbers exactly).
Non-blank, non-comment lines. Rust: `//`, `///` and `//!` lines are excluded,
and "test" is everything from a file's `#[cfg(test)] mod` to its end, plus all
of `tests/*.rs`. Python: `#` comments and docstrings are excluded; the count
including docstrings is given in parentheses.

| | Rust | Python |
|---|---|---|
| Application code | **19233** (`src/`, outside test modules; tier 4: 12498) | **20709 (25657)** in `toydb/` (tier 4: 14930 (18895)) + 30 (35) `bin/` launchers; of the former, 1212 are stand-ins for Rust std/crates, unchanged |
| Test code | 7557: 6999 test modules in `src/` + 558 `tests/*.rs` (tier 4: 4307) | 16037 (17530) in `tests/` + 142 (173) `run_goldenscripts.py` (tier 4: 11797 (12803) + 140 (171)) |
| - of which end-to-end | 558 (`tests/tests.rs`, `tests/testcluster.rs`) | 382 (479) `tests/e2e/` (+ the `pg` and backup runner modules, 307 (364), in `tests/pgwire/` and `tests/backup/`) |
| - of which golden script framework | 0 (the `goldenscript` crate) | 1251, unchanged |
| Golden scripts | 342 files, 29471 lines (tier 4: 276 files, 23987 lines) | the same 342 files |
| `check.sh` | 57 lines (unchanged) | 71 lines (unchanged) |

**The Rust application code is 19233 lines, 767 lines (3.8%) below the 20k
target.** The nine tier-5 extensions add 6735 lines of Rust application code
(and 5779 of Python); the design notes budgeted 8000-8500. Nothing was padded
to close the gap.

### The tier-5 extensions alone

| | Rust | Python |
|---|---|---|
| Application code added | **6735** = 4288 in 21 new modules + 2447 in 32 existing files | **5779 (6762)** = 3490 (4215) in 20 new modules + 2289 (2547) in 27 existing files; + 6 (7) for the `bin/toybackup` launcher |
| Largest changes to existing files | `execution/session.rs` +498, `engine/raft.rs` +258, `engine/local.rs` +214, `parser/lexer.rs` +181, `bin/toysql.rs` +157, `parser/parser.rs` +157, `parser/ast.rs` +144, `encoding/format.rs` +96, `parser/digest.rs` +90, `parser/format.rs` +88, `execution/prepared.rs` +85 | `engine/raft.py` +412, `execution/session.py` +396, `engine/local.py` +310, `bin/toysql.py` +148, `parser/ast.py` +143, `parser/parser.py` +137, `server.py` +88, `encoding/format.py` +77, `planner/plan.py` +65, `execution/digest.py` +62, `parser/format.py` +61 |
| Test code added | 3250: 2660 in the new modules, 334 in existing ones (of which 246 the `pg`/backup commands of the SQL runner in `sql/mod.rs`), 256 in `tests/*.rs` | 4240 (4727) in `tests/`: 2570 (2878) in 16 new mirrored test modules, 1670 (1849) in other new and existing ones (including the `pg`/backup runner modules); + 2 `run_goldenscripts.py` |
| Unit tests added | 305 (530 → 835) | 198 (271 → 469) |
| Golden scripts added | 66 files, 5484 lines (`ext5_scripts/`) | the same |

Per extension (new modules only; the changes to existing files are shared and
listed above):

| Extension | Rust app | Rust test | Python app | Python test | Golden scripts (sql + cluster, lines) |
|---|---|---|---|---|---|
| users | 548 = 40 `types/user.rs` + 299 `execution/privileges.rs` + 209 `execution/users.rs` | 500 | 438 (558) = 69 (97) `types/user.py` + 253 (307) `execution/privileges.py` + 116 (154) `execution/users.py` | 433 (475) | 6 + 1, 829 |
| audit | 197 `execution/audit.rs` | 161 | 196 (235) `execution/audit.py` | 252 (276) | 6 + 1, 422 |
| checks | 92 = 15 `types/check.rs` + 77 `execution/checks.rs` | 204 | 69 (111) = 20 (28) `types/check.py` + 49 (83) `execution/checks.py` | 168 (197) | 7 + 1, 536 |
| policies | 169 = 31 `types/policy.rs` + 138 `execution/policies.rs` | 268 | 116 (164) = 38 (51) `types/policy.py` + 78 (113) `execution/policies.py` | 251 (288) | 7 + 1, 609 |
| pgwire | 1913 = 49 `mod.rs` + 526 `message.rs` + 169 `types.rs` + 826 `connection.rs` + 286 `client.rs` + 57 `encoding/md5.rs` | 586 | 1494 (1760) = 29 (35) `__init__.py` + 338 (420) `message.py` + 163 (207) `types.py` + 678 (783) `connection.py` + 286 (315) `client.py` | 667 (733) + 227 (264) runner | 7 + 1, 1016 |
| backup | 739 = 126 `backup/mod.rs` + 508 `backup/format.rs` + 105 `bin/toybackup.rs` | 393 | 676 (789) = 91 (108) `backup/__init__.py` + 456 (532) `backup/format.py` + 129 (149) `bin/toybackup.py` | 423 (478) + 80 (100) runner | 7 + 1, 699 |
| catalog | 483 `execution/system.rs` | 192 | 373 (422) `execution/system.py` | 214 (238) | 6 + 1, 556 |
| matviews | 105 = 20 `types/matview.rs` + 85 `execution/matviews.rs` | 240 | 94 (127) = 30 (42) `types/matview.py` + 64 (85) `execution/matviews.py` | 212 (247) | 6 + 1, 484 |
| profile | 42 `execution/profile.rs` | 116 | 34 (49) `execution/profile.py` | 125 (147) | 5 + 1, 333 |
| **total** | **4288** | **2660** | **3490 (4215)** | **2745 (3079)** + 307 (364) runners | **57 + 9, 5484** |

Python test lines are those of the mirrored test modules (`tests/.../test_<module>.py`;
pgwire: `test_{message,types,connection}.py`; backup: `test_format.py` +
`test_backup.py`). The Rust tier-5 application code is 17% larger than the
Python code (6735 vs 5779 lines), the new modules 23% (4288 vs 3490); most of
the gap is the PostgreSQL protocol (Rust's own MD5, message enums and their
encoders and decoders, the client's format tracking) and the system tables.
