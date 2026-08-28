"""Static HTML command center for the multi-agent Revenue OS.

render_html() is pure: a report dict (from report.pipeline_report), a
timestamp, and the persisted agent / session / spend / goal state in -
one self-contained HTML document out. Inline CSS + inline SVG, no
JavaScript, no external requests, no external images.

Every value shown is traceable to a file on disk (goal.json,
agent_log.json, agent_session.json, llm_spend.json, the candidate
store). Nothing is invented - no fake agents, revenue, progress,
uptime, or activity. All external text is HTML-escaped.
"""

from __future__ import annotations

from html import escape

from . import lifecycle
from .opportunity import CRITERIA
from .report import NEUTRAL_SCORE

# ---------------------------------------------------------------------------
# theme
# ---------------------------------------------------------------------------

_STYLE = """
:root{
  --bg:#070b14; --bg2:#0a1120; --surface:#0d1526; --surface2:#101c30;
  --edge:#1c2c47; --edge-hi:#2a4166; --text:#c9d6ea; --dim:#63779b;
  --glow:#22d3ee; --good:#34d399; --warn:#fbbf24; --bad:#f87171;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#eef2f8; --bg2:#e7ecf4; --surface:#ffffff; --surface2:#f4f7fb;
    --edge:#d4ddea; --edge-hi:#b9c7db; --text:#1b2536; --dim:#5b6b85;
    --glow:#0891b2; --good:#0f9d63; --warn:#a06a00; --bad:#d64545;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:13px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:var(--glow);text-decoration:none}
h2{font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
  margin:0 0 .6rem;font-weight:600}
p{margin:.35rem 0}

.shell{display:grid;grid-template-columns:180px 1fr;min-height:100vh}
.rail{background:var(--bg2);border-right:1px solid var(--edge);padding:1rem .7rem;
  position:sticky;top:0;height:100vh;overflow:auto}
.brand{font-size:.72rem;letter-spacing:.2em;text-transform:uppercase;color:var(--glow);
  margin-bottom:1.4rem;font-weight:700}
.nav a,.nav span{display:flex;align-items:center;gap:.55rem;padding:.4rem .5rem;
  border-radius:6px;color:var(--dim);font-size:.8rem;margin-bottom:.15rem}
.nav a:hover{background:var(--surface);color:var(--text)}
.nav span{opacity:.4}
.nav svg{width:14px;height:14px;flex:none}

main{padding:1.1rem 1.3rem 2.5rem;max-width:1500px}

.topbar{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:.7rem;margin-bottom:1rem}
.metric{background:var(--surface);border:1px solid var(--edge);border-radius:8px;
  padding:.7rem .8rem;box-shadow:inset 0 0 24px -12px var(--glow)}
.metric .k{font-size:.6rem;letter-spacing:.13em;text-transform:uppercase;color:var(--dim)}
.metric .v{font-size:1.05rem;font-weight:600;margin-top:.2rem}
.metric .s{font-size:.72rem;color:var(--dim);margin-top:.1rem}

.attention{background:linear-gradient(90deg,rgba(251,191,36,.14),transparent);
  border:1px solid var(--warn);border-left:3px solid var(--warn);border-radius:8px;
  padding:.7rem .9rem;margin-bottom:1rem}
.attention .k{font-size:.62rem;letter-spacing:.13em;text-transform:uppercase;
  color:var(--warn);font-weight:700}
.attention ul{margin:.35rem 0 0;padding-left:1.1rem}
.attention li{margin:.15rem 0}

section{background:var(--surface);border:1px solid var(--edge);border-radius:10px;
  padding:.95rem 1rem;margin-bottom:.9rem}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:.9rem}
.cols>section{margin-bottom:0}
@media (max-width:1000px){.cols{grid-template-columns:1fr}}
@media (max-width:820px){.shell{grid-template-columns:1fr}
  .rail{position:static;height:auto;display:flex;flex-wrap:wrap;gap:.3rem}
  .brand{width:100%}}

.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:.75rem}
.acard{background:var(--surface2);border:1px solid var(--edge);border-radius:10px;
  padding:.8rem;position:relative;overflow:hidden}
.acard::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--acc)}
.acard .top{display:flex;align-items:center;gap:.6rem;margin-bottom:.5rem}
.avatar{width:34px;height:34px;flex:none;border-radius:9px;display:grid;place-items:center;
  color:var(--acc);border:1px solid var(--edge-hi);
  background:radial-gradient(circle at 30% 30%,color-mix(in srgb,var(--acc) 22%,transparent),transparent 70%)}
.avatar svg{width:20px;height:20px}
.acard .name{font-weight:600;font-size:.86rem}
.acard .role{font-size:.68rem;color:var(--dim);letter-spacing:.04em;text-transform:uppercase}
.acard .task{font-size:.78rem;color:var(--text);margin:.25rem 0 .45rem;min-height:1.1rem;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acard .task.none{color:var(--dim)}
.acard .meta{display:flex;flex-wrap:wrap;gap:.15rem .8rem;font-size:.68rem;color:var(--dim);
  border-top:1px solid var(--edge);padding-top:.4rem;margin-top:.1rem}
.acard .meta b{color:var(--text);font-weight:600}

.pill{display:inline-flex;align-items:center;gap:.35rem;font-size:.66rem;font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;padding:.18rem .45rem;border-radius:20px;
  border:1px solid var(--edge-hi);color:var(--dim)}
.dot{width:.5rem;height:.5rem;border-radius:50%;background:currentColor;flex:none}
.st-working{color:var(--glow);border-color:color-mix(in srgb,var(--glow) 50%,var(--edge))}
.st-working .dot{animation:pulse 1.6s ease-in-out infinite}
.st-active{color:var(--good);border-color:color-mix(in srgb,var(--good) 45%,var(--edge))}
.st-waiting{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,var(--edge))}
.st-idle{color:var(--dim)}
.st-deterministic{color:var(--dim)}
.st-disabled{color:var(--dim);opacity:.6}
.st-error{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 50%,var(--edge))}
.st-error .dot{animation:pulse 1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}

table{border-collapse:collapse;width:100%;font-size:.8rem}
th{text-align:left;color:var(--dim);font-weight:600;font-size:.62rem;letter-spacing:.06em;
  text-transform:uppercase;padding:.32rem .5rem;border-bottom:1px solid var(--edge)}
td{padding:.34rem .5rem;border-bottom:1px solid var(--edge);vertical-align:top}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.low{color:var(--bad);text-align:right;font-weight:600}
.muted{color:var(--dim)}
.marker{color:var(--glow);font-weight:700}
.warn{color:var(--warn)} .bad{color:var(--bad)} .good{color:var(--good)}
.bar-wrap{background:var(--edge);border-radius:3px;height:.5rem;overflow:hidden}
.bar{background:var(--glow);height:.5rem}
.feed{max-height:20rem;overflow:auto}
.progress{margin:.5rem 0}
.progress .lbl{display:flex;justify-content:space-between;font-size:.72rem;color:var(--dim);
  margin-bottom:.25rem}
details{margin:.3rem 0}
summary{cursor:pointer;padding:.28rem 0}
details table{margin:.4rem 0 .7rem;max-width:460px}
""".strip()

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_MARKERS = ("session_start", "session_end")


def _esc(v: object) -> str:
    return escape(str(v), quote=True)


def _num(v: object) -> str:
    return _esc(v)


# ---------------------------------------------------------------------------
# agent visual identities  (label / role / accent / avatar SVG)
# ---------------------------------------------------------------------------

def _svg(paths: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
        f"{paths}</svg>"
    )


_AVATARS = {
    "operator": _svg('<path d="M12 3v3m0 12v3m9-9h-3M6 12H3"/>'
                     '<circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1.2"/>'),
    "discovery": _svg('<circle cx="12" cy="12" r="3"/><path d="M12 3a9 9 0 0 1 9 9"/>'
                      '<path d="M12 7a5 5 0 0 1 5 5"/><path d="M12 12 20 8"/>'),
    "evaluator": _svg('<path d="M4 20h16"/><rect x="5" y="12" width="3" height="6"/>'
                      '<rect x="10.5" y="8" width="3" height="10"/>'
                      '<rect x="16" y="4" width="3" height="14"/>'),
    "researcher": _svg('<circle cx="10" cy="10" r="6"/><path d="m20 20-5.5-5.5"/>'),
    "planner": _svg('<rect x="6" y="4" width="12" height="16" rx="1.5"/>'
                    '<path d="M9 3h6v3H9z"/><path d="M9 11h6M9 15h4"/>'),
    "offer": _svg('<path d="m20.5 12.5-8-8H5.5v7l8 8a2 2 0 0 0 2.8 0l4.2-4.2a2 2 0 0 0 0-2.8Z"/>'
                  '<circle cx="9" cy="9" r="1.3"/>'),
    "decision": _svg('<circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/>'
                     '<circle cx="12" cy="18" r="2"/><path d="M6 8v3a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V8"/>'
                     '<path d="M12 14v2"/>'),
    "generic": _svg('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>'),
}
_ACCENT = {
    "operator": "#22d3ee", "discovery": "#38bdf8", "evaluator": "#a78bfa",
    "researcher": "#34d399", "planner": "#fbbf24", "offer": "#f472b6",
    "decision": "#f87171", "generic": "#94a3b8",
}

# llm_spend activity  ->  (agent key, display name, role, Goal field, llm-mode value)
_WORKERS = (
    ("evaluate", "evaluator", "Evaluator", "Scoring",        "evaluator",       "llm"),
    ("research", "researcher", "Research Agent", "Due diligence", "research",   "llm"),
    ("plan",     "planner",    "Planner",   "Validation plan", "planner",       "llm"),
    ("offer",    "offer",      "Offer Agent", "First offer",  "proposer",       "llm"),
    ("decide",   "decision",   "Decision Policy", "Orchestration", "decision_policy", "llm"),
)

_STATUS_TEXT = {
    "working": "WORKING", "active": "ACTIVE", "waiting": "WAITING FOR HUMAN",
    "idle": "IDLE", "deterministic": "DETERMINISTIC", "disabled": "DISABLED",
    "error": "BLOCKED (COST)",
}


def _pill(status: str) -> str:
    return (
        f"<span class='pill st-{status}'><span class='dot'></span>"
        f"{_STATUS_TEXT.get(status, status.upper())}</span>"
    )


def _avatar(key: str) -> str:
    return (
        f"<div class='avatar' style=\"--acc:{_ACCENT.get(key, _ACCENT['generic'])}\">"
        f"{_AVATARS.get(key, _AVATARS['generic'])}</div>"
    )


def _agent_card(*, key, name, role, status, task, meta) -> str:
    acc = _ACCENT.get(key, _ACCENT["generic"])
    task_cls = " none" if not task else ""
    meta_html = "".join(
        f"<span>{k} <b>{v}</b></span>" for k, v in meta if v not in (None, "")
    )
    return (
        f"<div class='acard' style=\"--acc:{acc}\">"
        f"<div class='top'>{_avatar(key)}"
        f"<div><div class='name'>{_esc(name)}</div>"
        f"<div class='role'>{_esc(role)}</div></div></div>"
        f"<div class='task{task_cls}'>{task or 'No current task'}</div>"
        f"{_pill(status)}"
        f"<div class='meta'>{meta_html}</div>"
        "</div>"
    )


def _agents(agent_log, spend_entries, goal, session, queue_open: bool = False) -> str:
    goal = goal or {}
    decisions = [e for e in (agent_log or []) if e.get("action") not in _MARKERS]
    running = bool(session) and not session.get("ended_at")
    by_act: dict[str, list[dict]] = {}
    for e in (spend_entries or []):
        by_act.setdefault(e.get("activity"), []).append(e)

    if not decisions and not by_act:
        return "<p class='muted'>No agent activity yet.</p>"

    cards = []

    # --- operator (CEO) -------------------------------------------------
    if decisions:
        last = decisions[-1]
        note = f"{_esc(last.get('action', ''))} &mdash; {_esc(last.get('reason', ''))}"
        if running:
            op_status = "working"
        elif last.get("action") == "stop" and queue_open:
            op_status = "waiting"
        else:
            op_status = "idle"
        cards.append(_agent_card(
            key="operator", name="operator (CEO)", role="Coordinator",
            status=op_status, task=note,
            meta=[("decisions", len(decisions)), ("last", _esc(last.get("ts", "")))],
        ))
    else:
        cards.append(_agent_card(
            key="operator", name="operator (CEO)", role="Coordinator",
            status="idle", task="", meta=[("decisions", 0)],
        ))

    # --- discovery (deterministic, always available) ------------------
    disc = [e for e in decisions if e.get("action") == "discover"]
    src = ", ".join(goal.get("sources", ["static"])) if goal else "static"
    cards.append(_agent_card(
        key="discovery", name="Discovery", role="Signal intake",
        status="active" if (running and disc) else ("idle"),
        task=f"sources: {_esc(src)}",
        meta=[("runs", len(disc)),
              ("last", _esc(disc[-1]["ts"]) if disc else "&mdash;"),
              ("filter", "on" if goal.get("filter") else "off")],
    ))

    # --- the configurable workers ------------------------------------
    for activity, key, name, role, gkey, on_val in _WORKERS:
        entries = by_act.get(activity, [])
        runs = len(entries)
        mode = goal.get(gkey) if goal else None
        ceiling = bool(entries and entries[-1].get("ceiling_hit"))
        if ceiling:
            status = "error"
        elif runs:
            status = "working" if running else "active"
        elif mode == "off":
            status = "disabled"
        elif mode is not None and mode != on_val:
            status = "deterministic"
        else:
            status = "idle"
        calls = sum(int(e.get("api_calls", 0)) for e in entries)
        cost = round(sum(float(e.get("cost_usd", 0.0)) for e in entries), 4)
        hits = sum(int(e.get("cache_hits", 0)) for e in entries)
        misses = sum(int(e.get("cache_misses", 0)) for e in entries)
        task = f"mode: {_esc(mode)}" if mode is not None else ""
        if runs:
            task += f" &mdash; {calls} call(s)"
        meta = [("runs", runs)]
        if runs:
            meta += [("cost", f"${_num(cost)}"),
                     ("cache", f"{hits}/{hits + misses}")]
        if mode is not None:
            meta.append(("enabled", "yes" if mode == on_val else "no"))
        cards.append(_agent_card(key=key, name=name, role=role,
                                 status=status, task=task, meta=meta))

    return f"<div class='agrid'>{''.join(cards)}</div>"


# ---------------------------------------------------------------------------
# top bar + attention
# ---------------------------------------------------------------------------

def _session_line(session: dict | None) -> str:
    if not session:
        return "no session"
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


def _metric(k: str, v: str, s: str = "") -> str:
    sub = f"<div class='s'>{s}</div>" if s else ""
    return f"<div class='metric'><div class='k'>{k}</div><div class='v'>{v}</div>{sub}</div>"


def _topbar(report, session, agent_log, spend_entries, goal) -> str:
    goal = goal or {}
    counts = report["status_counts"]
    queue = report["action_queue"]
    spend = report.get("llm_spend") or {}
    roi = report["roi"]

    beyond = counts["validated"] + counts["launched"] + counts["earning"]
    tv = goal.get("target_validated")
    goal_v = f"{beyond} / {tv} validated" if tv else "continuous discovery"
    llm_on = [n for a, k, n, r, g, o in _WORKERS if goal.get(g) == o]
    if goal.get("evaluator") == "llm" and "Evaluator" not in llm_on:
        pass
    goal_s = ("LLM: " + ", ".join(llm_on)) if llm_on else "deterministic workers"

    decisions = [e for e in (agent_log or []) if e.get("action") not in _MARKERS]
    by_act = {e.get("activity") for e in (spend_entries or [])}
    active = 1 if decisions else 0
    active += 1 if any(e.get("action") == "discover" for e in decisions) else 0
    active += len(by_act)

    n_stale = sum(1 for i in queue if i.get("stale"))
    q_sub = f"{n_stale} stale" if n_stale else "nothing overdue"

    total = spend.get("total_cost_usd", 0.0)
    cap = spend.get("cap_usd")
    spend_v = f"${_num(total)}"
    spend_s = (f"of ${_num(cap)} cap &middot; ${_num(spend.get('remaining_usd', ''))} left"
               if cap is not None else f"{spend.get('total_api_calls', 0)} calls")

    rev = roi["grand_revenue"]
    rev_v = f"${_num(rev)}" if rev else "none recorded"
    rev_s = f"net ${_num(roi['grand_net'])}" if rev else "logged from real payments only"

    return (
        "<div class='topbar'>"
        + _metric("Main goal", _esc(goal_v), _esc(goal_s))
        + _metric("Session", _esc(_session_line(session)))
        + _metric("Active agents", str(active), "with recorded activity this session")
        + _metric("Awaiting you", str(len(queue)), q_sub)
        + _metric("LLM spend", spend_v, spend_s)
        + _metric("Revenue", rev_v, rev_s)
        + "</div>"
    )


def _attention(queue: list[dict]) -> str:
    if not queue:
        return ""
    items = "".join(
        f"<li>{_esc(i['next_action'])} &mdash; <span class='mono'>{_esc(i['name'])}</span>"
        + (" <span class='bad'>(stale)</span>" if i.get("stale") else "")
        + "</li>"
        for i in queue
    )
    return (
        "<div class='attention'><div class='k'>Human action required</div>"
        f"<ul>{items}</ul></div>"
    )


# ---------------------------------------------------------------------------
# lower panels
# ---------------------------------------------------------------------------

_ACT_AGENT = {
    "discover": "discovery", "investigate": "planner",
    "prepare_launch": "offer", "research": "researcher", "stop": "operator",
}


def _task_queue(queue: list[dict]) -> str:
    if not queue:
        return "<p class='muted'>Nothing awaiting a human.</p>"
    rows = ""
    for i in queue:
        flag = " <span class='bad'>stale</span>" if i.get("stale") else ""
        res = ""
        if i["status"] == "shortlisted" and i.get("researched"):
            res = f" <span class='muted'>[researched:{_esc(i['researched'])}]</span>"
        rows += (
            f"<tr><td>{_esc(i['name'])}{res}</td><td>{_esc(i['status'])}</td>"
            f"<td class='num'>{_num(i.get('age_days', 0))}d{flag}</td>"
            f"<td>{_esc(i['next_action'])}</td><td class='muted'>you</td></tr>"
        )
    return (
        "<p class='muted'>Agent tasks run synchronously inside each cycle; these are "
        "the units of work now waiting on you.</p>"
        "<table><tr><th>candidate</th><th>stage</th><th>age</th>"
        f"<th>next action</th><th>owner</th></tr>{rows}</table>"
    )


def _activity(agent_log: list[dict], limit: int = 20) -> str:
    entries = list(reversed((agent_log or [])[-limit:]))
    if not entries:
        return "<p class='muted'>No agent decisions recorded.</p>"
    rows = ""
    for e in entries:
        action = e.get("action", "")
        cls = " class='marker'" if action in _MARKERS else ""
        who = _ACT_AGENT.get(action, "operator")
        rows += (
            f"<tr><td class='muted mono'>{_esc(e.get('ts', ''))}</td>"
            f"<td{cls}>{_esc(action)}</td>"
            f"<td class='muted'>{_esc(who)}</td>"
            f"<td>{_esc(e.get('reason', ''))}</td></tr>"
        )
    return (
        "<div class='feed'><table><tr><th>when</th><th>action</th><th>agent</th>"
        f"<th>reason</th></tr>{rows}</table></div>"
    )


def _pipeline(status_counts: dict) -> str:
    mx = max(status_counts.values() or [0]) or 1
    rows = ""
    for s in lifecycle.STATUSES:
        n = status_counts.get(s, 0)
        pct = int(round(100 * n / mx))
        rows += (
            f"<tr><td>{_esc(s)}</td><td class='num'>{_num(n)}</td>"
            f"<td style='width:60%'><div class='bar-wrap'>"
            f"<div class='bar' style='width:{pct}%'></div></div></td></tr>"
        )
    return f"<table><tr><th>stage</th><th>n</th><th></th></tr>{rows}</table>"


def _progress(label: str, done: float, total: float) -> str:
    total = total or 1
    pct = max(0, min(100, int(round(100 * done / total))))
    return (
        f"<div class='progress'><div class='lbl'><span>{_esc(label)}</span>"
        f"<span>{_num(done)} / {_num(total)}</span></div>"
        f"<div class='bar-wrap'><div class='bar' style='width:{pct}%'></div></div></div>"
    )


def _spend(spend: dict | None) -> str:
    if not spend or spend.get("runs", 0) == 0:
        return "<p class='muted'>No LLM runs recorded.</p>"
    by = spend["by_activity"]
    mx = max(by.values() or [0]) or 1
    budget = ""
    if "cap_usd" in spend:
        budget = _progress("Budget used", spend["total_cost_usd"], spend["cap_usd"])
    rows = ""
    for a in by:
        pct = int(round(100 * by[a] / mx))
        rows += (
            f"<tr><td>{_esc(a)}</td><td class='num'>${_num(by[a])}</td>"
            f"<td style='width:55%'><div class='bar-wrap'>"
            f"<div class='bar' style='width:{pct}%'></div></div></td></tr>"
        )
    return (
        f"<p>total <b>${_num(spend['total_cost_usd'])}</b> &middot; "
        f"{_num(spend['runs'])} run(s) &middot; "
        f"{_num(spend['total_api_calls'])} api call(s)</p>{budget}"
        f"<table><tr><th>activity</th><th>cost</th><th></th></tr>{rows}</table>"
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
        f"<tr><td>{_esc(f)}</td><td class='num'>{_esc(entry.get(f, ''))}</td></tr>"
        for f in _DISCOVERY_FIELDS if f in entry
    )
    return (
        f"<p class='muted mono'>{_esc(entry.get('ts', ''))}</p>"
        f"<table><tr><th>field</th><th>value</th></tr>{rows}</table>"
    )


def _outcomes(retro: dict | None) -> str:
    retro = retro or {}
    counts = retro.get("counts", {})
    have = counts.get("validated", 0) + counts.get("rejected", 0)
    if not retro.get("ready"):
        return f"<p class='muted'>Need more recorded outcomes; have {have}.</p>"
    tot = retro["total"]
    weights = retro.get("weights")
    head = (
        f"<p>validated <b>{_num(counts['validated'])}</b> &middot; "
        f"rejected <b>{_num(counts['rejected'])}</b> &middot; "
        f"avg {_num(tot['validated_avg'])} vs {_num(tot['rejected_avg'])}</p>"
    )
    rows = ""
    for name in CRITERIA:
        c = retro["by_criterion"][name]
        w = "-" if weights is None else _num(weights.get(name, ""))
        rows += (
            f"<tr><td>{_esc(name)}</td><td class='num'>{_num(c['validated_avg'])}</td>"
            f"<td class='num'>{_num(c['rejected_avg'])}</td>"
            f"<td class='num'>{_num(c['gap'])}</td><td class='num'>{w}</td></tr>"
        )
    return head + (
        "<table><tr><th>criterion</th><th>validated</th><th>rejected</th>"
        f"<th>gap</th><th>weight</th></tr>{rows}</table>"
    )


def _breakdown(breakdown: dict) -> str:
    if not breakdown:
        return "<p class='muted'>No score breakdown.</p>"
    rows = ""
    for name in CRITERIA:
        v = breakdown.get(name)
        if v is None:
            continue
        low = float(v) < NEUTRAL_SCORE
        cell = (f"<td class='low'>{_num(v)} &lt;</td>" if low
                else f"<td class='num'>{_num(v)}</td>")
        rows += f"<tr><td>{_esc(name)}</td>{cell}</tr>"
    return f"<table><tr><th>criterion</th><th>score</th></tr>{rows}</table>"


def _candidates(candidates: list[dict]) -> str:
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
        rat = (f"<p>{_esc(rationale)}</p>" if rationale
               else "<p class='muted'>No rationale.</p>")
        research = c.get("research") or {}
        res = ""
        if research.get("verdict"):
            res = (
                f"<p><b>research: {_esc(research['verdict'])}</b> &mdash; "
                f"{_esc(research.get('rationale', ''))} "
                f"<span class='muted'>({_esc(research.get('basis', ''))})</span></p>"
            )
        budget = ""
        if c.get("plan_needs_budget"):
            budget = (
                f"<p class='warn'><b>validation needs a budget decision: "
                f"~${_num(c.get('plan_max_cost', 0.0))}</b></p>"
            )
        offer = c.get("offer") or {}
        off = ""
        if offer.get("what_is_sold"):
            pos = offer.get("positioning", "")
            off = (
                f"<p>Offer: {_esc(offer['what_is_sold'])} &mdash; "
                f"{_num(offer.get('price', 0))} {_esc(offer.get('currency', 'USD'))}"
                + (f" &mdash; {_esc(pos)}" if pos else "") + "</p>"
            )
        blocks += (
            f"<details><summary>{summary}</summary>{rat}{res}{budget}{off}"
            f"{_breakdown(c.get('breakdown', {}))}</details>"
        )
    return blocks


def _roi(roi: dict, report: dict, goal: dict | None) -> str:
    goal = goal or {}
    totals = (
        f"<p>revenue <b>{_num(roi['grand_revenue'])}</b> &middot; "
        f"spent <b>{_num(roi['grand_spent'])}</b> &middot; "
        f"net <b>{_num(roi['grand_net'])}</b></p>"
    )
    tv = goal.get("target_validated")
    prog = ""
    if tv:
        c = report["status_counts"]
        beyond = c["validated"] + c["launched"] + c["earning"]
        prog = _progress("Validated toward goal", min(beyond, tv), tv)
    per = roi.get("candidates", {})
    if not per:
        return totals + prog + "<p class='muted'>No revenue recorded yet.</p>"
    rows = "".join(
        f"<tr><td>{_esc(n)}</td><td>{_esc(r['status'])}</td>"
        f"<td class='num'>{_num(r['revenue'])}</td>"
        f"<td class='num'>{_num(r['budget'])}</td>"
        f"<td class='num'>{_num(r['authorized'])}</td>"
        f"<td class='num'>{_num(r['spent'])}</td>"
        f"<td class='num'>{_num(r['net'])}</td>"
        f"<td class='num'>{_esc('-' if r['roi_ratio'] is None else r['roi_ratio'])}</td></tr>"
        for n, r in sorted(per.items())
    )
    return totals + prog + (
        "<table><tr><th>candidate</th><th>status</th><th>revenue</th><th>budget</th>"
        "<th>authorized</th><th>spent</th><th>net</th><th>roi</th></tr>"
        f"{rows}</table>"
    )


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------

_NAV = (
    ("Dashboard", "#top", "operator"),
    ("Agents", "#agents", "generic"),
    ("Tasks", "#tasks", "planner"),
    ("Opportunities", "#opportunities", "discovery"),
    ("Finances", "#finances", "offer"),
    ("Automations", None, "decision"),
    ("Logs", "#activity", "researcher"),
    ("Settings", None, "evaluator"),
)


def _sidebar() -> str:
    items = ""
    for label, href, icon in _NAV:
        ic = _AVATARS.get(icon, _AVATARS["generic"])
        if href:
            items += f"<a href='{href}'>{ic}{_esc(label)}</a>"
        else:
            items += f"<span>{ic}{_esc(label)}</span>"
    return (
        "<nav class='rail'><div class='brand'>Revenue&nbsp;OS</div>"
        f"<div class='nav'>{items}</div></nav>"
    )


def render_html(report: dict, generated_at: str, *,
                agent_log: list[dict] | None = None,
                session: dict | None = None,
                spend_entries: list[dict] | None = None,
                goal: dict | None = None) -> str:
    """Build the full standalone command-center HTML document."""
    queue = report["action_queue"]
    body = (
        _sidebar()
        + "<main id='top'>"
        + f"<p class='muted mono' style='margin:.2rem 0 .8rem'>generated {_esc(generated_at)}</p>"
        + _topbar(report, session, agent_log, spend_entries, goal)
        + _attention(queue)
        + "<section id='agents'><h2>Agents</h2>"
        + _agents(agent_log or [], spend_entries, goal, session, bool(queue))
        + "</section>"
        + "<div class='cols'>"
        + "<section id='tasks'><h2>Task queue</h2>"
        + _task_queue(report["action_queue"]) + "</section>"
        + "<section id='activity'><h2>Recent activity</h2>"
        + _activity(agent_log or []) + "</section>"
        + "</div>"
        + "<div class='cols'>"
        + "<section id='opportunities'><h2>Pipeline</h2>"
        + _pipeline(report["status_counts"]) + "</section>"
        + "<section id='finances'><h2>LLM spend</h2>"
        + _spend(report.get("llm_spend")) + "</section>"
        + "</div>"
        + "<div class='cols'>"
        + "<section><h2>Last discovery</h2>"
        + _last_discovery(report.get("last_discovery")) + "</section>"
        + "<section><h2>Outcomes</h2>"
        + _outcomes(report.get("outcomes")) + "</section>"
        + "</div>"
        + "<section><h2>Revenue / ROI</h2>"
        + _roi(report["roi"], report, goal) + "</section>"
        + "<section><h2>Candidates</h2>"
        + _candidates(report.get("candidates", [])) + "</section>"
        + "</main>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Revenue OS &mdash; command center</title>\n"
        f"<style>\n{_STYLE}\n</style>\n</head>\n"
        f"<body>\n<div class='shell'>{body}</div>\n</body>\n</html>\n"
    )
