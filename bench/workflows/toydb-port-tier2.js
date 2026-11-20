export const meta = {
  name: 'toydb-port-tier2',
  description: 'Extend the Python toydb port to tier 2: raft, sql raft engine, server, client, binaries, e2e test cluster (Fable 5.2)',
  phases: [
    { title: 'Raft', detail: 'raft log/message/state/node + runners, 68 scripts', model: 'claude-fable-5-2' },
    { title: 'Server', detail: 'sql raft engine, server, client, bins, e2e cluster tests', model: 'claude-fable-5-2' },
    { title: 'Integrate', detail: 'all 200+ scripts green, report', model: 'claude-fable-5-2' },
    { title: 'Verify', detail: 'independent faithfulness/cheating review', model: 'claude-fable-5-2' },
  ],
}
const ROOT = '/home/user/agentbench/toydb'
const M = 'claude-fable-5-2'
const COMMON = `You are part of a team porting toydb (a Rust SQL database) to Python. Working directory: ${ROOT}.
- Rust reference (read-only): ${ROOT}/rust. Python port: ${ROOT}/python. Tier 1 (encoding, storage, the whole SQL layer except sql/engine/raft.rs, the goldenscript runner) is already ported and passes 130/130 SQL+storage golden scripts and 117 unit tests: read ${ROOT}/python/PORT_REPORT.md first. You are now porting tier 2: the rest of toydb.
- Read ${ROOT}/PORTING.md and follow it exactly: one-to-one module mirroring, same names, idiomatic fully type-annotated Python 3.12, stdlib only (replace crates with stdlib equivalents: clap->argparse, rustyline->readline/input, config/yaml->a small parser for the config file format toydb actually uses, crossbeam->queue/threading, hdrhistogram->simple percentile code, uuid->uuid, petname->a small word list, log/simplelog->logging), byte-identical golden-script output, NEVER hardcode test outputs or special-case test inputs.
- Do not break tier 1: python3 python/run_goldenscripts.py rust/src/sql/testscripts rust/src/storage/testscripts must stay 130/130 and the unit tests must stay green (cd python && python3 -m unittest discover -s . -t .).
- Do not touch ${ROOT}/tiers (frozen snapshots used by another team). Never modify ${ROOT}/rust. You may build and run the Rust code (cd ${ROOT}/rust && ~/.cargo/bin/cargo test / cargo run) or a scratch copy of it to inspect behaviour.
- Do not spawn subagents.`
const RESULT = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
  scripts_passed: { type: 'integer' }, scripts_total: { type: 'integer' },
  python_loc: { type: 'integer' }, notes: { type: 'string' } },
  required: ['status', 'scripts_passed', 'scripts_total', 'notes'] }

phase('Raft')
const raft = await agent(`${COMMON}

Your scope: port src/raft/ (mod.rs, log.rs, message.rs, state.rs, node.rs) to python/toydb/raft/, plus the Rust test runners in their #[cfg(test)] modules (log and node goldenscript runners, including the simulated cluster/network harness in node.rs tests) under python/tests/raft/, registered in python/run_goldenscripts.py. Complete the Raft formatter in python/toydb/encoding/format.py and remove any stubs/TODOs that tier 1 left for raft. Acceptance: every script under rust/src/raft/testscripts passes byte-for-byte (python3 python/run_goldenscripts.py rust/src/raft/testscripts), and tier 1 stays green. Report scripts_passed/total for the raft scripts.`,
  { label: 'raft', phase: 'Raft', model: M, schema: RESULT, agentType: 'general-purpose' })
log(`raft: ${raft && raft.scripts_passed}/${raft && raft.scripts_total}`)

phase('Server')
const server = await agent(`${COMMON}

Your scope: everything else in toydb. Port src/sql/engine/raft.rs (the Raft SQL engine and state machine), src/server.rs, src/client.rs, src/lib.rs re-exports (python/toydb/__init__.py), and src/bin/ (toydb.rs, toysql.rs, toydump.rs, workload.rs) as runnable modules (python3 -m toydb.bin.toydb etc., plus thin executable scripts python/bin/toydb, toysql, toydump, workload), the config file handling (rust/config/toydb.yaml must work unchanged), and the end-to-end test harness tests/testcluster.rs and tests/tests.rs as python/tests/e2e/ (testcluster.py, test_tests.py) that spin up real clusters of the Python server binary as subprocesses, registered in python/run_goldenscripts.py for rust/tests/scripts. Also port any unit tests in these files. The Raft layer was just ported under python/toydb/raft (read it). Acceptance: every script under rust/tests/scripts passes byte-for-byte (python3 python/run_goldenscripts.py rust/tests/scripts), raft and tier-1 scripts stay green, and the binaries work interactively (start a 3-node cluster with the Python server and run a few SQL statements through toysql). Report scripts_passed/total for rust/tests/scripts.`,
  { label: 'server', phase: 'Server', model: M, schema: RESULT, agentType: 'general-purpose' })
log(`server: ${server && server.scripts_passed}/${server && server.scripts_total}`)

phase('Integrate')
const integ = await agent(`${COMMON}

Your scope: integration and fix-up of the complete port. Target: 100% of all toydb golden scripts pass byte-for-byte (python3 python/run_goldenscripts.py with no arguments runs everything under rust/: sql, storage, raft and tests/scripts), all unit tests pass, and the e2e tests are not flaky (run them 3 times). Fix any failures anywhere. Review the tier-2 modules for PORTING.md compliance and fix deviations. Then update ${ROOT}/python/PORT_REPORT.md to cover the whole port: Rust file -> Python file mapping with non-blank non-comment line counts for both (application code only, tests separately; mark which files are tier 1 and which tier 2), known deviations, final pass counts. Report total scripts_passed/total and python_loc (whole application).`,
  { label: 'integrate', phase: 'Integrate', model: M, schema: RESULT, agentType: 'general-purpose' })
log(`integrate: ${integ && integ.scripts_passed}/${integ && integ.scripts_total}`)

phase('Verify')
const VERDICT = { type: 'object', properties: {
  faithful: { type: 'boolean' }, cheating_found: { type: 'boolean' },
  issues: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, problem: { type: 'string' }, severity: { type: 'string', enum: ['blocker', 'major', 'minor'] } }, required: ['file', 'problem', 'severity'] } },
  scripts_passed: { type: 'integer' }, scripts_total: { type: 'integer' } },
  required: ['faithful', 'cheating_found', 'issues', 'scripts_passed', 'scripts_total'] }
const verdict = await agent(`You are an independent reviewer of a Rust-to-Python port of toydb. Working directory: ${ROOT}. Rust: rust/ (reference). Python: python/. Conventions: PORTING.md. Focus on the tier-2 modules (raft/, sql/engine/raft, server, client, bin/, tests/e2e) but re-run everything.
Do not fix anything; only review and report. Check:
1. Re-run the acceptance yourself: python3 python/run_goldenscripts.py (all scripts) and the unit tests (cd python && python3 -m unittest discover -s . -t .). Report the real counts.
2. Cheating: application code (python/toydb) that reads .test files, matches on specific test inputs, hardcodes expected outputs, or passes tests without implementing the behaviour. Look hard.
3. Faithfulness: structure mirrors the Rust (spot-check every tier-2 module: Raft node roles and message handling, log, state machine apply, server request handling, client, bins), same algorithms, idiomatic and type-annotated Python.
List concrete issues with file and severity. Do not spawn subagents.`,
  { label: 'verify', phase: 'Verify', model: M, schema: VERDICT, agentType: 'general-purpose' })
return { raft, server, integ, verdict }
