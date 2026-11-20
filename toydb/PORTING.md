# Porting toydb (Rust) to Python — conventions

The Rust original in `rust/` (toydb commit 2d7cd32) is the reference. The Python port lives in `python/`.
It will be used as one arm of a Rust-vs-Python experiment, so the two codebases must be **structurally parallel**
(same modules, same type and function names, same algorithms) and **behaviourally identical** (byte-identical
golden-script output). It must also read as good, idiomatic, fully type-annotated Python, because coding agents
will later work on it as if it were a normal Python project.

## Acceptance

The golden scripts in `rust/src/**/testscripts/` and `rust/tests/scripts/` are the spec. The Python port passes
when `python3 python/run_goldenscripts.py <dir>...` reports every script identical to the Rust expectations.
Never edit the scripts. Never special-case test inputs, hardcode expected outputs, or read the `.test` files
from application code: the application must compute everything. Test-runner code (the Python equivalent of the
Rust `#[cfg(test)]` runners) lives under `python/tests/` only.

## Layout

Mirror `rust/src/` one-to-one under `python/toydb/`:

| Rust | Python |
|---|---|
| `src/sql/parser/lexer.rs` | `toydb/sql/parser/lexer.py` |
| `src/sql/parser/mod.rs` | `toydb/sql/parser/__init__.py` (re-exports, as the Rust `mod.rs` does) |
| `src/error.rs` | `toydb/error.py` |
| `#[cfg(test)] mod tests` in `src/x/y.rs` | `python/tests/x/test_y.py` (runner classes + any unit tests worth porting) |

Keep the Rust doc comments as docstrings (shortened where they only describe Rust mechanics).
Python 3.12, standard library only. No third-party packages.

## Types and names

- Struct → `@dataclass` (frozen when the Rust type is immutable and `Clone`/`Eq`). Same name, same field names.
- Enum with data → an abstract base class with the enum's name, plus one `@dataclass(frozen=True)` subclass per
  variant, named `<EnumName><Variant>` only if the bare variant name would clash within the module, otherwise
  the bare variant name. Attach each variant to the base for discoverability (`Expression.Constant = Constant`).
  Dispatch on them with `match` statements, mirroring the Rust `match` arms in the same order.
- Fieldless enum → `enum.Enum` (or `IntEnum` where the Rust discriminant matters for encoding).
- Traits → `typing.Protocol` or ABC. Generic structs → `typing.Generic`.
- Functions/methods keep their Rust names (already snake_case). Constructors `new(...)` become `__init__` or a
  `@classmethod new` when the Rust type has several constructors.
- `Result<T>` → return `T` or raise `toydb.error.Error` (a hierarchy mirroring the Rust `Error` enum variants).
  `Option<T>` → `T | None`. `Box<T>`, `Rc`, `Arc`: just references.
- Iterators: Python generators/iterators. `Mutex`/channels: `threading.Lock`, `queue.Queue`.
- Full type hints everywhere (it must type-check sensibly with pyright, even though we don't require it yet).

## Formatting must match Rust exactly

Golden outputs include Rust `Display` and `Debug` formatting. Put shared helpers in `toydb/fmt.py`:
`f64` Display (Rust never uses exponents for `{}`; `NaN`, `inf`, `-inf`, `-0` rules), `{:?}` of strings (Rust
escaping), of floats, of `Option`, of structs/enums when the tests print them, and so on. Match error message
text exactly (`rust/src/error.rs` plus every `errinput!`/`errdata!` call site).

## Encoding must match byte-for-byte

`[ops]`, `dump` and raw outputs print encoded keys and values. `toydb/encoding/keycode.py` must reproduce the
Rust `keycode` order-preserving encoding, and `toydb/encoding/bincode.py` must reproduce `bincode` 2.x
`config::standard()` with serde (varint integers with zigzag for signed, `u32` varint enum variant indices,
length-prefixed sequences/strings/bytes, `Option` as a 0/1 tag, structs as fields in order, `f64` little-endian).
Each serializable type defines how it maps to that representation, in the same field/variant order as the Rust
`#[derive(Serialize, Deserialize)]`.

## Golden-script runner

`python/tests/goldenscript.py` is a port of the `goldenscript` crate (source:
`~/.cargo/registry/src/*/goldenscript-0.8.0/src`, README in `GOLDENSCRIPT_README.md`): parsing (commands,
prefixes, tags, args, `!` failure expectation, `?` panic expectation if supported, comments, blocks, `---`
separators), the `Runner` protocol (`run`, `start_script`, `end_script`, `start_block`, `end_block`,
`start_command`, `end_command`), and output generation/comparison identical to the crate. `python/run_goldenscripts.py`
maps each testscript directory to its Python runner (the same mapping as the Rust tests), runs every file, and
prints a per-file PASS/FAIL with a unified diff for failures, plus a total.
