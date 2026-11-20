"""Grade a finished v2 run.  usage: grade2.py <run_id> [...]"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from make_run2 import cases as expected_cases  # noqa: E402
from runner import run_cases  # noqa: E402
from tasks2 import TASKS2, V2_HIDDEN  # noqa: E402
from tasks3 import TASKS3  # noqa: E402

ROOT = Path("/home/user/agentbench")
HIDDEN = ROOT / "hidden" / "data2"


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, **kw)


def driver():
    from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TypeDB
    return TypeDB.driver("127.0.0.1:1729", Credentials("admin", "password"), DriverOptions(DriverTlsConfig.disabled()))


def structural(check, ws: Path, lang: str, db: str) -> str | None:
    src = ws / ("app/lending" if lang == "python" else "app/src")
    schema = (src / ("schema.py" if lang == "python" else "schema.rs")).read_text()
    if check == "no_loan_returned_field":
        model = (src / ("model.py" if lang == "python" else "model.rs")).read_text()
        if re.search(r"^\s+(pub\s+)?(returned|lost)\s*:\s*(Optional\[|Option<)", model, re.M):
            return "a loan type still has optional returned/lost fields"
    elif check == "member_id_integer":
        if not re.search(r"attribute\s+member-id\s*,\s*value\s+integer", schema):
            return "member-id is not an integer attribute"
    elif check == "name_renamed":
        if not re.search(r"attribute\s+full-name\s*,", schema) or re.search(r"attribute\s+name\s*,", schema):
            return "schema not renamed"
    elif check == "deleted_stored":
        try:
            d = driver()
            from typedb.driver import TransactionType
            with d.transaction(db, TransactionType.READ) as tx:
                n = len(list(tx.query("match $m isa member;").resolve().as_concept_rows()))
            d.close()
            if n != 41:
                return f"expected 41 stored members, found {n}"
        except Exception as e:  # noqa: BLE001
            return f"could not count members: {e}"
    return None


def grade(run_id: str) -> dict:
    meta = json.loads((ROOT / "meta" / f"{run_id}.json").read_text())
    ws, lang = Path(meta["ws"]), meta["lang"]
    t = TASKS3.get(meta["task"]) or TASKS2[meta["task"]]
    hidden = ROOT / "hidden" / "data3" / meta["task"] if meta["task"] in TASKS3 else HIDDEN
    res: dict = dict(meta)
    sh("git add -A", ws)
    res["tampered"] = sh(f"git diff --cached --name-only {meta['baseline']} -- check.sh tests", ws).stdout.split()
    res["diff_shortstat"] = sh(f"git diff --cached --shortstat {meta['baseline']} -- app", ws).stdout.strip()
    nums = re.findall(r"(\d+) (insertion|deletion)", res["diff_shortstat"])
    res["lines_added"] = sum(int(n) for n, k in nums if k == "insertion")
    res["lines_deleted"] = sum(int(n) for n, k in nums if k == "deletion")
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / f"{run_id}.diff").write_text(sh(f"git diff --cached {meta['baseline']} -- app", ws).stdout)

    db = f"grade_{run_id}"
    env = dict(os.environ, LENDING_DB=db, CARGO_NET_OFFLINE="true", PATH=f"{Path.home()}/.cargo/bin:" + os.environ["PATH"])
    if lang == "python":
        b = sh("python3 -s -m compileall -q app/lending", ws)
        app = ["python3", "-s", "-W", "ignore", "-m", "lending"]
        env["PYTHONPATH"] = f"{ws}/app:{ws}/driver"
    else:
        b = sh("cargo build --quiet --color never", ws / "app", timeout=900, env=env)
        app = [str(ws / "app/target/debug/lending")]
    res["build_ok"] = b.returncode == 0
    res["build_output"] = (b.stdout + b.stderr)[-2000:]
    cs = expected_cases(t["oracle"], hidden, t["hidden"])
    if res["build_ok"]:
        results, setup_ok = run_cases(app, env, str(hidden), cs, cwd=str(ws), verbose=False)
    else:
        results, setup_ok = [dict(args=c["args"], passed=False, reason="build failed") for c in cs], False
    n_base = len(V2_HIDDEN)
    res.update(setup_ok=setup_ok, hidden=results, hidden_passed=sum(r["passed"] for r in results), hidden_total=len(results),
               regressions_failed=[r["args"] for r in results[:n_base] if not r["passed"]],
               task_failed=[r["args"] for r in results[n_base:] if not r["passed"]])
    res["structural_failures"] = [m for m in (structural(c, ws, lang, db) for c in t["structural"]) if m] if res["build_ok"] else []
    res["pass"] = bool(res["build_ok"] and res["hidden_passed"] == res["hidden_total"] and not res["structural_failures"]
                       and not res["tampered"])

    log = ROOT / "logs" / f"{run_id}.checks.jsonl"
    checks = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    res.update(checks=len(checks), check_build_s=round(sum(c["build_s"] for c in checks), 1),
               check_test_s=round(sum(c["test_s"] for c in checks), 1),
               check_build_failures=sum(1 for c in checks if not c["build_ok"]),
               check_test_failures=sum(1 for c in checks if c["build_ok"] and not c["tests_ok"]),
               final_visible_ok=bool(checks and checks[-1]["build_ok"] and checks[-1]["tests_ok"]))
    try:
        d = driver()
        for name in (db, f"ws_{run_id}"):
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
              f"regress={[' '.join(a) for a in r['regressions_failed']]} task={[' '.join(a) for a in r['task_failed']]} "
              f"struct={r['structural_failures']} tamper={r['tampered']} checks={r['checks']} "
              f"bfail={r['check_build_failures']} tfail={r['check_test_failures']} final_vis={r['final_visible_ok']}")
