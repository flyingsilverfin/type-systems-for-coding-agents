"""Run CLI test cases against the app: init, load a dataset, run each case, compare stdout + exit code."""
from __future__ import annotations

import difflib
import json
import subprocess
import sys
from pathlib import Path


def run_cases(app_cmd: list[str], env: dict, data_dir: str, cases: list[dict], cwd: str, verbose: bool = True, timeout: int = 120):
    results = []
    for step in (["init"], ["load", data_dir]):
        p = subprocess.run(app_cmd + step, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            if verbose:
                print(f"SETUP FAILED: lending {' '.join(step)} (exit {p.returncode})")
                print((p.stdout + p.stderr)[-3000:])
            return [dict(args=c["args"], passed=False, reason="setup failed") for c in cases], False
    for c in cases:
        try:
            p = subprocess.run(app_cmd + c["args"], env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            out, code, err = p.stdout.splitlines(), p.returncode, p.stderr
        except subprocess.TimeoutExpired:
            out, code, err = [], -999, "timeout"
        ok = out == c["expected"] and code == c["exit"]
        results.append(dict(args=c["args"], passed=ok, exit=code, expected_exit=c["exit"]))
        if verbose:
            name = "lending " + " ".join(c["args"])
            if ok:
                print(f"PASS  {name}")
            else:
                print(f"FAIL  {name}")
                if code != c["exit"]:
                    print(f"      exit status {code}, expected {c['exit']}")
                diff = list(difflib.unified_diff(c["expected"], out, "expected", "actual", lineterm="", n=1))
                for line in diff[:40]:
                    print("      " + line)
                if err.strip():
                    print("      stderr: " + err.strip()[-1500:].replace("\n", "\n              "))
    return results, True


if __name__ == "__main__":
    # usage: run_tests.py <cases.json> <data_dir> -- <app command...>
    cases_file, data_dir = sys.argv[1], sys.argv[2]
    app_cmd = sys.argv[sys.argv.index("--") + 1:]
    import os
    cases = json.loads(Path(cases_file).read_text())
    results, _ = run_cases(app_cmd, dict(os.environ), data_dir, cases, cwd=os.getcwd())
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} visible tests passed")
    sys.exit(0 if passed == len(results) else 1)
