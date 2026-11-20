"""Apply each tier-1 run's final diff (excluding task script dirs) to a fresh tier-2 base and write meta for grading.

usage: transplant_t1_t2.py   -> runs/x12_<task>_<l>_<tag>, meta/x12_*.json
"""
import json, shutil, subprocess
from pathlib import Path
ROOT = Path("/home/user/agentbench"); TOYDB = ROOT / "toydb"
for r in json.load(open(ROOT / "bench/runs_t1.json")):
    src = ROOT / "runs" / r["run"]; m1 = json.loads((ROOT / "meta" / f"{r['run']}.json").read_text())
    run = "x12_" + r["run"][3:]; ws = ROOT / "runs" / run
    if ws.exists(): shutil.rmtree(ws)
    subprocess.run(["cp", "-a", str(TOYDB / "tiers/t2" / m1["lang"]), str(ws)], check=True)
    if m1["task"] == "bugfix":
        subprocess.run(f"patch -p1 -s < {TOYDB}/tasks/bugfix/inject/t2-{m1['lang']}.patch", shell=True, cwd=ws, check=True)
    diff = subprocess.run(["git", "diff", "--binary", m1["baseline"], "--", ".", ":(exclude)src/sql/testscripts/task",
                           ":(exclude)tests/sql/testscripts/task", ":(exclude)TASK.md", ":(exclude)check.sh", ":(exclude).gitignore"],
                          cwd=src, capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "add", "-A", "-N", "."], cwd=src)  # untracked new files
    diff = subprocess.run(["git", "diff", "--binary", m1["baseline"], "--", ".", ":(exclude)src/sql/testscripts/task",
                           ":(exclude)tests/sql/testscripts/task", ":(exclude)TASK.md", ":(exclude)check.sh", ":(exclude).gitignore",
                           ":(exclude)target", ":(exclude)**/__pycache__/**"], cwd=src, capture_output=True, text=True, check=True).stdout
    p = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], input=diff, cwd=ws, capture_output=True, text=True)
    meta = dict(m1, run=run, tier="t2", ws=str(ws), transplant_of=r["run"], apply_ok=p.returncode == 0, apply_err=p.stderr[-500:])
    (ROOT / "meta" / f"{run}.json").write_text(json.dumps(meta, indent=1))
    print(run, "apply_ok" if p.returncode == 0 else "APPLY FAILED " + p.stderr[-200:], len(diff.splitlines()))
