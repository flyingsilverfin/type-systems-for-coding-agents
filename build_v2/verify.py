"""Verify a v2 build against the oracle on both datasets.  usage: verify.py python|rust"""
import json, os, sys
from pathlib import Path
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import oracle2, tasks2
from runner import run_cases
lang = sys.argv[1]
if lang == "python":
    app = ["python3", "-m", "lending"]; cwd = HERE / "python"
    env = dict(os.environ, PYTHONPATH=str(HERE / "python/app"))
else:
    import subprocess
    b = subprocess.run("~/.cargo/bin/cargo build --quiet 2>&1", shell=True, cwd=HERE / "rust/app")
    if b.returncode: sys.exit("build failed")
    app = [str(HERE / "rust/app/target/debug/lending")]; cwd = HERE / "rust"
    env = dict(os.environ)
total_fail = 0
for name, data, cases in [("visible", HERE / "data_visible", tasks2.V2_VISIBLE), ("hidden", HERE / "data_hidden", tasks2.V2_HIDDEN)]:
    env["LENDING_DB"] = f"build_{lang}_{name}"
    cs = [dict(args=a, expected=oracle2.run("BASE", data, a)[0], exit=oracle2.run("BASE", data, a)[1]) for a in cases]
    print(f"== {name}")
    res, ok = run_cases(app, env, str(data), cs, cwd=str(cwd), verbose=True)
    fails = sum(not r["passed"] for r in res)
    total_fail += fails
    print(f"{name}: {len(res) - fails}/{len(res)} passed")
sys.exit(1 if total_fail else 0)
