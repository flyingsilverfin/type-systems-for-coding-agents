
Rust crate in `app/` (binary `lending`), source in `app/src/`:

- `main.rs`: command-line entry point, dispatch and usage text
- `config.rs`, `error.rs`: configuration from the environment; application errors
- `db.rs`: thin wrapper around the TypeDB driver (connect, transactions, `read_rows`, `quote`)
- `schema.rs`: the TypeQL schema
- `model.rs`: domain types
- `csv.rs`, `ingest.rs`: CSV reader; CSV to TypeQL insert queries
- `repo/members.rs`, `repo/books.rs`, `repo/loans.rs`: one query function per command; each runs its own
  TypeQL query and reads the columns with the driver API
- `report/members.rs`, `report/books.rs`, `report/loans.rs`: output formatting, one function per command

The app uses the TypeDB Rust driver (`typedb-driver`, with the `sync` feature: the same API as the async
driver, but blocking; promises are resolved with `.resolve()`) and `chrono`. Dependencies are already built;
no network access is needed. The driver's source is in `driver-src/` for reference. An example program using
the (async) driver is in `docs/driver_example.rs`.

Build: `cd app && cargo build` (if `cargo` is not on your PATH, it is `~/.cargo/bin/cargo`). Run:
`app/target/debug/lending <command>`.
