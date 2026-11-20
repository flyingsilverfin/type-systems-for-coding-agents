export const meta = {
  name: 'toydb-t2n-runs',
  description: 'Tier-2 no-hint ablation: Haiku, case/date/distinct/release, Rust/Python, 2 reps, one agent at a time',
  phases: [{ title: 'Runs' }],
}
const prompt = (ws) => `You are working on a codebase in ${ws}. Your task is described in ${ws}/TASK.md.

Rules:
- Work only inside ${ws}. Do not read, list or search any file or directory outside it (you may read installed toolchain and dependency sources, e.g. under ~/.cargo/registry or the Python standard library). Do not use the web. Do not spawn subagents.
- Run ${ws}/check.sh to build and run the tests.
- When you are finished, reply with exactly two lines: \`STATUS: done\` or \`STATUS: gave-up\`, then \`SUMMARY: <one sentence>\`.`
const out = []
for (const r of args) {
  const text = await agent(prompt(`/home/user/agentbench/runs/${r.run}`), {
    label: r.run, phase: 'Runs', model: r.model, effort: r.effort, agentType: 'general-purpose',
  })
  log(`${r.run}: ${(text || 'null').slice(0, 80)}`)
  out.push({ run: r.run, text })
}
return out
