export const meta = {
  name: 'toydb-t5r-runs',
  description: 'Experiment 1 at tier 5 (README of tier 2, no extension docs): 6 tasks x Rust/Python x Haiku-low, Sonnet-medium, Opus-high x 3 repeats; two lanes in parallel (one Rust agent and one Python agent at a time)',
  phases: [{ title: 'Runs' }],
}
const prompt = (ws) => `You are working on a codebase in ${ws}. Your task is described in ${ws}/TASK.md.

Rules:
- Work only inside ${ws}. Do not read, list or search any file or directory outside it (you may read installed toolchain and dependency sources, e.g. under ~/.cargo/registry or the Python standard library). Do not use the web. Do not spawn subagents.
- Run ${ws}/check.sh to build and run the tests.
- When you are finished, reply with exactly two lines: \`STATUS: done\` or \`STATUS: gave-up\`, then \`SUMMARY: <one sentence>\`.`

phase('Runs')
const lane = async (runs) => {
  const res = []
  for (const r of runs) {
    const text = await agent(prompt(`/home/user/agentbench/runs/${r.run}`), {
      label: r.run, phase: 'Runs', model: r.model, effort: r.effort, agentType: 'general-purpose',
    })
    log(`${r.run}: ${(text || 'null').slice(0, 80)}`)
    res.push({ run: r.run, text })
  }
  return res
}
const lanes = await parallel([
  () => lane(args.filter(r => r.run.includes('_rs_'))),
  () => lane(args.filter(r => r.run.includes('_py_'))),
])
return lanes.filter(Boolean).flat()
