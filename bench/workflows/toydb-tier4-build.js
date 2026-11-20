export const meta = {
  name: 'toydb-tier4-build',
  description: 'One Opus agent at a time: tier-4 extension layers (spec -> per extension: Rust impl, Python impl, reconcile -> finish: doc parity, README, grader, layout), then per task port refs and add tier-4 hidden scripts',
  phases: [
    { title: 'Spec', model: 'claude-opus-5-5' },
    { title: 'Extensions', detail: 'per extension: Rust, Python, reconcile', model: 'claude-opus-5-5' },
    { title: 'Finish', model: 'claude-opus-5-5' },
    { title: 'Tasks', detail: 'per task: port tier-3 refs to tier 4, hidden_t4 scripts, site analysis', model: 'claude-opus-5-5' },
  ],
}
const ROOT = '/home/user/agentbench/toydb'
const T3 = `${ROOT}/tiers/t3`
const T4 = `${ROOT}/tiers/t4`
const M = 'claude-opus-5-5'
const SCR = '/tmp/claude-1001/t4scratch'
const RESUME = 'If some of your output files already exist, they are from an interrupted earlier attempt of this step: inspect them, keep what is correct, and complete or redo the rest.'
const KEEP = `Never modify ${ROOT}/rust, ${ROOT}/python, anything under ${ROOT}/tiers/t1, ${ROOT}/tiers/t2 or ${T3}, or any tier-1..3 task files (${ROOT}/tasks/*/{SPEC.md,SPEC_t2.md,SPEC_t3.md,visible,hidden,hidden_t2,hidden_t3,ref/t1-*,ref/t2-*,ref/t3-*,inject/t1-*,inject/t2-*,inject/t3-*}). Do not spawn subagents. Do not use the web.`
const HYG = `Disk and scratch hygiene: put scratch copies only under ${SCR}/<your step label>/ (never put a Cargo.toml directly in ${SCR}), and delete your scratch dir, including any Rust target/ dirs in it, before you finish. Upstream toydb's bitcask tests leak a 400 MB /tmp/toydb* dir per cargo test run; a background sweeper removes them, so ignore them. Build and test one thing at a time (cluster tests flake under heavy load).`
const RES = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] }, notes: { type: 'string' } },
  required: ['status', 'notes'] }
const SPECRES = { type: 'object', properties: {
  status: { type: 'string', enum: ['done', 'partial', 'blocked'] }, notes: { type: 'string' },
  extensions: { type: 'array', items: { type: 'object', properties: {
    id: { type: 'string', description: 'short slug, e.g. prepared' }, title: { type: 'string' }, section: { type: 'string', description: 'its section in EXT4_SPEC.md' } },
    required: ['id', 'title', 'section'] } } },
  required: ['status', 'notes', 'extensions'] }
const A = (prompt, label, ph, schema) => agent(prompt, { label, phase: ph, model: M, schema: schema || RES, agentType: 'general-purpose' })

const CONTEXT = `Context: a Rust-vs-Python experiment for a talk asks whether a type system helps coding agents. Codebases: toydb (a distributed SQL database) in Rust and a faithful Python port, at growing sizes. Tier 3 = ${T3}/{rust,python}: full toydb plus four extension layers (JSON output, statement digests, column statistics, export/import) that consume the core types; see ${T3}/LAYOUT.md, ${T3}/EXT_SPEC.md, ${T3}/EXT_AMENDMENTS.md. About 10.1k lines of Rust / 12.7k of Python application code. Seven benchmark tasks (${ROOT}/tasks/*/SPEC.md: between_in, case, date, distinct, alter, release, bugfix) run at every tier.
Findings so far: types tell an agent WHERE a change must land, not WHAT to write there. When the README did not mention the extensions, the weak model (Haiku) in Python missed extension sites that Rust's compiler flags (6/18 runs vs 0/18 in Rust), but Sonnet and Opus found every such site themselves (0/36), so their remaining failures were content errors no type system checks. Tier 4 asks whether the effect returns for strong models once the codebase is much larger and the consumers of a change are more numerous and harder to find.
Tier 4 = tier 3 plus MORE EXTENSION LAYERS: realistic subsystems that CONSUME the core types (Value, DataType, the AST and plan Expression, plan Node, Aggregate, ast Statement, StatementResult, the catalog Table/Column schema, the Raft Write/Read commands), adding roughly 10k lines of application code per language, so the Rust application code reaches at least 20k lines.`
const FAIR = `Fairness rules (critical; the talk's credibility depends on them): each language's code must be what a strong engineer would naturally write in that language, not rigged for or against either. Rust: idiomatic matching (exhaustive where a careful engineer would be exhaustive, wildcards where they'd naturally use one), derives where natural. Python: idiomatic Python following the existing port's conventions (match with case _: panic where the port does that, generic fallbacks where natural, type hints like the existing port), no artificial fragility and no artificial robustness. Do not add anything whose only purpose is to make a future task fail or pass.
Documentation parity (critical): the two implementations must document their code identically. Every new module's doc comment (Rust //! or /// ; Python module/class/function docstrings) must use the exact text given in ${T4}/EXT4_DOCS.md for that module, in the language's comment syntax; do not add rules, examples or hints in comments that the other language's code does not also have. Ordinary short code comments are fine but must not state behavioural rules that only one side states.`

phase('Spec')
const spec = await A(`${CONTEXT}
You are designing the tier-4 extension layers. ${RESUME}
Study both tier-3 workspaces (code, README, tests), ${T3}/EXT_SPEC.md and EXT_AMENDMENTS.md, the 7 task SPECs (and SPEC_t2.md, NOTES*.md), and how golden scripts are run (SQL runner and cluster runner).
Design 5-7 new extensions totalling roughly 10k lines of application code per language (plus tests), each a realistic subsystem in its own module(s), wired into the real system (reachable from SQL, the client or the bins) and exercised by golden scripts. Candidates (pick and refine; better ideas welcome): prepared statements with typed parameters ($1, ...) and a plan cache with invalidation on schema change; DESCRIBE / result-type inference for queries without executing them; row-count and cost estimates in EXPLAIN using the tier-3 column statistics; a SQL pretty-printer/formatter of statements; views (CREATE VIEW stored as canonical SQL, re-planned on use, with dependency checks on DROP/ALTER); a change-data-capture log of applied Raft writes with a stable encoding of every write command; a lint/analysis pass that reports warnings about statements; a binary backup/restore format; column-level access control. Requirements:
- Each extension consumes the core types broadly, so that EVERY one of the 6 feature tasks (all except bugfix) has to change several tier-4 sites in addition to the tier-3 ones, some compiler-checkable in Rust and some not, spread across modules a developer working on the SQL core would not naturally open.
- Every output is defined by GENERIC rules (as in tier 3), so the correct behaviour for a type, expression, node, aggregate, statement or command added by a task follows from the rules without naming it. Write the rules so they cover future additions explicitly.
- Existing behaviour and all 235 existing golden scripts stay unchanged.
Write: ${T4}/EXT4_SPEC.md (language-neutral, complete, exact; one numbered section per extension, plus shared definitions; the user docs will be derived from it); ${T4}/EXT4_DOCS.md (for every new module, the exact doc-comment text both implementations must use, plus the README section text for each extension; keep them equally informative for both languages); ${T4}/ext4_scripts/<extension id>/{sql,cluster}/ (golden scripts with expected outputs reasoned exactly from the rules and existing display conventions, at least 6 per extension); ${T4}/EXT4_DESIGN_NOTES.md (experimenter-only: per task, which tier-4 sites it must reach, which are compiler-checkable in Rust, how Python would fail if missed). Do not implement. Return the extension list in build order (an extension may depend on earlier ones). ${FAIR} ${HYG} ${KEEP}`, 'ext4-spec', 'Spec', SPECRES)
log(`ext4-spec: ${spec && spec.status} ${spec && (spec.extensions || []).map(x => x.id).join(',')}`)
if (!spec || spec.status === 'blocked' || !(spec.extensions || []).length) return { spec }

phase('Extensions')
const IMPL = (lang, x, i) => `${CONTEXT}
You are implementing tier-4 extension ${i + 1} of ${spec.extensions.length}, "${x.title}" (id ${x.id}, ${x.section} of ${T4}/EXT4_SPEC.md), in ${lang === 'rust' ? 'Rust' : 'Python'}. ${RESUME}
Workspace: ${T4}/${lang}. ${i === 0 ? `It does not exist yet: create it as a copy, cp -a ${T3}/${lang} ${T4}/${lang}${lang === 'rust' ? ' (then build once to warm target/)' : ''}.` : 'It already contains the earlier tier-4 extensions; build on them.'}
Implement only this extension (and the shared definitions it needs), completely, as natural, well-structured ${lang === 'rust' ? 'Rust' : 'Python'} in the codebase's style, with unit tests in the codebase's style, using the doc-comment texts of ${T4}/EXT4_DOCS.md. Install ${T4}/ext4_scripts/${x.id}/{sql,cluster}/* as permanent golden scripts (next to the existing ext scripts, e.g. an ext4/ dir, registered like them) and make them pass together with every existing test (./check.sh green). If a script's expected output contradicts EXT4_SPEC.md, report it and follow the spec; do not edit ${T4}/ext4_scripts.
Do not read ${T4}/${lang === 'rust' ? 'python' : 'rust'}, ${T3}/${lang === 'rust' ? 'python' : 'rust'}, ${T4}/EXT4_DESIGN_NOTES.md or anything under ${ROOT}/tasks. Do not edit README.md yet. ${FAIR} ${HYG} ${KEEP}
Report status and notes (files, application/test line counts, deviations, spec ambiguities).`
const out = { spec, ext: {} }
for (const [i, x] of spec.extensions.entries()) {
  const rs = await A(IMPL('rust', x, i), `ext4:${x.id}:rust`, 'Extensions')
  const py = await A(IMPL('python', x, i), `ext4:${x.id}:python`, 'Extensions')
  const rec = await A(`${CONTEXT}
You are reconciling tier-4 extension "${x.title}" (id ${x.id}, ${x.section} of ${T4}/EXT4_SPEC.md) between two independent implementations in ${T4}/rust and ${T4}/python. ${RESUME}
Reports: Rust: ${JSON.stringify(rs && rs.notes || '').slice(0, 1500)} / Python: ${JSON.stringify(py && py.notes || '').slice(0, 1500)}
1. Make both pass every ${T4}/ext4_scripts/${x.id} script byte-identically, plus all existing tests. For each discrepancy decide with EXT4_SPEC.md as authority: fix the deviating implementation; fix the script if it is wrong; make the spec explicit if ambiguous. Record every change in ${T4}/EXT4_AMENDMENTS.md (section for ${x.id}). Cross-run each implementation's own extra tests/probes against the other to find untested ambiguities.
2. Review both for the fairness and documentation-parity rules and for quality; fix anything rigged, unidiomatic, sloppy, or documented differently, and record what you changed.
3. Record, for each of the 6 feature tasks, which of this extension's sites the Rust compiler would flag and which would fail loudly or silently in Python (append to the table in EXT4_AMENDMENTS.md).
${FAIR} ${HYG} ${KEEP} Report status and notes.`, `ext4:${x.id}:reconcile`, 'Extensions')
  log(`ext4:${x.id}: rs=${rs && rs.status} py=${py && py.status} rec=${rec && rec.status}`)
  out.ext[x.id] = { rs, py, rec }
  if (!rec || rec.status === 'blocked') return out
}

phase('Finish')
out.finish = await A(`${CONTEXT}
You are finishing tier 4: ${T4}/{rust,python} now contain all tier-4 extensions (${spec.extensions.map(x => x.id).join(', ')}). ${RESUME}
1. Documentation parity across ALL extension modules, tier 3 included: compare every tier-3 and tier-4 extension module's doc comments between the two languages and make them state the same rules with the same text (in each language's comment syntax). Known case: the Rust tier-3 digest module comment states "Optional words are left out (e.g. INNER, OUTER, ASC, TRANSACTION)" and "Constructs are never rewritten into equivalent ones" as explicit rules, while the Python digest docstring only implies them; make both say the same thing. Change only comments/docstrings for this, and list every change in ${T4}/EXT4_AMENDMENTS.md.
2. README.md in both: the tier-3 README plus the tier-4 extension sections from EXT4_DOCS.md, identical in content between the languages except for language-specific paths and commands.
3. Workspaces follow the tier-3 conventions: same two task dirs, check.sh (same output contract; keep the lines \`trap 'rm -f "$log"' EXIT\` and \`echo "== test"\` exactly), warm Rust target/. Write ${T4}/grade.py (same CLI/JSON as ${T3}/grade.py; copy and adapt) and ${T4}/LAYOUT.md (as tier 3, plus where the tier-4 extensions live, baseline totals, warm check.sh timings, application/test line counts of both measured like tier 3, and per-extension line counts). If Rust application code is below 20k lines, say so and by how much.
4. Verify: clean check.sh in both; grade.py with an empty scripts dir = full regression; workspaces cp -a'd elsewhere work.
${FAIR} ${HYG} ${KEEP} Report status and notes.`, 'ext4-finish', 'Finish')
log(`ext4-finish: ${out.finish && out.finish.status}`)
if (!out.finish || out.finish.status === 'blocked') return out

phase('Tasks')
const TASKS = ['between_in', 'case', 'date', 'distinct', 'alter', 'release', 'bugfix']
const PORT = (t) => `${CONTEXT}
You are porting benchmark task '${t}' from tier 3 to tier 4. ${RESUME}
Read first: ${ROOT}/tasks/BRIEFS.md, ${ROOT}/tasks/${t}/ (SPEC.md, SPEC_t2.md/SPEC_t3.md if any, visible/, hidden/, hidden_t2/, hidden_t3/, NOTES*.md, ref/, inject/), ${T3}/LAYOUT.md, ${T4}/LAYOUT.md, ${T4}/EXT4_SPEC.md, ${T4}/EXT4_AMENDMENTS.md, ${T4}/EXT4_DESIGN_NOTES.md.
${t === 'bugfix' ? `For bugfix: port the injection (inject/t4-{rust,python}.patch applying cleanly to ${T4}/{rust,python}); verify the bases pass all bugfix visible+hidden scripts, the injected copies fail all of them, and check.sh on the injected copies stays green in both languages (if an extension test now catches a bug in only one language, move the injection as was done at tier 3 and document it). If a bug is observable through a tier-4 extension in a way the existing scripts miss, you may add hidden_t4 scripts for it. The references are the bases. Write NOTES_t4.md.` : `Do:
1. Create ${T4}/refs/${t}/{rust,python} as cp -a copies of ${T4}/{rust,python} and apply ref/t3-{rust,python}.patch (adapt hunks if needed).
2. Make all tier-1 sql scripts (visible+hidden), hidden_t2 cluster scripts and hidden_t3 scripts pass with ${T4}/grade.py (scripts dir with sql/ and cluster/), full regression green.
3. Before fixing anything else, grade the "tier-3-only" refs (just the t3 patch applied) on step 4's scripts once they exist, and record exactly what fails, and how: compile error, existing test failing, runtime error, or silently wrong output.
4. Find every tier-4 extension site the feature must reach (EXT4_SPEC.md's generic rules determine the correct behaviour) and implement it in both refs in the codebase's style. Author 4-8 hidden tier-4 scripts in ${ROOT}/tasks/${t}/hidden_t4/{sql,cluster}/ that exercise the feature through the tier-4 extensions (names unique across all of the task's scripts), with expected output that follows from SPEC.md + SPEC_t2/3.md + EXT_SPEC.md + EXT4_SPEC.md; if a tested behaviour is not determined by them, add the minimum to ${ROOT}/tasks/${t}/SPEC_t4.md (only if unavoidable). Both refs must pass byte-identically; each script must fail on the unmodified tier-4 base and should, where possible, fail on the tier-3-only refs.
5. Write ${ROOT}/tasks/${t}/NOTES_t4.md: a table of every site (core, tier 2, tier 3, tier 4) the change must touch per language, and for each whether a tier-3-only solution is caught by the compiler, by an existing test (which), or not at all (silent). Write ref/t4-{rust,python}.patch (diff -ruN base ref, excluding target/, __pycache__ and both task dirs; check they reproduce the refs) and a tier-4 section in ref/STATS.md. Delete the refs' Rust target/ dir when done (it can be rebuilt).`}
Use ${T4}/grade.py for all grading, one thing at a time. Do not modify ${T4}/rust, ${T4}/python or ${T4}/grade.py. ${HYG} ${KEEP} Report status and notes, including the step-3 table.`
for (const t of TASKS) {
  const r = await A(PORT(t), `port4:${t}`, 'Tasks')
  log(`port4:${t}: ${r && r.status}`)
  out[t] = r
}
return out