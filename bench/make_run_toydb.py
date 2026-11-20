"""Create a benchmark workspace for a toydb task.

usage: make_run_toydb.py <task> <rust|python> <run_id> [model] [effort] [tier]

The workspace is a copy of tiers/<tier>/<lang> with the task's visible golden script in the task dir, the
task SPEC as TASK.md, check.sh instrumented to log build/test timings, and (for bugfix) the bugs injected.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/user/agentbench")
TOYDB = ROOT / "toydb"
TASKDIR = {"rust": "src/sql/testscripts/task", "python": "tests/sql/testscripts/task"}
CLUSTERDIR = {"rust": "tests/scripts/task", "python": "tests/e2e/scripts/task"}

HOW = """
## How to work

- Read `README.md` first: it describes the project layout and how to run the tests.
- Validate with `./check.sh`: it builds the project and runs every test (the unit tests and all golden scripts).
  `{taskdir}/` holds a small visible golden script for this task; you may add your own scripts there.
- Your work will be graded by a larger set of golden scripts you cannot see, covering everything in this
  task description, and by the existing test suite, which must keep passing.{extra}
- Do not modify `check.sh` or the existing golden scripts.
"""

TIMING = r'''
# --- benchmark timing (do not modify) ---
_bench_t0=$(date +%s.%N)
_bench_log="{log}"
_bench_mark=$(mktemp /tmp/bench-mark.XXXXXX)
_bench_sweep() {{ find /tmp -maxdepth 1 -type d -name 'toydb*' -newer "$_bench_mark" -exec sh -c '[ "$(ls -A "$1")" = bitcask ] && rm -rf "$1"' _ {{}} \; 2>/dev/null; rm -f "$_bench_mark"; }}  # upstream bitcask tests leak a 400 MB temp dir per cargo test
_bench_end() {{ local rc=$?; _bench_sweep; python3 -c "import json,sys; a=sys.argv; print(json.dumps(dict(t=float(a[1]), build_s=(float(a[2]) if a[2] else float(a[3]))-float(a[1]), test_s=(float(a[3])-float(a[2])) if a[2] else 0.0, rc=int(a[4]))))" "$_bench_t0" "${{_bench_t1:-}}" "$(date +%s.%N)" "$rc" >> "$_bench_log" 2>/dev/null; rm -f "${{log:-}}"; }}
'''


def make(task: str, lang: str, run_id: str, model: str = "", effort: str = "", tier: str = "t1", hint: bool = True,
         readme_from: str | None = None) -> Path:
    ws = ROOT / "runs" / run_id
    if ws.exists():
        shutil.rmtree(ws)
    ws.parent.mkdir(exist_ok=True)
    subprocess.run(["cp", "-a", "--reflink=auto", str(TOYDB / "tiers" / tier / lang), str(ws)], check=True)
    for p in ws.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    if readme_from:  # ablation: documentation of an earlier tier (code unchanged)
        shutil.copy(TOYDB / "tiers" / readme_from / lang / "README.md", ws / "README.md")
    tdir = TOYDB / "tasks" / task

    if task == "bugfix":
        patch = tdir / "inject" / f"{tier}-{lang}.patch"
        subprocess.run(f"patch -p1 --quiet --no-backup-if-mismatch < {patch}", shell=True, cwd=ws, check=True)
        leftovers = list(ws.rglob("*.orig")) + list(ws.rglob("*.rej"))
        assert not leftovers, leftovers

    task_dir = ws / TASKDIR[lang]
    task_dir.mkdir(parents=True, exist_ok=True)
    for f in (tdir / "visible").iterdir():
        shutil.copy(f, task_dir / f.name)

    spec = (tdir / "SPEC.md").read_text().rstrip() + "\n"
    extra = ""
    if tier != "t1":
        for n in range(2, int(tier[1:]) + 1):
            add = tdir / f"SPEC_t{n}.md"
            if add.exists():
                spec += "\n" + add.read_text().rstrip() + "\n"
        if hint:
            extra = ("\n  The hidden scripts include SQL golden scripts and end-to-end cluster golden scripts (the format of the\n"
                     f"  existing cluster scripts; their task dir is `{CLUSTERDIR[lang]}/`).")
    (ws / "TASK.md").write_text(spec + HOW.format(taskdir=TASKDIR[lang], extra=extra))

    # Instrument check.sh: log build/test wall times per invocation.
    logf = ROOT / "logs" / f"{run_id}.checks.jsonl"
    logf.parent.mkdir(exist_ok=True)
    if logf.exists():
        logf.unlink()
    check = (ws / "check.sh").read_text()
    check = check.replace("trap 'rm -f \"$log\"' EXIT", TIMING.format(log=logf).strip() + "\ntrap _bench_end EXIT", 1)
    check = check.replace('echo "== test"', '_bench_t1=$(date +%s.%N)\necho "== test"', 1)
    assert "_bench_end EXIT" in check and "_bench_t1=" in check
    (ws / "check.sh").write_text(check)
    os.chmod(ws / "check.sh", 0o755)

    # Warm: a copied target dir rebuilds once because the path changed; do it now, not on the agent's clock.
    r = subprocess.run(["./check.sh"], cwd=ws, capture_output=True, text=True)
    logf.unlink(missing_ok=True)
    warm_ok = "check: OK" in r.stdout or task == "bugfix" or "FAILED" in r.stdout  # visible task script fails by design

    (ws / ".gitignore").write_text("target/\n__pycache__/\n")
    subprocess.run("git init -q && git add -A && git -c user.name=bench -c user.email=bench@localhost commit -qm baseline",
                   shell=True, cwd=ws, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True).stdout.strip()
    meta = dict(run=run_id, task=task, suite=f"toydb-{tier}", tier=tier, lang=lang, model=model, effort=effort,
                ws=str(ws), hint=hint, readme_from=readme_from, baseline=base, created=time.time(), warm_ok=warm_ok)
    (ROOT / "meta").mkdir(exist_ok=True)
    (ROOT / "meta" / f"{run_id}.json").write_text(json.dumps(meta, indent=1))
    return ws


if __name__ == "__main__":
    print(make(*sys.argv[1:]))
