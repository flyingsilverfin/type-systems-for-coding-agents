"""Create an isolated v2 workspace.  usage: make_run2.py <task> <python|rust> <run_id> [model] [effort]"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import oracle2  # noqa: E402
import oracle3  # noqa: E402
from tasks3 import TASKS3  # noqa: E402
from make_run import CHECK_SH, DRIVER_REPO, LANG, PY_DRIVER, RS_DRIVER  # noqa: E402
from tasks2 import TASKS2, UPGRADE_HOW  # noqa: E402

ROOT = Path("/home/user/agentbench")
BENCH = ROOT / "bench"
TPL = ROOT / "template2"
VISIBLE = ROOT / "data2" / "visible"


def cases(oracle_id, data_dir, case_args):
    out = []
    for a in case_args:
        lines, code = (oracle3 if oracle_id in TASKS3 else oracle2).run(oracle_id, data_dir, a)
        out.append(dict(args=a, expected=lines, exit=code))
    return out


def make(task_id, lang, run_id, model="", effort="", novis=""):
    novis = novis == "novis"
    t = TASKS3.get(task_id) or TASKS2[task_id]
    visible = ROOT / "data3" / task_id if task_id in TASKS3 else VISIBLE
    ws = ROOT / "runs" / run_id
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    drv = ROOT / "drivers" / t["driver"] / lang if t["driver"] else None

    if lang == "python":
        shutil.copytree(TPL / "python/lending", ws / "app/lending", ignore=shutil.ignore_patterns("__pycache__"))
        if drv:
            shutil.copytree(drv / "typedb", ws / "driver/typedb", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(drv / "CHANGELOG.md", ws / "DRIVER_CHANGELOG.md")
            where, src = "`driver/typedb/` now contains typedb-driver 3.13.0", "driver/typedb/"
        else:
            shutil.copytree(PY_DRIVER, ws / "driver/typedb", ignore=shutil.ignore_patterns("__pycache__"))
        (ws / "docs").mkdir()
        shutil.copy(DRIVER_REPO / "python/example.py", ws / "docs/driver_example.py")
        readme_lang = (BENCH / "readme2_python.md").read_text()
    else:
        (ws / "app").mkdir()
        shutil.copytree(TPL / "rust/src", ws / "app/src")
        shutil.copy(TPL / "rust/Cargo.lock", ws / "app/Cargo.lock")
        subprocess.run(["cp", "-r", "--reflink=auto", str(TPL / "rust/target"), str(ws / "app/target")], check=True)
        cargo = (TPL / "rust/Cargo.toml").read_text()
        if drv:
            shutil.copytree(drv, ws / "driver")
            shutil.move(str(ws / "driver/CHANGELOG.md"), ws / "DRIVER_CHANGELOG.md")
            cargo = cargo.replace('typedb-driver = { version = "=3.12.0", features = ["sync"] }',
                                  'typedb-driver = { path = "../driver", features = ["sync"] }')
            assert 'path = "../driver"' in cargo
            where, src = "`app/Cargo.toml` now depends on typedb-driver 3.13.0, vendored in `driver/`", "driver/"
        else:
            shutil.copytree(RS_DRIVER, ws / "driver-src")
        (ws / "app/Cargo.toml").write_text(cargo)
        (ws / "docs").mkdir()
        shutil.copy(DRIVER_REPO / "rust/example.rs", ws / "docs/driver_example.rs")
        readme_lang = (BENCH / "readme2_rust.md").read_text()
        if drv:
            readme_lang = readme_lang.replace("The driver's source is in `driver-src/` for reference.",
                                              "The driver (3.13.0) is vendored in `driver/`.")

    spec = t["spec"].replace("{UPGRADE}", UPGRADE_HOW.format(where=where, src=src).strip() if drv else "")
    (ws / "README.md").write_text((BENCH / "app2_readme.md").read_text() + readme_lang)
    (ws / "TASK.md").write_text(
        f"# Task: {t['title']}\n\n{spec}\n"
        "## How to work\n\n"
        "- Read `README.md` first: it describes the commands, output formats and code layout.\n"
        + ("- There are no automated tests for this app. `./check.sh` builds the app, then runs `init` and `load` on the\n"
           "  sample data in `tests/visible/data` against a TypeDB server that is already running; you can run any\n"
           "  command yourself afterwards (see README.md). Your work will be graded by a set of tests you cannot see,\n"
           "  on different data, covering every command.\n" if novis else
        "- Validate with `./check.sh`: it builds the app and runs the visible tests against a TypeDB server that\n"
        "  is already running. The visible tests are a small sample (8 of the 21 commands) on a small dataset.\n"
        "  Your work will also be graded by a larger set of tests you cannot see, on different data, covering\n"
        "  every command.\n")
        + "- Do not modify `check.sh` or anything under `tests/`.\n")

    (ws / "tests/visible").mkdir(parents=True)
    shutil.copytree(visible, ws / "tests/visible/data")
    shutil.copy(BENCH / "runner.py", ws / "tests/run_tests.py")
    (ws / "tests/visible/cases.json").write_text(json.dumps([] if novis else cases(t["oracle"], visible, t["visible"]), indent=1))
    (ws / "check.sh").write_text(CHECK_SH.format(run=run_id, logdir=ROOT / "logs", **LANG[lang]))
    os.chmod(ws / "check.sh", 0o755)

    if lang == "rust":  # warm: build whatever compiles (upgrade tasks fail to compile by design, deps still get built)
        subprocess.run("~/.cargo/bin/cargo build --quiet --offline", shell=True, cwd=ws / "app", capture_output=True)
    (ws / ".gitignore").write_text("target/\n__pycache__/\n")
    subprocess.run("git init -q && git add -A && git -c user.name=bench -c user.email=bench@localhost commit -qm baseline",
                   shell=True, cwd=ws, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True).stdout.strip()
    meta = dict(run=run_id, task=task_id, suite="v3" if task_id in TASKS3 else "v2", lang=lang, model=model, effort=effort, novis=novis, ws=str(ws),
                baseline=base, created=time.time())
    (ROOT / "meta" / f"{run_id}.json").write_text(json.dumps(meta, indent=1))
    log = ROOT / "logs" / f"{run_id}.checks.jsonl"
    if log.exists():
        log.unlink()
    return ws


if __name__ == "__main__":
    print(make(*sys.argv[1:]))
