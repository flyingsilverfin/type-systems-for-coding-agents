# Held back until after the talk

The repo is public, so anything that reveals what a correct answer looks like is withheld for now.
Otherwise future models could train on the answers and reruns would no longer be clean.

- `toydb/tasks/*/hidden*/`: hidden golden scripts (tier 1 SQL, tier 2 cluster, tier 3 extension).
- `toydb/tasks/*/ref/`, `toydb/tiers/*/refs/`: reference solutions (patches and full trees).
- `toydb/tasks/*/inject/`: the bugfix task's bug injections.
- `toydb/tasks/*/NOTES*.md`, `AMENDMENTS.md`, `toydb/tasks/BRIEFS.md`, `toydb/tiers/t3/EXT_DESIGN_NOTES.md`,
  `toydb/tiers/t3/EXT_AMENDMENTS.md`: experimenter notes that describe the hidden tests and solution sites.
- `results/*.log`: grading logs, which print expected hidden output in their diffs.
- The agents' run workspaces and diffs: passing runs are working solutions.
- Tier 0: `hidden/` datasets, `build_v2/data_hidden/`, the oracles (`bench/oracle*.py`) and data generators.
- `toydb/tiers/t4/`: tier 4 is still being built.
