export const meta = {
  name: 'toydb-port-tier1',
  description: 'Port toydb SQL core + storage + encoding from Rust to Python with Fable agents, verified by golden scripts',
  phases: [
    { title: 'Scaffold', detail: 'goldenscript runner, error, fmt, encoding', model: 'claude-fable-5-2' },
    { title: 'Port', detail: 'storage || sql types+parser -> planner', model: 'claude-fable-5-2' },
    { title: 'SQL engine', detail: 'execution + engine + SQL runners', model: 'claude-fable-5-2' },
    { title: 'Integrate', detail: 'all tier-1 scripts green', model: 'claude-fable-5-2' },
    { title: 'Verify', detail: 'independent faithfulness/cheating review', model: 'claude-fable-5-2' },
  ],
}
const ROOT = '/home/user/agentbench/toydb'
const COMMON = `You are part of a team porting toydb (a Rust SQL database) to Python. Working directory: ${ROOT}.
- Rust reference (read-only): ${ROOT}/rust (toydb). Python port: ${ROOT}/python.
- Read ${ROOT}/PORTING.md first and follow it exactly: one-to-one module mirroring, same names, idiomatic fully type-annotated Python 3.12, stdlib only, byte-identical golden-script output, and NEVER hardcode test outputs or special-case test inputs.
- The goldenscript crate source is at ~/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/goldenscript-0.8.0/src (README: ${ROOT}/GOLDENSCRIPT_README.md).
- You may run the Rust tests (cd ${ROOT}/rust && ~/.cargo/bin/cargo test ...) to inspect real behaviour, e.g. by adding temporary debug output in a scratch copy, but never modify ${ROOT}/rust itself.
- Stay inside your assigned scope; other agents work on other modules in parallel. Where you depend on a module that does not exist yet, code against the Rust names exactly as PORTING.md maps them.
- Do not spawn subagents.`
const RESULT = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
  scripts_passed: { type: 'integer' }, scripts_total: { type: 'integer' },
  python_loc: { type: 'integer' }, notes: { type: 'string' } },
  required: ['status', 'scripts_passed', 'scripts_total', 'notes'] }

phase('Scaffold')
const s0 = await agent(`${COMMON}

Your scope (do this first; others build on it):
1. python/tests/goldenscript.py: a faithful port of the goldenscript crate (parser, Runner protocol and hooks, output generation and comparison). Port its own tests too if the crate has test scripts, and make them pass.
2. python/run_goldenscripts.py: runs testscript files/dirs with the right Python runner (registry of dir -> runner, mirroring the Rust test modules; runners will be added by other agents under python/tests/), prints PASS/FAIL per file with a unified diff on failure, and a final "N/M scripts passed" line. Unknown dirs should print a clear "no runner yet" message, not crash.
3. python/toydb/__init__.py, error.py (mirror src/error.rs exactly, incl. Display text), fmt.py (Rust Display/Debug helpers), and the whole python/toydb/encoding/ package (keycode, bincode, format) mirroring src/encoding/, with the Rust inline unit tests ported under python/tests/encoding/ and passing (python3 -m unittest).
Report scripts_passed/total for whatever golden scripts you could run (goldenscript self-tests), and python_loc of what you wrote.`,
  { label: 'scaffold', phase: 'Scaffold', model: 'claude-fable-5-2', schema: RESULT, agentType: 'general-purpose' })
log(`scaffold: ${s0 && s0.status} — ${s0 && s0.notes}`)

phase('Port')
const [storage, sqlfront] = await parallel([
  () => agent(`${COMMON}

Your scope: the storage layer. Port src/storage/ (engine.rs, memory.rs, bitcask.rs, mvcc.rs, mod.rs) to python/toydb/storage/, plus the Rust test runners for them (BitCaskRunner, MVCCRunner, the engine/memory test runners and any test helper engine such as an operation-emitting test engine) under python/tests/storage/, registered in python/run_goldenscripts.py. Acceptance: every script under rust/src/storage/testscripts/ passes byte-for-byte via python3 python/run_goldenscripts.py rust/src/storage/testscripts. Report scripts_passed/total for that directory.`,
    { label: 'storage', phase: 'Port', model: 'claude-fable-5-2', schema: RESULT, agentType: 'general-purpose' }),
  () => agent(`${COMMON}

Your scope: the SQL front half, in this order: src/sql/types/ (value.rs, expression.rs, schema.rs, mod.rs) then src/sql/parser/ (lexer.rs, ast.rs, parser.rs, mod.rs), ported to python/toydb/sql/types/ and python/toydb/sql/parser/, plus python/toydb/sql/__init__.py. Port the Rust inline parser/lexer unit tests to python/tests/sql/ and make them pass. Do NOT port the planner, execution or engine (another agent will, right after you); leave clean, Rust-mirroring interfaces for them. Report the unit tests passed/total as scripts_passed/scripts_total.`,
    { label: 'sql-types-parser', phase: 'Port', model: 'claude-fable-5-2', schema: RESULT, agentType: 'general-purpose' }),
])
log(`storage: ${storage && storage.scripts_passed}/${storage && storage.scripts_total}; sql front: ${sqlfront && sqlfront.status}`)

const planner = await agent(`${COMMON}

Your scope: port src/sql/planner/ (plan.rs, planner.rs, optimizer.rs, mod.rs) to python/toydb/sql/planner/, building on the already-ported python/toydb/sql/types and python/toydb/sql/parser (fix small issues there if you must, but keep their structure). Plan display (the tree rendering with ├─ └─) must match Rust exactly. Do not port execution/engine (next agent). Port any planner unit tests. Report status and notes.`,
  { label: 'sql-planner', phase: 'Port', model: 'claude-fable-5-2', schema: RESULT, agentType: 'general-purpose' })
log(`planner: ${planner && planner.status} — ${planner && planner.notes}`)

phase('SQL engine')
const engine = await agent(`${COMMON}

Your scope: port src/sql/execution/ (executor, aggregator, join, session, mod) and src/sql/engine/ (engine.rs, local.rs, mod.rs — NOT raft.rs, that is a later tier) to python/toydb/sql/, wiring together the already-ported types, parser, planner, storage (MVCC) and encoding. Then port the SQL test runners from src/sql/mod.rs (SQLRunner, ExpressionRunner and the test engine they use) to python/tests/sql/ and register them in python/run_goldenscripts.py for every subdirectory of rust/src/sql/testscripts. Iterate until as many of the 93 SQL scripts as possible pass byte-for-byte: python3 python/run_goldenscripts.py rust/src/sql/testscripts. Fix bugs anywhere in the port as needed, but keep the structure mirroring Rust. Report scripts_passed/total for the SQL scripts.`,
  { label: 'sql-engine', phase: 'SQL engine', model: 'claude-fable-5-2', schema: RESULT, agentType: 'general-purpose' })
log(`sql engine: ${engine && engine.scripts_passed}/${engine && engine.scripts_total}`)

phase('Integrate')
const integ = await agent(`${COMMON}

Your scope: integration and fix-up of the whole tier-1 port (everything except raft/, server.rs, client.rs, bin/, sql/engine/raft.rs). Target: 100% of rust/src/sql/testscripts and rust/src/storage/testscripts pass byte-for-byte, and all ported unit tests pass (python3 -m unittest discover -s python/tests -t python). Fix any failures anywhere in the port. Then review the whole port for PORTING.md compliance (module mirroring, names, type hints, no hardcoded outputs) and fix deviations. Finally write ${ROOT}/python/PORT_REPORT.md: Rust file -> Python file mapping with non-blank non-comment line counts for both (application code only, tests separately), known deviations, and the final pass counts. Report the combined scripts_passed/total and python_loc (application code only).`,
  { label: 'integrate', phase: 'Integrate', model: 'claude-fable-5-2', schema: RESULT, agentType: 'general-purpose' })
log(`integrate: ${integ && integ.scripts_passed}/${integ && integ.scripts_total}`)

phase('Verify')
const VERDICT = { type: 'object', properties: {
  faithful: { type: 'boolean' }, cheating_found: { type: 'boolean' },
  issues: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, problem: { type: 'string' }, severity: { type: 'string', enum: ['blocker', 'major', 'minor'] } }, required: ['file', 'problem', 'severity'] } },
  scripts_passed: { type: 'integer' }, scripts_total: { type: 'integer' } },
  required: ['faithful', 'cheating_found', 'issues', 'scripts_passed', 'scripts_total'] }
const verdict = await agent(`You are an independent reviewer of a Rust-to-Python port of toydb. Working directory: ${ROOT}. Rust: rust/ (reference). Python: python/. Conventions: PORTING.md.
Do not fix anything; only review and report. Check:
1. Re-run the acceptance yourself: python3 python/run_goldenscripts.py rust/src/sql/testscripts rust/src/storage/testscripts, and the unit tests (python3 -m unittest discover -s python/tests -t python). Report the real counts.
2. Cheating: any application code (python/toydb) that reads .test files, matches on specific test inputs, hardcodes expected outputs, or otherwise passes tests without implementing the behaviour. Look hard (grep for testscripts, literal fragments of expected outputs, suspicious conditionals).
3. Faithfulness: module/type/function structure mirrors the Rust (spot-check every module), algorithms are the same (e.g. the optimizers, MVCC, bitcask compaction, join implementations), and the Python is idiomatic and type-annotated.
List concrete issues with file and severity. Do not spawn subagents.`,
  { label: 'verify', phase: 'Verify', model: 'claude-fable-5-2', schema: VERDICT, agentType: 'general-purpose' })
return { s0, storage, sqlfront, planner, engine, integ, verdict }