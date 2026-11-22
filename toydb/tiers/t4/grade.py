#!/usr/bin/env python3
"""Grades a tier-4 toydb workspace against a directory of golden scripts.

    grade.py <rust|python> <workspace> <scripts_dir> [--json out.json] [--log grade.log] [--timeout SECS]

<scripts_dir> contains a `sql/` and/or a `cluster/` subdirectory of golden
scripts (snake_case file names, unique across both). The `sql/` scripts are
copied into the workspace's SQL task script directory and run by the ordinary
SQL golden script runner; the `cluster/` scripts are copied into the
end-to-end task script directory and run by the end-to-end runner against
real server clusters. Both task directories are replaced for the duration of
the run and restored afterwards (also on failure). The full regression suite
runs along with them: every other golden script (SQL, storage, Raft and the
end-to-end cluster scripts, including the extension scripts in the SQL and
end-to-end `ext/` and `ext4/` directories) and the unit tests.

Reports, per given script, pass/fail; the regression suite's passed/total;
whether the build succeeded; and wall times. The complete tool output,
including the diffs of failing scripts, is written to the log file.

Exits 0 if the build is ok, all given scripts pass and there are no regression
failures, 1 otherwise, 2 on usage errors.

Standard library only. The workspace may be a copy anywhere on disk. Grades of
different workspace copies may run concurrently (test clusters allocate their
ports through shared lock files); grades of the same workspace may not.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The task script directories, by kind of script.
TASK_DIRS = {
    "rust": {"sql": "src/sql/testscripts/task", "cluster": "tests/scripts/task"},
    "python": {"sql": "tests/sql/testscripts/task", "cluster": "tests/e2e/scripts/task"},
}
KINDS = ("sql", "cluster")

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")

# Rust test names of golden script runs in src/lib.rs, as opposed to unit
# tests. Every test in tests/tests.rs is an end-to-end golden script run.
RUST_GOLDEN = re.compile(r"(sql::tests|storage::[a-z]+::tests::(engine|scripts)|raft::[a-z]+::tests::scripts)::")
RUST_LIB = "src/lib.rs"
RUST_E2E = "tests/tests.rs"
RUST_TASK = {"sql": (RUST_LIB, "sql::tests::task::"), "cluster": (RUST_E2E, "task::")}

PYTHON_BUILD = r"""
import importlib, pathlib, sys
ok = True
for path in sorted(p for d in ("toydb", "tests") for p in pathlib.Path(d).rglob("*.py")):
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as error:
        ok = False
        print(f"{path}:{error.lineno}: {error.msg}")
sys.path.insert(0, ".")
if ok:
    for path in sorted(pathlib.Path("toydb").rglob("*.py")):
        importlib.import_module(".".join(path.with_suffix("").parts).removesuffix(".__init__"))
sys.exit(0 if ok else 1)
"""


class Log:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "w", encoding="utf-8")

    def write(self, text: str) -> None:
        self.file.write(text if text.endswith("\n") else text + "\n")
        self.file.flush()


def run(log: Log, argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str, float]:
    """Runs a command, returning its exit status (-1 on timeout), combined
    output, and wall time. The output is appended to the log. The command runs
    in its own process group, which is killed on timeout or interruption (so
    no test cluster servers are left behind)."""
    log.write(f"\n===== $ {' '.join(argv)}   (cwd {cwd})")
    start = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        raw, _ = process.communicate(timeout=timeout)
        status = process.returncode
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        raw, _ = process.communicate()
        raw += f"\n*** TIMEOUT after {timeout:.0f}s, killed\n".encode()
        status = -1
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    else:
        # Kill any stragglers left in the process group (e.g. servers).
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    elapsed = time.monotonic() - start
    output = raw.decode("utf-8", errors="replace")
    log.write(output)
    log.write(f"===== exit status {status}, {elapsed:.1f}s")
    return status, output, elapsed


def grade_rust(ws: Path, kinds: dict[str, str], log: Log, timeout: float) -> dict:
    env = dict(os.environ)
    env.pop("UPDATE_GOLDENFILES", None)
    env["PATH"] = f"{Path.home() / '.cargo' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["CARGO_TARGET_DIR"] = str(ws / "target")
    env["CARGO_TERM_COLOR"] = "never"

    # Golden scripts are discovered at compile time: force recompilation.
    touch_rust_runners(ws)

    status, _, build_s = run(log, ["cargo", "test", "--no-run"], ws, env, timeout)
    result: dict = {"build_ok": status == 0, "times": {"build_s": round(build_s, 1), "test_s": 0.0}}
    scripts = {name: "fail" for name in kinds}
    golden: dict[str, bool] = {}
    unit: dict[str, bool] = {}
    if status == 0:
        status, output, test_s = run(log, ["cargo", "test", "--no-fail-fast"], ws, env, timeout)
        result["times"]["test_s"] = round(test_s, 1)
        # Every test target that started must have reported its result.
        targets = len(re.findall(r"^\s*Running ", output, re.M))
        reported = len(re.findall(r"^test result: ", output, re.M))
        result["tests_completed"] = status in (0, 101) and targets > 0 and reported == targets
        target = ""
        for line in output.splitlines():
            if match := re.match(r"^\s*Running (?:unittests )?(\S+)", line):
                target = match.group(1)
                continue
            match = re.match(r"^test (\S+)(?: - should panic)? \.\.\. (ok|FAILED)$", line)
            if not match:
                continue
            test, ok = match.group(1), match.group(2) == "ok"
            task = next(
                (test[len(prefix):] for t, prefix in RUST_TASK.values() if t == target and test.startswith(prefix)),
                None,
            )
            if task is not None:
                if task in scripts:
                    scripts[task] = "pass" if ok else "fail"
            elif target == RUST_E2E:
                golden[f"tests::{test}"] = ok
            elif target == RUST_LIB and RUST_GOLDEN.match(test):
                golden[test] = ok
            else:
                unit[test if target == RUST_LIB else f"{target}::{test}"] = ok
    result["scripts"] = scripts
    result["regression"] = summarize(golden, unit)
    return result


def touch_rust_runners(ws: Path) -> None:
    for runner in ("src/sql/mod.rs", "tests/tests.rs"):
        if (ws / runner).is_file():
            (ws / runner).touch()


def grade_python(
    ws: Path, kinds: dict[str, str], staged: Path, tasks: dict[str, Path], log: Log, timeout: float
) -> dict:
    env = dict(os.environ)
    env.pop("UPDATE_GOLDENFILES", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    python = sys.executable or "python3"

    status, _, build_s = run(log, [python, "-c", PYTHON_BUILD], ws, env, timeout)
    result: dict = {"build_ok": status == 0, "times": {"build_s": round(build_s, 1), "test_s": 0.0}}
    scripts = {name: "fail" for name in kinds}
    golden: dict[str, bool] = {}
    unit: dict[str, bool] = {}
    if status == 0:
        # The regression suite, with empty task directories.
        status1, output, t1 = run(log, [python, "run_goldenscripts.py", "-q"], ws, env, timeout)
        for match in re.finditer(r"^(PASS|FAIL|SKIP) (\S+?):?(?: \((\S+)\))?(?::.*)?$", output, re.M):
            golden[f"{match.group(2)} ({match.group(3)})"] = match.group(1) == "PASS"
        status2, output, t2 = run(
            log, [python, "-m", "unittest", "discover", "-v", "-s", "tests", "-t", "."], ws, env, timeout
        )
        failed = set(re.findall(r"^(?:FAIL|ERROR): \S+ \(([^)]+)\)", output, re.M))
        for match in re.finditer(r"^(\w+) \(([\w.]+)\)", output, re.M):
            if match.group(2).endswith("." + match.group(1)):
                unit[match.group(2)] = match.group(2) not in failed
        for test in failed:
            unit[test] = False
        ran = re.search(r"^Ran (\d+) tests?", output, re.M)
        result["tests_completed"] = status1 in (0, 1) and status2 in (0, 1) and ran is not None

        # The given scripts.
        t3 = 0.0
        if kinds:
            for name, kind in kinds.items():
                shutil.copyfile(staged / kind / name, tasks[kind] / name)
            dirs = [str(tasks[kind]) for kind in KINDS if kind in kinds.values()]
            status3, output, t3 = run(log, [python, "run_goldenscripts.py", *dirs], ws, env, timeout)
            for match in re.finditer(r"^(PASS|FAIL) (\S+?):?(?: .*)?$", output, re.M):
                name = Path(match.group(2)).name
                if name in scripts:
                    scripts[name] = "pass" if match.group(1) == "PASS" else "fail"
            if not re.search(r"^\d+/\d+ scripts passed$", output, re.M):
                result["tests_completed"] = False
        result["times"]["test_s"] = round(t1 + t2 + t3, 1)
    result["scripts"] = scripts
    result["regression"] = summarize(golden, unit)
    return result


def summarize(golden: dict[str, bool], unit: dict[str, bool]) -> dict:
    both = {**golden, **unit}
    return {
        "passed": sum(both.values()),
        "total": len(both),
        "golden_passed": sum(golden.values()),
        "golden_total": len(golden),
        "unit_passed": sum(unit.values()),
        "unit_total": len(unit),
        "failed": sorted(test for test, ok in both.items() if not ok),
    }


def collect(scripts_dir: Path) -> dict[str, Path] | str:
    """Returns the given scripts by name, or an error message."""
    sources: dict[str, Path] = {}
    for entry in sorted(scripts_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if not (entry.is_dir() and entry.name in KINDS):
            return f"{scripts_dir} may only contain the subdirectories {' and '.join(KINDS)}, found {entry.name}"
    for kind in KINDS:
        if not (scripts_dir / kind).is_dir():
            continue
        for path in sorted((scripts_dir / kind).iterdir()):
            if path.name.startswith("."):
                continue
            if not path.is_file():
                return f"{path} is not a file"
            if not NAME.match(path.name):
                return f"script names must be snake_case identifiers, got {kind}/{path.name}"
            if path.name in sources:
                return f"script name {path.name} is used in both sql/ and cluster/"
            sources[path.name] = path
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("language", choices=sorted(TASK_DIRS))
    parser.add_argument("workspace", type=Path)
    parser.add_argument("scripts_dir", type=Path, help="directory with sql/ and/or cluster/ subdirectories")
    parser.add_argument("--json", type=Path, help="write the result as JSON to this file")
    parser.add_argument("--log", type=Path, help="log file (default: <json>.log, or ./grade.log)")
    parser.add_argument("--timeout", type=float, default=1800, help="timeout per command in seconds")
    args = parser.parse_args()

    ws: Path = args.workspace.resolve()
    marker = "Cargo.toml" if args.language == "rust" else "run_goldenscripts.py"
    if not (ws / marker).is_file():
        print(f"error: {ws} is not a {args.language} workspace (no {marker})", file=sys.stderr)
        return 2
    if not args.scripts_dir.is_dir():
        print(f"error: {args.scripts_dir} is not a directory", file=sys.stderr)
        return 2
    sources = collect(args.scripts_dir)
    if isinstance(sources, str):
        print(f"error: {sources}", file=sys.stderr)
        return 2
    kinds = {name: path.parent.name for name, path in sources.items()}

    log_path = args.log or (args.json.with_suffix(".log") if args.json else Path("grade.log"))
    log_path = log_path.resolve()
    log = Log(log_path)
    log.write(
        f"grade.py {args.language} {ws} {args.scripts_dir.resolve()}\n"
        + "\n".join(f"{kind} scripts: {' '.join(n for n, k in kinds.items() if k == kind)}" for kind in KINDS)
    )

    # Turn SIGTERM into an exception, so that the task directories are
    # restored and the running command is killed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))

    start = time.monotonic()
    tasks = {kind: ws / path for kind, path in TASK_DIRS[args.language].items()}
    with tempfile.TemporaryDirectory(prefix="grade-") as tmp:
        # Snapshot the scripts (scripts_dir may be anywhere, even inside a task
        # directory), then swap out the task directories.
        staged = Path(tmp) / "staged"
        for kind in KINDS:
            (staged / kind).mkdir(parents=True)
        for name, source in sources.items():
            shutil.copyfile(source, staged / kinds[name] / name)
        backups: dict[str, Path] = {}
        try:
            for kind, task in tasks.items():
                if task.exists():
                    backups[kind] = Path(tmp) / f"backup-{kind}"
                    shutil.move(str(task), str(backups[kind]))
                task.mkdir(parents=True)
            if args.language == "rust":
                for name, kind in kinds.items():
                    shutil.copyfile(staged / kind / name, tasks[kind] / name)
                result = grade_rust(ws, kinds, log, args.timeout)
            else:
                result = grade_python(ws, kinds, staged, tasks, log, args.timeout)
        finally:
            for kind, task in tasks.items():
                shutil.rmtree(task, ignore_errors=True)
                if kind in backups:
                    shutil.move(str(backups[kind]), str(task))
            if args.language == "rust":
                touch_rust_runners(ws)  # the restored scripts need a rebuild

    scripts = result["scripts"]
    regression = result["regression"]
    result = {
        "language": args.language,
        "workspace": str(ws),
        "scripts_dir": str(args.scripts_dir.resolve()),
        "build_ok": result["build_ok"],
        "tests_completed": result.get("tests_completed", False),
        "scripts": scripts,
        "script_kinds": kinds,
        "scripts_passed": sum(v == "pass" for v in scripts.values()),
        "scripts_total": len(scripts),
        "regression": regression,
        "times": {**result["times"], "total_s": round(time.monotonic() - start, 1)},
        "log": str(log_path),
    }
    ok = (
        result["build_ok"]
        and result["tests_completed"]
        and result["scripts_passed"] == result["scripts_total"]
        and regression["passed"] == regression["total"]
        and regression["total"] > 0
    )
    result["ok"] = ok
    log.write("\n===== result\n" + json.dumps(result, indent=2))

    print(f"build:      {'ok' if result['build_ok'] else 'FAILED'}")
    for name, verdict in scripts.items():
        print(f"script:     {verdict.upper():4} {kinds[name]}/{name}")
    print(f"scripts:    {result['scripts_passed']}/{result['scripts_total']} passed")
    print(
        f"regression: {regression['passed']}/{regression['total']} passed "
        f"(golden {regression['golden_passed']}/{regression['golden_total']}, "
        f"unit {regression['unit_passed']}/{regression['unit_total']})"
    )
    for test in regression["failed"][:20]:
        print(f"  failed: {test}")
    if not result["tests_completed"] and result["build_ok"]:
        print("warning: the test run did not complete (crash or timeout); see the log")
    times = result["times"]
    print(f"time:       build {times['build_s']}s, test {times['test_s']}s, total {times['total_s']}s")
    print(f"log:        {log_path}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
