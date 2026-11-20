export const meta = {
  name: 'agentbench-v3-spread',
  description: 'Run v3 per-site-judgment tasks (Rust vs Python) with haiku-low and opus-high subagents',
  phases: [{ title: 'Haiku low' }, { title: 'Opus high' }],
}
const prompt = (ws) => `You are working on a small codebase in ${ws}. Your task is described in ${ws}/TASK.md.

Rules:
- Work only inside ${ws}. Do not read, list or search any file or directory outside it; everything you need (including the TypeDB driver source and docs) is inside the workspace. Do not use the web. Do not spawn subagents.
- Run ${ws}/check.sh to build and run the visible tests.
- When you are finished, reply with exactly two lines: \`STATUS: done\` or \`STATUS: gave-up\`, then \`SUMMARY: <one sentence>\`.`
const out = await parallel(args.map(r => () =>
  agent(prompt(`/home/user/agentbench/runs/${r.run}`), {
    label: r.run, phase: r.model === 'haiku' ? 'Haiku low' : 'Opus high',
    model: r.model, effort: r.effort, agentType: 'general-purpose',
  }).then(text => ({ run: r.run, text }))))
return out.filter(Boolean)