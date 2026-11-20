#!/usr/bin/env bash
# Builds the project and runs every test: the unit tests and all golden
# scripts, including those in tests/sql/testscripts/task. Prints a short
# summary and exits non-zero on any failure. Pass -v to print the full test
# output.
set -u
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1

verbose=0
[ "${1:-}" = "-v" ] && verbose=1
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

mkdir -p tests/sql/testscripts/task

# There is no compile step, so "build" checks that every source file compiles
# to bytecode and that the package imports.
echo "== build"
if ! python3 - >"$log" 2>&1 <<'PY'
import importlib, pathlib, sys
ok = True
for path in sorted(p for d in ("toydb", "tests") for p in pathlib.Path(d).rglob("*.py")):
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as error:
        ok = False
        print(f"{path}:{error.lineno}: {error.msg}")
sys.path.insert(0, ".")
if ok:
    for path in sorted(pathlib.Path("toydb").rglob("*.py")):
        name = ".".join(path.with_suffix("").parts).removesuffix(".__init__")
        importlib.import_module(name)
sys.exit(0 if ok else 1)
PY
then
    cat "$log"
    echo "check: FAILED (build failed)"
    exit 1
fi
echo "build: ok"

echo "== test"
python3 run_goldenscripts.py >"$log" 2>&1
golden_status=$?
if [ "$verbose" -eq 1 ]; then
    cat "$log"
elif [ "$golden_status" -ne 0 ]; then
    grep -v '^PASS ' "$log"
fi
golden="$(grep '^[0-9]*/[0-9]* scripts passed$' "$log" | tail -1 | cut -d' ' -f1)"

python3 -m unittest discover -s tests -t . >"$log" 2>&1
unit_status=$?
if [ "$verbose" -eq 1 ] || [ "$unit_status" -ne 0 ]; then
    cat "$log"
fi
unit_total="$(sed -n 's/^Ran \([0-9]*\) tests\{0,1\} in .*/\1/p' "$log" | tail -1)"
unit_total="${unit_total:-0}"
unit_failed="$(sed -n 's/^\(FAIL\|ERROR\): \([^ ]*\) (\([^)]*\)).*/\3/p' "$log" | sort -u | wc -l)"
unit_passed=$((unit_total - unit_failed))
[ "$unit_passed" -lt 0 ] && unit_passed=0

echo "golden scripts: ${golden:-0/0} passed"
echo "unit tests: $unit_passed/$unit_total passed"
if [ "$golden_status" -ne 0 ] || [ "$unit_status" -ne 0 ] || [ "$unit_total" -eq 0 ]; then
    echo "check: FAILED"
    exit 1
fi
echo "check: OK"
