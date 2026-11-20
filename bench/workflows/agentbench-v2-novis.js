export const meta = {
  name: 'agentbench-v2-novis',
  description: 'Run v2 tasks with no visible tests (haiku-low, Rust vs Python) plus one rerun',
  phases: [{ title: 'Haiku low, no visible tests' }],
}
const prompt = (ws) => `You are working on a small codebase in ${ws}. Your task is described in ${ws}/TASK.md.

Rules:
- Work only inside ${ws}. Do not read, list or search any file or directory outside it; everything you need (including the TypeDB driver source and docs) is inside the workspace. Do not use the web. Do not spawn subagents.
- Run ${ws}/check.sh to build and run the visible tests.
- When you are finished, reply with exactly two lines: \`STATUS: done\` or \`STATUS: gave-up\`, then \`SUMMARY: <one sentence>\`.`
const out = await parallel(args.map(run => () =>
  agent(prompt(`/home/user/agentbench/runs/${run}`), {
    label: run, phase: 'Haiku low, no visible tests', model: 'haiku', effort: 'low', agentType: 'general-purpose',
  }).then(text => ({ run, text }))))
return out.filter(Boolean)