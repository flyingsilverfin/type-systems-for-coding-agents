"""The toyDB executables, mirroring `src/bin/`. Each module is runnable, e.g.
`python3 -m toydb.bin.toydb`, and has a thin wrapper script under `bin/`:

* `toydb`: the toyDB server.
* `toysql`: the toySQL command-line client.
* `toydump`: prints a toyDB BitCask file in human-readable form.
* `workload`: runs workload benchmarks against a toyDB cluster.
"""
