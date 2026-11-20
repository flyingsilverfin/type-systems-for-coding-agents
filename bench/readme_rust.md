
Rust crate in `app/` (binary `lending`), source in `app/src/`:

- `main.rs`: command-line entry point and dispatch
- `config.rs`: configuration from the environment
- `error.rs`: application errors
- `db.rs`: thin wrapper around the TypeDB driver; typed column accessors
- `schema.rs`: the TypeQL schema
- `model.rs`: domain types (`Member`, `Book`, `Loan`)
- `csv.rs`: CSV reader
- `ingest.rs`: CSV to TypeQL insert queries
- `repo.rs`: read queries returning domain objects
- `report.rs`: output formatting

The app uses the TypeDB Rust driver (`typedb-driver` 3.12, with the `sync` feature: the same API as the async
driver, but blocking; promises are resolved with `.resolve()`) and `chrono`. Dependencies are already built;
no network access is needed. The driver's source is in `driver-src/` for reference. An example program using
the (async) driver is in `docs/driver_example.rs`.

Build: `cd app && cargo build` (if `cargo` is not on your PATH, it is `~/.cargo/bin/cargo`). Run: `app/target/debug/lending <command>`.
