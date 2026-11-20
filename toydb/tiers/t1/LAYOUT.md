# Tier 1 layout

Tier 1 is the single-node core of toydb: encoding, storage, and the SQL layer
on the local (MVCC) engine. Excluded: `raft/`, `server.rs`, `client.rs`,
`bin/`, `sql/engine/raft.rs`, `tests/` (cluster tests), and what only existed
for them (the `Raft` and `SQLCommand` formatters, `Session::status`, the
related `From` impls in `error.rs`, docs/cluster/config, unused dependencies).

```
tiers/t1/
  rust/                  Rust workspace (trimmed copy of ../../rust)
  python/                Python workspace (self-contained, from python_port_snapshot)
  python_port_snapshot/  the port as delivered (reads scripts from ../../rust; not handed out)
  grade.py               grader for both workspaces
  LAYOUT.md              this file
```

## Workspaces

| | Rust (`rust/`) | Python (`python/`) |
|---|---|---|
| Application code | `src/**/*.rs` (outside `#[cfg(test)] mod`) | `toydb/**/*.py` |
| Unit tests | `#[cfg(test)] mod tests` at the end of each source file | `tests/**/test_*.py`, mirroring `toydb/` |
| Golden script framework | `goldenscript` crate (dev-dependency) | `tests/goldenscript.py` (+ `tests/test_goldenscript.py`, `tests/goldenscript_testdata/`) |
| SQL golden scripts (93) | `src/sql/testscripts/{expressions,optimizers,queries,schema,transactions,writes}/` | `tests/sql/testscripts/<same>/` |
| Storage golden scripts (33 files, 37 runs) | `src/storage/testscripts/{bitcask,engine,memory,mvcc}/` | `tests/storage/testscripts/<same>/` |
| SQL script runner | `test_goldenscript` / `test_goldenscript_expr` in `src/sql/mod.rs` | same names in `tests/sql/test_mod.py` |
| Storage script runners | `src/storage/{memory,bitcask,mvcc}.rs` (helpers in `engine.rs` `mod test`) | `tests/storage/test_{memory,bitcask,mvcc,engine}.py` |
| Script registry | `test_each_path!` lines in the runners' test modules (compile time) | `REGISTRY` in `run_goldenscripts.py` (run time) |
| **Task script dir** | `src/sql/testscripts/task/` | `tests/sql/testscripts/task/` |
| Run everything | `./check.sh` (`cargo build --all-targets`, `cargo test`) | `./check.sh` (compile/import check, `run_goldenscripts.py`, `unittest discover`) |
| Docs | `README.md` | `README.md` |
| Baseline | 130 golden runs + 80 unit tests | 130 golden runs + 117 unit tests |

The 126 golden script files are byte-identical in both workspaces, and the
4 `storage/testscripts/engine` scripts run once per engine in both (hence 130
runs). The Rust sources are byte-identical to upstream except `Cargo.toml`,
`Cargo.lock`, `.gitignore`, `README.md`, `src/lib.rs`, `src/error.rs`,
`src/encoding/format.rs`, `src/sql/engine/mod.rs`,
`src/sql/execution/session.rs` (Raft parts removed) and `src/sql/mod.rs` (one
added `test_each_path!` line). Removed dependencies: clap, config,
hdrhistogram, petname, rand, rustyline, rustyline-derive, simplelog, uuid,
escargot.

`check.sh` prints `build: ok`, `golden scripts: P/T passed`,
`unit tests: P/T passed` and `check: OK` or `check: FAILED`, and exits non-zero
on any failure (`-v` prints the full test output). The Rust one exports
`PATH="$HOME/.cargo/bin:$PATH"` and `CARGO_TARGET_DIR="$PWD/target"`;
`rust/target/` is warm. Warm wall time is about 7 s for either workspace.

## Task script directory

Both workspaces have an (initially empty) directory for task-specific SQL
golden scripts, run by the ordinary SQL runner (`test_goldenscript`, the one
that runs `queries/`, `schema/`, ...):

* Rust: `rust/src/sql/testscripts/task/`, registered in `src/sql/mod.rs` by
  `test_each_path! { in "src/sql/testscripts/task" as task => test_goldenscript }`.
  Tests are named `sql::tests::task::<name>`.
* Python: `python/tests/sql/testscripts/task/`, registered in `REGISTRY` in
  `run_goldenscripts.py` and in the directory list of
  `tests/sql/test_mod.py`.

Script file names must be snake_case identifiers without extension (the Rust
macro turns them into test function names). Notes:

* The directory may be empty, but in Rust it must exist (the macro reads it at
  compile time). `check.sh` and `grade.py` create it if missing; an empty
  directory is lost by tools that don't track empty directories (e.g. git), so
  copy workspaces with `cp -r`/`rsync`, or run `check.sh` first.
* Rust discovers scripts at compile time, so `src/sql/mod.rs` must be
  recompiled after adding or removing a script. `check.sh` and `grade.py`
  `touch` it; a bare `cargo test` does not notice a new script. Python
  discovers scripts at run time.
* `UPDATE_GOLDENFILES=1` rewrites the expected output in both
  (`cargo test sql::tests` / `python3 run_goldenscripts.py tests/sql`).
  `grade.py` unsets it.

## grade.py

```
python3 grade.py <rust|python> <workspace> <scripts_dir> [--json out.json] [--log grade.log] [--timeout SECS]
```

Standard library only; `<workspace>` may be a copy of `rust/` or `python/`
anywhere on disk. It

1. snapshots the files in `<scripts_dir>` (names must be snake_case
   identifiers) and moves the workspace's task dir aside, so scripts the
   agent left there are neither graded nor counted;
2. builds (Rust: `cargo test --no-run` with the target dir in the workspace;
   Python: compile and import every module);
3. runs the given scripts with the SQL runner, and the full regression suite
   (all other golden scripts plus the unit tests);
4. restores the task dir, also on failure.

Output (stdout, and the same data as JSON with `--json`): `build_ok`,
per-script `pass`/`fail` (`scripts`, `scripts_passed`/`scripts_total`),
`regression` (`passed`/`total`, split into `golden_*` and `unit_*`, plus the
names of `failed` tests), `tests_completed` (false on a crash or timeout),
`times` (`build_s`, `test_s`, `total_s`), `ok`, and `log`. The log (default
`<json>.log`, else `./grade.log`) holds the complete tool output including the
diff of each failing script. Exit status: 0 if the build is ok, every given
script passes and nothing regressed; 1 otherwise; 2 on usage errors.

A script that fails to build in Rust makes all given scripts `fail` with
`build_ok: false`. Regression totals with a clean workspace: Rust 210 (130
golden + 80 unit), Python 247 (130 golden + 117 unit).

## Line counts

Non-blank, non-comment lines. Rust: `//`, `///` and `//!` lines are excluded,
and "test" is everything from a file's `#[cfg(test)] mod` to its end. Python:
`#` comments and docstrings (the equivalent of Rust doc comments) are
excluded; the count including docstrings is given in parentheses.

| | Rust | Python |
|---|---|---|
| Application code | 6013 (`src/`, outside test modules) | 7641 (9826) in `toydb/`, of which 751 are stand-ins for Rust std/serde/bincode (`fmt.py`, `iter.py`, `ops.py`, `encoding/serde.py`, `storage/btree.py`) |
| Test code | 1092 (test modules in `src/`) | 7522 (8038) in `tests/` + 125 (156) `run_goldenscripts.py` |
| - of which golden script framework | 0 (the `goldenscript` crate) | 1251: `tests/goldenscript.py` 761 + `tests/test_goldenscript.py` 490 |
| Golden scripts | 126 files, 12014 lines | the same 126 files |
| `check.sh` | 52 lines | 70 lines |

The Python test code is larger mainly because of generated unit tests without
a Rust counterpart (parser, lexer, expression, value, schema, planner; see
`python_port_snapshot/PORT_REPORT.md`) and the ported goldenscript framework.
