# toydb Python port report

The whole of toyDB (`rust/`, commit 2d7cd32) is ported to Python 3.12 (standard library only) under
`python/`, following `PORTING.md`. It was done in two tiers:

- **Tier 1**: `encoding/`, `error.rs`, `lib.rs`, `storage/`, the SQL layer (`sql/` except `sql/engine/raft.rs`),
  and the goldenscript runner.
- **Tier 2**: `raft/`, `sql/engine/raft.rs`, `server.rs`, `client.rs`, `bin/` (`toydb`, `toysql`, `toydump`,
  `workload`), and the end-to-end tests (`tests/testcluster.rs`, `tests/tests.rs`).

## Final pass counts

| Check | Result |
|---|---|
| `python3 python/run_goldenscripts.py` (no arguments: every toyDB script dir under `rust/`) | **203/203** script runs identical to the Rust expectations |
| - `rust/src/sql/testscripts` (93 files) | 93/93 |
| - `rust/src/storage/testscripts` (33 files; the 4 `engine/` scripts run once per engine, Memory and BitCask, as in Rust) | 37/37 |
| - `rust/src/raft/testscripts` (`log/` and `node/`, 68 files) | 68/68 |
| - `rust/tests/scripts` (5 files, real 1- to 5-node clusters of the Python server as subprocesses) | 5/5 |
| `python3 python/run_goldenscripts.py rust/src/sql/testscripts rust/src/storage/testscripts` (tier 1) | 130/130 |
| `cd python && python3 -m unittest discover -s . -t .` | 122 tests, OK (includes the end-to-end scripts) |
| End-to-end flakiness | full unit-test suite run 3x and `rust/tests/scripts` run 8x more: all green every time |

The counts match the Rust `test_each_path!` wiring (93 + 2x4 + 29 + 68 + 5 = 203). The runner's mismatch
detection was checked by running modified copies of a raft and an e2e script (both reported FAIL).

## Line counts

Non-blank, non-comment lines. Rust: `//`, `///`, `//!` and `/* */` lines excluded; "test" is the trailing
`#[cfg(test)] mod` of the file (including test helper modules such as `raft/state.rs` `pub mod test`), and
`rust/tests/*.rs` is all test code. Python: `#` lines excluded; "code" also excludes docstrings (the
equivalent of Rust doc comments), "+doc" includes them. Python test files are counted as code (no docstrings).

| Tier | Rust file (`rust/`) | Rust app | Rust test | Python file (`python/`) | Py code | Py +doc | Python test file | Py test code |
|---|---|---|---|---|---|---|---|---|
| 2 | `src/bin/toydb.rs` | 94 | 0 | `toydb/bin/toydb.py` | 93 | 123 | - | 0 |
| 2 | `src/bin/toydump.rs` | 36 | 0 | `toydb/bin/toydump.py` | 47 | 59 | - | 0 |
| 2 | `src/bin/toysql.rs` | 217 | 0 | `toydb/bin/toysql.py` | 231 | 276 | - | 0 |
| 2 | `src/bin/workload.rs` | 399 | 0 | `toydb/bin/workload.py` | 482 | 550 | - | 0 |
| 2 | `src/client.rs` | 89 | 0 | `toydb/client.py` | 107 | 134 | - | 0 |
| 1 | `src/encoding/bincode.rs` | 30 | 0 | `toydb/encoding/bincode.py` | 245 | 288 | `tests/encoding/test_bincode.py` | 165 |
| 1 | `src/encoding/format.rs` | 227 | 0 | `toydb/encoding/format.py` | 206 | 228 | - | 0 |
| 1 | `src/encoding/keycode.rs` | 398 | 145 | `toydb/encoding/keycode.py` | 187 | 264 | `tests/encoding/test_keycode.py` | 252 |
| 1 | `src/encoding/mod.rs` | 41 | 0 | `toydb/encoding/__init__.py` | 41 | 83 | - | 0 |
| 1 | `src/error.rs` | 165 | 0 | `toydb/error.py` | 143 | 185 | - | 0 |
| 1 (updated in 2) | `src/lib.rs` | 14 | 0 | `toydb/__init__.py` | 16 | 21 | - | 0 |
| 2 | `src/raft/log.rs` | 206 | 253 | `toydb/raft/log.py` | 223 | 324 | `tests/raft/test_log.py` | 204 |
| 2 | `src/raft/message.rs` | 77 | 0 | `toydb/raft/message.py` | 336 | 447 | - | 0 |
| 2 | `src/raft/mod.rs` | 14 | 0 | `toydb/raft/__init__.py` | 40 | 239 | - | 0 |
| 2 | `src/raft/node.rs` | 704 | 833 | `toydb/raft/node.py` | 628 | 784 | `tests/raft/test_node.py` | 654 |
| 2 | `src/raft/state.rs` | 7 | 125 | `toydb/raft/state.py` | 9 | 48 | `tests/raft/test_state.py` | 172 |
| 2 | `src/server.rs` | 250 | 0 | `toydb/server.py` | 382 | 446 | - | 0 |
| 1 | `src/sql/engine/engine.rs` | 35 | 0 | `toydb/sql/engine/engine.py` | 51 | 90 | - | 0 |
| 1 | `src/sql/engine/local.rs` | 287 | 0 | `toydb/sql/engine/local.py` | 279 | 324 | - | 0 |
| 1 | `src/sql/engine/mod.rs` | 6 | 0 | `toydb/sql/engine/__init__.py` | 12 | 21 | - | 0 |
| 2 | `src/sql/engine/raft.rs` | 287 | 0 | `toydb/sql/engine/raft.py` | 453 | 524 | - | 0 |
| 1 | `src/sql/execution/aggregator.rs` | 95 | 0 | `toydb/sql/execution/aggregator.py` | 122 | 144 | - | 0 |
| 1 | `src/sql/execution/executor.rs` | 232 | 0 | `toydb/sql/execution/executor.py` | 203 | 248 | - | 0 |
| 1 | `src/sql/execution/join.rs` | 122 | 0 | `toydb/sql/execution/join.py` | 100 | 138 | - | 0 |
| 1 | `src/sql/execution/mod.rs` | 6 | 0 | `toydb/sql/execution/__init__.py` | 3 | 6 | - | 0 |
| 1 | `src/sql/execution/session.rs` | 171 | 0 | `toydb/sql/execution/session.py` | 252 | 286 | - | 0 |
| 1 | `src/sql/mod.rs` | 5 | 208 | `toydb/sql/__init__.py` | 0 | 55 | `tests/sql/test_mod.py` | 188 |
| 1 | `src/sql/parser/ast.rs` | 238 | 0 | `toydb/sql/parser/ast.py` | 423 | 531 | `tests/sql/parser/test_ast.py` | 102 |
| 1 | `src/sql/parser/lexer.rs` | 429 | 0 | `toydb/sql/parser/lexer.py` | 404 | 482 | `tests/sql/parser/test_lexer.py` | 245 |
| 1 | `src/sql/parser/mod.rs` | 5 | 0 | `toydb/sql/parser/__init__.py` | 4 | 7 | - | 0 |
| 1 | `src/sql/parser/parser.rs` | 650 | 0 | `toydb/sql/parser/parser.py` | 688 | 872 | `tests/sql/parser/test_parser.py` | 1504 |
| 1 | `src/sql/planner/mod.rs` | 7 | 0 | `toydb/sql/planner/__init__.py` | 4 | 8 | - | 0 |
| 1 | `src/sql/planner/optimizer.rs` | 290 | 0 | `toydb/sql/planner/optimizer.py` | 233 | 268 | - | 0 |
| 1 | `src/sql/planner/plan.rs` | 471 | 0 | `toydb/sql/planner/plan.py` | 737 | 848 | - | 0 |
| 1 | `src/sql/planner/planner.rs` | 598 | 0 | `toydb/sql/planner/planner.py` | 555 | 670 | `tests/sql/planner/test_planner.py` | 199 |
| 1 | `src/sql/types/expression.rs` | 388 | 0 | `toydb/sql/types/expression.py` | 624 | 699 | `tests/sql/types/test_expression.py` | 2261 |
| 1 | `src/sql/types/mod.rs` | 6 | 0 | `toydb/sql/types/__init__.py` | 14 | 17 | - | 0 |
| 1 | `src/sql/types/schema.rs` | 172 | 0 | `toydb/sql/types/schema.py` | 159 | 197 | `tests/sql/types/test_schema.py` | 268 |
| 1 | `src/sql/types/value.rs` | 331 | 0 | `toydb/sql/types/value.py` | 516 | 612 | `tests/sql/types/test_value.py` | 281 |
| 1 | `src/storage/bitcask.rs` | 240 | 181 | `toydb/storage/bitcask.py` | 224 | 297 | `tests/storage/test_bitcask.py` | 158 |
| 1 | `src/storage/engine.rs` | 45 | 281 | `toydb/storage/engine.py` | 54 | 89 | `tests/storage/test_engine.py` | 182 |
| 1 | `src/storage/memory.rs` | 59 | 12 | `toydb/storage/memory.py` | 32 | 37 | `tests/storage/test_memory.py` | 16 |
| 1 | `src/storage/mod.rs` | 7 | 0 | `toydb/storage/__init__.py` | 15 | 19 | - | 0 |
| 1 | `src/storage/mvcc.rs` | 384 | 264 | `toydb/storage/mvcc.py` | 473 | 728 | `tests/storage/test_mvcc.py` | 267 |
| 2 | `tests/testcluster.rs` | 0 | 97 | - | 0 | 0 | `tests/e2e/testcluster.py` | 102 |
| 2 | `tests/tests.rs` | 0 | 130 | - | 0 | 0 | `tests/e2e/test_tests.py` | 123 |

Python application modules with no Rust module counterpart (stand-ins for Rust std / crates):

| Tier | Python file (`python/`) | Py code | Py +doc | Stands in for |
|---|---|---|---|---|
| 1 | `toydb/fmt.py` | 241 | 304 | Rust `Display`/`Debug` formatting (mandated by PORTING.md) |
| 1 | `toydb/encoding/serde.py` | 232 | 347 | the `serde` data model (`Serializer`/`Deserializer`, derive equivalents) |
| 1 | `toydb/storage/btree.py` | 141 | 180 | `std::collections::{BTreeMap, BTreeSet}`, `DoubleEndedIterator` |
| 1 | `toydb/iter.py` | 111 | 145 | clonable `std::iter` adapters (`Rows` must be `Clone`) |
| 1 | `toydb/ops.py` | 26 | 35 | `std::ops::Bound` / `RangeBounds` |
| 2 | `toydb/config.py` | 328 | 362 | the `config` crate: defaults, a YAML-subset parser for toyDB config files, `TOYDB_*` env vars, typed deserialization with the crate's error messages |
| 2 | `toydb/net.py` | 58 | 76 | `std::net` address resolution, bind/connect, `SocketAddr`/`io::Error` display |
| 2 | `toydb/simplelog.py` | 75 | 99 | `log`/`simplelog` output format on top of `logging` |
| 2 | `toydb/bin/__init__.py` | 0 | 7 | (package marker for the binaries) |

Test infrastructure and launchers (no Rust module counterpart):

| Tier | Python file (`python/`) | Py code | Py +doc | Stands in for |
|---|---|---|---|---|
| 1 | `tests/goldenscript.py` | 756 | 967 | the `goldenscript` 0.8.0 crate |
| 1 | `tests/test_goldenscript.py` | 496 | 548 | the crate's own tests |
| 1 | `tests/test_fmt.py` | 179 | 181 | unit tests for `toydb/fmt.py` |
| 1/2 | `run_goldenscripts.py` | 134 | 163 | `test_each_path!` wiring (raft and e2e dirs added in tier 2) |
| 2 | `bin/{toydb,toysql,toydump,workload}` | 4 x 6 | 4 x 7 | `cargo run --bin` (wrapper scripts running `toydb.bin.*`) |

Totals:

| | Rust | Python |
|---|---|---|
| Application, tier 1 | 6154 | 7019 mirrored modules (8762 incl. docstrings) + 751 stand-ins |
| Application, tier 2 | 2380 | 3031 mirrored modules (3954 incl. docstrings) + 461 stand-ins |
| **Application, whole port** | **8534** | **11262** in `python/toydb/` (14271 incl. docstrings); plus 24 in `bin/` launchers |
| Test code | 2529 (+ goldenscript crate) | 8774 under `tests/` + 134 `run_goldenscripts.py` |

Python is larger mainly because serde/bincode derives, `Debug` formatting and enum variant classes are
written out by hand (e.g. `raft/message.py` 336 vs 77, `sql/engine/raft.py` 453 vs 287, `server.py` 382 vs
250), and because the Rust `encoding/bincode.rs` only wraps the `bincode` crate. `raft/__init__.py` holds the
long `raft/mod.rs` module documentation as its docstring.

## Known deviations

### Whole port

- **Extra modules** (tables above): `fmt.py`, `iter.py`, `ops.py`, `encoding/serde.py`, `storage/btree.py`,
  `config.py`, `net.py`, `simplelog.py` replace Rust std / crate functionality; no Rust module corresponds.
- **Enum variant names**: variants are bare-named unless the name clashes within the module, in which case all
  clashing variants are prefixed (`Request.Execute` is `RequestExecute` because `Response.Execute` exists;
  `Message.Read` is `MessageRead`; `Read.Status` is `ReadStatus`; `Key.Entry` is `KeyEntry`). They are always
  reachable as attributes of the base class, e.g. `Request.Execute`, which is how the code refers to them.
- **Associated types / trait impls** (`type Item`, `impl Display::fmt`, `PartialOrd::partial_cmp`, `TryFrom`,
  `From`, `ops::Add`) map onto Python protocols (`__next__`, `__str__`/`fmt_debug`, `__lt__`, conversion
  methods such as `try_into_i64()` and `RawNode.into()`).
- **`Drop`** is ported as explicit `close()` methods (BitCask flush/unlock with a best-effort `__del__`,
  Session implicit rollback, `Client`, `TestCluster`/`TestServer`); context managers where useful.
- **Test runners** (`#[cfg(test)]` modules, `storage/engine.rs` and `raft/state.rs` `pub mod test`) live under
  `python/tests/` per PORTING.md. `cfg!(test)` switches (BitCask fsync, MVCC scan buffer size) are class
  attributes (`BitCask.FSYNC`, `ScanIterator.BUFFER_SIZE`) set by the test modules.
- **Extra unit tests**: `test_parser.py`, `test_lexer.py`, `test_ast.py`, `test_expression.py`, `test_value.py`,
  `test_schema.py`, `test_planner.py`, `test_bincode.py` have no Rust unit-test counterpart; their expected
  values were generated from the Rust implementation.
- **Type checking**: every function in `python/` has full parameter and return annotations (AST check);
  pyright is not installed, so no type-checker run was made.
- No application code references testscripts, `.test` files or expected outputs.

### Tier 1

- **`encoding/keycode.py`** is smaller than `keycode.rs`: the serde `SerializeSeq`/`SeqAccess`/`EnumAccess`
  plumbing collapses into the `serde.py` interfaces; `unimplemented!()` serde methods are not mirrored.
- **`Engine::scan_dyn`** is a default on the `Engine` base class calling `scan` (no static/dynamic dispatch
  distinction in Python).

### Tier 2

- **Raft router select**: Rust `crossbeam::select!`s over a ticker, `node_rx`, `peers_rx` and `request_rx`.
  Python queues can't be selected on, so peer messages and client requests share one inbox queue (the SQL
  engine's `RequestSender` is a `Protocol` with `put`, so it accepts the shared queue); ticks are driven by the
  inbox `get()` timeout (a due tick is handled before waiting, so a busy inbox can't starve it; missed ticks are
  not delivered in bursts, like a crossbeam ticker); and `node_rx` is drained after each step/tick, which is
  when the node emits messages.
- **Threads**: `std::thread::scope` threads are daemon `threading.Thread`s; `serve()` joins them (forever).
- **Sockets**: the SQL client and server sockets set `TCP_NODELAY` (Rust uses the OS default). This only
  affects latency, not behaviour or wire format.
- **toysql**: rustyline is replaced by a small `Editor` class over the `readline` module and `input()`, with the
  same `InputValidator` helper deciding when multiline input is complete (`validate()` returns a bool instead of
  a `ValidationResult`). History skips empty and consecutive duplicate entries like rustyline, and is stored in
  `~/.toysql.history` in readline's own file format.
- **workload**: `random.Random(seed)` replaces `StdRng` (so generated data differs from Rust for a given seed);
  latency percentiles are computed exactly from recorded values instead of an HDR histogram (`Histogram`
  stand-in class); a small built-in word list replaces the `petname` crate; the clap `Subcommand` enum is a
  `type Subcommand = Read | Write | Bank` alias dispatched with `match`; workers stop on the first error
  instead of propagating it at the end of a thread scope.
- **CLI parsing**: argparse replaces clap; option names, short flags, defaults and help texts follow the clap
  definitions, but argparse's usage/error output format differs from clap's.
- **Server binary in tests**: `TestServer` runs `python/bin/toydb` with the current interpreter instead of
  building the binary with escargot.
- **`assert_eq`/`assert_ne`** (`toydb/error.py`): the message argument is optional, like the Rust macros.
- **`toydb/__init__.py`** imports its submodules and re-exports `Client`, `Server` and `StatementResult`
  eagerly, like `lib.rs` (the tier-1 lazy `__getattr__` was removed).

Cross-checked against the Rust binaries in tier 2: identical server log lines and config error messages,
identical `toysql` output (including `!status`) against the same server, identical `toydump` (`--raw`,
`--raft`) output on a data directory written by the Rust server, and the Python server opens Rust-written data
dirs. `rust/config/toydb.yaml` and `rust/cluster/*/toydb.yaml` load unchanged.

## Integration review (final pass)

Changes made while reviewing the tier-2 modules for PORTING.md compliance:

- `bin/workload.py`: restored the Rust `ReadGenerator` and `WriteGenerator` iterator types (they had been
  folded into generator functions) and the `Subcommand` type with `match` dispatch in `main()`; help texts now
  match the clap doc comments.
- `bin/toysql.py`: restored the Rust `InputValidator` type (it had become a free function) and a `Shell.editor`
  field (`Editor`, the rustyline stand-in) owning history loading/saving and multiline input.
- `sql/engine/raft.py` (`Read`/`Write` variants) and `raft/node.py` (`Node` variants): enum variants are now
  `@dataclass(frozen=True)` as PORTING.md requires.
- `server.py`: `Execute` renamed `RequestExecute` (it clashes with `Response.Execute`; the other clashing
  request variants were already prefixed).
- `tests/e2e/testcluster.py`: added a missing `-> None` annotation.

Smoke-tested after the changes against a live single-node Python server: `toysql` (statement argument, piped
multiline input, `!headers`, `!tables`, `!table`, `!status`, errors, history file), `workload` `read`, `write`
and `bank` (all verified their datasets), and `toydump`.
