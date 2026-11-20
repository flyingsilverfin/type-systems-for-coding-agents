"""Create an isolated workspace for one agent run.

usage: make_run.py <task> <python|rust> <run_id> [model] [effort]
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import oracle  # noqa: E402
from tasks import TASKS  # noqa: E402

ROOT = Path("/home/user/agentbench")
BENCH = ROOT / "bench"
VISIBLE_DATA = ROOT / "data" / "visible"
PY_DRIVER = Path.home() / ".local/lib/python3.12/site-packages/typedb"
RS_DRIVER = Path(glob.glob(str(Path.home() / ".cargo/registry/src/*/typedb-driver-3.12.0"))[0])
DRIVER_REPO = ROOT.parent / "typedb-dev/repositories/typedb-driver"

CHECK_SH = r"""#!/usr/bin/env bash
# Build the app and run the visible tests.  Usage: ./check.sh
# Do not modify this file.
set -u
WS="$(cd "$(dirname "$0")" && pwd)"
cd "$WS"
export LENDING_DB="ws_{run}"
export CARGO_NET_OFFLINE=true
export PATH="$HOME/.cargo/bin:$PATH"
LOG="{logdir}/{run}.checks.jsonl"
t0=$(date +%s.%N)
echo "== build"
{build}
build_rc=$?
t1=$(date +%s.%N)
tests_rc=1
if [ $build_rc -eq 0 ]; then
  echo "== visible tests"
  python3 -s tests/run_tests.py tests/visible/cases.json tests/visible/data -- {app}
  tests_rc=$?
else
  echo "BUILD FAILED"
fi
t2=$(date +%s.%N)
python3 -c "import json,sys; print(json.dumps(dict(t=float(sys.argv[1]), build_s=float(sys.argv[2])-float(sys.argv[1]), test_s=float(sys.argv[3])-float(sys.argv[2]), build_ok=sys.argv[4]=='0', tests_ok=sys.argv[5]=='0')))" "$t0" "$t1" "$t2" "$build_rc" "$tests_rc" >> "$LOG" 2>/dev/null
[ $build_rc -eq 0 ] && [ $tests_rc -eq 0 ]
"""

LANG = {
    "python": dict(
        build='python3 -s -m compileall -q app/lending',
        app='env PYTHONPATH="$WS/app:$WS/driver" python3 -s -m lending',
    ),
    "rust": dict(
        build='(cd app && cargo build --quiet --color never 2>&1)',
        app='"$WS/app/target/debug/lending"',
    ),
}


def expected_cases(task: str, data_dir: Path, case_args: list[list[str]]) -> list[dict]:
    out = []
    for args in case_args:
        lines, code = oracle.run(task, data_dir, args)
        out.append(dict(args=args, expected=lines, exit=code))
    return out


def apply_injections(task: dict, lang: str, app_dir: Path) -> None:
    for rel, old, new in task["inject"].get(lang, []):
        p = app_dir / rel
        s = p.read_text()
        assert s.count(old) == 1, f"injection anchor not found exactly once in {rel}"
        p.write_text(s.replace(old, new))


def make(task_id: str, lang: str, run_id: str, model: str = "", effort: str = "") -> Path:
    task = TASKS[task_id]
    ws = ROOT / "runs" / run_id
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "meta").mkdir(exist_ok=True)

    # app source
    if lang == "python":
        shutil.copytree(ROOT / "template/python/lending", ws / "app/lending", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(PY_DRIVER, ws / "driver/typedb", ignore=shutil.ignore_patterns("__pycache__"))
        (ws / "docs").mkdir()
        shutil.copy(DRIVER_REPO / "python/example.py", ws / "docs/driver_example.py")
        readme_lang = (BENCH / "readme_python.md").read_text()
    else:
        (ws / "app").mkdir()
        for f in ("Cargo.toml", "Cargo.lock"):
            shutil.copy(ROOT / "template/rust" / f, ws / "app" / f)
        shutil.copytree(ROOT / "template/rust/src", ws / "app/src")
        subprocess.run(["cp", "-r", "--reflink=auto", str(ROOT / "template/rust/target"), str(ws / "app/target")], check=True)
        shutil.copytree(RS_DRIVER, ws / "driver-src")
        (ws / "docs").mkdir()
        shutil.copy(DRIVER_REPO / "rust/example.rs", ws / "docs/driver_example.rs")
        readme_lang = (BENCH / "readme_rust.md").read_text()
    apply_injections(task, lang, ws / "app")

    (ws / "README.md").write_text((BENCH / "app_readme.md").read_text() + readme_lang)
    (ws / "TASK.md").write_text(
        f"# Task: {task['title']}\n\n{task['spec']}\n"
        "## How to work\n\n"
        "- Read `README.md` first: it describes the commands, output formats and code layout.\n"
        "- Validate with `./check.sh`: it builds the app and runs the visible tests against a TypeDB server that\n"
        "  is already running. The visible tests are a small sample on a small dataset. Your work will also be\n"
        "  graded by a larger set of tests you cannot see, on different data, covering every command\n"
        "  (including ones this task does not mention).\n"
        "- Do not modify `check.sh` or anything under `tests/`.\n"
    )

    # visible tests
    (ws / "tests/visible").mkdir(parents=True)
    shutil.copytree(VISIBLE_DATA, ws / "tests/visible/data")
    shutil.copy(BENCH / "runner.py", ws / "tests/run_tests.py")
    cases = expected_cases(task_id, VISIBLE_DATA, task["visible"])
    (ws / "tests/visible/cases.json").write_text(json.dumps(cases, indent=1))

    check = CHECK_SH.format(run=run_id, logdir=ROOT / "logs", **LANG[lang])
    (ws / "check.sh").write_text(check)
    os.chmod(ws / "check.sh", 0o755)

    if lang == "rust":  # warm the incremental build so the agent's first check is not a cold compile
        subprocess.run("~/.cargo/bin/cargo build --quiet", shell=True, cwd=ws / "app", capture_output=True)
    (ws / ".gitignore").write_text("target/\n__pycache__/\n")
    subprocess.run("git init -q && git add -A && git -c user.name=bench -c user.email=bench@localhost commit -qm baseline",
                   shell=True, cwd=ws, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True).stdout.strip()

    meta = dict(run=run_id, task=task_id, lang=lang, model=model, effort=effort, ws=str(ws), baseline=base,
                created=time.time())
    (ROOT / "meta" / f"{run_id}.json").write_text(json.dumps(meta, indent=1))
    log = ROOT / "logs" / f"{run_id}.checks.jsonl"
    if log.exists():
        log.unlink()
    return ws


if __name__ == "__main__":
    print(make(*sys.argv[1:]))
