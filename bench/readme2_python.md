
Python 3.12 package in `app/lending/`:

- `__main__.py`: command-line entry point, dispatch and usage text
- `config.py`, `errors.py`: configuration from the environment; application errors
- `db.py`: thin wrapper around the TypeDB driver (connect, transactions, `read_rows`, `quote`)
- `schema.py`: the TypeQL schema
- `model.py`: domain types and the small row types returned by the repository
- `csvio.py`, `ingest.py`: CSV reader; CSV to TypeQL insert queries
- `repo/members.py`, `repo/books.py`, `repo/loans.py`: one query function per command; each runs its own
  TypeQL query and reads the columns with the driver API
- `report/members.py`, `report/books.py`, `report/loans.py`: output formatting, one function per command

The TypeDB Python driver is vendored in `driver/typedb/` (source included; its public API is in
`driver/typedb/api/` and `driver/typedb/driver.py`). An example program using the driver is in
`docs/driver_example.py`.

Run the app: `PYTHONPATH=app:driver python3 -s -m lending <command>` (from the workspace root).
