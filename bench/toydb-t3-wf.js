export const meta = {
  name: 'toydb-t3-runs',
  description: 'Tier-3 toydb benchmark: 7 tasks x Rust/Python x haiku-low/opus-high, then mechanical merge-conflict resolution for the t2->t3 transplants; one agent at a time',
  phases: [{ title: 'Runs' }, { title: 'Resolve' }],
}
const prompt = (ws) => `You are working on a codebase in ${ws}. Your task is described in ${ws}/TASK.md.

Rules:
- Work only inside ${ws}. Do not read, list or search any file or directory outside it (you may read installed toolchain and dependency sources, e.g. under ~/.cargo/registry or the Python standard library). Do not use the web. Do not spawn subagents.
- Run ${ws}/check.sh to build and run the tests.
- When you are finished, reply with exactly two lines: \`STATUS: done\` or \`STATUS: gave-up\`, then \`SUMMARY: <one sentence>\`.`

const resolve = (ws, files) => `The directory ${ws} is a source tree where an automated three-way merge left conflict markers (\`<<<<<<< t3\`, \`=======\`, \`>>>>>>> t2run\`) in these files:
${files.map(f => '- ' + f).join('\n')}

Side \`t3\` is a newer base version of the code; side \`t2run\` is a change someone made against an older base. Your only job is to resolve each conflict region mechanically so that BOTH sides' content is kept, as a careful merge tool operator would:
- Keep every line each side adds (e.g. both imports, both enum variants, both match arms, both keywords in sorted position).
- Where both sides add an item with the same serialization/variant number or tag, keep the t3 item's number and give the t2run item the next free number, updating that item's serialize and deserialize sides consistently. Keep each side's code otherwise as written.
- Where both sides change the same count or constant in a test (e.g. a keyword count), combine both changes (base + both increments).
- Do not edit anything outside the conflict regions, except the renumbering above. Do not add, complete, or fix any feature code; do not make the change work with the newer base beyond resolving the markers. It does not matter whether the result passes tests.
- Do not build, compile, type-check or run any tests or programs. Read only files inside ${ws}. Do not use the web or spawn subagents.

When finished, reply with one line per file: \`<file>: <n> regions resolved; <short note of any renumbering or count change>\`.`

const out = []
phase('Runs')
for (const r of args.runs) {
  const text = await agent(prompt(`/home/user/agentbench/runs/${r.run}`), {
    label: r.run, phase: 'Runs', model: r.model, effort: r.effort, agentType: 'general-purpose',
  })
  log(`${r.run}: ${(text || 'null').slice(0, 80)}`)
  out.push({ run: r.run, text })
}
phase('Resolve')
for (const c of args.conflicts) {
  const text = await agent(resolve(`/home/user/agentbench/runs/${c.run}`, c.files), {
    label: `resolve:${c.run}`, phase: 'Resolve', model: 'claude-opus-5-5', effort: 'medium', agentType: 'general-purpose',
  })
  out.push({ run: c.run, resolve: text })
}
return out
