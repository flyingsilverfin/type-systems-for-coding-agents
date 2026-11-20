"""Extract per-run agent metrics from subagent transcripts.

usage: transcripts.py  -> writes /home/user/agentbench/analysis/agent_metrics.json
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

SUB = Path.home() / ".claude/projects/-home-user/0e981355-0d3b-4c00-86f4-706c60e10216/subagents"
ROOT = Path("/home/user/agentbench")
MAP = ROOT / "analysis/agent_map.tsv"

PATH_RE = re.compile(r"(/[A-Za-z0-9_.~\-][^\s'\"|;&<>()`]*)")
ALLOWED_PREFIXES = ("/dev/null", "/tmp", "/usr", "/bin", "/proc/self", "/etc/os-release")


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def run_of(f: Path) -> str | None:
    """The run id a benchmark agent transcript belongs to, from its first prompt."""
    with open(f) as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("type") != "user":
                continue
            c = (d.get("message") or {}).get("content")
            text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            m = re.search(r"You are working on a (?:small )?codebase in /home/user/agentbench/runs/(\S+?)\.", text)
            if m:
                return m.group(1)
            if "[Workflow harness — user request]" not in text:
                return None
    return None


def analyse(run: str, f: Path) -> dict | None:
    agent = f.stem.replace("agent-", "")
    ws = str(ROOT / "runs" / run)
    seen = set()
    out = dict(run=run, agent=agent, relayed_user_request="[Workflow harness — user request]" in f.read_text()[:20000], turns=0, tool_calls=0, out_tokens=0, thinking_tokens=0,
               in_tokens=0, cache_read=0, cache_write=0, check_calls=0, tools={}, violations=[], final_text="")
    times = []
    tool_start = {}
    check_time = 0.0
    for line in f.read_text().splitlines():
        d = json.loads(line)
        if "timestamp" in d:
            times.append(ts(d["timestamp"]))
        m = d.get("message") or {}
        if d.get("type") == "assistant":
            mid = m.get("id")
            if mid not in seen:
                seen.add(mid)
                u = m.get("usage") or {}
                out["turns"] += 1
                out["out_tokens"] += u.get("output_tokens", 0)
                out["thinking_tokens"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
                out["in_tokens"] += u.get("input_tokens", 0)
                out["cache_read"] += u.get("cache_read_input_tokens", 0)
                out["cache_write"] += u.get("cache_creation_input_tokens", 0)
            for c in m.get("content") or []:
                if c.get("type") == "tool_use":
                    out["tool_calls"] += 1
                    out["tools"][c["name"]] = out["tools"].get(c["name"], 0) + 1
                    inp = json.dumps(c.get("input"))
                    if "check.sh" in inp:
                        out["check_calls"] += 1
                        tool_start[c["id"]] = ts(d["timestamp"])
                    for p in PATH_RE.findall(inp.replace("\\n", " ")):
                        if p.startswith(ws) or p.startswith(ALLOWED_PREFIXES) or p.startswith(("/home/user/.cargo/bin", "/home/user/.cargo/registry", "/home/user/.rustup")):
                            continue
                        if p.startswith("/home") or p.startswith("/root") or "agentbench" in p:
                            out["violations"].append(p)
                elif c.get("type") == "text":
                    out["final_text"] = c["text"]
        elif d.get("type") == "user":
            for c in (m.get("content") or []) if isinstance(m.get("content"), list) else []:
                if c.get("type") == "tool_result" and c.get("tool_use_id") in tool_start:
                    check_time += ts(d["timestamp"]) - tool_start.pop(c["tool_use_id"])
    out["start"] = times[0] if times else 0
    out["wall_s"] = round(times[-1] - times[0], 1) if times else 0
    out["check_wall_s"] = round(check_time, 1)
    out["wall_ex_check_s"] = round(out["wall_s"] - check_time, 1)
    out["total_in_tokens"] = out["in_tokens"] + out["cache_read"] + out["cache_write"]
    out["violations"] = sorted(set(out["violations"]))
    st = re.search(r"STATUS:\s*(\S+)", out["final_text"])
    out["status"] = st.group(1) if st else "?"
    out["finished"] = bool(st)
    return out


def main():
    rows = []
    for f in sorted(SUB.rglob("agent-*.jsonl")):
        run = run_of(f)
        if run:
            r = analyse(run, f)
            if r:
                rows.append(r)
    latest = {}
    for r in rows:  # a rerun reuses its run id: keep the most recent attempt
        if r["run"] not in latest or r["start"] > latest[r["run"]]["start"]:
            latest[r["run"]] = r
    rows = sorted(latest.values(), key=lambda r: r["run"])
    (ROOT / "analysis/agent_metrics.json").write_text(json.dumps(rows, indent=1))
    for r in rows:
        print(f"{r['run']:16} turns={r['turns']:3} tools={r['tool_calls']:3} out={r['out_tokens']:6} "
              f"in={r['total_in_tokens']:8} wall={r['wall_s']:6} chk={r['check_calls']} chk_s={r['check_wall_s']} "
              f"status={r['status']} viol={r['violations'][:3]}")


if __name__ == "__main__":
    main()
