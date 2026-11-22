#!/usr/bin/env bash
# Build all tier-4 experiment-1 workspaces (README of tier 2), two at a time.
cd /home/user/agentbench/bench
python3 -c "
import json
for r in json.load(open('runs_t4r.json')):
    _, rest = r['run'].split('_', 1)
    task, lang = rest.rsplit('_', 2)[0], rest.rsplit('_', 2)[1]
    print(task, 'rust' if lang == 'rs' else 'python', r['run'], r['model'], r['effort'])
" | xargs -P 2 -L 1 sh -c 'python3 -c "import make_run_toydb as m; m.make(\"$0\",\"$1\",\"$2\",\"$3\",\"$4\",tier=\"t4\",readme_from=\"t2\")" && echo "ok $2" || echo "FAIL $2"'
echo ALLDONE
