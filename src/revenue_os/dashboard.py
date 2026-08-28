"""Static HTML command center for the multi-agent pipeline.

render_html() is pure: a report dict (from report.pipeline_report), a
timestamp, and the persisted agent/session/spend/goal state in - one
self-contained HTML document out. Inline CSS, no JavaScript, no external
requests. Every value shown is traceable to a file on disk; nothing is
invented. All text is HTML-escaped (candidate titles come from an
untrusted external source).
"""

from __future__ import annotations

from html import escape

from . import lifecycle
from .opportunity import CRITERIA
from .report import NEUTRAL_SCORE

_STYLE = """
:root {
  --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#c9d1d9; --dim:#8b949e;
  --accent:#58a6ff; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --off:#484f58;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg:#f6f8fa; --panel:#ffffff; --border:#d0d7de; --text:#1f2328; --dim:#656d76;
    --accent:#0969da; --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; --off:#afb8c1;
  }
}
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--text); margin:0; padding:1rem;
  font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
h1 { font-size:.95rem; letter-spacing:.12em; text-transform:uppercase; margin:0; }
h2 { font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; color:var(--dim);
  margin:0 0 .55rem; padding-bottom:.3rem; border-bottom:1px solid var(--border); }
p { margin:.3rem 0; }
a { color:var(--accent); }
.hdr { display:flex; flex-wrap:wrap; gap:1.4rem; align-items:baseline; margin-bottom:.9rem; }
.hdr .k { color:var(--dim); font-size:.68rem; text-transform:uppercase;
  letter-spacing:.08em; margin-right:.35rem; }
.grid { display:grid; grid-template-columns:repeat(12,1fr); gap:.75rem; }
.panel { background:var(--panel); border:1px solid var(--border); border-radius:6px;
  padding:.85rem; overflow-x:auto; }
.c12 { grid-column:span 12; } .c8 { grid-column:span 8; }
.c6 { grid-column:span 6; } .c4 { grid-column:span 4; }
@media (max-width:920px) { .panel { grid-column:span 12 !important; } }
table { border-collapse:collapse; width:100%; font-size:.82rem; }
th { text-align:left; color:var(--dim); font-weight:600; font-size:.68rem;
  text-transform:uppercase; letter-spacing:.05em; padding:.32rem .5rem;
  border-bottom:1px solid var(--border); }
td { padding:.32rem .5rem; border-bottom:1px solid var(--border); vertical-align:top; }
tr:last-child td { border-bottom:none; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.low { color:var(--bad); font-weight:bold; text-align:right; }
.muted { color:var(--dim); }
.marker { color:var(--accent); font-weight:bold; }
.warn { color:var(--warn); } .bad { color:var(--bad); } .ok { color:var(--ok); }
.dot { display:inline-block; width:.55rem; height:.55rem; border-radius:50%;
  margin-right:.45rem; vertical-align:middle; }
.dot.s-on { background:var(--ok); }
.dot.s-idle { background:var(--off); }
.dot.s-off { background:transparent; border:1px solid var(--off); }
.barbg { background:var(--border); border-radius:2px; height:.55rem; width:100%; }
.bar { background:var(--accent); border-radius:2px; height:.55rem; }
.feed { max-height:24rem; overflow-y:auto; }
details { margin:.3rem 0; }
summary { cursor:pointer; padding:.25rem 0; }
details table { margin:.4rem 0 .7rem; max-width:460px; }
""".strip()

# llm_spend activity -> conceptual agent + its Goal key + the deterministic mode
_WORKERS = (
    ("evaluator", "scoring", "evaluator", "evaluate", "keyword", "llm"),
    ("researcher", "due diligence", "research", "research", "off", "llm"),
    ("planner", "validation plan", "planner", "plan", "template", "llm"),
    ("offer proposer", "first offer", "proposer", "offer", "template", "llm"),
    ("decision policy", "orchestration", "decision_policy", "decide", "rules", "llm"),
)
_MARKERS = ("session_start", "session_end")
_STATE_CLS = {"on": "s-on", "idle": "s-idle", "off": "s-off"}


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def _num(value: object) -> str:
    return _esc(value)


# --- header -------------------------------------------------------------

def _session_line(session: dict | None) -> str:
    """Plain-text session state. Same substrings the tests assert on."""
    if not session:
        return "&mdash;"
    t, c = session.get("ticks", 0), session.get("cycles", 0)
    if session.get("ended_at"):
        return (
            f"session ended: {_esc(session.get('end_reason', 'stopped'))} "
            f"after {_num(t)} tick(s), {_num(c)} cycle(s)"
        )
    return (
        f"session running: {_num(t)} tick(s), {_num(c)} cycle(s), "
        f"last {_esc(session.get('last_tick_at', ''))}"
    )


def _header(generated_at: str, session: dict | None, report: dict,
            goal: dict | None) -> str:
    goal = goal or {}
    spend = report.get("llm_spend") or {}
    total = spend.get("total_cost_usd", 0.0)
    cap = spend.get("cap_usd")
    budget = f"${_num(total)} / ${_num(cap)}" if cap is not None else f"${_num(total)}"
    return (
        "<div class='hdr'>"
        "<h1>AI-Revenue-OS &mdash; command center</h1>"
        f"<span><span class='k'>generated</span>{_esc(generated_at)}</span>"
        f"<span><span class='k'>session</span>{_session_line(session)}</span>"
        f"<span><span class='k'>model</span>{_esc(goal.get('model', 'claude-sonnet-5'))}"
        "</span>"
        f"<span><span class='k'>llm spend</span>{budget}</span>"
        "</div>"
    )


# --- agents -----------------------------------------------------------

def _agent_roster(agent_log: list[dict], spend_entries: list[dict] | None,
                  goal: dict | None) -> str:
    goal = goal or {}
    decisions = [e for e in (agent_log or []) if e.get("action") not in _MARKERS]
    by_act: dict[str, list[dict]] = {}
    for e in (spend_entries or []):
        by_act.setdefault(e.get("activity"), []).append(e)

    if not decisions and not by_act:
        return "<p class='muted'>No agent activity yet.</p>"

    def _row(name, role, state, last, runs, calls, cost, note):
        return (
            f"<tr><td><span class='dot {_STATE_CLS[state]}'></span>{_esc(name)}</td>"
            f"<td class='muted'>{_esc(role)}</td>"
            f"<td>{_esc(state)}</td>"
            f"<td class='muted'>{_esc(last)}</td>"
            f"<td class='num'>{_num(runs)}</td>"
            f"<td class='num'>{_esc(calls)}</td>"
            f"<td class='num'>{_esc(cost)}</td>"
            f"<td>{note}</td></tr>"
        )

    rows = ""
    # operator / CEO - always the coordinator
    if decisions:
        last = decisions[-1]
        note = f"{_esc(last.get('action', ''))} &mdash; {_esc(last.get('reason', ''))}"
        rows += _row("operator (CEO)", "coordinator", "on", last.get("ts", ""),
                     len(decisions), "-", "-", note)
    else:
        rows += _row("operator (CEO)", "coordinator", "idle", "", 0, "-", "-",
                     "<span class='muted'>no decisions yet</span>")

    # discovery - deterministic, part of every discover step
    disc = [e for e in decisions if e.get("action") == "discover"]
    src = ", ".join(goal.get("sources", ["static"])) if goal else "static"
    rows += _row("discovery", "signal intake", "on" if disc else "idle",
                 disc[-1]["ts"] if disc else "", len(disc), "-", "-",
                 f"sources: {_esc(src)}")

    for name, role, gkey, activity, det, on_val in _WORKERS:
        entries = by_act.get(activity, [])
        runs = len(entries)
        mode = goal.get(gkey) if goal else None
        if runs:
            state = "on"
        elif mode is not None:
            state = "idle" if mode == on_val else "off"
        else:
            state = "idle"
        calls = sum(int(e.get("api_calls", 0)) for e in entries)
        cost = round(sum(float(e.get("cost_usd", 0.0)) for e in entries), 4)
        hits = sum(int(e.get("cache_hits", 0)) for e in entries)
        misses = sum(int(e.get("cache_misses", 0)) for e in entries)
        parts = []
        if mode is not None:
            parts.append(f"mode: {_esc(mode)}")
        if runs:
            parts.append(f"cache {_num(hits)}/{_num(hits + misses)}")
        note = " &nbsp; ".join(parts) or "<span class='muted'>&mdash;</span>"
        rows += _row(name, role, state, entries[-1].get("ts", "") if entries else "",
                     runs, calls if runs else "-",
                     f"${_num(cost)}" if runs else "-", note)

    return (
        "<table><tr><th>agent</th><th>role</th><th>state</th><th>last active</th>"
        "<th>runs</th><th>calls</th><th>cost</th><th>note</th></tr>"
        f"{rows}</table>"
    )


def _decision_feed(agent_log: list[dict], limit: int = 20) -> str:
    entries = list(reversed((agent_log or [])[-limit:]))
    if not entries:
        return "<p class='muted'>No agent decisions recorded.</p>"
    rows = ""
    for e in entries:
        action = e.get("action", "")
        cls = " class='marker'" if action in _MARKERS else ""
        rows += (
            f"<tr><td class='muted'>{_esc(e.get('ts', ''))}</td>"
            f"<td{cls}>{_esc(action)}</td>"
            f"<td>{_esc(e.get('reason', ''))}</td></tr>"
        )
    return (
        "<div class='feed'><table><tr><th>when</th><th>action</th><th>reason</th></tr>"
        f"{rows}</table></div>"
    )


# --- pipeline -------------------------------------------------------

def _funnel(status_counts: dict) -> str:
    mx = max(status_counts.values()) if status_counts else 0
    mx = mx or 1
    rows = ""
    for status in lifecycle.STATUSES:
        n = status_counts.get(status, 0)
        pct = int(round(100 * n / mx))
        rows += (
            f"<tr><td>{_esc(status)}</td>"
            f"<td class='num'>{_num(n)}</td>"
            f"<td style='width:65%'><div class='barbg'>"
            f"<div class='bar' style='width:{pct}%'></div></div></td></tr>"
        )
    return f"<table><tr><th>status</th><th>n</th><th></th></tr>{rows}</table>"


def _action_queue(queue: list[dict]) -> str:
    if not queue:
        return "<p class='muted'>Nothing awaiting a human.</p>"
    any_research = any(i.get("researched") for i in queue)
    rows = ""
    for item in queue:
        flag = "<span class='bad'>stale</span>" if item.get("stale") else ""
        res = ""
        if any_research and item["status"] == "shortlisted":
            res = (
                f" <span class='muted'>[researched:{_esc(item['researched'])}]</span>"
                if item.get("researched")
                else " <span class='muted'>[not researched]</span>"
            )
        rows += (
            f"<tr><td>{_esc(item['name'])}{res}</td>"
            f"<td>{_esc(item['status'])}</td>"
            f"<td class='num'>{_num(item.get('age_days', 0))}d {flag}</td>"
            f"<td>{_esc(item['next_action'])}</td></tr>"
        )
    n_stale = sum(1 for i in queue if i.get("stale"))
    cap = f"<p>{len(queue)} awaiting a human"
    cap += f", {n_stale} stale</p>" if n_stale else "</p>"
    return cap + (
        "<table><tr><th>candidate</th><th>status</th><th>age</th>"
        f"<th>next action</th></tr>{rows}</table>"
    )


_DISCOVERY_FIELDS = (
    "source", "limit", "fetched", "filtered_out", "dropped_below_score",
    "evaluated", "kept", "new", "refreshed", "shortlisted",
    "evaluator", "est_cost_usd", "actual_cost_usd", "cost_ceiling_hit",
    "eval_cache_hits", "eval_cache_misses", "calibrated", "weights_applied",
)


def _last_discovery(entry: dict | None) -> str:
    if not entry:
        return "<p class='muted'>No discovery run recorded.</p>"
    rows = "".join(
        f"<tr><td>{_esc(field)}</td>"
        f"<td class='num'>{_esc(entry.get(field, ''))}</td></tr>"
        for field in _DISCOVERY_FIELDS
        if field in entry
    )
    return (
        f"<p class='muted'>{_esc(entry.get('ts', ''))}</p>"
        f"<table><tr><th>field</th><th>value</th></tr>{rows}</table>"
    )


def _llm_spend_section(spend: dict | None) -> str:
    if not spend or spend.get("runs", 0) == 0:
        return "<p class='muted'>No LLM runs recorded.</p>"
    by = spend["by_activity"]
    mx = max(by.values()) or 1
    cap_html = ""
    if "cap_usd" in spend:
        cap_html = (
            f"<p>cap <strong>${_num(spend['cap_usd'])}</strong> &nbsp; "
            f"remaining <strong>${_num(spend['remaining_usd'])}</strong></p>"
        )
    rows = ""
    for a in by:
        pct = int(round(100 * by[a] / mx))
        rows += (
            f"<tr><td>{_esc(a)}</td><td class='num'>${_num(by[a])}</td>"
            f"<td style='width:55%'><div class='barbg'>"
            f"<div class='bar' style='width:{pct}%'></div></div></td></tr>"
        )
    return (
        f"<p>total <strong>${_num(spend['total_cost_usd'])}</strong> over "
        f"{_num(spend['runs'])} run(s), {_num(spend['total_api_calls'])} api call(s)</p>"
        f"{cap_html}"
        f"<table><tr><th>activity</th><th>cost</th><th></th></tr>{rows}</table>"
    )


def _outcomes_section(retro: dict | None) -> str:
    retro = retro or {}
    counts = retro.get("counts", {})
    have = counts.get("validated", 0) + counts.get("rejected", 0)
    if not retro.get("ready"):
        return f"<p class='muted'>Need more recorded outcomes; have {have}.</p>"
    tot = retro["total"]
    head = (
        f"<p>validated <strong>{_num(counts['validated'])}</strong> &nbsp; "
        f"rejected <strong>{_num(counts['rejected'])}</strong> &nbsp; "
        f"avg score {_num(tot['validated_avg'])} vs {_num(tot['rejected_avg'])}</p>"
    )
    weights = retro.get("weights")
    rows = ""
    for name in CRITERIA:
        c = retro["by_criterion"][name]
        w = "-" if weights is None else _num(weights.get(name, ""))
        rows += (
            f"<tr><td>{_esc(name)}</td>"
            f"<td class='num'>{_num(c['validated_avg'])}</td>"
            f"<td class='num'>{_num(c['rejected_avg'])}</td>"
            f"<td class='num'>{_num(c['gap'])}</td>"
            f"<td class='num'>{w}</td></tr>"
        )
    return head + (
        "<table><tr><th>criterion</th><th>validated</th><th>rejected</th>"
        f"<th>gap</th><th>weight</th></tr>{rows}</table>"
    )


def _breakdown_table(breakdown: dict) -> str:
    if not breakdown:
        return "<p class='muted'>No score breakdown.</p>"
    rows = ""
    for name in CRITERIA:
        value = breakdown.get(name)
        if value is None:
            continue
        low = float(value) < NEUTRAL_SCORE
        cell = (
            f"<td class='low'>{_num(value)} &lt;</td>" if low
            else f"<td class='num'>{_num(value)}</td>"
        )
        rows += f"<tr><td>{_esc(name)}</td>{cell}</tr>"
    return f"<table><tr><th>criterion</th><th>score</th></tr>{rows}</table>"


def _candidate_blocks(candidates: list[dict]) -> str:
    if not candidates:
        return "<p class='muted'>No candidates.</p>"
    blocks = ""
    for c in candidates:
        summary = (
            f"{_esc(c['name'])} &mdash; {_esc(c['status'])} &mdash; "
            f"{_num(c['score'])} ({_esc(c['verdict'])}) "
            f"[{_esc(c.get('estimate_source', 'keyword'))}]"
        )
        rationale = c.get("rationale", "")
        rationale_html = (
            f"<p>{_esc(rationale)}</p>" if rationale
            else "<p class='muted'>No rationale.</p>"
        )
        research = c.get("research") or {}
        research_html = ""
        if research.get("verdict"):
            research_html = (
                f"<p><strong>research: {_esc(research['verdict'])}</strong> &mdash; "
                f"{_esc(research.get('rationale', ''))} "
                f"<span class='muted'>({_esc(research.get('basis', ''))})</span></p>"
            )
        budget_html = ""
        if c.get("plan_needs_budget"):
            budget_html = (
                f"<p class='warn'><strong>validation needs a budget decision: "
                f"~${_num(c.get('plan_max_cost', 0.0))}</strong></p>"
            )
        offer = c.get("offer") or {}
        offer_html = ""
        if offer.get("what_is_sold"):
            pos = offer.get("positioning", "")
            offer_html = (
                f"<p>Offer: {_esc(offer['what_is_sold'])} &mdash; "
                f"{_num(offer.get('price', 0))} {_esc(offer.get('currency', 'USD'))}"
                + (f" &mdash; {_esc(pos)}" if pos else "")
                + "</p>"
            )
        blocks += (
            f"<details><summary>{summary}</summary>{rationale_html}{research_html}"
            f"{budget_html}{offer_html}"
            f"{_breakdown_table(c.get('breakdown', {}))}</details>"
        )
    return blocks


def _roi_section(roi: dict) -> str:
    totals = (
        f"<p>revenue <strong>{_num(roi['grand_revenue'])}</strong> &nbsp; "
        f"spent <strong>{_num(roi['grand_spent'])}</strong> &nbsp; "
        f"net <strong>{_num(roi['grand_net'])}</strong></p>"
    )
    per = roi.get("candidates", {})
    if not per:
        return totals + (
            "<p class='muted'>No revenue recorded yet &mdash; ROI stays $0 "
            "until a real payment is logged.</p>"
        )
    rows = "".join(
        f"<tr><td>{_esc(name)}</td><td>{_esc(row['status'])}</td>"
        f"<td class='num'>{_num(row['revenue'])}</td>"
        f"<td class='num'>{_num(row['budget'])}</td>"
        f"<td class='num'>{_num(row['authorized'])}</td>"
        f"<td class='num'>{_num(row['spent'])}</td>"
        f"<td class='num'>{_num(row['net'])}</td>"
        f"<td class='num'>{_esc('-' if row['roi_ratio'] is None else row['roi_ratio'])}</td>"
        "</tr>"
        for name, row in sorted(per.items())
    )
    header = (
        "<tr><th>candidate</th><th>status</th><th>revenue</th><th>budget</th>"
        "<th>authorized</th><th>spent</th><th>net</th><th>roi</th></tr>"
    )
    return totals + f"<table>{header}{rows}</table>"


def _panel(cls: str, title: str, inner: str) -> str:
    return f"<div class='panel {cls}'><h2>{title}</h2>{inner}</div>"


def render_html(report: dict, generated_at: str, *,
                agent_log: list[dict] | None = None,
                session: dict | None = None,
                spend_entries: list[dict] | None = None,
                goal: dict | None = None) -> str:
    """Build the full standalone command-center HTML document."""
    body = (
        _header(generated_at, session, report, goal)
        + "<div class='grid'>"
        + _panel("c12", "Agents",
                 _agent_roster(agent_log or [], spend_entries, goal))
        + _panel("c8", "Pipeline", _funnel(report["status_counts"]))
        + _panel("c4", "Action queue", _action_queue(report["action_queue"]))
        + _panel("c6", "Agent decisions", _decision_feed(agent_log or []))
        + _panel("c6", "LLM spend", _llm_spend_section(report.get("llm_spend")))
        + _panel("c6", "Last discovery", _last_discovery(report.get("last_discovery")))
        + _panel("c6", "Outcomes", _outcomes_section(report.get("outcomes")))
        + _panel("c12", "Candidates", _candidate_blocks(report.get("candidates", [])))
        + _panel("c12", "ROI", _roi_section(report["roi"]))
        + "</div>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>AI-Revenue-OS command center</title>\n"
        f"<style>\n{_STYLE}\n</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )
