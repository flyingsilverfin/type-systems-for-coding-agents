# Type systems for coding agents

This repository was built to test whether type systems are still useful in the age of agentic
programming. It accompanies a Rust meetup talk.

Type systems were designed for human developers. They catch whole classes of mistakes before the program
runs, they document interfaces where the code is, and they take some of the load off a developer's memory.
Instead of tracking every consequence of a change in their head, a developer can change a type and let the
compiler list every place that has to follow. Part of the job of keeping the program correct is handed to
an external verifier.

## Two hypotheses

Coding agents change that trade, and it isn't obvious in which direction.

**Agents might need types more than people do.** An agent has no lasting memory of a codebase. Every
session starts cold, like a new contributor on their first day, and the agent knows only the files it
chose to open. Code that depends on a change but was never opened is invisible to it. Agents also retry
cheaply, so a verifier that says exactly what is wrong is cheap for them to use.

**Agents might need types less.** A context window holds more code at once than a person keeps in mind,
and an agent will read, grep and re-read without getting tired. Most of what a type error would tell it,
it can find by searching.

Which effect wins is an empirical question, and the answer probably depends on how strong the agent is
and how large the codebase is. So we varied both.

## Inspiration: query generation

The idea came from TypeDB's
[*Benchmarking LLM query generation across SQL, Cypher and TypeQL*](https://typedb.com/blog/benchmarking-llm-query-generation-across-sql-cypher-and-typeql).
With in-context resources and a retry loop, Claude Sonnet 5 reached 92% accuracy on TypeQL, against 83%
for SQL and 82% for Cypher. Retries helped TypeQL the most, because TypeDB's schema turns semantic
mistakes into type-check errors the model can see and fix, where a wrong SQL query usually just returns
wrong rows. DeepSeek V4 Pro didn't show the same lead, and the post covers the full results and caveats.
The question it left us with was whether a stricter verifier helps agents with general application code
too, where the verifier is a compiler.

## The experiment: Rust vs. Python

To test this, we compared Rust (strong static types) with Python (dynamic types) on the same codebase.
That codebase is [toydb](https://github.com/erikgrinaker/toydb), a distributed SQL database written in
Rust, together with a structurally parallel Python port that passes the same golden tests byte for byte.
Both are cut into four tiers of increasing size:

| Tier | What it adds | Size (approx. lines of application code) |
|---|---|---|
| 1 | The SQL core, in-process | Rust 6k, Python 7.6k |
| 2 | The full database: Raft, server, client, cluster tests | Rust 8.5k, Python 11.3k |
| 3 | Four extension layers: JSON output, statement digests, column statistics, export/import | Rust 10.1k, Python 12.7k |
| 4 | Seven more: views, FORMAT, DESCRIBE, ESTIMATE, prepared statements, LINT, change log | Rust 12.5k, Python 14.9k |

Line counts are non-blank, non-comment lines, excluding tests (see each tier's `LAYOUT.md`).

AI agents of varying strength (Claude Haiku, Sonnet and Opus) were each given an identical task. Their
only feedback was what a developer would have: compiler errors (in Rust), one visible test and a local
`./check.sh` script. Hidden tests decided whether a run passed.

## Key findings

**Types tell you *where* a change must go, not *what* it must do.**

**Small codebases show no difference.** At tiers 1 and 2, agents in both languages pass almost
everything. Rust costs somewhat more time and tokens, but Python's lack of types costs nothing in success
rate.

**Types reach code the agent never saw.** In a transplant test, we applied each agent's finished solution
unchanged to the next tier up, which contains code that depends on the change but wasn't in the agent's
workspace. In Python, solutions often passed every test the agent could run and then failed at code it
had never seen: hand-written serialization at tier 2, JSON writers and import parsing at tier 3. From
tier 1 to 2, 2 of 5 Python solutions failed silently, against none of 10 in Rust. In Rust, a
non-exhaustive `match` or a derived impl flagged those sites or handled them automatically: from tier 2
to 3 the compiler flagged 10 of 12 solutions, while Python's own checks caught 6 of 12.

**The compiler rescues weak models.** At tier 3 we removed the README's description of the extension
layers, so the affected code could only be found from the code itself, and ran each task 3 times per
model (108 runs):

| Model | Rust pass | Python pass | Python missed a site the Rust compiler flags |
|---|---|---|---|
| Haiku · low effort | 10/18 | 5/18 | 6/18 (Rust 0/18, p ≈ 0.02) |
| Sonnet · medium effort | 11/18 | 12/18 | 0/18 |
| Opus · high effort | 18/18 | 13/18 | 0/18 |

Haiku, writing Python, missed sites the compiler would have pointed it to. Sonnet and Opus found that code
on their own in both languages, and their remaining failures were in things no type system checks: digest
text, parser expansions, statistics upkeep. Part of Opus's 18 vs 13 comes from a documentation
difference: the Rust digest module stated a rule ("optional words are left out") that the Python
docstring only implied. Exhaustive matching cut the other way too. Weaker Rust agents sometimes filled a
match arm the compiler forced on them with the wrong content, in places where Python's generic fallback
was right automatically.

**At scale, the bottleneck shifts to semantics.** Tier 4 adds seven more extension layers and uses
identical doc comments in both languages. With the extension docs again left out of the README (108
runs, graded after a post-run audit of the hidden tests):

| Model | Rust pass | Python pass | Python crashed at an unhandled variant |
|---|---|---|---|
| Haiku · low effort | 2/18 | 1/18 | 0/18 |
| Sonnet · medium effort | 3/18 | 2/18 | 1/18 |
| Opus · high effort | 8/18 | 4/18 (p ≈ 0.29) | 0/18 |

At this size, Python agents of every strength reached the sites the Rust compiler names, simply by
grepping for a sibling variant. Rust still leads at every level, but none of the gaps is statistically
significant. Failures in both languages converged on rules no type system checks: lint rules that must
also apply through the BETWEEN/IN and CASE expansions (no run of 36 got this right), how DESCRIBE types
date arithmetic, the ALTER check for views that depend on a table, and constructs rewritten in the parser.

## What this means so far

Both hypotheses hold, at different points. For a weak model, or for code the agent has no reason to
open, the compiler is a search tool that does what the agent can't, and types matter more. For a strong
agent with grep, on a codebase it can search through, the compiler finds nothing the agent wouldn't have
found, and types matter less. Neither language helped with the rules themselves.

That connects back to the query benchmark: a verifier helps only as far as what it checks. TypeDB's
schema checks what a query means. Rust's types here check which cases exist, not what each case must do,
so they catch a forgotten variant but not a lint rule that silently skips a CASE expansion.

Two questions are still open. Does finding the sites get hard again once the codebase is too big to grep
through comfortably? A tier 5, with more than 20k lines of Rust, is being built to find out. And would
a codebase that puts more of its rules into types let the compiler help with the *what* as well?

## Method in brief

- **Isolation.** Every run gets a fresh workspace and may read only that. Hidden tests, reference
  solutions and other runs are locked (`chmod 000`) while agents run. Transcripts are audited for any reads
  outside the workspace.
- **Oracle.** For every task, an author writes the spec and the hidden tests. Two independent
  implementations, one per language, must agree byte for byte, and a reconciler settles every disagreement
  against the spec.
- **Fairness.** Each language's code must be idiomatic: exhaustive matching where a careful Rust engineer
  would use it, and the Python port's own conventions. Nothing is added only to make a task pass or fail.
- **Metrics.**
  - Correctness on the hidden tests.
  - Escapes: the agent's own last `check.sh` was green, but grading failed.
  - Output and input tokens, and turns.
  - Wall time, split into compile, test and agent time.

The full write-up, with per-run tables, is generated by `analysis/render.py` into `artifact/index.html`.
Open that file in a browser.

## Layout

| Path | What |
|---|---|
| `toydb/rust`, `toydb/python` | [toydb](https://github.com/erikgrinaker/toydb) (Rust, upstream commit 2d7cd32) and a faithful, structurally parallel Python port that passes the same golden scripts byte for byte (`toydb/PORTING.md`) |
| `toydb/tiers/t1..t4/{rust,python}` | The benchmark codebases. Specs: tier 3 in `toydb/tiers/t3/EXT_SPEC.md`, tier 4 in `toydb/tiers/t4/EXT4_SPEC.md` with module docs in `EXT4_DOCS.md`. Each tier has `check.sh`, `grade.py` and `LAYOUT.md` |
| `toydb/tasks/<task>/` | The 7 tasks (between_in, case, date, distinct, alter, release, bugfix): `SPEC.md` (plus tier addenda) and the visible golden script |
| `bench/` | The harness: workspace builders (`make_run*.py`), graders, run lists, the transplant scripts, the usage guard, and `bench/workflows/` (the scripts that orchestrated the build and ran the agents) |
| `analysis/` | `transcripts.py` (tokens, turns, timings and the isolation audit, from agent transcripts), `render.py` (the report), `notes.json` (findings) |
| `results/`, `meta/`, `logs/` | Per-run grades (JSON), workspace metadata, and per-`check.sh` timing logs |

## Reproducing

The harness was run from Claude Code workflows (`bench/workflows/`), with paths under
`/home/user/agentbench`. To grade a workspace, run
`python3 toydb/tiers/<tier>/grade.py <lang> <ws> <scripts-dir>`. The hidden tests and reference solutions
are not in this repo yet (see `HELD_BACK.md`), so end-to-end grading needs them.

## Licence

Apache-2.0 (`LICENSE`). toydb is Copyright Erik Grinaker, Apache-2.0. See `NOTICE` for what this repo
changed and added.
