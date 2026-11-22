#!/usr/bin/env python3
"""Runs the toyDB golden scripts.

    python3 run_goldenscripts.py [PATH...]

Each path is a golden script or a directory of scripts, e.g.
`tests/storage/testscripts/mvcc`. Directories without a runner are
searched recursively, so `tests` runs every script below it. Without
arguments, all script directories in `REGISTRY` are run.

Prints PASS or FAIL for each script, with a unified diff of the expected and
actual output for failures, and finally the number of scripts that passed.
Exits with status 0 if all scripts passed.

Each script directory is run by the test function registered for it in
`REGISTRY`. The scripts are never modified.
"""

import argparse
import importlib
import sys
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

# Make the `toydb` and `tests` packages importable regardless of the working directory.
sys.path.insert(0, str(ROOT_DIR))

from tests import goldenscript  # noqa: E402


@dataclass(frozen=True)
class Test:
    """A golden script test function, such as `test_goldenscript(path)` in
    `tests/sql/test_mod.py`. It takes the path of a script, runs it with a new
    runner via `goldenscript.run()`, and raises on failure.
    """

    modules: tuple[str, ...]
    """The module containing the function. If several are given, the first
    that exists is used."""
    function: str = "test_goldenscript"
    """The name of the test function."""

    def __str__(self) -> str:
        return f"{self.modules[0]}.{self.function}"

    def load(self) -> Callable[[Path], None] | None:
        """Loads the test function, or returns None if the module or function
        doesn't exist yet. Other import errors are raised."""
        for name in self.modules:
            try:
                module = importlib.import_module(name)
            except ModuleNotFoundError as error:
                # Only ignore a missing test module, not its broken imports.
                if error.name is not None and (name == error.name or name.startswith(error.name + ".")):
                    continue
                raise
            return getattr(module, self.function, None)
        return None


def test(*modules: str, function: str = "test_goldenscript") -> Test:
    return Test(modules, function)


SQL = "tests.sql.test_mod"

# Maps script directories to the tests that run them, by path suffix. Every
# file in a directory is a script, named as a snake_case identifier. Note that
# the storage engine scripts are run for each engine.
REGISTRY: dict[str, tuple[Test, ...]] = {
    # tests/raft/test_log.py
    "tests/raft/testscripts/log": (test("tests.raft.test_log"),),
    # tests/raft/test_node.py
    "tests/raft/testscripts/node": (test("tests.raft.test_node"),),
    # tests/storage/test_memory.py and tests/storage/test_bitcask.py
    "tests/storage/testscripts/engine": (
        test("tests.storage.test_memory"),
        test("tests.storage.test_bitcask"),
    ),
    "tests/storage/testscripts/memory": (test("tests.storage.test_memory"),),
    "tests/storage/testscripts/bitcask": (test("tests.storage.test_bitcask"),),
    # tests/storage/test_mvcc.py
    "tests/storage/testscripts/mvcc": (test("tests.storage.test_mvcc"),),
    # tests/sql/test_mod.py
    "tests/sql/testscripts/expressions": (test(SQL, function="test_goldenscript_expr"),),
    "tests/sql/testscripts/ext": (test(SQL),),
    "tests/sql/testscripts/ext4": (test(SQL),),
    "tests/sql/testscripts/optimizers": (test(SQL),),
    "tests/sql/testscripts/queries": (test(SQL),),
    "tests/sql/testscripts/schema": (test(SQL),),
    "tests/sql/testscripts/task": (test(SQL),),
    "tests/sql/testscripts/transactions": (test(SQL),),
    "tests/sql/testscripts/writes": (test(SQL),),
    # tests/e2e/test_tests.py (end-to-end, against real server clusters)
    "tests/e2e/scripts": (test("tests.e2e.test_tests"),),
    "tests/e2e/scripts/ext": (test("tests.e2e.test_tests"),),
    "tests/e2e/scripts/ext4": (test("tests.e2e.test_tests"),),
    "tests/e2e/scripts/task": (test("tests.e2e.test_tests"),),
}


def lookup(path: Path) -> tuple[Test, ...] | None:
    """Looks up the tests for a script directory or single script."""
    resolved = path.resolve().as_posix()
    for suffix, tests in REGISTRY.items():
        if resolved.endswith("/" + suffix):
            return tests
    return None


def is_script(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".")


def find_scripts(path: Path) -> Iterator[tuple[Path, tuple[Test, ...] | None]]:
    """Finds the scripts at or below a path, along with their tests (None if
    there is no runner for them)."""
    if path.is_file():
        yield path, lookup(path) or lookup(path.parent)
        return
    tests = lookup(path)
    if tests is not None:
        for script in sorted(filter(is_script, path.iterdir())):
            yield script, lookup(script) or tests
        # Registered subdirectories, e.g. tests/e2e/scripts/task.
        for child in sorted(path.iterdir()):
            if child.is_dir() and lookup(child) is not None:
                yield from find_scripts(child)
        return
    # An unregistered directory: look for registered directories inside it.
    found = False
    for child in sorted(path.iterdir()):
        if child.is_dir() or lookup(child) is not None:
            for result in find_scripts(child):
                if result[1] is not None:
                    found = True
                    yield result
    if not found:
        yield path, None


def display(path: Path) -> str:
    """Formats a path relative to the working directory, where possible."""
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def run_script(path: Path, test: Test, show_diff: bool) -> bool | None:
    """Runs a script with a test function and prints the result. Returns
    whether it passed, or None if the test doesn't exist yet."""
    try:
        function = test.load()
        if function is None:
            print(f"SKIP {display(path)}: no runner yet (expected {test})", flush=True)
            return None
        function(path)
    except goldenscript.Mismatch as mismatch:
        print(f"FAIL {display(path)} ({test})", flush=True)
        if show_diff:
            print(mismatch.diff(), end="", flush=True)
        return False
    except Exception as error:
        print(f"FAIL {display(path)} ({test}): {type(error).__name__}: {error}", flush=True)
        if show_diff:
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
        return False
    print(f"PASS {display(path)} ({test})", flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Runs toyDB golden scripts.")
    parser.add_argument("paths", nargs="*", type=Path, help="scripts or script directories")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="don't print diffs and tracebacks for failures"
    )
    args = parser.parse_args()

    paths: list[Path] = args.paths
    if not paths:
        # Registered directories inside other registered directories are run
        # along with them.
        paths = [
            ROOT_DIR / key
            for key in REGISTRY
            if (ROOT_DIR / key).exists() and lookup((ROOT_DIR / key).parent) is None
        ]

    passed, total = 0, 0
    for path in paths:
        if not path.exists():
            print(f"FAIL {display(path)}: no such file or directory", flush=True)
            total += 1
            continue
        for script, tests in find_scripts(path):
            if tests is None:
                print(f"SKIP {display(script)}: no runner yet (unknown script directory)")
                total += 1
                continue
            for test in tests:
                total += 1
                if run_script(script, test, not args.quiet):
                    passed += 1

    print(f"{passed}/{total} scripts passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
