# Build spec: `lending` v2

You are extending an existing CLI app (`lending`, a library-lending tool backed by TypeDB) from 6 commands to
21 commands. The same app is being built in **Python** (`python/app/lending/`) and **Rust** (`rust/app/`) by two
different people; the two versions must have the **same architecture, module names and function names**
(modulo language naming conventions) and produce **byte-identical output**.

The output of every command is specified by `oracle2.py` (function `run`, task id `"BASE"`): it computes the
expected stdout lines and exit code straight from the CSV files. That file is the spec. Do not change it.
`verify.py <python|rust>` runs your build against the oracle on a small and a large dataset (a TypeDB server is
running at 127.0.0.1:1729; `verify.py` sets everything up). You are done when `verify.py` reports 0 failures.

## Data

CSV columns (all already in the data files):

- `members.csv`: `member_id,name,email,phone,joined,deleted` — email, phone optional; **`deleted` must be ignored** (not loaded, not in schema).
- `books.csv`: `isbn,title,subtitle,year,genre,copies` — subtitle, genre optional.
- `loans.csv`: `loan_id,member_id,isbn,borrowed,due,returned,fee_cents,lost` — returned, fee_cents, lost optional.

Schema: keep the v1 schema and add `phone` (string, member), `subtitle` (string, book), `fee-cents`
(integer, loan), `lost` (date, loan). Load them all (skip empty fields).

## Architecture (mandatory — this is the point of the exercise)

The app is used as a test subject for API-migration experiments, so the driver API must be used **directly and
pervasively**, the way many real codebases do:

1. **One repository function per command** (21 query functions; `book` and `member` may use two queries each),
   each with **its own TypeQL query** that fetches only the columns that command needs. Do not route every
   command through a shared "load all loans" function. Group them in three modules:
   `repo/members` (members, member, contact, inactive, top, fees, member-fees),
   `repo/books` (books, book, stats, decades, genre, never-borrowed, available, search),
   `repo/loans` (history, overdue, due-soon, returned-late, lost, monthly).
   Aggregations (counts, sums, grouping, sorting) are done in application code, not TypeQL `reduce`.
2. **Column values are extracted inline in each repository function**, with the raw driver API:
   `row.get("var")` and then `concept.try_get_string()` / `try_get_integer()` / `try_get_date()`.
   **No shared column-accessor helpers** (delete v1's `string`/`integer`/`date_value`/`req_*` helpers in
   `db.py`/`db.rs`). Optional columns come from `try { ... };` blocks and must be handled where they are read.
   A required column that is missing, or a value of the wrong type, is an `AppError` data error (in Rust use `?`
   and `ok_or_else`; in Python raise `DataError`). Small local closures/lambdas inside one function are fine.
3. `db` keeps only: connect, recreate, define, write(queries), read_rows(query) -> list/Vec of rows, quote().
4. Domain types in `model`: `Member` (id, name, email?, phone?, joined), `Book` (isbn, title, subtitle?, year,
   genre?, copies), `Loan` (id, member_id, isbn, borrowed, due, returned?, lost?, fee_cents?) with
   `is_open()` (not returned and not lost), `is_returned()`, `is_lost()`; lost wins over returned.
   Repository functions may return domain objects or small purpose-built row structs/tuples — but dates stay
   real date types (`datetime.date` / `chrono::NaiveDate`) and integers stay integers.
5. Formatting lives in `report/books`, `report/members`, `report/loans` (one formatting function per command,
   returning lines). The CLI dispatch is in `__main__.py` / `main.rs`, with a usage text listing every command.
6. Errors: unknown member/book ids print `error: no such member: <id>` / `error: no such book: <isbn>` to
   stderr, exit 1; bad arguments (non-integer or negative `top`/`due-soon` numbers, bad dates, missing args)
   print `error: ...` and exit 1.
7. Keep the code idiomatic and type-annotated (Python: full type hints, dataclasses, `Optional`; Rust: no
   `unwrap()` on data paths, no `unsafe`). No new third-party dependencies (Rust: `typedb-driver`, `chrono` only).

Also update `README.md` text inside the code layout section is not needed — just the code.

## Rules

- Work only inside your own directory (`python/` or `rust/`) plus reading `oracle2.py`, `tasks2.py`,
  `runner.py`, `verify.py`, `data_*`. Do not touch the other language's directory.
- Rust: `cargo` is at `~/.cargo/bin/cargo`; dependencies are already built (offline is fine).
- Python: the TypeDB driver is installed for `python3` already.
