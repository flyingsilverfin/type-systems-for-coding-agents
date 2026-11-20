export const meta = {
  name: 'toydb-sequential',
  description: 'One Fable 5.2 agent at a time: tier-1 workspaces + 7 tasks with cross-validated references, then the tier-2 Python port',
  phases: [
    { title: 'Workspaces', detail: 'trimmed tier-1 Rust crate + self-contained Python project + grader', model: 'claude-fable-5-2' },
    { title: 'Tasks', detail: 'per task: author -> rust ref -> python ref -> reconcile (sequential)', model: 'claude-fable-5-2' },
    { title: 'Tier 2 port', detail: 'raft -> server/client/bins/e2e -> integrate -> verify', model: 'claude-fable-5-2' },
  ],
}
const ROOT = '/home/user/agentbench/toydb'
const T1 = `${ROOT}/tiers/t1`
const M = 'claude-fable-5-2'
const RESUME = 'NOTE: an earlier attempt at this step was interrupted part-way, so some of your output files may already exist partially written. Inspect them first, keep what is correct, and complete or redo the rest.'
const NOSUB = 'Do not spawn subagents.'
const RES = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] }, notes: { type: 'string' } },
  required: ['status', 'notes'] }
// Agents that completed on Fable 5.2 keep their model (cache hits on resume); everything after runs on Opus 5.5 at the user's request.
const DONE_ON_FABLE = new Set(['workspaces', 'author:between_in', 'ref:between_in:rust', 'ref:between_in:python', 'reconcile:between_in', 'author:case', 'ref:case:rust', 'ref:case:python', 'reconcile:case', 'author:date'])
const A = (prompt, label, ph, schema) => agent(prompt, { label, phase: ph, model: DONE_ON_FABLE.has(label) ? M : 'claude-opus-5-5', schema: schema || RES, agentType: 'general-purpose' })

// ---------------- Tier 1 workspaces ----------------
phase('Workspaces')
const builder = await A(`You are preparing the tier-1 codebases of a Rust-vs-Python coding-agent experiment. Working directory: ${ROOT}. ${RESUME}
Context: ${ROOT}/rust is toydb (Rust SQL database). ${T1}/python_port_snapshot is a faithful Python port of its tier-1 subset (encoding, storage, the SQL layer without sql/engine/raft.rs, the goldenscript runner); see its PORT_REPORT.md. Tier 1 deliberately excludes raft/, server.rs, client.rs, bin/, sql/engine/raft.rs and tests/. Coding agents will later get one of these two codebases plus a task, and hidden golden scripts will grade them. The two must be equivalent in scope and tests.

Build:
1. ${T1}/rust: a copy of ${ROOT}/rust trimmed to the tier-1 subset: remove raft/, server.rs, client.rs, bin/, sql/engine/raft.rs, tests/, the Raft formatter and anything else that only exists for those, the raft testscripts, docs/cluster/config, and every Cargo dependency that is no longer needed (Cargo.lock updated). It must compile warning-free and \`cargo test\` must pass all remaining tests (130 golden script runs: 93 sql + 37 storage, plus the remaining unit tests). Use ~/.cargo/bin/cargo. Keep everything else byte-identical to upstream.
2. ${T1}/python: a self-contained Python project made from python_port_snapshot: the same golden scripts copied inside the project (choose a natural location, e.g. tests/ mirroring src/**/testscripts), run_goldenscripts.py defaulting to running all of them, the unit tests. No reference to a rust/ directory may remain. It must pass everything.
3. A task script directory in both, for task-specific golden scripts run by the ordinary SQL runner: in Rust src/sql/testscripts/task/ with one extra \`test_each_path!\` line in src/sql/mod.rs next to the others; in Python the equivalent directory and registry entry. It must work when it holds one or more scripts (make sure an empty or placeholder state compiles too; a script placed there must be picked up by the next test run, e.g. by touching src/sql/mod.rs if the macro needs it — do this in check.sh). Script file names are snake_case identifiers.
4. In both: check.sh at the root (build, then run every test: unit tests and all golden scripts incl. task/; print a short summary; exit non-zero on any failure; for Rust export PATH="$HOME/.cargo/bin:$PATH" and keep the target dir inside the workspace), and README.md describing the project layout and how to run the tests (same content and structure for both languages apart from language specifics; no mention of the other language or of an experiment). Run check.sh in Rust once so target/ is warm. Remove __pycache__.
5. ${T1}/grade.py (stdlib Python): \`grade.py <rust|python> <workspace> <scripts_dir> [--json out.json]\` copies the given scripts into the workspace's task dir (temporarily, restoring the task dir afterwards), runs them plus the full regression suite, and reports per-script pass/fail for the given scripts (with diffs saved to a log), regression passed/total, build ok, and wall times. It must work on copies of the workspaces in other directories. Test it with a passing script and with a deliberately wrong expected output.
6. ${T1}/LAYOUT.md: where things are in both workspaces, the task-dir mechanism, grade.py usage, and non-blank non-comment line counts (application code / test code) of both codebases.
Report status and notes (include line counts, check.sh wall times for both, and the exact task-dir paths). Never modify ${ROOT}/rust or ${ROOT}/python. ${NOSUB}`, 'workspaces', 'Workspaces')
log(`workspaces: ${builder && builder.status}`)
if (!builder || builder.status === 'blocked') return { builder }

// ---------------- Tier 1 tasks ----------------
phase('Tasks')
const KEEP = `Never modify ${ROOT}/rust or ${ROOT}/python. ${NOSUB}`
const AUTHOR_COMMON = `You are authoring a benchmark task for a Rust-vs-Python coding-agent experiment on toydb, a SQL database. Read ${ROOT}/tasks/BRIEFS.md first and follow it exactly. Study the tier-1 codebase (Rust: ${T1}/rust, especially src/sql/; the structurally identical Python port: ${T1}/python; layout in ${T1}/LAYOUT.md), the existing golden scripts and the SQL test runner (src/sql/mod.rs, mod tests) so your scripts use the exact runner syntax and existing output conventions (value display, error message prefixes, header formatting). You may build and run the Rust code in a scratch copy (cp -a ${T1}/rust to your scratchpad) to see how existing behaviour is displayed; use ${T1}/grade.py to check scripts against the base workspaces.`
const AUTHOR_OUT = (t) => `Write into ${ROOT}/tasks/${t}/:
- SPEC.md: the complete task description given to the agent (see BRIEFS.md: complete, exact, language-neutral, every hidden error message spelled out, "nothing else changes").
- visible/<name>: the one small visible golden script.
- hidden/<name>...: 8-12 focused hidden golden scripts with expected outputs (the expected output must be exactly what a correct implementation prints; reason carefully about the existing display conventions).
- NOTES.md (for the experimenters only, never shown to agents): the places in the code a correct implementation must touch (in both languages if you can tell), which hidden script probes which place, and the traps a careless implementation falls into.
Script file names: snake_case identifiers. Do not modify ${T1}/rust, ${T1}/python or ${T1}/grade.py. ${KEEP}`
const IMPL = (t, lang) => `You are writing the ${lang === 'rust' ? 'Rust' : 'Python'} reference solution for a benchmark task on toydb (a SQL database). Your workspace: ${T1}/refs/${t}/${lang}. If it does not exist, create it as an exact copy of the base: mkdir -p ${T1}/refs/${t} && cp -a ${T1}/${lang} ${T1}/refs/${t}/${lang}. If it already exists, it is from an interrupted earlier attempt of this same step: continue from it. The task: ${ROOT}/tasks/${t}/SPEC.md. Copy ${ROOT}/tasks/${t}/visible/* into the workspace's task script directory (see ${T1}/LAYOUT.md).
Implement the SPEC completely and exactly, as a careful senior engineer would: in the style and structure of the codebase, handling every case the SPEC states, all places in the code that need changing (think about every place that matches on or handles the affected types/constructs), and adding your own golden scripts to the task dir to test the SPEC thoroughly (in the existing style). ./check.sh must be green at the end (all existing tests unchanged and passing). Your implementation will later be compared with an independent implementation in the other language, so follow the SPEC literally; where it is genuinely ambiguous, choose the most natural reading and list the ambiguity in your notes.
Rules: work only in your workspace. Do NOT read ${ROOT}/tasks/${t}/hidden, ${ROOT}/tasks/${t}/NOTES.md, or ${T1}/refs/${t}/${lang === 'rust' ? 'python' : 'rust'}. ${KEEP}
Report status and notes (files touched, ambiguities, your added scripts).`
const RECONCILE = (t) => `You are reconciling a benchmark task on toydb (a SQL database) for a Rust-vs-Python coding-agent experiment. Task: ${ROOT}/tasks/${t}/ (SPEC.md, visible/, hidden/, NOTES.md). Two independent reference solutions: ${T1}/refs/${t}/rust and ${T1}/refs/${t}/python, made from the base workspaces ${T1}/rust and ${T1}/python (see ${T1}/LAYOUT.md for grade.py).
Goal: a correct, unambiguous task whose hidden scripts both references pass byte-identically.
1. Grade both refs on visible+hidden scripts with ${T1}/grade.py. For each failure decide who is wrong, with SPEC.md as the authority: fix the reference that deviates from the SPEC (keeping its code quality); if the hidden script's expected output is wrong, fix the script; if the SPEC is ambiguous or silent on a tested behaviour, make the SPEC explicit (choose the most natural reading), and fix the scripts/refs accordingly. Record every SPEC or script change with its reason in ${ROOT}/tasks/${t}/AMENDMENTS.md.
2. Also run each ref's own extra task scripts against the other ref; differences reveal untested ambiguities: resolve them the same way and promote useful ones to hidden scripts (keep 8-14 hidden scripts).
3. Grade the unmodified base workspaces on the hidden scripts: ideally every hidden script fails on the base; report how many pass and why.
4. Finish only when both refs pass every visible and hidden script and the full regression suite. Then write the patches: ${ROOT}/tasks/${t}/ref/t1-rust.patch and t1-python.patch (diff -ruN base ref, excluding target/, __pycache__, and the task script dir), and ref/STATS.md with files touched and lines added/removed per language.
Do not weaken the task to make things pass: the SPEC must still cover everything BRIEFS.md (${ROOT}/tasks/BRIEFS.md) asks for. ${KEEP}`
const REC_SCHEMA = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
  hidden_scripts: { type: 'integer' }, rust_pass: { type: 'integer' }, python_pass: { type: 'integer' },
  base_pass: { type: 'integer' }, spec_amendments: { type: 'integer' }, ref_fixes_rust: { type: 'integer' },
  ref_fixes_python: { type: 'integer' }, rust_lines_changed: { type: 'integer' }, python_lines_changed: { type: 'integer' },
  notes: { type: 'string' } },
  required: ['status', 'hidden_scripts', 'rust_pass', 'python_pass', 'base_pass', 'notes'] }

const tasks = {}
for (const t of ['between_in', 'case', 'date', 'distinct', 'alter', 'release']) {
  const author = await A(`${AUTHOR_COMMON}\nYour task: "${t}" in BRIEFS.md.\n${AUTHOR_OUT(t)}`, `author:${t}`, 'Tasks')
  const rust = await A(IMPL(t, 'rust'), `ref:${t}:rust`, 'Tasks')
  const python = await A(IMPL(t, 'python'), `ref:${t}:python`, 'Tasks')
  const rec = await A(RECONCILE(t), `reconcile:${t}`, 'Tasks', REC_SCHEMA)
  log(`${t}: rust ${rec && rec.rust_pass}/${rec && rec.hidden_scripts}, python ${rec && rec.python_pass}, base ${rec && rec.base_pass}`)
  tasks[t] = { author, rust, python, rec }
}
const bugAuthor = await A(`${AUTHOR_COMMON}
Your task is "bugfix" in BRIEFS.md. Inject four bugs into copies ${T1}/refs/bugfix/rust-buggy and ${T1}/refs/bugfix/python-buggy (cp -a from the bases), equivalently in both languages; check that ./check.sh stays green in both buggy copies, that each hidden script fails on the buggy copies and passes on the bases (use ${T1}/grade.py), and that each bug is caught by at least two hidden scripts. Save the injections as ${ROOT}/tasks/bugfix/inject/t1-rust.patch and t1-python.patch (diff -ruN base buggy, excluding target/ and __pycache__).
${AUTHOR_OUT('bugfix')} In NOTES.md give each bug's exact location and the fix, in both languages.`, 'author:bugfix', 'Tasks')
const bugVerify = await A(`You are independently verifying the "bugfix" control task of a Rust-vs-Python coding-agent experiment on toydb. Read ${ROOT}/tasks/BRIEFS.md (bugfix section), ${ROOT}/tasks/bugfix/ (SPEC.md, NOTES.md, visible/, hidden/, inject/), ${T1}/LAYOUT.md. Check, fixing anything that is wrong (record changes in ${ROOT}/tasks/bugfix/AMENDMENTS.md): the patches apply to fresh copies of ${T1}/rust and ${T1}/python; the bugs are truly equivalent across languages; ./check.sh is green on both buggy copies; every hidden and visible script fails on the buggy copies and passes on the bases (grade.py); SPEC.md describes symptoms only and gives enough for a competent engineer to find and fix each bug; the expected outputs are what correct toydb prints. ${KEEP}`, 'verify:bugfix', 'Tasks')
tasks.bugfix = { author: bugAuthor, verify: bugVerify }

// ---------------- Tier 2 port ----------------
phase('Tier 2 port')
const COMMON2 = `You are part of a team porting toydb (a Rust SQL database) to Python. Working directory: ${ROOT}. ${RESUME}
- Rust reference (read-only): ${ROOT}/rust. Python port: ${ROOT}/python. Tier 1 (encoding, storage, the whole SQL layer except sql/engine/raft.rs, the goldenscript runner) is already ported and passes 130/130 SQL+storage golden scripts and 117 unit tests: read ${ROOT}/python/PORT_REPORT.md first. You are now porting tier 2: the rest of toydb.
- Read ${ROOT}/PORTING.md and follow it exactly: one-to-one module mirroring, same names, idiomatic fully type-annotated Python 3.12, stdlib only (replace crates with stdlib equivalents: clap->argparse, rustyline->readline/input, config/yaml->a small parser for the config file format toydb actually uses, crossbeam->queue/threading, hdrhistogram->simple percentile code, uuid->uuid, petname->a small word list, log/simplelog->logging), byte-identical golden-script output, NEVER hardcode test outputs or special-case test inputs.
- Do not break tier 1: python3 python/run_goldenscripts.py rust/src/sql/testscripts rust/src/storage/testscripts must stay 130/130 and the unit tests must stay green (cd python && python3 -m unittest discover -s . -t .).
- Do not touch ${ROOT}/tiers or ${ROOT}/tasks. Never modify ${ROOT}/rust. You may build and run the Rust code (cd ${ROOT}/rust && ~/.cargo/bin/cargo test / cargo run) or a scratch copy of it to inspect behaviour.
- ${NOSUB}`
const RESULT = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
  scripts_passed: { type: 'integer' }, scripts_total: { type: 'integer' },
  python_loc: { type: 'integer' }, notes: { type: 'string' } },
  required: ['status', 'scripts_passed', 'scripts_total', 'notes'] }
const raft = await A(`${COMMON2}

Your scope: port src/raft/ (mod.rs, log.rs, message.rs, state.rs, node.rs) to python/toydb/raft/, plus the Rust test runners in their #[cfg(test)] modules (log and node goldenscript runners, including the simulated cluster/network harness in node.rs tests) under python/tests/raft/, registered in python/run_goldenscripts.py. Complete the Raft formatter in python/toydb/encoding/format.py and remove any stubs/TODOs that tier 1 left for raft. Acceptance: every script under rust/src/raft/testscripts passes byte-for-byte (python3 python/run_goldenscripts.py rust/src/raft/testscripts), and tier 1 stays green. Report scripts_passed/total for the raft scripts.`, 'raft', 'Tier 2 port', RESULT)
log(`raft: ${raft && raft.scripts_passed}/${raft && raft.scripts_total}`)
const server = await A(`${COMMON2}

Your scope: everything else in toydb. Port src/sql/engine/raft.rs (the Raft SQL engine and state machine), src/server.rs, src/client.rs, src/lib.rs re-exports (python/toydb/__init__.py), and src/bin/ (toydb.rs, toysql.rs, toydump.rs, workload.rs) as runnable modules (python3 -m toydb.bin.toydb etc., plus thin executable scripts python/bin/toydb, toysql, toydump, workload), the config file handling (rust/config/toydb.yaml must work unchanged), and the end-to-end test harness tests/testcluster.rs and tests/tests.rs as python/tests/e2e/ (testcluster.py, test_tests.py) that spin up real clusters of the Python server binary as subprocesses, registered in python/run_goldenscripts.py for rust/tests/scripts. Also port any unit tests in these files. The Raft layer is ported under python/toydb/raft (read it). Acceptance: every script under rust/tests/scripts passes byte-for-byte (python3 python/run_goldenscripts.py rust/tests/scripts), raft and tier-1 scripts stay green, and the binaries work interactively (start a 3-node cluster with the Python server and run a few SQL statements through toysql). Report scripts_passed/total for rust/tests/scripts.`, 'server', 'Tier 2 port', RESULT)
log(`server: ${server && server.scripts_passed}/${server && server.scripts_total}`)
const integ = await A(`${COMMON2}

Your scope: integration and fix-up of the complete port. Target: 100% of all toydb golden scripts pass byte-for-byte (python3 python/run_goldenscripts.py with no arguments runs everything under rust/: sql, storage, raft and tests/scripts), all unit tests pass, and the e2e tests are not flaky (run them 3 times). Fix any failures anywhere. Review the tier-2 modules for PORTING.md compliance and fix deviations. Then update ${ROOT}/python/PORT_REPORT.md to cover the whole port: Rust file -> Python file mapping with non-blank non-comment line counts for both (application code only, tests separately; mark which files are tier 1 and which tier 2), known deviations, final pass counts. Report total scripts_passed/total and python_loc (whole application).`, 'integrate', 'Tier 2 port', RESULT)
const VERDICT = { type: 'object', properties: {
  faithful: { type: 'boolean' }, cheating_found: { type: 'boolean' },
  issues: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, problem: { type: 'string' }, severity: { type: 'string', enum: ['blocker', 'major', 'minor'] } }, required: ['file', 'problem', 'severity'] } },
  scripts_passed: { type: 'integer' }, scripts_total: { type: 'integer' } },
  required: ['faithful', 'cheating_found', 'issues', 'scripts_passed', 'scripts_total'] }
const verdict = await A(`You are an independent reviewer of a Rust-to-Python port of toydb. Working directory: ${ROOT}. Rust: rust/ (reference). Python: python/. Conventions: PORTING.md. Focus on the tier-2 modules (raft/, sql/engine/raft, server, client, bin/, tests/e2e) but re-run everything.
Do not fix anything; only review and report. Check:
1. Re-run the acceptance yourself: python3 python/run_goldenscripts.py (all scripts) and the unit tests (cd python && python3 -m unittest discover -s . -t .). Report the real counts.
2. Cheating: application code (python/toydb) that reads .test files, matches on specific test inputs, hardcodes expected outputs, or passes tests without implementing the behaviour. Look hard.
3. Faithfulness: structure mirrors the Rust (spot-check every tier-2 module), same algorithms, idiomatic and type-annotated Python.
List concrete issues with file and severity. ${NOSUB}`, 'verify-t2', 'Tier 2 port', VERDICT)
return { builder, tasks, tier2: { raft, server, integ, verdict } }
