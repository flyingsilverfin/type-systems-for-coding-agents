# Type systems for coding agents

Do static types still help when the programmer is an AI agent? This repo holds an experiment, built for a
Rust meetup talk, that gives agents of different strength the same task on the same codebase in Rust and in
Python, and measures what the type system changes.

## The question

Type systems were designed for people. They catch whole classes of mistakes before the program runs, they
document interfaces where the code is, and they let a programmer hand part of the job of remembering a
large codebase to the compiler. When you change a type, the compiler lists every place that has to change
with it.

Coding agents change this trade in two directions, and it is not obvious which one wins.

- **Agents might need types more.** An agent has no lasting memory of a codebase. Every session starts
  cold, like a new contributor on their first day. It knows only the files it chose to open, so code that
  depends on a change but was never opened is invisible to it. Agents also retry cheaply, so a verifier
  whose errors say exactly what is wrong is cheap for them to use.
- **Agents might need types less.** A context window holds more code at once than a person keeps in mind,
  and an agent will read, grep and re-read without getting tired. Most of what a type error would tell it,
  it can find by searching.

Which effect dominates is an empirical question. The answer may well depend on how strong the agent is and
how large the codebase is, so the experiment varies both.

## Inspiration: query generation

This experiment was inspired by TypeDB's
[*Benchmarking LLM query generation across SQL, Cypher and TypeQL*](https://typedb.com/blog/benchmarking-llm-query-generation-across-sql-cypher-and-typeql)
(September 2026). The highlights:
- **Result.** With resources and a retry loop, Claude Sonnet 5 scored 92% on TypeQL, against 83% on SQL
  and 82% on Cypher.
- **Why.** Retries helped TypeQL most. TypeDB's schema turns semantic mistakes into type-check errors the
  model can see and fix, where a wrong SQL query usually just returns wrong rows.
- **Limit.** DeepSeek V4 Pro showed no such lead.

The post has the full results and caveats. The question it raises is whether a stricter verifier helps
agents in general application code too, with a compiler as the verifier.

## The experiment: Rust vs Python

The codebase is [toydb](https://github.com/erikgrinaker/toydb), a distributed SQL database written in Rust,
together with a structurally parallel Python port that passes the same golden tests byte for byte. Both are
cut into tiers of increasing size:

| Tier | What it adds | Size |
|---|---|---|
| t1 | SQL core, in-process | small |
| t2 | Full toydb: Raft, server, client, cluster tests | medium |
| t3 | Four extension layers: JSON output, statement digests, column statistics, export/import | larger |
| t4 | Seven more: views, FORMAT, DESCRIBE, ESTIMATE, prepared statements, LINT, change log | 12.5k lines of Rust application code, 14.9k of Python |

Each agent (Claude Haiku, Sonnet or Opus) gets one of seven feature or bugfix tasks. Its only feedback is
what a developer would have: the compiler (in Rust), one visible test and the project's `./check.sh`.
Hidden tests decide whether the run passes.

## Findings

**In short: types tell you *where* a change must go, not *what* it must do.**

### 1. On small codebases, the language makes no difference

At tiers 1 and 2, agents in both languages pass almost everything. Rust costs somewhat more time and tokens.

### 2. Types reach code the agent never looked at

A *transplant* test applies each agent's finished solution, unchanged, to the next tier up. That tier
contains code that depends on the change but did not exist in the agent's workspace.

In Python, solutions often passed every test the agent could run and then failed at code it had never
seen: hand-written serialization at tier 2, JSON writers and import parsing at tier 3. In Rust, a
non-exhaustive `match` or a derived impl either flagged those sites at compile time or handled them
automatically.

- Tier 1 → 2: 2 of 5 Python solutions failed silently, against 0 of 10 in Rust.
- Tier 2 → 3: the Rust compiler flagged 10 of 12 solutions. Python's own checks caught only 6 of 12.

### 3. The compiler rescues the weakest model

Experiment 1 ran tier 3 with the README's description of the extension layers removed, so the affected
code could only be found by reading the code itself. 6 feature tasks × 3 repeats × 3 models, in each
language (108 runs):

| Model | Rust pass | Python pass | Python missed a site the Rust compiler flags |
|---|---|---|---|
| Haiku · low effort | 10/18 | 5/18 | 6/18 (Rust 0/18, p ≈ 0.02) |
| Sonnet · medium effort | 11/18 | 12/18 | 0/18 |
| Opus · high effort | 18/18 | 13/18 | 0/18 |

Haiku, writing Python, missed sites the Rust compiler would have pointed at. Sonnet and Opus found them on
their own in both languages. Their remaining failures were in content no type system checks: digest text,
parser expansions, statistics upkeep. Part of Opus's 18 vs 13 comes from a documentation difference. The
Rust digest module stated a rule ("optional words are left out") that the Python docstring only implied
(see `analysis/notes.json`). Tier 4 uses identical doc comments in both languages.

Exhaustive matching also had a cost. Weaker Rust agents sometimes filled a match arm the compiler forced
them to write with the wrong content. In the same places, Python's generic fallback was right
automatically.

### 4. At larger scale, the bottleneck moves to the rules

Tier 4 adds seven more extension layers and keeps every doc comment identical across the two languages.
The README again has no extension docs. 108 runs, graded after a post-run audit of the hidden tests (the
audit is described in the report; its log is held back with the tests):

| Model | Rust pass | Python pass | Python crashed at an unhandled variant |
|---|---|---|---|
| Haiku · low effort | 2/18 | 1/18 | 0/18 |
| Sonnet · medium effort | 3/18 | 2/18 | 1/18 |
| Opus · high effort | 8/18 | 4/18 (p ≈ 0.29) | 0/18 |

Python agents of every strength now reach the sites the Rust compiler names, because grepping for a
sibling variant turns up the same list. Rust still leads at every level, but none of the gaps is
statistically significant. Nearly every failure, in both languages, is at a site no type checks:

- lint rules that also have to apply through the BETWEEN/IN and CASE expansions (no run of 36 got this
  right);
- how DESCRIBE types date arithmetic;
- the ALTER check for views that depend on a table;
- constructs that get rewritten in the parser.

## What this means so far

The compiler works as a search tool. It finds every place a change has to reach, quickly and with
certainty. That matters most when the agent can't do the search itself: a weak model, or code it has no
reason to open. A strong agent with grep reconstructs the same list at this scale, and then fails on the
same thing in both languages, the rules that no type encodes.

This links back to the query benchmark. A verifier helps only as far as what it can check. TypeDB's schema
checks the meaning of a query, so a wrong query fails visibly. Rust's types here check *which cases
exist*, not *what each case must do*. They catch a forgotten variant, but not a lint rule that silently
skips a CASE expansion.

Two questions are still open. The first is whether finding the sites gets hard again once a codebase is too
large to grep through comfortably. A tier 5 with more than 20k lines of Rust is being built to test this.
The second is whether a codebase that puts more of its rules into types would let the compiler help with the
*what* as well.

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
