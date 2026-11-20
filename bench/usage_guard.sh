#!/bin/sh
# Exit (and so notify) when the weekly Claude.ai usage reaches $1 percent (default 95), when the usage data
# goes stale for 30 minutes, or when the file named by $2 exists (the runs are done).
LIMIT=${1:-95}; DONE=${2:-/nonexistent}; STALE=${3:-1800}
F=$HOME/.claude/state/usage-latest.json
while true; do
  [ -e "$DONE" ] && { echo "DONE: $DONE exists"; exit 0; }
  out=$(python3 -c '
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("STALE no usage file"); sys.exit()
age = time.time() - d["t"]
w = ((d.get("rate_limits") or {}).get("seven_day") or {}).get("used_percentage")
if w is not None and w >= float(sys.argv[2]):
    print(f"PAUSE weekly usage {w}% >= {sys.argv[2]}%")
elif age > float(sys.argv[3]):
    print(f"STALE usage data {age/60:.0f} min old (weekly {w}%)")
' "$F" "$LIMIT" "$STALE")
  [ -n "$out" ] && { echo "$out"; exit 0; }
  sleep 60
done
