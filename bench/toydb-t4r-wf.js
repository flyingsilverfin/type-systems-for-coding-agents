export const meta = {
  name: 'toydb-t4r-runs',
  description: 'Experiment 1 at tier 4 (README of tier 2, no extension docs): 6 tasks x Rust/Python x Haiku-low, Sonnet-medium, Opus-high x 3 repeats, one agent at a time',
  phases: [{ title: 'Runs' }],
}
const prompt = (ws) => `You are working on a codebase in ${ws}. Your task is described in ${ws}/TASK.md.

Rules:
- Work only inside ${ws}. Do not read, list or search any file or directory outside it (you may read installed toolchain and dependency sources, e.g. under ~/.cargo/registry or the Python standard library). Do not use the web. Do not spawn subagents.
- Run ${ws}/check.sh to build and run the tests.
- When you are finished, reply with exactly two lines: \`STATUS: done\` or \`STATUS: gave-up\`, then \`SUMMARY: <one sentence>\`.`

const out = []
phase('Runs')
for (const r of args) {
  const text = await agent(prompt(`/home/user/agentbench/runs/${r.run}`), {
    label: r.run, phase: 'Runs', model: r.model, effort: r.effort, agentType: 'general-purpose',
  })
  log(`${r.run}: ${(text || 'null').slice(0, 80)}`)
  out.push({ run: r.run, text })
}
return out
