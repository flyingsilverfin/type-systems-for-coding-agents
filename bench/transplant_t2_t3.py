"""Carry each tier-2 run's final changes onto a fresh tier-3 base with a per-file three-way merge.

base = the tier-2 run's baseline commit, ours = the tier-3 base (plus bugfix injection), theirs = the run's final file.
usage: transplant_t2_t3.py   -> runs/x23_<task>_<l>_<tag>, meta/x23_*.json (conflicted files keep conflict markers)
"""
import json, shutil, subprocess, tempfile
from pathlib import Path
ROOT = Path("/home/user/agentbench"); TOYDB = ROOT / "toydb"
EXCL = ["src/sql/testscripts/task", "tests/sql/testscripts/task", "tests/scripts/task", "tests/e2e/scripts/task",
        "TASK.md", "check.sh", ".gitignore", "target"]


def excluded(p: str) -> bool:
    return any(p == e or p.startswith(e + "/") for e in EXCL) or "__pycache__" in p


for r in json.load(open(ROOT / "bench/runs_t2.min.json")):
    src = ROOT / "runs" / r["run"]; m2 = json.loads((ROOT / "meta" / f"{r['run']}.json").read_text())
    run = "x23_" + r["run"][3:]; ws = ROOT / "runs" / run
    if ws.exists(): shutil.rmtree(ws)
    subprocess.run(["cp", "-a", "--reflink=auto", str(TOYDB / "tiers/t3" / m2["lang"]), str(ws)], check=True)
    if m2["task"] == "bugfix":
        subprocess.run(f"patch -p1 -s --no-backup-if-mismatch < {TOYDB}/tasks/bugfix/inject/t3-{m2['lang']}.patch", shell=True, cwd=ws, check=True)
    subprocess.run(["git", "add", "-A", "-N", "."], cwd=src)
    ns = subprocess.run(["git", "diff", "--name-status", "--no-renames", m2["baseline"]], cwd=src, capture_output=True, text=True, check=True).stdout
    changed, conflicts, fast = [], {}, 0
    for line in ns.splitlines():
        st, path = line.split("\t", 1)
        if excluded(path):
            continue
        changed.append(path)
        ours = ws / path
        if st == "D":
            ours.unlink(missing_ok=True); continue
        theirs = (src / path).read_bytes()
        base = b"" if st == "A" else subprocess.run(["git", "show", f"{m2['baseline']}:{path}"], cwd=src, capture_output=True, check=True).stdout
        cur = ours.read_bytes() if ours.exists() else b""
        if cur == base:
            ours.parent.mkdir(parents=True, exist_ok=True); ours.write_bytes(theirs); fast += 1; continue
        with tempfile.TemporaryDirectory() as t:
            fb, fo, ft = Path(t, "b"), Path(t, "o"), Path(t, "t")
            fb.write_bytes(base); fo.write_bytes(cur); ft.write_bytes(theirs)
            p = subprocess.run(["git", "merge-file", "-p", "-L", "t3", "-L", "t2base", "-L", "t2run", str(fo), str(fb), str(ft)], capture_output=True)
        ours.write_bytes(p.stdout)
        if p.returncode:
            conflicts[path] = p.returncode
    meta = dict(m2, run=run, tier="t3", ws=str(ws), transplant_of=r["run"], apply_ok=not conflicts,
                merge_conflicts=conflicts, files_changed=len(changed), files_fast=fast)
    (ROOT / "meta" / f"{run}.json").write_text(json.dumps(meta, indent=1))
    print(f"{run:24} files={len(changed):3} fast={fast:3} conflicts={conflicts or '-'}")
