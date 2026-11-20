export const meta = {
  name: 'toydb-tasks-tier1',
  description: 'Build tier-1 toydb workspaces (Rust+Python), author 7 tasks with hidden golden scripts, and cross-validated reference solutions in both languages (Fable 5.2)',
  phases: [
    { title: 'Workspaces', detail: 'trimmed tier-1 Rust crate + self-contained Python project + grader', model: 'claude-fable-5-2' },
    { title: 'Author', detail: 'SPEC + visible + hidden scripts per task', model: 'claude-fable-5-2' },
    { title: 'Reference', detail: 'independent Rust and Python reference solutions', model: 'claude-fable-5-2' },
    { title: 'Reconcile', detail: 'both refs pass all hidden scripts byte-identically', model: 'claude-fable-5-2' },
  ],
}
const ROOT = '/home/user/agentbench/toydb'
const T1 = `${ROOT}/tiers/t1`
const M = 'claude-fable-5-2'
const NOSUB = 'Do not spawn subagents. Never modify ' + ROOT + '/rust or ' + ROOT + '/python (another team is extending ' + ROOT + '/python right now).'
const RES = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] }, notes: { type: 'string' } },
  required: ['status', 'notes'] }

phase('Workspaces')
const builderP = agent(`You are preparing the tier-1 codebases of a Rust-vs-Python coding-agent experiment. Working directory: ${ROOT}.
Context: ${ROOT}/rust is toydb (Rust SQL database). ${T1}/python_port_snapshot is a faithful Python port of its tier-1 subset (encoding, storage, the SQL layer without sql/engine/raft.rs, the goldenscript runner); see its PORT_REPORT.md. Tier 1 deliberately excludes raft/, server.rs, client.rs, bin/, sql/engine/raft.rs and tests/. Coding agents will later get one of these two codebases plus a task, and hidden golden scripts will grade them. The two must be equivalent in scope and tests.

Build:
1. ${T1}/rust: a copy of ${ROOT}/rust trimmed to the tier-1 subset: remove raft/, server.rs, client.rs, bin/, sql/engine/raft.rs, tests/, the Raft formatter and anything else that only exists for those, the raft testscripts, docs/cluster/config, and every Cargo dependency that is no longer needed (Cargo.lock updated). It must compile warning-free and \`cargo test\` must pass all remaining tests (130 golden script runs: 93 sql + 37 storage, plus the remaining unit tests). Use ~/.cargo/bin/cargo. Keep everything else byte-identical to upstream.
2. ${T1}/python: a self-contained Python project made from python_port_snapshot: the same golden scripts copied inside the project (choose a natural location, e.g. tests/ mirroring src/**/testscripts), run_goldenscripts.py defaulting to running all of them, the unit tests. No reference to a rust/ directory may remain. It must pass everything.
3. A task script directory in both, for task-specific golden scripts run by the ordinary SQL runner: in Rust src/sql/testscripts/task/ with one extra \`test_each_path!\` line in src/sql/mod.rs next to the others; in Python the equivalent directory and registry entry. It must work when it holds one or more scripts (make sure an empty or placeholder state compiles too; a script placed there must be picked up by the next test run, e.g. by touching src/sql/mod.rs if the macro needs it — do this in check.sh). Script file names are snake_case identifiers.
4. In both: check.sh at the root (build, then run every test: unit tests and all golden scripts incl. task/; print a short summary; exit non-zero on any failure; for Rust export PATH="$HOME/.cargo/bin:$PATH" and keep the target dir inside the workspace), and README.md describing the project layout and how to run the tests (same content and structure for both languages apart from language specifics; no mention of the other language or of an experiment). Run check.sh in Rust once so target/ is warm. Remove __pycache__.
5. ${T1}/grade.py (stdlib Python): \`grade.py <rust|python> <workspace> <scripts_dir> [--json out.json]\` copies the given scripts into the workspace's task dir (temporarily, restoring the task dir afterwards), runs them plus the full regression suite, and reports per-script pass/fail for the given scripts (with diffs saved to a log), regression passed/total, build ok, and wall times. It must work on copies of the workspaces in other directories. Test it with a passing script and with a deliberately wrong expected output.
6. ${T1}/LAYOUT.md: where things are in both workspaces, the task-dir mechanism, grade.py usage, and non-blank non-comment line counts (application code / test code) of both codebases.
Report status and notes (include line counts, check.sh wall times for both, and the exact task-dir paths). ${NOSUB}`,
  { label: 'workspaces', phase: 'Workspaces', model: M, schema: RES, agentType: 'general-purpose' })

const AUTHOR_COMMON = `You are authoring a benchmark task for a Rust-vs-Python coding-agent experiment on toydb, a SQL database. Read ${ROOT}/tasks/BRIEFS.md first and follow it exactly. Study the codebase (Rust: ${ROOT}/rust/src, especially src/sql/; a faithful Python port with identical behaviour: ${T1}/python_port_snapshot) and the existing golden scripts (src/sql/testscripts/**) and the SQL test runner in src/sql/mod.rs so your scripts use the exact runner syntax and the existing output conventions (value display, error message prefixes, header formatting). You may build and run the Rust code in a scratch copy (cp -a ${ROOT}/rust to your scratchpad; ~/.cargo/bin/cargo) to see how existing behaviour is displayed.`
const AUTHOR_OUT = (t) => `Write into ${ROOT}/tasks/${t}/:
- SPEC.md: the complete task description given to the agent (see BRIEFS.md: complete, exact, language-neutral, every hidden error message spelled out, "nothing else changes").
- visible/<name>: the one small visible golden script.
- hidden/<name>...: 8-12 focused hidden golden scripts with expected outputs (the expected output must be exactly what a correct implementation prints; reason carefully about the existing display conventions).
- NOTES.md (for the experimenters only, never shown to agents): the list of places in the code a correct implementation must touch (in both languages if you can tell), which hidden script probes which place, and the traps a careless implementation falls into.
Script file names: snake_case identifiers. ${NOSUB} Do not touch ${ROOT}/tiers.`

const TASKS = ['between_in', 'case', 'date', 'distinct', 'alter', 'release']
const IMPL = (t, lang) => `You are writing the ${lang === 'rust' ? 'Rust' : 'Python'} reference solution for a benchmark task on toydb (a SQL database). Your workspace: ${T1}/refs/${t}/${lang} (create it as an exact copy of ${T1}/${lang}: cp -a ${T1}/${lang} ${T1}/refs/${t}/${lang}). The task: ${ROOT}/tasks/${t}/SPEC.md. Copy ${ROOT}/tasks/${t}/visible/* into the workspace's task script directory (see ${T1}/LAYOUT.md).
Implement the SPEC completely and exactly, as a careful senior engineer would: in the style and structure of the codebase, handling every case the SPEC states, all places in the code that need changing (think about every place that matches on or handles the affected types/constructs), and adding your own golden scripts to the task dir to test the SPEC thoroughly (in the existing style). ./check.sh must be green at the end (all existing tests unchanged and passing). Your implementation will later be compared with an independent implementation in the other language, so follow the SPEC literally; where it is genuinely ambiguous, choose the most natural reading and list the ambiguity in your notes.
Rules: work only in your workspace. Do NOT read ${ROOT}/tasks/${t}/hidden, ${ROOT}/tasks/${t}/NOTES.md, or ${T1}/refs/${t}/${lang === 'rust' ? 'python' : 'rust'}. ${NOSUB}
Report status and notes (files touched, ambiguities, your added scripts).`
const RECONCILE = (t) => `You are reconciling a benchmark task on toydb (a SQL database) for a Rust-vs-Python coding-agent experiment. Task: ${ROOT}/tasks/${t}/ (SPEC.md, visible/, hidden/, NOTES.md). Two independent reference solutions: ${T1}/refs/${t}/rust and ${T1}/refs/${t}/python, made from the base workspaces ${T1}/rust and ${T1}/python (see ${T1}/LAYOUT.md for grade.py).
Goal: a correct, unambiguous task whose hidden scripts both references pass byte-identically.
1. Grade both refs on visible+hidden scripts with ${T1}/grade.py. For each failure decide who is wrong, with SPEC.md as the authority: fix the reference that deviates from the SPEC (keeping its code quality); if the hidden script's expected output is wrong, fix the script; if the SPEC is ambiguous or silent on a tested behaviour, make the SPEC explicit (choose the most natural reading), and fix the scripts/refs accordingly. Record every SPEC or script change with its reason in ${ROOT}/tasks/${t}/AMENDMENTS.md.
2. Also run each ref's own extra task scripts against the other ref; differences reveal untested ambiguities: resolve them the same way and promote useful ones to hidden scripts (keep 8-14 hidden scripts).
3. Grade the unmodified base workspaces on the hidden scripts: ideally every hidden script fails on the base; report how many pass and why.
4. Finish only when both refs pass every visible and hidden script and the full regression suite. Then write the patches: ${ROOT}/tasks/${t}/ref/t1-rust.patch and t1-python.patch (diff -ruN base ref, excluding target/, __pycache__, and the task script dir), and ref/STATS.md with files touched and lines added/removed per language.
Do not weaken the task to make things pass: the SPEC must still cover everything BRIEFS.md (${ROOT}/tasks/BRIEFS.md) asks for. ${NOSUB}`
const REC_SCHEMA = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
  hidden_scripts: { type: 'integer' }, rust_pass: { type: 'integer' }, python_pass: { type: 'integer' },
  base_pass: { type: 'integer' }, spec_amendments: { type: 'integer' }, ref_fixes_rust: { type: 'integer' },
  ref_fixes_python: { type: 'integer' }, rust_lines_changed: { type: 'integer' }, python_lines_changed: { type: 'integer' },
  notes: { type: 'string' } },
  required: ['status', 'hidden_scripts', 'rust_pass', 'python_pass', 'base_pass', 'notes'] }

const BUG_AUTHOR = `${AUTHOR_COMMON}
The tier-1 base workspaces are ready: ${T1}/rust and ${T1}/python (read ${T1}/LAYOUT.md). Your task is "bugfix" in BRIEFS.md. Inject four bugs into copies ${T1}/refs/bugfix/rust-buggy and ${T1}/refs/bugfix/python-buggy (cp -a from the bases), equivalently in both languages; check that ./check.sh stays green in both buggy copies, that each hidden script fails on the buggy copies and passes on the bases (use ${T1}/grade.py), and that each bug is caught by at least two hidden scripts. Save the injections as ${ROOT}/tasks/bugfix/inject/t1-rust.patch and t1-python.patch (diff -ruN base buggy, excluding target/ and __pycache__).
${AUTHOR_OUT('bugfix')} In NOTES.md give each bug's exact location and the fix, in both languages.`
const BUG_VERIFY = `You are independently verifying the "bugfix" control task of a Rust-vs-Python coding-agent experiment on toydb. Read ${ROOT}/tasks/BRIEFS.md (bugfix section), ${ROOT}/tasks/bugfix/ (SPEC.md, NOTES.md, visible/, hidden/, inject/), ${T1}/LAYOUT.md. Check, fixing anything that is wrong (record changes in ${ROOT}/tasks/bugfix/AMENDMENTS.md): the patches apply to fresh copies of ${T1}/rust and ${T1}/python; the bugs are truly equivalent across languages; ./check.sh is green on both buggy copies; every hidden and visible script fails on the buggy copies and passes on the bases (grade.py); SPEC.md describes symptoms only and gives enough for a competent engineer to find and fix each bug; the expected outputs are what correct toydb prints. ${NOSUB}`

const results = await parallel([
  ...TASKS.map(t => async () => {
    const a = await agent(`${AUTHOR_COMMON}\nYour task: "${t}" in BRIEFS.md.\n${AUTHOR_OUT(t)}`,
      { label: `author:${t}`, phase: 'Author', model: M, schema: RES, agentType: 'general-purpose' })
    const b = await builderP
    if (!b || b.status === 'blocked') return { t, author: a, error: 'workspaces not ready' }
    const [rust, python] = await parallel([
      () => agent(IMPL(t, 'rust'), { label: `ref:${t}:rust`, phase: 'Reference', model: M, schema: RES, agentType: 'general-purpose' }),
      () => agent(IMPL(t, 'python'), { label: `ref:${t}:python`, phase: 'Reference', model: M, schema: RES, agentType: 'general-purpose' }),
    ])
    const rec = await agent(RECONCILE(t), { label: `reconcile:${t}`, phase: 'Reconcile', model: M, schema: REC_SCHEMA, agentType: 'general-purpose' })
    log(`${t}: rust ${rec && rec.rust_pass}/${rec && rec.hidden_scripts}, python ${rec && rec.python_pass}, base ${rec && rec.base_pass}`)
    return { t, author: a, rust, python, rec }
  }),
  async () => {
    const b = await builderP
    log(`workspaces: ${b && b.status}`)
    if (!b || b.status === 'blocked') return { t: 'bugfix', error: 'workspaces not ready', builder: b }
    const a = await agent(BUG_AUTHOR, { label: 'author:bugfix', phase: 'Author', model: M, schema: RES, agentType: 'general-purpose' })
    const v = await agent(BUG_VERIFY, { label: 'verify:bugfix', phase: 'Reconcile', model: M, schema: RES, agentType: 'general-purpose' })
    return { t: 'bugfix', builder: b, author: a, verify: v }
  },
])
return results
