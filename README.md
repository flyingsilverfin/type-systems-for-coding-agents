# Type Systems for Coding Agents

This repository was built to test whether type systems remain fundamentally useful in the age of agentic
programming. 

Historically, type systems were designed as guardrails for human developers. They catch entire classes of
bugs before execution, co-locate interface documentation with the code, and offload cognitive overhead
from human working memory. Rather than mentally tracing every downstream consequence of a change, a
developer modifies a type and delegates the search to an external verifier: the compiler.

## Two Competing Hypotheses

AI coding agents alter this trade-off, and it is not obvious which way the balance tips.

**Agents might need types more than humans.** An agent has no persistent memory of a codebase. Every
session starts cold—like a new contributor on day one—and the agent only knows the files it chooses to
open. Downstream code that consumes a change but was never opened is effectively invisible. Because agents
retry cheaply, a strict external verifier that pinpoints exact errors should directly amplify their
ability to self-correct.

**Agents might need types less than humans.** A context window holds far more code at once than human
working memory, and an agent can read, grep, and re-read a repository tirelessly. Much of what a compiler
error flags, an agent can simply search for.

Which effect dominates is an empirical question—and the answer likely depends on both agent capability and
codebase scale. We built this benchmark to vary both.

## Prior Evidence: The Query Generation Benchmark

The motivation for this experiment came from TypeDB's
[*Benchmarking LLM Query Generation Across SQL, Cypher, and TypeQL*](https://typedb.com/blog/benchmarking-llm-query-generation-across-sql-cypher-and-typeql).

With in-context resources and an iterative retry loop, Claude Sonnet 5 reached 92% accuracy on TypeQL,
compared to 83% on SQL and 82% on Cypher. Retries benefited TypeQL the most because TypeDB's schema acts
as a semantic verifier: it turns logical mistakes into explicit type-check errors the model can consume
and fix, whereas a flawed SQL query silently returns wrong rows. (DeepSeek V4 Pro did not exhibit the same
lead; the blog post details the full results and caveats.)

That result raised a broader question: does a stricter external verifier yield the same gains in
general-purpose application code, where the verifier is a compiler?

## The Experiment: Rust vs. Python

To test this, we compared Rust (strong static types) against Python (dynamic types) on the same codebase:
[toydb](https://github.com/erikgrinaker/toydb), a distributed SQL database in Rust, paired with a
structurally parallel Python port that passes the same golden tests byte for byte. Both codebases are
split into four tiers of increasing complexity:

| Tier | What it adds | Size (approx. LOC, excl. comments/blanks/tests) |
|---|---|---|
| 1 | SQL core, in-process | Rust 6k · Python 7.6k |
| 2 | Full database: Raft, server, client, cluster tests | Rust 8.5k · Python 11.3k |
| 3 | Four extension layers: JSON output, statement digests, column statistics, export/import | Rust 10.1k · Python 12.7k |
| 4 | Seven more layers: views, FORMAT, DESCRIBE, ESTIMATE, prepared statements, LINT, change log | Rust 12.5k · Python 14.9k |

Agents of varying strength (Claude Haiku, Sonnet, and Opus) were given identical feature and bugfix tasks.
Their only feedback was a standard developer loop: compiler errors (in Rust), one visible test, and a local
`./check.sh` script. Held-back tests determined final correctness.

## Key Findings

**Types tell you *where* a change must go, not *what* it must do.**

### 1. Small codebases show no difference (Tiers 1–2)

At Tiers 1 and 2, agents in both languages pass almost every task. Rust incurs slightly more wall time and
token usage, while Python's lack of static types carries no penalty in success rate.

### 2. Types reach code the agent never saw (Transplant Tests)

In a transplant test, we applied each agent's finished solution unchanged to the next tier up—introducing
downstream code that depended on the change but was absent from the agent's workspace.

In Python, solutions frequently passed every local check and then failed silently at unseen sites
(hand-written serialization at Tier 2; JSON writers and import parsing at Tier 3). In Rust, non-exhaustive
match arms and derived traits either handled those sites automatically or flagged them immediately:

- **Tier 1 → 2:** Python suffered 2/5 silent failures; Rust had 0/10.
- **Tier 2 → 3:** Rust's compiler flagged 10/12 solutions; Python's own checks caught only 6/12.

### 3. The compiler rescues weak models (Tier 3)

At Tier 3, we stripped the extension-layer descriptions from the README so affected code had to be
discovered from the codebase itself (6 tasks × 3 repeats × 3 models × 2 languages = 108 runs):

| Model | Rust pass | Python pass | Python missed a site the Rust compiler flags |
|---|---|---|---|
| Haiku · low effort | 10/18 | 5/18 | 6/18 (Rust 0/18, p ≈ 0.02) |
| Sonnet · medium effort | 11/18 | 12/18 | 0/18 |
| Opus · high effort | 18/18 | 13/18 | 0/18 |

The Rust compiler actively rescued Haiku, pointing it to sites it missed in Python. Sonnet and Opus located
those sites on their own in both languages; their remaining failures were semantic details no type system
checks (digest formatting, parser expansions, statistics upkeep).

Two nuances stood out:

- **Documentation confound:** Part of Opus's 18 vs. 13 gap stemmed from a doc discrepancy—the Rust digest
  module explicitly stated a rule ("optional words are left out") that the Python docstring only implied.
- **The cost of exhaustive matching:** Forced match arms cut both ways. Weaker Rust agents occasionally
  filled a compiler-mandated arm with wrong logic in places where Python's generic fallback was
  automatically correct.

### 4. At scale, the bottleneck shifts to semantics (Tier 4)

Tier 4 adds seven more extension layers and enforces identical doc comments across both languages (108
runs, graded after a post-run audit of the hidden tests):

| Model | Rust pass | Python pass | Python crashed at an unhandled variant |
|---|---|---|---|
| Haiku · low effort | 2/18 | 1/18 | 0/18 |
| Sonnet · medium effort | 3/18 | 2/18 | 1/18 |
| Opus · high effort | 8/18 | 4/18 (p ≈ 0.29) | 0/18 |

Even at ~15k lines, Python agents of every strength reached the sites the Rust compiler named simply by
grepping for sibling variants. While Rust led for every model, none of the gaps were statistically
significant. Failures in both languages converged on semantic rules outside the type system's reach:

- LINT rules that must propagate through BETWEEN/IN and CASE expansions (0/36 runs got this right);
- DESCRIBE typing for date arithmetic;
- ALTER checks for views depending on a table;
- constructs rewritten directly in the parser.

## What This Means So Far

Both hypotheses hold—just at different boundaries.

For a weak model, or for downstream code an agent has no reason to open, the compiler acts as a
deterministic search heuristic that does what the agent cannot. For a strong model equipped with grep
operating in a searchable codebase, the compiler rarely finds a site the agent wouldn't have found itself.
In neither case did static types solve the underlying business logic.

This ties directly back to the TypeDB query benchmark: a verifier only helps as far as what it checks.
TypeDB's schema checks what a query means, catching semantic errors at check time. Rust's type system here
checks which cases exist, not what each case must do—it catches a forgotten enum variant, not a lint rule
that silently skips a CASE expansion.

Two open questions remain:

1. **Does site discovery break down again at larger scale?** A Tier 5 (20k+ lines of Rust) is currently in
   development to test whether grepping loses its edge once a codebase grows too large to navigate
   comfortably.
2. **Can encoding more invariants into types bridge the gap?** If a codebase pushes more of its semantic
   rules into the type system, can the compiler start helping with the *what* as well as the *where*?

## Method in Brief

- **Isolation:** Every run receives a fresh workspace and may read only that directory. Hidden tests,
  reference solutions, and other runs are locked (`chmod 000`) during execution, and transcripts are
  audited for out-of-workspace reads.
- **Oracle:** For each task, an author writes the spec and hidden tests. Independent Rust and Python
  reference implementations must agree byte for byte, with a reconciler resolving any divergence against
  the spec.
- **Fairness:** Both codebases are written idiomatically—exhaustive matching where a careful Rust engineer
  would use it, and standard conventions for the Python port. Nothing is engineered artificially to induce
  passes or failures.
- **Metrics:** Hidden-test correctness, escapes (runs where the agent's final `./check.sh` was green but
  grading failed), input/output tokens, turns, and wall time split across compile, test, and agent time.

The full report—including per-run tables—is generated by `analysis/render.py` into `artifact/index.html`.
Open that file in a browser to explore the data.

## Repository Layout

| Path | Description |
|---|---|
| `toydb/rust`, `toydb/python` | Upstream [toydb](https://github.com/erikgrinaker/toydb) (commit 2d7cd32) and its structurally parallel Python port (`toydb/PORTING.md`) |
| `toydb/tiers/t1..t4/{rust,python}` | The four benchmark tiers. Specs live in `toydb/tiers/t3/EXT_SPEC.md` and `toydb/tiers/t4/EXT4_SPEC.md` (module docs in `EXT4_DOCS.md`). Each tier contains `check.sh`, `grade.py`, and `LAYOUT.md` |
| `toydb/tasks/<task>/` | The 7 tasks (between_in, case, date, distinct, alter, release, bugfix): `SPEC.md` (+ tier addenda) and the visible golden script |
| `bench/` | Evaluation harness: workspace builders (`make_run*.py`), graders, run lists, transplant scripts, usage guard, and `bench/workflows/` |
| `analysis/` | `transcripts.py` (tokens, turns, timings, and isolation audit), `render.py` (HTML report generator), and `notes.json` |
| `results/`, `meta/`, `logs/` | Per-run JSON grades, workspace metadata, and per-`check.sh` timing logs |

## Reproducing

The harness was executed via Claude Code workflows (`bench/workflows/`) with paths under
`/home/user/agentbench`. To grade a workspace directly:

```bash
python3 toydb/tiers/<tier>/grade.py <lang> <ws> <scripts-dir>
```

Hidden tests and reference solutions are not yet published in this repository (see `HELD_BACK.md`), so
end-to-end grading requires them.

## Licence

Apache-2.0 (`LICENSE`). toydb is Copyright Erik Grinaker, Apache-2.0; see `NOTICE` for modifications and
additions made in this repository.
