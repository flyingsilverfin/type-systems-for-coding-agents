"""Render the tracking artifact from results + transcript metrics.

usage: render.py [--toydb-only] [--out PATH]

Extra sections come from render_extra.py when it is present. --toydb-only (implied when it is absent) leaves them
out and applies the `toydb_only` overrides of notes.json.
"""
from __future__ import annotations

import html
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/user/agentbench")
OUT = ROOT / "artifact/index.html"
sys.path.insert(0, str(ROOT / "bench"))
TOYDB_ONLY = "--toydb-only" in sys.argv
if "--out" in sys.argv:
    OUT = Path(sys.argv[sys.argv.index("--out") + 1])
sys.path.insert(0, str(ROOT / "analysis"))
try:
    import render_extra  # noqa: E402  optional extra sections
except ImportError:
    TOYDB_ONLY = True

NOTES = json.loads((ROOT / "analysis/notes.json").read_text())
if TOYDB_ONLY:
    ov = NOTES.get("toydb_only", {})
    for k in ("findings", "scale_plan"):
        if k + "_keep" in ov:
            NOTES[k] = [NOTES[k][i] for i in ov[k + "_keep"]]
    for k in ("lede", "design", "isolation", "metrics"):
        if k in ov:
            NOTES[k] = ov[k]
metrics = {m["run"]: m for m in json.loads((ROOT / "analysis/agent_metrics.json").read_text())}

SETTING_LABEL = {"hl": "Haiku · low", "hm": "Haiku · medium", "sl": "Sonnet · low", "sm": "Sonnet · medium",
                 "sh": "Sonnet · high", "om": "Opus · medium", "oh": "Opus · high"}


def setting(run):  # t3_date_py_hl -> hl
    return run.rsplit("_", 1)[1]


def e(s):
    return html.escape(str(s))


def med(xs):
    return st.median(xs) if xs else 0


def pill(ok, yes="pass", no="fail"):
    return f'<span class="pill {"ok" if ok else "bad"}">{yes if ok else no}</span>'


TOYDB_TASKS = ["between_in", "case", "date", "distinct", "alter", "release", "bugfix"]
toydb = {}
for f in sorted((ROOT / "results").glob("t*_*.json")):
    r = json.loads(f.read_text())
    if "tier" in r and r["run"] in metrics:
        r.update({k: v for k, v in metrics[r["run"]].items() if k not in r})
        lf = ROOT / "logs" / f"{r['run']}.checks.jsonl"
        cs = [json.loads(l) for l in lf.read_text().splitlines() if l.strip()] if lf.exists() else []
        r["red_build"] = sum(1 for c in cs if c["rc"] != 0 and c["test_s"] == 0)
        r["red_test"] = sum(1 for c in cs if c["rc"] != 0 and c["test_s"] > 0)
        toydb[r["run"]] = r


def toydb_summary(tier):
    out = ['<table><thead><tr><th>Setting</th><th>Lang</th><th class="n">Pass</th><th class="n">Hidden scripts</th>'
           '<th class="n">Escaped</th><th class="n">Turns</th><th class="n">Output tok</th><th class="n">Input tok</th>'
           '<th class="n">Wall s</th><th class="n">Compile s</th><th class="n">Test s</th><th class="n">Wall − check s</th>'
           '<th class="n">check.sh runs</th><th class="n">Red: build / test</th></tr></thead><tbody>']
    for s in ["hl", "oh"]:
        for lang in ["rust", "python"]:
            rs = [r for r in toydb.values() if r["run"].startswith(tier + "_") and setting(r["run"]) == s and r["lang"] == lang]
            if not rs:
                continue
            S = lambda k: sum(r[k] for r in rs)
            out.append(
                f"<tr><td>{e(SETTING_LABEL[s])}</td><td><span class='lang {lang}'>{lang}</span></td>"
                f"<td class='n'><b>{S('pass')}/{len(rs)}</b></td><td class='n'>{S('hidden_passed')}/{S('hidden_total')}</td>"
                f"<td class='n'>{S('escaped')}</td><td class='n'>{S('turns')}</td><td class='n'>{S('out_tokens'):,}</td>"
                f"<td class='n'>{S('total_in_tokens')/1e6:.1f}M</td><td class='n'>{S('wall_s'):,.0f}</td>"
                f"<td class='n'>{S('check_build_s'):.0f}</td><td class='n'>{S('check_test_s'):.0f}</td>"
                f"<td class='n'>{S('wall_s') - S('check_build_s') - S('check_test_s'):,.0f}</td>"
                f"<td class='n'>{S('checks')}</td><td class='n'>{S('red_build')} / {S('red_test')}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def toydb_runs(tier, prefix=None):
    out = ['<table class="runs"><thead><tr><th>Task</th><th>Setting</th><th>Lang</th><th>Result</th><th class="n">Hidden</th>'
           '<th>Failed scripts</th><th class="n">Turns</th><th class="n">Out tok</th><th class="n">Wall s</th>'
           '<th class="n">Checks</th><th class="n">Compile s</th><th class="n">Test s</th></tr></thead><tbody>']
    prefix = prefix or tier + "_"
    rows = [r for r in toydb.values() if r["tier"] == tier and r["run"].startswith(prefix)]
    for r in sorted(rows, key=lambda r: (TOYDB_TASKS.index(r["task"]), r["run"][-1], r["lang"] != "rust")):
        failed = [k for k, v in r["hidden"].items() if v != "pass"] + r["regression_failed"]
        out.append(
            f"<tr><td>{e(r['task'])}</td><td>{e(SETTING_LABEL.get(setting(r['run'])) or (SETTING_LABEL.get(setting(r['run'])[:2], '?') + ' · rep ' + r['run'][-1]))}</td>"
            f"<td><span class='lang {r['lang']}'>{r['lang']}</span></td><td>{pill(r['pass'])}</td>"
            f"<td class='n'>{r['hidden_passed']}/{r['hidden_total']}</td><td class='mono small'>{e(', '.join(failed)) or '—'}</td>"
            f"<td class='n'>{r['turns']}</td><td class='n'>{r['out_tokens']:,}</td><td class='n'>{r['wall_s']:.0f}</td>"
            f"<td class='n'>{r['checks']}</td><td class='n'>{r['check_build_s']:.1f}</td><td class='n'>{r['check_test_s']:.1f}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def transplant_table(prefix="x12"):
    rows = []
    for f in sorted((ROOT / "results").glob(f"{prefix}_*.json")):
        rows.append(json.loads(f.read_text()))
    if not rows:
        return "<p class='foot'>Pending.</p>"
    if prefix == "x12":
        head = '<th>Result at tier 2</th><th class="n">Tier-1 scripts</th><th class="n">Cluster scripts</th>'
    else:
        head = '<th>Result at tier 3</th><th>Builds</th><th class="n">Tier-1/2 scripts</th><th class="n">Extension scripts</th>'
    out = [f'<table><thead><tr><th>Task</th><th>Setting</th><th>Lang</th>{head}<th>Failed</th></tr></thead><tbody>']
    for r in sorted(rows, key=lambda r: (TOYDB_TASKS.index(r["task"]), setting(r["run"]), r["lang"] != "rust")):
        failed = [k for k, v in r["hidden"].items() if v != "pass"] + r["regression_failed"]
        if not r["build_ok"]:
            failed = ["build fails"]
        if prefix == "x12":
            t1 = r["hidden_passed"] - r["cluster_passed"] + r["visible_passed"]
            cells = (f"<td class='n'>{t1}/{r['hidden_total'] - r['cluster_total'] + r['visible_total']}</td>"
                     f"<td class='n'>{r['cluster_passed']}/{r['cluster_total']}</td>")
        else:
            low = r["hidden_passed"] - r["ext_passed"] + r["visible_passed"]
            cells = (f"<td>{pill(r['build_ok'], 'yes', 'no')}</td>"
                     f"<td class='n'>{low}/{r['hidden_total'] - r['ext_total'] + r['visible_total']}</td>"
                     f"<td class='n'>{r['ext_passed']}/{r['ext_total']}</td>")
        out.append(f"<tr><td>{e(r['task'])}</td><td>{e(SETTING_LABEL[setting(r['run'])])}</td>"
                   f"<td><span class='lang {r['lang']}'>{r['lang']}</span></td><td>{pill(r['pass'])}</td>{cells}"
                   f"<td class='mono small'>{e(', '.join(failed)) or '—'}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def sites_table(rows):
    if not rows:
        return ""
    out = ['<table><thead><tr><th>Task</th><th>Where the change must also land</th><th>Rust, tier-2 solution</th>'
           '<th>Python, tier-2 solution</th></tr></thead><tbody>']
    for r in rows:
        out.append(f"<tr><td>{e(r[0])}</td><td>{e(r[1])}</td><td>{e(r[2])}</td><td>{e(r[3])}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def simple_table(head, rows):
    if not rows:
        return ""
    out = ["<table><thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr></thead><tbody>"]
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def token_table():
    """Rust vs Python token totals per tier, over task/setting/repeat cells finished in both languages."""
    M = dict(metrics)
    part = ROOT / "analysis/t5r_partial_metrics.json"
    partial = False
    if part.exists() and not any(r.startswith("t5r_") for r in M):
        M.update(json.loads(part.read_text()))
        partial = True
    tot = lambda m: m["total_in_tokens"] + m["out_tokens"]
    pct = lambda r, p: f"{100 * (r - p) / p:+.0f}%" if p else "—"
    rows = [("t1_", "1 · SQL core (Haiku-low, Opus-high)"), ("t2_", "2 · full toydb (Haiku-low, Opus-high)"),
            ("t3r_", "3 · no README docs (3 models × 3)"), ("t4r_", "4 · (3 models × 3)"), ("t5r_", "5 · (3 models × 3)")]
    out = ['<table><thead><tr><th>Tier</th><th class="n">Pairs</th><th class="n">Rust tokens</th><th class="n">Python tokens</th>'
           '<th class="n">Rust vs Python</th><th class="n">Output only</th><th class="n">Haiku</th><th class="n">Sonnet</th>'
           '<th class="n">Opus</th></tr></thead><tbody>']
    for pre, label in rows:
        cells = {}
        for run, m in M.items():
            if not run.startswith(pre):
                continue
            p = run.split("_")
            cells.setdefault("_".join(p[:-2] + [p[-1]]), {})["py" if p[-2] == "py" else "rs"] = m
        cells = {k: v for k, v in cells.items() if len(v) == 2}
        if not cells:
            continue
        S = lambda l, f, ks=None: sum(f(v[l]) for k, v in cells.items() if ks is None or k.rsplit("_", 1)[1][:2] == ks)
        per = [pct(S("rs", tot, s), S("py", tot, s)) for s in ("hl", "sm", "oh")]
        lab = label + (" — partial, runs still going" if pre == "t5r_" and partial else "")
        out.append(f"<tr><td>{e(lab)}</td><td class='n'>{len(cells)}</td><td class='n'>{S('rs', tot)/1e6:.0f}M</td>"
                   f"<td class='n'>{S('py', tot)/1e6:.0f}M</td><td class='n'><b>{pct(S('rs', tot), S('py', tot))}</b></td>"
                   f"<td class='n'>{pct(S('rs', lambda m: m['out_tokens']), S('py', lambda m: m['out_tokens']))}</td>"
                   + "".join(f"<td class='n'>{x}</td>" for x in per) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def bullets(items):
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


X = {} if TOYDB_ONLY else render_extra.build(ROOT, NOTES, metrics, e, med, pill, setting, SETTING_LABEL)
results = X.get("results", {})
T0_SUMMARY, T0_TASKS, T0_RUNS = (X.get(k, "") for k in ("T0_SUMMARY", "T0_TASKS", "T0_RUNS"))
total = len(results) + len(toydb)
passed = sum(r["pass"] for r in results.values()) + sum(r["pass"] for r in toydb.values())
page = f"""<title>Types vs Agents Bench</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,650&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: a single reading column for prose that widens into full-bleed tables; ledger-like, rust-and-slate */
:root {{
  --bg: #f6f5f2; --surface: #ffffff; --fg: #1d2126; --muted: #5d6670; --line: #dcd9d2;
  --accent: #b3471d; --rust: #b3471d; --py: #2f5f8a; --ok: #2c7a4b; --bad: #b2302a; --chip: #efece6;
  --display: "Fraunces", Georgia, serif; --body: "IBM Plex Sans", system-ui, sans-serif; --mono: "IBM Plex Mono", ui-monospace, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #15181b; --surface: #1c2024; --fg: #e7e5e0; --muted: #9aa3ab; --line: #30353a;
  --accent: #e07a4d; --rust: #e07a4d; --py: #7fb0dc; --ok: #5cc28a; --bad: #ec6b63; --chip: #262b30; color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #15181b; --surface: #1c2024; --fg: #e7e5e0; --muted: #9aa3ab; --line: #30353a;
  --accent: #e07a4d; --rust: #e07a4d; --py: #7fb0dc; --ok: #5cc28a; --bad: #ec6b63; --chip: #262b30; color-scheme: dark; }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.6 var(--body); }}
main {{ max-width: 1180px; margin: 0 auto; padding-block: 40px 80px; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); display: grid; gap: 40px; }}
header {{ display: grid; gap: 10px; }}
.eyebrow {{ font: 500 12px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--accent); }}
h1 {{ font: 650 clamp(30px, 5vw, 46px)/1.08 var(--display); margin: 0; text-wrap: balance; }}
h2 {{ font: 600 24px/1.2 var(--display); margin: 0 0 12px; text-wrap: balance; }}
h3 {{ font: 600 15px/1.3 var(--body); margin: 18px 0 6px; }}
p, li {{ max-width: 72ch; }}
.lede {{ font-size: 17px; color: var(--muted); max-width: 70ch; margin: 0; }}
.status {{ display: flex; flex-wrap: wrap; gap: 10px; }}
.stat {{ background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 10px 14px; min-width: 0; }}
.stat b {{ display: block; font: 600 22px/1.2 var(--display); font-variant-numeric: tabular-nums; }}
.stat span {{ font-size: 12px; color: var(--muted); }}
.verdict {{ border-left: 3px solid var(--accent); padding: 4px 0 4px 16px; }}
.scroll {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ font: 500 11px/1.3 var(--mono); letter-spacing: .04em; text-transform: uppercase; color: var(--muted); white-space: nowrap; background: var(--chip); }}
td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
tr:last-child td {{ border-bottom: 0; }}
.mono {{ font-family: var(--mono); font-size: 12px; }}
.small {{ font-size: 11px; color: var(--muted); max-width: 340px; }}
.pill {{ font: 500 11px/1 var(--mono); padding: 3px 7px; border-radius: 99px; }}
.pill.ok {{ color: var(--ok); border: 1px solid var(--ok); }}
.pill.bad {{ color: var(--bad); border: 1px solid var(--bad); }}
.lang {{ font: 500 12px/1 var(--mono); }}
.lang.rust {{ color: var(--rust); }} .lang.python {{ color: var(--py); }}
.grid2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; }}
.grid2 > div {{ min-width: 0; }}
code {{ font: 12.5px var(--mono); background: var(--chip); padding: 1px 4px; border-radius: 3px; }}
.foot {{ font-size: 12px; color: var(--muted); }}
</style>
<main>
<header>
  <div class="eyebrow">Rust meetup talk · experiment log · {e(NOTES['updated'])}</div>
  <h1>Do types make weak agents better?</h1>
  <p class="lede">{NOTES['lede']}</p>
  <div class="status">
    <div class="stat"><b>{total}</b><span>runs graded</span></div>
    <div class="stat"><b>{passed}/{total}</b><span>passed all hidden tests</span></div>
    <div class="stat"><b>{X.get("n_tasks", 0) + len(TOYDB_TASKS)}</b><span>tasks × 2 languages</span></div>
    <div class="stat"><b>{e(NOTES['phase'])}</b><span>current phase</span></div>
  </div>
</header>

<section class="verdict">
  <h2>Where it stands</h2>
  {bullets(NOTES['findings'])}
</section>

{T0_SUMMARY}
<section class="grid2">
  <div>
    <h2>Design</h2>
    {bullets(NOTES['design'])}
  </div>
  <div>
    <h2>Isolation &amp; integrity</h2>
    {bullets(NOTES['isolation'])}
  </div>
</section>

{T0_TASKS}
<section>
  <h2>Metrics</h2>
  {bullets(NOTES['metrics'])}
</section>

<section>
  <h2>Tier 1 · toydb SQL core (about 6k lines Rust / 7.6k Python)</h2>
  {bullets(NOTES.get('tier1', []))}
  <div class="scroll">{toydb_summary('t1')}</div>
  <p class="foot">Sums over the 7 tasks. Compile s and Test s are the time inside the agents' own <code>check.sh</code> runs (logged by an instrumented <code>check.sh</code>); Wall − check is everything else (model time and tool calls). Red build = a check that failed before tests ran.</p>
  <h3>All tier-1 runs</h3>
  <div class="scroll">{toydb_runs('t1')}</div>
</section>

<section>
  <h2>Tier 2 · full toydb with Raft, server, client (about 8.5k lines Rust / 11.3k Python)</h2>
  {bullets(NOTES.get('tier2', []))}
  <div class="scroll">{toydb_summary('t2')}</div>
  <h3>Would the tier-1 solutions have survived at tier 2?</h3>
  {bullets(NOTES.get('transplant', []))}
  <div class="scroll">{transplant_table()}</div>
  <h3>Ablation: tier 2 without the cluster-script hint (Haiku, 2 repeats)</h3>
  {bullets(NOTES.get('nohint', []))}
  <div class="scroll">{toydb_runs('t2', 't2n_')}</div>
  <h3>All tier-2 runs</h3>
  <div class="scroll">{toydb_runs('t2')}</div>
</section>

<section>
  <h2>Tier 3 · toydb plus four extension layers (about 10k lines Rust / 12.7k Python)</h2>
  {bullets(NOTES.get('tier3', []))}
  <div class="scroll">{toydb_summary('t3')}</div>
  <h3>Where each task's change must also land, and who notices</h3>
  {bullets(NOTES.get('t3_sites_intro', []))}
  <div class="scroll">{sites_table(NOTES.get('t3_sites', []))}</div>
  <h3>Would the tier-2 solutions have survived at tier 3?</h3>
  {bullets(NOTES.get('transplant3', []))}
  <div class="scroll">{transplant_table('x23')}</div>
  <h3>All tier-3 runs</h3>
  <div class="scroll">{toydb_runs('t3')}</div>
</section>

<section>
  <h2>Experiment 1 · tier 3 without the README's extension docs (Haiku, Sonnet, Opus · 3 repeats each)</h2>
  {bullets(NOTES.get('exp1', []))}
  <div class="scroll">{simple_table(['Model', 'Rust pass', 'Python pass', 'Python missed a compiler-flagged site', 'Rust: right site, wrong content', 'Output tok/run (Rust · Python)', 'Turns/run', 'Wall s/run'], NOTES.get('exp1_models', []))}</div>
  <div class="scroll">{simple_table(['Task', 'With README, Haiku (n=1)', 'Haiku (n=3)', 'Sonnet (n=3)', 'Opus (n=3)', 'Failures without README'], NOTES.get('exp1_table', []))}</div>
  <h3>All experiment-1 runs</h3>
  <div class="scroll">{toydb_runs('t3', 't3r_')}</div>
</section>

<section>
  <h2>Tier 4 · seven more extension layers, no README extension docs (Haiku, Sonnet, Opus · 3 repeats each)</h2>
  {bullets(NOTES.get('tier4', []))}
  <div class="scroll">{simple_table(['Model', 'Rust pass', 'Python pass', 'Python crashed at an unhandled variant', 'Output tok/run (Rust · Python)', 'Turns/run', 'Wall s/run'], NOTES.get('tier4_models', []))}</div>
  <div class="scroll">{simple_table(['Task', 'Haiku R/P', 'Sonnet R/P', 'Opus R/P', 'Main failures (both languages unless noted)'], NOTES.get('tier4_tasks', []))}</div>
  <h3>Where the change had to land, and who got there</h3>
  {bullets(NOTES.get('tier4_sites', []))}
  <h3>All tier-4 runs (after the post-run test audit)</h3>
  <div class="scroll">{toydb_runs('t4', 't4r_')}</div>
</section>

<section>
  <h2>Tier 5 · nine more extension layers, about 19.2k lines Rust / 20.7k Python (Haiku, Sonnet, Opus · 3 repeats each)</h2>
  {bullets(NOTES.get('tier5', []))}
  <div class="scroll">{simple_table(['Model', 'Rust pass', 'Python pass', 'Missed a compiler-flagged site (Rust · Python, excl. between_in)', 'Hidden scripts passed (Rust · Python)', 'Output tok/run (Rust · Python)', 'Turns/run'], NOTES.get('tier5_models', []))}</div>
  <div class="scroll">{simple_table(['Task', 'Haiku R/P', 'Sonnet R/P', 'Opus R/P', 'Main failures'], NOTES.get('tier5_tasks', []))}</div>
  <h3>Where the change had to land, and who got there</h3>
  {bullets(NOTES.get('tier5_sites', []))}
  <h3>All tier-5 runs</h3>
  <div class="scroll">{toydb_runs('t5', 't5r_')}</div>
</section>

<section>
  <h2>Token cost: Rust vs Python</h2>
  {bullets(NOTES.get('tokens', []))}
  <div class="scroll">{token_table()}</div>
  <p class="foot">Tokens = input (uncached + cache reads + cache writes) + output, summed over every task × setting × repeat cell that finished in both languages. Rust vs Python = (Rust − Python) / Python. The Haiku/Sonnet/Opus columns are the same ratio per model; tiers 1–2 have no Sonnet runs.</p>
</section>

<section>
  <h2>Scale-up plan</h2>
  {bullets(NOTES['scale_plan'])}
</section>

<section>
  <h2>Next</h2>
  {bullets(NOTES['next'])}
</section>

{T0_RUNS}
</main>
"""
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(page)
print(OUT, total, passed)
