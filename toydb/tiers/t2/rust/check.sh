#!/usr/bin/env bash
# Builds the project and runs every test: the unit tests and all golden
# scripts, including the end-to-end cluster scripts in tests/scripts and those
# in the task directories src/sql/testscripts/task and tests/scripts/task.
# Prints a short summary and exits non-zero on any failure. Pass -v to print
# the full test output.
set -u
cd "$(dirname "$0")"
export PATH="$HOME/.cargo/bin:$PATH"
export CARGO_TARGET_DIR="$PWD/target"

verbose=0
[ "${1:-}" = "-v" ] && verbose=1
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

# Golden scripts are discovered at compile time, so make sure that scripts
# added or removed since the last build are picked up.
mkdir -p src/sql/testscripts/task tests/scripts/task
touch src/sql/mod.rs tests/tests.rs

echo "== build"
if ! cargo build --all-targets >"$log" 2>&1; then
    cat "$log"
    echo "check: FAILED (build failed)"
    exit 1
fi
warnings="$(grep -c '^warning' "$log")"
[ "$warnings" -gt 0 ] && grep -A12 '^warning' "$log"
echo "build: ok ($warnings warnings)"

echo "== test"
cargo test >"$log" 2>&1
status=$?
if [ "$verbose" -eq 1 ]; then
    cat "$log"
elif [ "$status" -ne 0 ]; then
    grep -v '^test .* \.\.\. ok$' "$log"
fi

# Prefix each test result with its test target (e.g. src/lib.rs): every test
# in tests/tests.rs is an end-to-end golden script.
results="$(awk '/^ *Running /{target = ($2 == "unittests") ? $3 : $2}
    /^test .* \.\.\. (ok|FAILED)$/{print target " " $0}' "$log")"
golden='^\(src/lib.rs test \(sql::tests\|storage::[a-z]*::tests::\(engine\|scripts\)\|raft::[a-z]*::tests::scripts\)::\|tests/tests.rs test \)'
count() { grep -c "$1" || true; }
golden_total="$(echo "$results" | grep "$golden" | count '')"
golden_passed="$(echo "$results" | grep "$golden" | count ' ok$')"
unit_total="$(echo "$results" | grep -v "$golden" | count ' test ')"
unit_passed="$(echo "$results" | grep -v "$golden" | count ' ok$')"
echo "golden scripts: $golden_passed/$golden_total passed"
echo "unit tests: $unit_passed/$unit_total passed"
if [ "$status" -ne 0 ] || [ "$golden_passed" -ne "$golden_total" ] || [ "$unit_passed" -ne "$unit_total" ] || [ "$unit_total" -eq 0 ]; then
    echo "check: FAILED"
    exit 1
fi
echo "check: OK"
