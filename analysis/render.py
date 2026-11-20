"""Render the tracking artifact from results + transcript metrics.  usage: render.py"""
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
from tasks import TASKS  # noqa: E402
from tasks2 import TASKS2  # noqa: E402
from tasks3 import TASKS3  # noqa: E402

NOTES = json.loads((ROOT / "analysis/notes.json").read_text())
metrics = {m["run"]: m for m in json.loads((ROOT / "analysis/agent_metrics.json").read_text())}
EXCLUDED = set(NOTES.get("excluded", []))
results = {}
for f in sorted((ROOT / "results").glob("*.json")):
    r = json.loads(f.read_text())
    if r["run"] in metrics and r["run"] not in EXCLUDED and "tier" not in r:
        r.update({k: v for k, v in metrics[r["run"]].items() if k not in r})
        results[r["run"]] = r

SETTING_LABEL = {"hl": "Haiku · low", "hm": "Haiku · medium", "sl": "Sonnet · low", "sm": "Sonnet · medium",
                 "sh": "Sonnet · high", "om": "Opus · medium", "oh": "Opus · high"}


def setting(run):  # b1_t01_py_hl -> hl
    return run.rsplit("_", 1)[1]


BATCH_LABEL = {"b1": "v1", "b2": "v2", "b3": "v2 · no tests", "b4": "v3", "b5": "v3 · combined"}


def batch(run):
    return BATCH_LABEL.get(run.split("_", 1)[0], run.split("_", 1)[0])


def e(s):
    return html.escape(str(s))


def med(xs):
    return st.median(xs) if xs else 0


rows_by_cell = defaultdict(list)
for r in results.values():
    rows_by_cell[(batch(r["run"]), setting(r["run"]), r["lang"])].append(r)

summary_rows = []
for (b, s, lang), rs in sorted(rows_by_cell.items()):
    passed = sum(r["pass"] for r in rs)
    summary_rows.append(dict(
        batch=b, setting=SETTING_LABEL.get(s, s), lang=lang, n=len(rs), passed=passed,
        hidden_frac=sum(r["hidden_passed"] for r in rs) / max(1, sum(r["hidden_total"] for r in rs)),
        tools=med([r["tool_calls"] for r in rs]), out=med([r["out_tokens"] for r in rs]),
        tin=med([r["total_in_tokens"] for r in rs]), wall=med([r["wall_s"] for r in rs]),
        chk=med([r["checks"] for r in rs]), chk_s=med([r["check_build_s"] + r["check_test_s"] for r in rs]),
        bfail=sum(r["check_build_failures"] for r in rs), tfail=sum(r["check_test_failures"] for r in rs),
        escaped=sum(1 for r in rs if r["final_visible_ok"] and not r["pass"]),
    ))


def pill(ok, yes="pass", no="fail"):
    return f'<span class="pill {"ok" if ok else "bad"}">{yes if ok else no}</span>'


def summary_table():
    out = ['<table><thead><tr><th>Batch</th><th>Setting</th><th>Lang</th><th class="n">Runs</th><th class="n">Pass</th>'
           '<th class="n">Hidden cases</th><th class="n">Escaped<sup>1</sup></th><th class="n">Tool calls</th>'
           '<th class="n">Output tok</th><th class="n">Input tok</th><th class="n">Wall s</th>'
           '<th class="n">check.sh runs</th><th class="n">Validator s</th><th class="n">Compile fails</th>'
           '<th class="n">Test fails</th></tr></thead><tbody>']
    for s in summary_rows:
        out.append(
            f"<tr><td>{e(s['batch'])}</td><td>{e(s['setting'])}</td><td><span class='lang {s['lang']}'>{e(s['lang'])}</span></td>"
            f"<td class='n'>{s['n']}</td><td class='n'><b>{s['passed']}/{s['n']}</b></td>"
            f"<td class='n'>{s['hidden_frac']:.0%}</td><td class='n'>{s['escaped']}</td><td class='n'>{s['tools']:.0f}</td>"
            f"<td class='n'>{s['out']:,.0f}</td><td class='n'>{s['tin']/1000:,.0f}k</td><td class='n'>{s['wall']:.0f}</td>"
            f"<td class='n'>{s['chk']:.0f}</td><td class='n'>{s['chk_s']:.1f}</td><td class='n'>{s['bfail']}</td>"
            f"<td class='n'>{s['tfail']}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def runs_table():
    out = ['<table class="runs"><thead><tr><th>Run</th><th>Task</th><th>Lang</th><th>Setting</th><th>Result</th>'
           '<th class="n">Hidden</th><th>Failed hidden cases</th><th class="n">Tools</th><th class="n">Out tok</th>'
           '<th class="n">Wall s</th><th class="n">Checks</th><th class="n">Compile fails</th><th class="n">±lines</th></tr></thead><tbody>']
    for run in sorted(results, key=lambda x: (batch(x), x.split("_")[1], setting(x), results[x]["lang"])):
        r = results[run]
        failed = [" ".join(a) for a in r["regressions_failed"] + r["task_failed"]] + r["structural_failures"] + \
                 (["tampered: " + ",".join(r["tampered"])] if r["tampered"] else [])
        out.append(
            f"<tr><td class='mono'>{e(run)}</td><td>{e(r['task'])}</td><td><span class='lang {r['lang']}'>{e(r['lang'])}</span></td>"
            f"<td>{e(SETTING_LABEL.get(setting(run), setting(run)))}</td><td>{pill(r['pass'])}</td>"
            f"<td class='n'>{r['hidden_passed']}/{r['hidden_total']}</td><td class='mono small'>{e('; '.join(failed)) or '—'}</td>"
            f"<td class='n'>{r['tool_calls']}</td><td class='n'>{r['out_tokens']:,}</td><td class='n'>{r['wall_s']:.0f}</td>"
            f"<td class='n'>{r['checks']}</td><td class='n'>{r['check_build_failures']}</td>"
            f"<td class='n'>+{r['lines_added']}/−{r['lines_deleted']}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


FAMILY = {"optional-field": "Optional field", "new-feature": "New feature", "refactor": "Refactor / API change",
          "control": "Control (types shouldn't help)", "driver-upgrade": "Driver upgrade (API break)",
          "optionality": "Required → optional", "cardinality": "Single → multi-valued", "semantics": "Per-command semantics",
          "release-notes": "Six small changes", "combined": "Combined changes"}


def tasks_table(tasks, probes):
    out = ['<table><thead><tr><th>Id</th><th>Task</th><th>Family</th><th>What it probes</th><th class="n">Visible</th>'
           '<th class="n">Hidden</th></tr></thead><tbody>']
    for t in tasks.values():
        out.append(f"<tr><td class='mono'>{t['id']}</td><td>{e(t['title'])}</td><td>{e(FAMILY[t['family']])}</td>"
                   f"<td>{e(probes.get(t['id'], ''))}</td><td class='n'>{len(t['visible'])}</td>"
                   f"<td class='n'>{len(t['hidden'])}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


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


def bullets(items):
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


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
  <p class="lede">The same codebase in Rust and Python: first a small CLI app on TypeDB (tier 0), then toydb, a SQL database, at growing sizes (tiers 1–3). Agents of different strength get identical tasks;
  a compiler (or not) plus a few visible tests are their only validators; held-back tests decide correctness.</p>
  <div class="status">
    <div class="stat"><b>{total}</b><span>runs graded</span></div>
    <div class="stat"><b>{passed}/{total}</b><span>passed all hidden tests</span></div>
    <div class="stat"><b>{len(TASKS) + len(TASKS2) + len(TASKS3) + len(TOYDB_TASKS)}</b><span>tasks × 2 languages</span></div>
    <div class="stat"><b>{e(NOTES['phase'])}</b><span>current phase</span></div>
  </div>
</header>

<section class="verdict">
  <h2>Where it stands</h2>
  {bullets(NOTES['findings'])}
</section>

<section>
  <h2>Results by setting</h2>
  <div class="scroll">{summary_table()}</div>
  <p class="foot"><sup>1</sup> Escaped: the agent's last <code>check.sh</code> was green, but hidden tests failed. Medians shown for tools, tokens, wall time and validator time; sums for failures.
  Input tokens include cache reads (about 30k of every run is the fixed agent system prompt).</p>
</section>

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

<section>
  <h2>Tasks</h2>
  <h3>v3: same 21-command app; each change needs a different rule per command, on data the visible tests don't cover</h3>
  <div class="scroll">{tasks_table(TASKS3, NOTES['probes3'])}</div>
  <h3>v2: 21-command app, about 1.5k lines per language, 80 (Python) / 111 (Rust) driver row-access call sites</h3>
  <div class="scroll">{tasks_table(TASKS2, NOTES['probes2'])}</div>
  <h3>v1: 6-command app, about 600 lines per language</h3>
  <div class="scroll">{tasks_table(TASKS, NOTES['probes'])}</div>
  <p class="foot">Visible = cases the agent can run via <code>check.sh</code> on a 6-member dataset. Hidden = the 9 base-command regression cases plus task cases, on a 41-member dataset with missing optional fields, quotes, backslashes, ties and boundary dates. Expected outputs come from an independent oracle that reads the CSVs directly (no TypeDB), validated against both base apps.</p>
</section>

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
  <h2>Scale-up plan</h2>
  {bullets(NOTES['scale_plan'])}
</section>

<section>
  <h2>Next</h2>
  {bullets(NOTES['next'])}
</section>

<section>
  <h2>All runs</h2>
  <div class="scroll">{runs_table()}</div>
</section>
</main>
"""
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(page)
print(OUT, total, passed)
