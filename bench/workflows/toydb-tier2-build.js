export const meta = {
  name: 'toydb-tier2-build',
  description: 'One Opus agent at a time: tier-2 toydb workspaces + grader, then per task port the references to tier 2 and add cluster-level hidden scripts',
  phases: [
    { title: 'Workspaces', detail: 'full toydb Rust + self-contained full Python port, two task dirs, check.sh, grade.py', model: 'claude-opus-5-5' },
    { title: 'Tasks', detail: 'per task: port refs, extend to raft/server/client paths, cluster hidden scripts, patches', model: 'claude-opus-5-5' },
  ],
}
const ROOT = '/home/user/agentbench/toydb'
const T1 = `${ROOT}/tiers/t1`
const T2 = `${ROOT}/tiers/t2`
const M = 'claude-opus-5-5'
const RESUME = 'If some of your output files already exist, they are from an interrupted earlier attempt of this step: inspect them, keep what is correct, and complete or redo the rest.'
const KEEP = `Never modify ${ROOT}/rust, ${ROOT}/python, anything under ${T1}, or ${ROOT}/tasks/*/{SPEC.md,visible,hidden,ref/t1-*,inject/t1-*}. Do not spawn subagents. Do not use the web.`
const RES = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] }, notes: { type: 'string' } },
  required: ['status', 'notes'] }
const A = (prompt, label, ph) => agent(prompt, { label, phase: ph, model: M, schema: RES, agentType: 'general-purpose' })

phase('Workspaces')
const ws = await A(`You are preparing the tier-2 codebases of a Rust-vs-Python coding-agent experiment on toydb (a distributed SQL database). Working directory: ${ROOT}. ${RESUME}
Context: ${ROOT}/rust is upstream toydb (Rust; never modify it). ${ROOT}/python is a faithful full Python port of it (raft, server, client, bins, e2e cluster tests included; 203/203 golden script runs pass, 122 unit tests; see ${ROOT}/python/PORT_REPORT.md and ${ROOT}/PORTING.md). Tier 1 already exists: read ${T1}/LAYOUT.md, ${T1}/grade.py, ${T1}/rust/check.sh, ${T1}/python/check.sh and both READMEs, and mirror their design closely. Tier 2 is the FULL codebase (tier 1 plus raft/, server, client, bins, sql/engine/raft, the e2e cluster tests). Coding agents will later get a copy of one tier-2 workspace plus a task; they may only see the workspace.

Build:
1. ${T2}/rust: a copy of ${ROOT}/rust (git metadata excluded) with a warm target/ dir. Keep the sources byte-identical to upstream except what the task dirs need.
2. ${T2}/python: a self-contained Python project made from ${ROOT}/python, with every golden script it uses copied inside the project (same locations scheme as ${T1}/python), no reference to any path outside the project. ${T1}/python shows how tier 1 did this.
3. Two task script dirs in both workspaces, each initially empty:
   - SQL task dir, exactly as in tier 1 (Rust src/sql/testscripts/task/, Python tests/sql/testscripts/task/), run by the ordinary SQL runner.
   - Cluster task dir for end-to-end cluster golden scripts in the format of upstream tests/scripts/ (cluster nodes=N, client prefixes, run through a real server cluster and the client): Rust tests/scripts/task/ (check that upstream's test_each_path discovery picks it up, or add a registration), Python the equivalent path in its e2e test layout. Make sure both an empty and a non-empty dir work.
4. check.sh in both (same output contract as tier 1: build: ok, golden scripts: P/T passed, unit tests: P/T passed, check: OK|FAILED, -v for full output; the golden count must now include the raft, cluster and task scripts). Keep the lines \`trap 'rm -f "$log"' EXIT\` and \`echo "== test"\` exactly as in tier 1 (a harness instruments them). Rust exports PATH="$HOME/.cargo/bin:$PATH" and CARGO_TARGET_DIR="$PWD/target". README.md in both (as in tier 1, extended with raft/server/client/bins/cluster tests and both task dirs).
5. ${T2}/grade.py (stdlib only), the tier-1 grader extended: \`grade.py <rust|python> <workspace> <scripts_dir> [--json out.json] [--log grade.log] [--timeout SECS]\` where <scripts_dir> contains a sql/ and/or a cluster/ subdirectory; it moves both workspace task dirs aside, installs the given scripts in the matching task dirs, builds, runs them plus the full regression suite (all other golden scripts incl. raft and cluster, plus unit tests), restores the task dirs (also on failure), and reports the same JSON fields as tier 1, with per-script results keyed by name (names are unique across sql/ and cluster/). Find out whether two grades (or a grade and a check.sh) can run concurrently on different workspace copies without clashing (e.g. TCP ports used by test clusters); if they can clash, make them safe (e.g. per-run port allocation, without changing upstream test behaviour visible to scripts) or, if not feasible, document it prominently.
6. ${T2}/LAYOUT.md: as tier 1 (where things are, task-dir mechanisms, grade.py, baseline regression totals, warm check.sh wall times for both, line counts application/test for both, measured exactly like tier 1).
Verify: both check.sh pass on clean workspaces; grade.py on an empty scripts dir reports full regression passing; a trivial sql and a trivial cluster script placed via grade.py pass in both languages and a deliberately wrong one fails; workspaces copied elsewhere with cp -a work.
Report status and notes (totals, timings, paths, concurrency finding). ${KEEP}`, 'workspaces-t2', 'Workspaces')
log(`workspaces-t2: ${ws && ws.status}`)
if (!ws || ws.status === 'blocked') return { ws }

phase('Tasks')
const TASKS = ['between_in', 'case', 'date', 'distinct', 'alter', 'release', 'bugfix']
const PORT = (t) => `You are porting a benchmark task of a Rust-vs-Python coding-agent experiment on toydb (a distributed SQL database) from tier 1 to tier 2. ${RESUME}
Read first: ${ROOT}/tasks/BRIEFS.md (task rules), ${ROOT}/tasks/${t}/ (SPEC.md, visible/, hidden/, NOTES.md, AMENDMENTS.md, ref/), ${T1}/LAYOUT.md, ${T2}/LAYOUT.md. Tier 1 = the single-node SQL core; tier 2 = the full codebase (adds raft, the Raft SQL engine, server, client, bins, cluster tests). The point of the experiment is that the same task, on a larger codebase, must reach more code. The tier-1 SPEC stays the task; agents at tier 2 get the same SPEC.

Do:
1. Create the tier-2 references ${T2}/refs/${t}/rust and ${T2}/refs/${t}/python as copies of ${T2}/rust and ${T2}/python (cp -a), and port the tier-1 solution into them (${ROOT}/tasks/${t}/ref/t1-{rust,python}.patch; ${t === 'bugfix' ? 'for bugfix the task is the reverse: see step 5' : 'adapt hunks where the tier-2 files differ'}).
2. Make the tier-1 visible and hidden SQL scripts pass on both tier-2 refs with ${T2}/grade.py (scripts in <dir>/sql/), with the full tier-2 regression suite green.
3. Find every tier-2 code path the change must also reach for the feature to work in the full system: e.g. the Raft SQL engine and its command/query serialization, the catalog over Raft, server/client protocol encoding, the client and toysql display, anything that matches on or serializes the affected types or plan nodes. Implement what is needed in both refs, in the codebase's style, as a careful senior engineer would. Write in ${ROOT}/tasks/${t}/NOTES_t2.md which tier-2 places must change in each language and why (and which change automatically in one language but not the other, e.g. derived serde in Rust).
4. ${t === 'bugfix' ? 'Skip cluster scripts unless one of the bugs is observable through the cluster path in a way the SQL scripts miss.' : `Author 2-4 hidden cluster golden scripts in ${ROOT}/tasks/${t}/hidden_t2/cluster/ (upstream tests/scripts format, real multi-node cluster, at least one using several clients on different nodes) that exercise the feature end to end through those tier-2 paths, so that an implementation which only changes the tier-1 code fails them. Every behaviour they test must follow from SPEC.md; if a tested behaviour is not determined by SPEC.md (e.g. how the client prints something new), add the minimum needed to ${ROOT}/tasks/${t}/SPEC_t2.md (a short addendum that agents at tier 2 get after the SPEC; omit the file if nothing is needed). Both refs must pass them byte-identically, and each must fail on the unmodified tier-2 base; report any that don't.`}
5. ${t === 'bugfix' ? `Port the bug injection: ${ROOT}/tasks/bugfix/inject/t1-{rust,python}.patch -> inject/t2-{rust,python}.patch applying cleanly (patch -p1) to ${T2}/rust and ${T2}/python. Verify: the unmodified bases pass all bugfix visible+hidden scripts, the injected copies fail all of them, and check.sh on the injected copies (apart from the visible task script, which an agent would add) stays green, including raft and cluster tests; if the tier-2 suite catches a bug that tier 1 did not, report it. The tier-2 references for bugfix are just the bases.` : `Write ${ROOT}/tasks/${t}/ref/t2-rust.patch and t2-python.patch (diff -ruN base ref, excluding target/, __pycache__ and both task script dirs) and add a tier-2 section to ref/STATS.md (files touched, lines added/removed per language, and how many of them are outside the tier-1 file set).`}
Finish only when both refs pass every tier-1 visible/hidden script and every hidden_t2 script, and the full regression suite. Use ${T2}/grade.py for all grading. Report status and notes. ${KEEP} Do not modify ${T2}/rust, ${T2}/python or ${T2}/grade.py (if you find a grader bug, report it and work around it).`
const out = { ws }
for (const t of TASKS) {
  const r = await A(PORT(t), `port:${t}`, 'Tasks')
  log(`port:${t}: ${r && r.status}`)
  out[t] = r
}
return out