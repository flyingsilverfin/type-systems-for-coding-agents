
Python 3.12 package in `app/lending/`:

- `__main__.py`: command-line entry point and dispatch
- `config.py`: configuration from the environment
- `errors.py`: application errors
- `db.py`: thin wrapper around the TypeDB driver; typed column accessors
- `schema.py`: the TypeQL schema
- `model.py`: domain types (`Member`, `Book`, `Loan`)
- `csvio.py`: CSV reader
- `ingest.py`: CSV to TypeQL insert queries
- `repo.py`: read queries returning domain objects
- `report.py`: output formatting

The TypeDB Python driver (`typedb-driver` 3.12) is vendored in `driver/typedb/` (source included; its public
API is in `driver/typedb/api/` and `driver/typedb/driver.py`). An example program using the driver is in
`docs/driver_example.py`.

Run the app: `PYTHONPATH=app:driver python3 -s -m lending <command>` (from the workspace root).
