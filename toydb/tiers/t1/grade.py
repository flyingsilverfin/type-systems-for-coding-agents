#!/usr/bin/env python3
"""Grades a tier-1 toydb workspace against a directory of golden scripts.

    grade.py <rust|python> <workspace> <scripts_dir> [--json out.json] [--log grade.log]

The scripts in <scripts_dir> (SQL golden scripts, snake_case file names) are
copied into the workspace's task script directory, replacing its contents for
the duration of the run; the directory is restored afterwards. The scripts are
run with the workspace's ordinary SQL golden script runner, along with the full
regression suite (every other golden script and the unit tests).

Reports, per given script, pass/fail; the regression suite's passed/total;
whether the build succeeded; and wall times. The complete tool output,
including the diffs of failing scripts, is written to the log file.

Exits 0 if the build is ok, all given scripts pass and there are no regression
failures, 1 otherwise, 2 on usage errors.

Standard library only. The workspace may be a copy anywhere on disk.
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

TASK_DIR = {
    "rust": "src/sql/testscripts/task",
    "python": "tests/sql/testscripts/task",
}

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")

# Rust test names of golden script runs, as opposed to unit tests.
RUST_GOLDEN = re.compile(r"(sql::tests|storage::[a-z]+::tests::(engine|scripts))::")
RUST_TASK = "sql::tests::task::"

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
    output, and wall time. The output is appended to the log."""
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
    elapsed = time.monotonic() - start
    output = raw.decode("utf-8", errors="replace")
    log.write(output)
    log.write(f"===== exit status {status}, {elapsed:.1f}s")
    return status, output, elapsed


def grade_rust(ws: Path, names: list[str], log: Log, timeout: float) -> dict:
    env = dict(os.environ)
    env.pop("UPDATE_GOLDENFILES", None)
    env["PATH"] = f"{Path.home() / '.cargo' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["CARGO_TARGET_DIR"] = str(ws / "target")
    env["CARGO_TERM_COLOR"] = "never"

    # Golden scripts are discovered at compile time: force recompilation.
    (ws / "src/sql/mod.rs").touch()

    status, _, build_s = run(log, ["cargo", "test", "--no-run"], ws, env, timeout)
    result: dict = {"build_ok": status == 0, "times": {"build_s": round(build_s, 1), "test_s": 0.0}}
    scripts = {name: "fail" for name in names}
    regression: dict[str, bool] = {}
    if status == 0:
        status, output, test_s = run(log, ["cargo", "test", "--no-fail-fast"], ws, env, timeout)
        result["times"]["test_s"] = round(test_s, 1)
        result["tests_completed"] = status in (0, 101) and "test result:" in output
        for match in re.finditer(r"^test (\S+)(?: - should panic)? \.\.\. (ok|FAILED)$", output, re.M):
            test, ok = match.group(1), match.group(2) == "ok"
            if test.startswith(RUST_TASK):
                name = test[len(RUST_TASK):]
                if name in scripts:
                    scripts[name] = "pass" if ok else "fail"
            else:
                regression[test] = ok
    result["scripts"] = scripts
    result["regression"] = summarize(
        {t: ok for t, ok in regression.items() if RUST_GOLDEN.match(t)},
        {t: ok for t, ok in regression.items() if not RUST_GOLDEN.match(t)},
    )
    return result


def grade_python(ws: Path, names: list[str], staged: Path, task: Path, log: Log, timeout: float) -> dict:
    env = dict(os.environ)
    env.pop("UPDATE_GOLDENFILES", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    python = sys.executable or "python3"

    status, _, build_s = run(log, [python, "-c", PYTHON_BUILD], ws, env, timeout)
    result: dict = {"build_ok": status == 0, "times": {"build_s": round(build_s, 1), "test_s": 0.0}}
    scripts = {name: "fail" for name in names}
    golden: dict[str, bool] = {}
    unit: dict[str, bool] = {}
    if status == 0:
        # The regression suite, with an empty task directory.
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
        for name in names:
            shutil.copyfile(staged / name, task / name)
        _, output, t3 = run(log, [python, "run_goldenscripts.py", str(task)], ws, env, timeout)
        for match in re.finditer(r"^(PASS|FAIL) (\S+?):?(?: .*)?$", output, re.M):
            name = Path(match.group(2)).name
            if name in scripts:
                scripts[name] = "pass" if match.group(1) == "PASS" else "fail"
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("language", choices=sorted(TASK_DIR))
    parser.add_argument("workspace", type=Path)
    parser.add_argument("scripts_dir", type=Path)
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
    sources = sorted(p for p in args.scripts_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    names = [p.name for p in sources]
    bad = [name for name in names if not NAME.match(name)]
    if bad or not names:
        print(f"error: script names must be snake_case identifiers, got {bad or 'no scripts'}", file=sys.stderr)
        return 2

    log_path = args.log or (args.json.with_suffix(".log") if args.json else Path("grade.log"))
    log_path = log_path.resolve()
    log = Log(log_path)
    log.write(f"grade.py {args.language} {ws} {args.scripts_dir.resolve()}\nscripts: {' '.join(names)}")

    start = time.monotonic()
    task = ws / TASK_DIR[args.language]
    with tempfile.TemporaryDirectory(prefix="grade-") as tmp:
        # Snapshot the scripts (scripts_dir may be anywhere, even the task
        # directory itself), then swap out the task directory.
        staged = Path(tmp) / "staged"
        staged.mkdir()
        for source in sources:
            shutil.copyfile(source, staged / source.name)
        backup = Path(tmp) / "backup"
        existed = task.exists()
        if existed:
            shutil.move(str(task), str(backup))
        task.mkdir(parents=True)
        try:
            if args.language == "rust":
                for name in names:
                    shutil.copyfile(staged / name, task / name)
                result = grade_rust(ws, names, log, args.timeout)
            else:
                result = grade_python(ws, names, staged, task, log, args.timeout)
        finally:
            shutil.rmtree(task, ignore_errors=True)
            if existed:
                shutil.move(str(backup), str(task))
            if args.language == "rust" and (ws / "src/sql/mod.rs").is_file():
                (ws / "src/sql/mod.rs").touch()  # the restored scripts need a rebuild

    scripts = result["scripts"]
    regression = result["regression"]
    result = {
        "language": args.language,
        "workspace": str(ws),
        "scripts_dir": str(args.scripts_dir.resolve()),
        "build_ok": result["build_ok"],
        "tests_completed": result.get("tests_completed", False),
        "scripts": scripts,
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
        print(f"script:     {verdict.upper():4} {name}")
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
