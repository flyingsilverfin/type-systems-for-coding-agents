"""Grade toydb benchmark runs with tiers/<tier>/grade.py on the task's hidden + visible scripts.

usage: grade_toydb.py <run_id>...   -> writes /home/user/agentbench/results/<run_id>.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/home/user/agentbench")
TOYDB = ROOT / "toydb"


def hidden_files(tdir: Path, tier: str, kind: str) -> list[Path]:
    """Hidden scripts of every tier from t2 up to `tier` (kind: sql or cluster)."""
    out = []
    for n in range(2, int(tier[1:]) + 1):
        d = tdir / f"hidden_t{n}" / kind
        if d.exists():
            out += sorted(d.iterdir())
    return out


def grade(run: str) -> dict:
    meta = json.loads((ROOT / "meta" / f"{run}.json").read_text())
    tier, lang, task = meta["tier"], meta["lang"], meta["task"]
    tdir = TOYDB / "tasks" / task
    out = ROOT / "results" / f"{run}.json"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        scripts = Path(tmp) / "scripts"
        sql = scripts / "sql" if tier != "t1" else scripts
        sql.mkdir(parents=True)
        for f in list((tdir / "hidden").iterdir()) + list((tdir / "visible").iterdir()) + hidden_files(tdir, tier, "sql"):
            shutil.copy(f, sql / f.name)
        cl = hidden_files(tdir, tier, "cluster")
        if tier != "t1" and cl:
            (scripts / "cluster").mkdir()
            for f in cl:
                shutil.copy(f, scripts / "cluster" / f.name)
        gj = Path(tmp) / "grade.json"
        subprocess.run([sys.executable, str(TOYDB / "tiers" / tier / "grade.py"), lang, meta["ws"], str(scripts),
                        "--json", str(gj), "--log", str(out.with_suffix(".log")), "--timeout", "900"],
                       capture_output=True, text=True)
        g = json.loads(gj.read_text()) if gj.exists() else {"build_ok": False, "scripts": {}, "regression": {}}
    visible = {f.name for f in (tdir / "visible").iterdir()}
    sc = g.get("scripts", {})
    hidden = {k: v for k, v in sc.items() if k not in visible}
    cluster = {f.name for f in hidden_files(tdir, tier, "cluster")}
    ext = {f.name for f in hidden_files(tdir, tier, "sql") + hidden_files(tdir, tier, "cluster") if f"hidden_{tier}" in str(f)} if tier >= "t3" else set()
    reg = g.get("regression", {})
    checks = []
    lf = ROOT / "logs" / f"{run}.checks.jsonl"
    if lf.exists():
        checks = [json.loads(l) for l in lf.read_text().splitlines() if l.strip()]
    res = dict(run=run, task=task, tier=tier, lang=lang, model=meta.get("model"), effort=meta.get("effort"),
               build_ok=g.get("build_ok", False),
               hidden_passed=sum(v == "pass" for v in hidden.values()), hidden_total=len(hidden),
               visible_passed=sum(sc.get(v) == "pass" for v in visible), visible_total=len(visible),
               cluster_passed=sum(sc.get(c) == "pass" for c in cluster), cluster_total=len(cluster),
               ext_passed=sum(sc.get(c) == "pass" for c in ext), ext_total=len(ext),
               regression_passed=reg.get("passed"), regression_total=reg.get("total"),
               regression_failed=reg.get("failed", []), hidden=hidden, tests_completed=g.get("tests_completed"),
               grade_times=g.get("times"), checks=len(checks), check_build_s=round(sum(c["build_s"] for c in checks), 1),
               check_test_s=round(sum(c["test_s"] for c in checks), 1),
               last_check_ok=bool(checks) and checks[-1]["rc"] == 0)
    res["pass"] = bool(res["build_ok"] and res["hidden_passed"] == res["hidden_total"] and res["visible_passed"] == res["visible_total"]
                       and not res["regression_failed"])
    res["escaped"] = res["last_check_ok"] and not res["pass"]
    out.write_text(json.dumps(res, indent=1))
    cleanup(Path(meta["ws"]))
    return res


def sweep_tmp() -> None:
    """Remove the temp dirs that upstream toydb's bitcask tests leak (one 400 MB file each)."""
    for d in Path("/tmp").glob("toydb*"):
        try:
            if d.is_dir() and os.listdir(d) == ["bitcask"]:
                shutil.rmtree(d)
        except OSError:
            pass


def cleanup(ws: Path) -> None:
    """After grading: drop the build output (sources and git history stay) and leaked test temp dirs."""
    sweep_tmp()
    for t in [ws / "target", ws / "app" / "target"]:
        if t.is_dir() and ROOT / "runs" in t.parents:
            shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    for r in sys.argv[1:]:
        x = grade(r)
        print(f"{r:28} pass={x['pass']!s:5} hidden={x['hidden_passed']}/{x['hidden_total']} vis={x['visible_passed']}/{x['visible_total']} cl={x['cluster_passed']}/{x['cluster_total']} ext={x['ext_passed']}/{x['ext_total']} "
              f"reg_failed={len(x['regression_failed'])} checks={x['checks']} build_s={x['check_build_s']} test_s={x['check_test_s']}")
