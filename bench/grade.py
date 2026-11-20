"""Grade a finished run against the hidden tests.  usage: grade.py <run_id> [...]"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from make_run import expected_cases  # noqa: E402
from runner import run_cases  # noqa: E402
from tasks import TASKS  # noqa: E402

ROOT = Path("/home/user/agentbench")
HIDDEN_DATA = ROOT / "hidden" / "data"


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, **kw)


def grade(run_id: str) -> dict:
    meta = json.loads((ROOT / "meta" / f"{run_id}.json").read_text())
    ws = Path(meta["ws"])
    task = TASKS[meta["task"]]
    lang = meta["lang"]
    res: dict = dict(meta)

    # tamper detection + diff
    sh("git add -A", ws)
    tamper = sh(f"git diff --cached --name-only {meta['baseline']} -- check.sh tests", ws).stdout.split()
    res["tampered"] = tamper
    res["diff_shortstat"] = sh(f"git diff --cached --shortstat {meta['baseline']} -- app", ws).stdout.strip()
    nums = re.findall(r"(\d+) (insertion|deletion)", res["diff_shortstat"])
    res["lines_added"] = sum(int(n) for n, k in nums if k == "insertion")
    res["lines_deleted"] = sum(int(n) for n, k in nums if k == "deletion")
    diff = sh(f"git diff --cached {meta['baseline']} -- app ':!app/target'", ws).stdout
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / f"{run_id}.diff").write_text(diff)

    # build
    env = dict(os.environ, LENDING_DB=f"grade_{run_id}", CARGO_NET_OFFLINE="true",
               PATH=f"{Path.home()}/.cargo/bin:" + os.environ["PATH"])
    if lang == "python":
        b = sh("python3 -s -m compileall -q app/lending", ws)
        app = ["python3", "-s", "-m", "lending"]
        env["PYTHONPATH"] = f"{ws}/app:{ws}/driver"
    else:
        b = sh("cargo build --quiet --color never", ws / "app", timeout=900, env=env)
        app = [str(ws / "app/target/debug/lending")]
    res["build_ok"] = b.returncode == 0
    res["build_output"] = (b.stdout + b.stderr)[-2000:]

    cases = expected_cases(meta["task"], HIDDEN_DATA, task["hidden"])
    if res["build_ok"]:
        results, setup_ok = run_cases(app, env, str(HIDDEN_DATA), cases, cwd=str(ws), verbose=False)
    else:
        results, setup_ok = [dict(args=c["args"], passed=False, reason="build failed") for c in cases], False
    res["setup_ok"] = setup_ok
    res["hidden"] = results
    res["hidden_passed"] = sum(r["passed"] for r in results)
    res["hidden_total"] = len(results)
    res["regressions_failed"] = [r["args"] for r in results[:9] if not r["passed"]]
    res["task_failed"] = [r["args"] for r in results[9:] if not r["passed"]]

    # structural checks
    struct = []
    if meta["task"] == "T08":
        src = "app/lending" if lang == "python" else "app/src"
        if sh(f"grep -rn 'read_rows' {src}", ws).stdout.strip():
            struct.append("read_rows still present")
    res["structural_failures"] = struct

    res["pass"] = bool(res["build_ok"] and res["hidden_passed"] == res["hidden_total"] and not struct and not tamper)

    # validator log
    log = ROOT / "logs" / f"{run_id}.checks.jsonl"
    checks = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    res["checks"] = len(checks)
    res["check_build_s"] = round(sum(c["build_s"] for c in checks), 1)
    res["check_test_s"] = round(sum(c["test_s"] for c in checks), 1)
    res["check_build_failures"] = sum(1 for c in checks if not c["build_ok"])
    res["check_test_failures"] = sum(1 for c in checks if c["build_ok"] and not c["tests_ok"])
    res["final_visible_ok"] = bool(checks and checks[-1]["build_ok"] and checks[-1]["tests_ok"])

    # drop the grading database
    try:
        from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TypeDB
        d = TypeDB.driver("127.0.0.1:1729", Credentials("admin", "password"), DriverOptions(DriverTlsConfig.disabled()))
        for name in (f"grade_{run_id}", f"ws_{run_id}"):
            if d.databases.contains(name):
                d.databases.get(name).delete()
        d.close()
    except Exception as e:  # noqa: BLE001
        res["cleanup_error"] = str(e)

    (ROOT / "results" / f"{run_id}.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    for rid in sys.argv[1:]:
        r = grade(rid)
        print(f"{rid}: pass={r['pass']} hidden={r['hidden_passed']}/{r['hidden_total']} build={r['build_ok']} "
              f"regress={r['regressions_failed']} task_failed={r['task_failed']} struct={r['structural_failures']} "
              f"tamper={r['tampered']} checks={r['checks']} build_s={r['check_build_s']}")
