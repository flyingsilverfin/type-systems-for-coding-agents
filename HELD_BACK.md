# Held back until after the talk

The repo is public, so anything that reveals what a correct answer looks like is withheld for now.
Otherwise future models could train on the answers and reruns would no longer be clean.

- `toydb/tasks/*/hidden*/`: hidden golden scripts (tier 1 SQL, tier 2 cluster, tier 3, tier 4 and tier 5 extension), and the pre-audit copies of the tier-4 and tier-5 ones (`prerun_backup/`, `toydb/tiers/t5/prerun_audit_backup/`).
- `toydb/tasks/*/ref/`, `toydb/tiers/*/refs/`: reference solutions (patches and full trees).
- `toydb/tasks/*/inject/`: the bugfix task's bug injections.
- `toydb/tasks/*/NOTES*.md`, `AMENDMENTS.md`, `toydb/tasks/BRIEFS.md`, `toydb/tiers/t3/EXT_DESIGN_NOTES.md`,
  `toydb/tiers/t3/EXT_AMENDMENTS.md`, `toydb/tiers/t4/EXT4_DESIGN_NOTES.md`, `toydb/tiers/t4/EXT4_AMENDMENTS.md`,
  `toydb/tiers/t4/POSTRUN_AMENDMENTS.md` (the post-run test audit), `toydb/tiers/t5/EXT5_DESIGN_NOTES.md`, `toydb/tiers/t5/EXT5_AMENDMENTS.md`, `toydb/tiers/t5/PRERUN_AUDIT.md`, `toydb/tasks/*/sites_t5.json`, `toydb/tasks/bugfix/probe_t4/`, `probe_t5/`, and the tier-5 post-run reviews and per-site analysis: experimenter notes that describe the hidden tests and solution sites.
- `results/*.log`: grading logs, which print expected hidden output in their diffs.
- The agents' run workspaces and diffs: passing runs are working solutions.
- `results/t4r_prefix_backup/`: the tier-4 grades before the post-run audit (the raw numbers are in the report).
