export const meta = {
  name: 'toydb-tier3-build',
  description: 'One Opus agent at a time: tier-3 extension layers (spec -> independent Rust and Python implementations -> reconcile + workspaces/grader), then per task port refs and add extension-level hidden scripts',
  phases: [
    { title: 'Extensions', detail: 'spec, Rust impl, Python impl, reconcile + workspaces + grader', model: 'claude-opus-5-5' },
    { title: 'Tasks', detail: 'per task: port tier-2 refs to tier 3, extension hidden scripts, site analysis', model: 'claude-opus-5-5' },
  ],
}
const ROOT = '/home/user/agentbench/toydb'
const T2 = `${ROOT}/tiers/t2`
const T3 = `${ROOT}/tiers/t3`
const M = 'claude-opus-5-5'
const RESUME = 'If some of your output files already exist, they are from an interrupted earlier attempt of this step: inspect them, keep what is correct, and complete or redo the rest.'
const KEEP = `Never modify ${ROOT}/rust, ${ROOT}/python, anything under ${ROOT}/tiers/t1 or ${T2}, or any tier-1/tier-2 task files (${ROOT}/tasks/*/{SPEC.md,SPEC_t2.md,visible,hidden,hidden_t2,ref/t1-*,ref/t2-*,inject/t1-*,inject/t2-*}). Do not spawn subagents. Do not use the web.`
const RES = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] }, notes: { type: 'string' } },
  required: ['status', 'notes'] }
const A = (prompt, label, ph) => agent(prompt, { label, phase: ph, model: M, schema: RES, agentType: 'general-purpose' })

const CONTEXT = `Context: a Rust-vs-Python experiment for a talk asks whether a type system helps weak coding agents. Codebases: toydb (a distributed SQL database) in Rust and a faithful Python port, at growing sizes. Tier 2 = ${T2}/{rust,python} (full toydb; see ${T2}/LAYOUT.md). Seven benchmark tasks (${ROOT}/tasks/*/SPEC.md: between_in, case, date, distinct, alter, release, bugfix) are run at every tier. Finding so far: when the code that consumes a change is out of the agent's view, Python solutions can miss it silently (hand-written serialization), while Rust's derives/exhaustive matches cover or flag it. Tier 3 = tier 2 plus EXTENSION LAYERS: substantial, realistic subsystems that CONSUME the core types (Value, DataType, the plan Expression, plan Node, Aggregate, the catalog/Table/Column schema, the Raft Write commands and StatementResult), so the same tasks have to reach code far from the SQL core.`
const FAIR = `Fairness rules (critical; the talk's credibility depends on them): each language's code must be what a strong engineer would naturally write in that language, not rigged for or against either. Rust: idiomatic matching (exhaustive where a careful engineer would be exhaustive, wildcards where they'd naturally use one), derives where natural. Python: idiomatic Python (match/if-elif/dict dispatch/isinstance, generic fallbacks where natural, defensive raise where natural; type hints like the existing port), no artificial fragility and no artificial robustness. Do not add anything whose only purpose is to make a future task fail or pass.`

phase('Extensions')
const spec = await A(`${CONTEXT}
You are designing the tier-3 extension layers. ${RESUME}
Study both tier-2 workspaces, the 7 task SPECs (and SPEC_t2.md, NOTES.md, NOTES_t2.md), and how golden scripts are run (SQL runner and cluster runner).
Design 4-6 extensions totalling roughly 3-5k lines of application code per language, e.g. (pick and refine; better ideas welcome): table export/import in CSV and JSON-lines (SQL statements or toysql/toydump commands); column statistics (ANALYZE-style: counts, nulls, min/max, distinct estimate, simple histograms per data type, persisted through the catalog and Raft); EXPLAIN in a structured JSON format; a change-data-capture / audit log of applied Raft writes in a documented stable encoding; a query fingerprint / plan cache keyed on normalized plans; a schema migration/diff tool; a second wire format for the client (e.g. JSON results in toysql). Requirements:
- Each extension is wired into the real system (reachable from SQL or the client/bins) and is exercised by golden scripts (SQL runner and/or cluster runner) with byte-exact outputs; existing behaviour and all 203 existing golden scripts stay unchanged.
- Each extension consumes the core types broadly, so that EVERY one of the 6 feature tasks (all except bugfix) has to change at least one extension for its feature to work end to end, and across the tasks some sites are compiler-checkable in Rust and some are not. Together they should give each task several distant sites.
- Every output is defined by GENERIC rules (e.g. "values are written as their SQL literal form; strings are quoted as ...; a data type is written as its SQL name"), so the correct extension behaviour for a type, expression, node, aggregate or command added by a task follows from the rules without further specification. Write the rules so they cover future additions explicitly where needed (e.g. "every expression kind is written as {\\"kind\\": <variant name in snake_case>, ...fields}").
Write: ${T3}/EXT_SPEC.md (language-neutral, complete, exact: what a developer of either language needs; the extensions' user docs for agents will be derived from it), ${T3}/ext_scripts/{sql,cluster}/ (golden scripts with expected outputs reasoned exactly from the rules and existing display conventions, at least 6 per extension), and ${T3}/EXT_DESIGN_NOTES.md (experimenter-only: per task, which extension sites it must reach and why). Do not implement. ${FAIR} ${KEEP}`, 'ext-spec', 'Extensions')
log(`ext-spec: ${spec && spec.status}`)
if (!spec || spec.status === 'blocked') return { spec }

const IMPL = (lang) => `${CONTEXT}
You are implementing the tier-3 extension layers in ${lang === 'rust' ? 'Rust' : 'Python'}. ${RESUME}
Workspace: ${T3}/${lang}. If it does not exist, create it as a copy: cp -a ${T2}/${lang} ${T3}/${lang} (then for Rust build once to warm target/).
Implement ${T3}/EXT_SPEC.md completely, as natural, well-structured ${lang === 'rust' ? 'Rust' : 'Python'} in the style of the codebase (new modules where natural, wired into the system as specified), with unit tests in the codebase's style. Install ${T3}/ext_scripts/sql/* and cluster/* as permanent golden scripts in the workspace (choose natural locations next to the existing testscripts and register them like the existing ones; they become part of the regression suite) and make them all pass, together with every existing test (./check.sh green). If a script's expected output contradicts EXT_SPEC.md, report it in your notes and follow EXT_SPEC.md; do not edit ${T3}/ext_scripts. Also update README.md to document the extensions briefly, as the project README would.
Do not read ${T3}/${lang === 'rust' ? 'python' : 'rust'} or ${T3}/EXT_DESIGN_NOTES.md or anything under ${ROOT}/tasks. ${FAIR} ${KEEP}
Report status and notes (files, line counts, deviations, spec ambiguities).`
const rs = await A(IMPL('rust'), 'ext-rust', 'Extensions')
log(`ext-rust: ${rs && rs.status}`)
const py = await A(IMPL('python'), 'ext-python', 'Extensions')
log(`ext-python: ${py && py.status}`)

const rec = await A(`${CONTEXT}
You are reconciling and finishing tier 3. ${RESUME}
Inputs: ${T3}/EXT_SPEC.md, ${T3}/ext_scripts, and two independent implementations ${T3}/rust and ${T3}/python (reports: Rust: ${JSON.stringify(rs && rs.notes || '').slice(0, 1500)} / Python: ${JSON.stringify(py && py.notes || '').slice(0, 1500)}).
1. Make both pass every ext script byte-identically and all existing tests. For each discrepancy decide with EXT_SPEC.md as authority: fix the implementation that deviates; fix the script if it is wrong; make the spec explicit if it is ambiguous (record every change in ${T3}/EXT_AMENDMENTS.md). Cross-run each implementation's own extra tests/scripts against the other to find untested ambiguities.
2. Review both for the fairness rules below and for quality; fix anything rigged, unidiomatic, or sloppy, and record what you changed.
3. Tier-3 workspaces must follow the tier-2 conventions: same two task dirs, check.sh (same output contract; keep the lines \`trap 'rm -f "$log"' EXIT\` and \`echo "== test"\` exactly), README.md documenting the extensions, warm Rust target/. Write ${T3}/grade.py (same CLI/JSON as ${T2}/grade.py; copy and adapt) and ${T3}/LAYOUT.md (as tier 2, plus where the extensions live, baseline totals, warm check.sh timings, application/test line counts of both measured like tier 2, and the extension line counts separately).
4. Verify: clean check.sh both; grade.py with empty dir = full regression; workspaces copied elsewhere work.
${FAIR} ${KEEP} Report status and notes.`, 'ext-reconcile', 'Extensions')
log(`ext-reconcile: ${rec && rec.status}`)
if (!rec || rec.status === 'blocked') return { spec, rs, py, rec }

phase('Tasks')
const TASKS = ['between_in', 'case', 'date', 'distinct', 'alter', 'release', 'bugfix']
const PORT = (t) => `${CONTEXT}
You are porting benchmark task '${t}' from tier 2 to tier 3. ${RESUME}
Read first: ${ROOT}/tasks/BRIEFS.md, ${ROOT}/tasks/${t}/ (SPEC.md, SPEC_t2.md if any, visible/, hidden/, hidden_t2/, NOTES*.md, AMENDMENTS.md, ref/, inject/), ${T2}/LAYOUT.md, ${T3}/LAYOUT.md, ${T3}/EXT_SPEC.md, ${T3}/EXT_DESIGN_NOTES.md.
${t === 'bugfix' ? `For bugfix: port the injection (inject/t3-{rust,python}.patch applying cleanly to ${T3}/{rust,python}); verify the bases pass all bugfix visible+hidden scripts, the injected copies fail all of them, and check.sh on the injected copies stays green (report any extension test that now catches a bug). If a bug is observable through an extension in a way the existing scripts miss, you may add hidden_t3 scripts for it. The references are the bases. Write NOTES_t3.md.` : `Do:
1. Create ${T3}/refs/${t}/{rust,python} as cp -a copies of ${T3}/{rust,python} and apply ref/t2-{rust,python}.patch (adapt hunks if needed).
2. Make all tier-1 sql scripts (visible+hidden) and hidden_t2 cluster scripts pass with ${T3}/grade.py (scripts dir with sql/ and cluster/), full regression green.
3. Before fixing anything else, grade the "tier-2-only" refs (just the t2 patch applied) on step 4's scripts once they exist, and record exactly what fails, and how: compile error, existing test failing, runtime error, or silently wrong output.
4. Find every extension site the feature must reach (EXT_SPEC.md's generic rules determine the correct behaviour) and implement it in both refs in the codebase's style. Author 3-6 hidden tier-3 scripts in ${ROOT}/tasks/${t}/hidden_t3/{sql,cluster}/ that exercise the feature through the extensions (names unique across all of the task's scripts), with expected output that follows from SPEC.md + SPEC_t2.md + EXT_SPEC.md; if a tested behaviour is not determined by them, add the minimum to ${ROOT}/tasks/${t}/SPEC_t3.md (only if unavoidable). Both refs must pass byte-identically; each script must fail on the unmodified tier-3 base and should, where possible, fail on the tier-2-only refs.
5. Write ${ROOT}/tasks/${t}/NOTES_t3.md: a table of every site (tier-1 core, tier-2, extensions) the change must touch per language, and for each whether a tier-2-only solution is caught by the compiler, by an existing test (which), or not at all (silent). Write ref/t3-{rust,python}.patch (diff -ruN base ref, excluding target/, __pycache__ and both task dirs) and a tier-3 section in ref/STATS.md.`}
Use ${T3}/grade.py for all grading and grade one thing at a time (cluster tests flake under heavy load). Do not modify ${T3}/rust, ${T3}/python or ${T3}/grade.py. ${KEEP} Report status and notes, including the step-3 table.`
const out = { spec, rs, py, rec }
for (const t of TASKS) {
  const r = await A(PORT(t), `port3:${t}`, 'Tasks')
  log(`port3:${t}: ${r && r.status}`)
  out[t] = r
}
return out