# Type Systems for Coding Agents

This repository was built to test whether type systems remain fundamentally useful in the age of agentic
programming. 

Type systems do two jobs for human developers.

1. **Guardrails.** They catch entire classes of bugs before execution, co-locate interface documentation
   with the code, and offload cognitive overhead from human working memory. Rather than mentally tracing every
   downstream consequence of a change, a developer modifies a type and delegates the search to an external
   verifier: the compiler.
2. **Units of abstraction.** Developers also use types (structs, enums, classes, traits) because they are
   close to how we think about a problem. We use types to model the problem, not just to guard the code, and a
   well-chosen type makes the software easier to reason about before anything is checked.

**This project tests the first job.** It measures correctness and maintainability: agents change an existing
codebase, and we count the mistakes the type system catches, or fails to catch. We don't yet know how to test the
second job, whether thinking in types makes an agent better at *writing* the code in the first place. That
question is open to discussion and research (see
[What this does not test](#what-this-does-not-test-types-as-a-way-to-think)).

## Two Competing Hypotheses

AI coding agents alter the guardrail trade-off, and it is not obvious which way the balance tips. Both
hypotheses below are about types as guardrails.

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
and fix, whereas a flawed SQL query silently returns wrong rows.

That result raised a broader question: does a stricter external verifier yield the same gains in
general-purpose application code, where the verifier is a compiler?

## The Experiment: Rust vs. Python

To test this, we compared Rust (strong static types) against Python (dynamic types) on the same codebase:
[toydb](https://github.com/erikgrinaker/toydb), a distributed SQL database in Rust, paired with a
structurally parallel Python port that passes the same golden tests byte for byte. Both codebases are
split into five tiers of increasing complexity:

| Tier | What it adds | Size (approx. LOC, excl. comments/blanks/tests) |
|---|---|---|
| 1 | SQL core, in-process | Rust 6k · Python 7.6k |
| 2 | Full database: Raft, server, client, cluster tests | Rust 8.5k · Python 11.3k |
| 3 | Four extension layers: JSON output, statement digests, column statistics, export/import | Rust 10.1k · Python 12.7k |
| 4 | Seven more layers: views, FORMAT, DESCRIBE, ESTIMATE, prepared statements, LINT, change log | Rust 12.5k · Python 14.9k |
| 5 | Nine more layers: users and privileges, audit log, CHECK constraints, row-level security, PostgreSQL wire protocol, backups, system tables, materialized views, query profiles | Rust 19.2k · Python 20.7k |

Agents of varying strength (Claude Haiku, Sonnet, and Opus) were given identical feature and bugfix tasks.
Their only feedback was a standard developer loop: compiler errors (in Rust), one visible test, and a local
`./check.sh` script. Held-back tests determined final correctness.

## Key Findings

**Types tell you *where* a change must go, not *what* it must do.**

### 1. Small codebases show no difference (Tiers 1–2)

At Tiers 1 and 2, agents in both languages pass almost every task (27/28 and 28/28). Python's lack of static
types carries no penalty in success rate, and Rust costs little extra: about 12% more output tokens for Haiku
at Tier 1, and slightly fewer than Python at Tier 2.

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

### 5. At about 20k lines, the same split (Tier 5)

Tier 5 adds nine more layers, about 19k lines of Rust in all. They were designed so that once an agent finds a
site, what has to go there follows mechanically from the module's documentation. That isolates the question of
whether *finding* the sites gets hard again at this size. As before, 108 runs were graded, and a post-run review
found no test faults:

| Model | Rust pass | Python pass | Missed a site the Rust compiler flags (Rust · Python) |
|---|---|---|---|
| Haiku · low effort | 1/18 | 0/18 | 0/15 · 6/15 (p ≈ 0.02) |
| Sonnet · medium effort | 4/18 | 3/18 | 0/15 · 2/15 |
| Opus · high effort | 10/18 | 4/18 (p ≈ 0.09) | 0/15 · 0/15 |

The site counts come from a per-site analysis of every run's diff. They exclude BETWEEN/IN: there, 15 of 18 runs
rewrote the construct in the parser, so no new type existed for the compiler to check.

For Sonnet and Opus, finding the sites did not get hard again. No Opus run in either language missed a site the
compiler names. For Haiku it looks like Tier 3: the compiler took every Rust Haiku run to every flagged site, while
Python Haiku missed them in 6 of 15 runs. Being taken there didn't make Haiku pass, though: it filled those sites
with wrong content.

Opus's Rust lead is the largest so far, so we traced every cell where the two languages differ to the sites that
decided it. The compiler's net effect on passes is about zero. It rescued one Haiku run: its Python counterpart
failed only on two sites the compiler flags. It cost two Sonnet runs, whose forced JSON arms got wrong content where
Python's generic fallback was automatically right. The rest of Rust's 8-run lead (15/54 vs 7/54):

- **A test-suite asymmetry (+3).** Python's base has a unit test pinning the list of change-log kinds, and Rust has
  none, since a Rust test can't enumerate enum variants. Python agents cite that test as their reason not to record
  ALTER in the change log: 0/9 Python runs did, against 4/9 Rust.
- **Possibly types as notation (+2).** All three Rust Opus runs typed date arithmetic with an unknown operand
  correctly, writing patterns like `Some(Integer) | None`. All three Python Opus runs matched only concrete types
  and failed on that one rule. No compiler check is involved, and n = 3.
- **Silent semantic sites and content (+4)**, which looks like chance at three repeats.

Sites nothing flags were missed in both languages, Rust 41 times and Python 62. They include the lint rules through
simple-CASE expansions (missed by all 18 runs) and BETWEEN/IN and NOT LIKE rewritten in the parser.

### 6. Rust carries no fixed token tax

Across Tiers 1–4, Rust and Python total token use differs by −7% to +8%. At Tier 5 it runs the other way: Python
used 25% more (622M vs 499M tokens), because Haiku and Sonnet took more turns in Python (90 vs 71 and 53 vs 43 per
run). The largest Rust premium was Haiku at Tier 4 (+37%). Compile time is small throughout. At Tier 1, for
example, Rust builds took 59–62 s across seven tasks, out of about 2,000 s of wall time.

A separate replication by Kimi K3.1 Pro, on small SQL engines it wrote itself, found Rust used 28.6% more tokens.
But its Rust code was 1.8× the size of its Python, so code size may explain that gap rather than the types. Here
the Rust codebase is the smaller one.

## Specific Outcomes, For and Against

Pass rates compress a lot. The outcomes below are individual cases, each chosen because it shows one mechanism
clearly.

### For: the compiler finds what the agent doesn't see, or doesn't look for

- **A green solution that broke the cluster (Tier 1 → 2).** Haiku's Python solutions for CASE and DATE passed
  every Tier 1 test. Applied unchanged to the full database, they also passed the entire Tier 2 suite, then crashed
  on the first cluster request carrying the new expression or value (`UnexpectedEof`). The Python port serializes
  values and plans by hand, and at Tier 1 nothing called that code, so it looked dead. Rust derives serialization
  with serde: none of the 10 transplanted Rust solutions failed this way.
- **An import path nothing tested (Tier 2 → 3).** Tier 2 DATE and release solutions, carried to Tier 3, passed the
  agent's visible script and every existing test in Python. Then they failed all six extension scripts: importing
  a date crashed, and nothing but IMPORT reaches that code. In Rust the same solutions did not compile. For DATE,
  the four errors named the JSON value form, the SQL literal and the CSV and JSON import parsers. That was exactly
  the list of remaining work.
- **Haiku's missing JSON writer (Tier 3).** With the extension docs removed from the README, Haiku in Python
  missed a compiler-flagged site in 6 of 18 runs:
  - CASE and BOOL_AND were never added to the JSON expression writer. It crashes, but only under `[json]` output,
    which the agent never tried.
  - DISTINCT was silently dropped by the JSON aggregate writer and by the hand-written Raft serialization.

  Rust Haiku missed none of 18, because each of those sites is a non-exhaustive match, a missing struct field or
  derived serde.
- **Haiku's only Tier 5 pass.** One of 36 Haiku runs passed at Tier 5: a Rust DISTINCT run. A Python Haiku run on
  the same task failed only on the two sites the compiler names there: the statement-digest printer for
  `SELECT DISTINCT` and for `f(DISTINCT x)`. It is the cleanest case of the compiler deciding a run.

### Against: capable agents find the sites anyway, and types don't check meaning

- **Opus never needed the compiler to find a site.** Across Tiers 3, 4 and 5, no Python Opus run (0 of 51) left a
  compiler-flagged site unhandled. Opus found every JSON writer, digest printer and formatter by searching for an
  existing sibling of the new variant and visiting each hit. In a consistent codebase, that search returns the
  same list as the compiler.
- **Forced arms, wrong content (Tiers 3–5).** Exhaustive matching makes the agent write an arm, not the right arm.
  When adding DATE, Rust agents were forced to add a JSON arm, and often wrote the date as a plain string instead
  of the typed `{"type":"DATE",…}` form. At Tier 4 Rust got this right in 3 of 9 runs and Python in 9 of 9,
  because Python's generic fallback already handled every typed value. At Tier 5 the same pattern cost Rust two
  Sonnet DISTINCT runs (aggregate-name casing, an extra JSON field), which Python's fallback got right.
- **Rules nobody followed (Tiers 3–5).** LINT rules must also apply to the expressions a simple CASE or BETWEEN/IN
  expands into. No run did this: 0 of 36 at Tier 4, and 0 of 18 for CASE at Tier 5, across every model and both
  languages. Most runs at every tier also implemented BETWEEN/IN by rewriting it in the parser into `>= … AND <=`,
  against the digest documentation's "constructs are never rewritten" (15 of 18 at Tiers 4 and 5). No new type
  exists then, so there is nothing for a compiler to check.

### Looked like types, but wasn't

- **The change log (Tiers 4–5).** At Tier 4, 8 of 9 Rust ALTER runs recorded the change in the change log,
  against 0 of 9 Python runs. It looked as if Rust's data-carrying `enum Change` made the new variant natural. The
  transcripts point elsewhere: Python's base has a unit test that pins the exact list of change kinds, and Python
  agents read it as a prohibition. One Sonnet run wrote "`test_kind` pins the list of change kinds, so I did not
  add one". Rust has no such test, since a Rust test can't enumerate an enum's variants. That one test accounts
  for 3 of Rust's 8 extra passes at Tier 5.
- **The DESCRIBE rule (Tier 5, unresolved).** Date arithmetic with an operand of unknown type must be typed DATE.
  All three Rust Opus runs got this right, writing patterns like `(true, Some(Date), Some(Integer) | None)`. All
  three Python Opus runs matched only concrete types and failed on that one rule. The compiler did not force it,
  since both languages used a catch-all. But Rust's `Option` puts "unknown" in front of the agent as a value to
  match. With three runs each (p ≈ 0.1), and Rust Haiku and Sonnet wrong in 6 of 6, it is a hint, not a result.
- **A documentation difference (Tier 3).** Part of Opus's 18 vs. 13 at Tier 3 came from the ALTER digest. The Rust
  module doc stated "optional words are left out" as a rule, and the Python docstring only implied it. Opus got it
  right in Rust 3/3 and in Python 0/3. From Tier 4 on, the docs are identical in both languages.

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

Tier 5 bears this out at 19k lines of Rust: grepping did not lose its edge for strong models.

### Are types more critical for agents than for humans?

There was no human arm, so this is inference. The evidence does not support the strong form of the claim. The
agents types helped were the weak one, and agents whose downstream code was out of view. Those are the same
conditions under which types help a human newcomer. Strong agents behaved like a diligent engineer with good
search tools: they found every site the compiler would have named.

The instinct that agents need types less because they hold more in working memory is half right. Opus did not
hold 20k lines in context; it searched systematically, and Haiku didn't. What separates the two is how thoroughly
they search, not how much they remember. And what both lacked at the larger tiers was the semantic rules, which
these types don't encode.

Two things do tilt the balance toward agents. Their retries are cheap: a compile error costs an agent a few
seconds and no fatigue, and Rust carried no consistent token premium. And every session starts cold, so
"downstream code I have never seen" is an agent's normal state rather than a newcomer's first week. The
defensible claim is narrower: types help an agent in proportion to how weak its search is, and how much of the
code that depends on a change it never looks at.

### What this does not test: types as a way to think

Everything above treats the type system as a verifier. In that role, the results fit a simple summary: **types
didn't make the agents better at writing the code; they caught the agents' mistakes more often and made them fix
them.** A compiler-flagged site a weak agent would have missed became a compile error it had to clear. The content
it then wrote was no better than in Python, and sometimes worse, where a forced arm replaced a correct generic
fallback.

That framing almost assumes that the model gains nothing from types as abstractions, and the experiment's design
leans the same way. Every task changes an existing codebase whose types a human-led port already fixed. No agent
designed a data model, chose between an enum and a set of flags, or decided where a new concept should live. So
the second job of a type system, as a way to model the problem, barely comes up.

There is one hint that it might matter. At Tier 5, Rust Opus wrote the "operand of unknown type" case as
`Some(Integer) | None`, a value of the type it was matching, and got the rule right 3 times out of 3. Python Opus,
whose code was annotated `DataType | None` but matched only concrete types, missed it 3 times out of 3. The compiler
enforced nothing there; if anything helped, it was the notation. Three runs a side can't settle it.

Testing types as abstractions would need a different experiment, for example:

- greenfield tasks, where the agent designs its own types and data model, judged on correctness and on how well
  later changes go;
- the same codebase written with rich domain types and with loose primitive types (strings, dicts, tuples), in
  one language, so that the compiler is held constant and only the modelling changes;
- measuring whether agents that state a type-level design first write fewer semantic bugs.

We'd welcome ideas on how to do this well.

Open questions:

1. **Does documentation close the semantic gap?** The Tier 3 repeat runs, Tier 4 and Tier 5 all hid the extension docs from the README.
   A run with them restored would show whether the silent semantic misses are a documentation problem.
2. **Can encoding more invariants into types bridge the gap?** If a codebase pushes more of its semantic
   rules into the type system, can the compiler start helping with the *what* as well as the *where*?
3. **Do agents think better in types?** This is the abstraction role of types, which this project does not
   test (see above).

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
| `toydb/tiers/t1..t5/{rust,python}` | The five benchmark tiers. Specs live in `toydb/tiers/t3/EXT_SPEC.md`, `toydb/tiers/t4/EXT4_SPEC.md` and `toydb/tiers/t5/EXT5_SPEC.md` (module docs in `EXT4_DOCS.md`, `EXT5_DOCS.md`). Each tier contains `check.sh`, `grade.py`, and `LAYOUT.md` |
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
