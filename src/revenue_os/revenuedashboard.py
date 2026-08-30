"""Static HTML command center for the multi-agent Revenue OS.

render_html() is pure: a report dict (from report.pipeline_report), a
timestamp, and the persisted agent / session / spend / goal state in -
one self-contained HTML document out. Inline CSS + inline SVG, no
JavaScript, no external requests, no external images.

Every value shown is traceable to a file on disk (goal.json,
agent_log.json, agent_session.json, llm_spend.json, acquisition.json,
outreach.json, revenue.json, the candidate store). Nothing is invented -
no fake agents, revenue, progress, uptime, or activity. The "first sale
readiness" panel is explicit about the one thing it cannot check without
an API call (the live PayPal payment path). All external text is
HTML-escaped; no prospect URLs are rendered.
"""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

from . import lifecycle, roster
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
  --grid:rgba(120,160,220,.06);
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#eef2f8; --bg2:#e7ecf4; --surface:#ffffff; --surface2:#f4f7fb;
    --edge:#d4ddea; --edge-hi:#b9c7db; --text:#1b2536; --dim:#5b6b85;
    --glow:#0891b2; --good:#0f9d63; --warn:#a06a00; --bad:#d64545;
    --grid:rgba(30,60,110,.05);
  }
}
*{box-sizing:border-box}
body{margin:0;background-color:var(--bg);color:var(--text);
  font:13px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  background-image:
    radial-gradient(1100px 560px at 82% -12%,color-mix(in srgb,var(--glow) 12%,transparent),transparent 62%),
    radial-gradient(820px 520px at -12% 112%,color-mix(in srgb,#a78bfa 10%,transparent),transparent 60%),
    linear-gradient(var(--grid) 1px,transparent 1px),
    linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:auto,auto,46px 46px,46px 46px;background-attachment:fixed}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:var(--glow);text-decoration:none}
h2{font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
  margin:0 0 .7rem;font-weight:600;display:flex;align-items:center;gap:.5rem}
h2::before{content:"";width:6px;height:6px;flex:none;background:var(--glow);
  transform:rotate(45deg);box-shadow:0 0 8px var(--glow)}
p{margin:.35rem 0}

.shell{display:grid;grid-template-columns:184px 1fr;min-height:100vh}
.rail{background:linear-gradient(180deg,var(--bg2),var(--bg));
  border-right:1px solid var(--edge);padding:1rem .7rem;box-shadow:1px 0 24px -12px var(--glow);
  position:sticky;top:0;height:100vh;overflow:auto}
.brand{display:flex;flex-direction:column;gap:.22rem;margin-bottom:1.4rem}
.brand b{font-size:.72rem;letter-spacing:.2em;text-transform:uppercase;color:var(--glow);
  font-weight:700}
.brand-sub{font-size:.56rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
  font-weight:500}
.nav a,.nav span{display:flex;align-items:center;gap:.55rem;padding:.4rem .5rem;
  border-radius:6px;color:var(--dim);font-size:.8rem;margin-bottom:.15rem}
.nav a:hover{background:var(--surface);color:var(--text);box-shadow:inset 2px 0 0 var(--glow)}
.nav span{opacity:.38}
.nav svg{width:14px;height:14px;flex:none}

main{padding:1.1rem 1.3rem 2.5rem;max-width:1520px}

.topbar{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:.7rem;margin-bottom:1rem}
.metric{position:relative;overflow:hidden;background:var(--surface);
  border:1px solid var(--edge);border-radius:8px;padding:.7rem .8rem .7rem .95rem;
  box-shadow:inset 0 0 30px -14px var(--glow)}
.metric::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;
  background:var(--glow);opacity:.55}
.metric .k{font-size:.6rem;letter-spacing:.13em;text-transform:uppercase;color:var(--dim)}
.metric .v{font-size:1.05rem;font-weight:600;margin-top:.2rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.metric .s{font-size:.72rem;color:var(--dim);margin-top:.1rem}

.attention{position:relative;background:linear-gradient(90deg,rgba(251,191,36,.16),transparent);
  border:1px solid var(--warn);border-left:3px solid var(--warn);border-radius:8px;
  padding:.7rem .9rem;margin-bottom:1rem;box-shadow:0 0 30px -12px var(--warn)}
.attention .k{font-size:.62rem;letter-spacing:.13em;text-transform:uppercase;
  color:var(--warn);font-weight:700}
.attention ul{margin:.35rem 0 0;padding-left:1.1rem}
.attention li{margin:.15rem 0}

.flash{border:1px solid var(--good);border-left:3px solid var(--good);border-radius:8px;
  padding:.55rem .8rem;margin-bottom:1rem;font-size:.82rem;
  background:linear-gradient(90deg,color-mix(in srgb,var(--good) 14%,transparent),transparent)}
.flash.err{border-color:var(--bad);border-left-color:var(--bad);
  background:linear-gradient(90deg,color-mix(in srgb,var(--bad) 14%,transparent),transparent)}
.gate-form{display:flex;align-items:center;flex-wrap:wrap;gap:.5rem;
  padding:.45rem 0;border-top:1px solid color-mix(in srgb,var(--warn) 25%,transparent)}
.gate-form:first-of-type{border-top:none}
.gate-form .who{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.8rem;
  min-width:14rem}
.gate-form label{font-size:.72rem;color:var(--dim);display:flex;align-items:center;gap:.3rem}
.gate-form input{background:var(--bg2);border:1px solid var(--edge-hi);color:var(--text);
  border-radius:6px;padding:.28rem .45rem;font:inherit;font-size:.8rem;width:9rem}
.gate-btn{font:inherit;font-size:.66rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;padding:.32rem .7rem;border-radius:20px;cursor:pointer;
  border:1px solid var(--edge-hi);background:var(--surface2);color:var(--text)}
.gate-btn:hover{filter:brightness(1.15)}
.gate-btn.approve,.gate-btn.validated,.gate-btn.launch,.gate-btn.pay{
  border-color:color-mix(in srgb,var(--good) 55%,var(--edge));color:var(--good)}
.gate-btn.reject,.gate-btn.rejected{
  border-color:color-mix(in srgb,var(--bad) 55%,var(--edge));color:var(--bad)}

section{background:var(--surface);border:1px solid var(--edge);border-radius:10px;
  padding:.95rem 1rem;margin-bottom:.9rem}
#agents{background:linear-gradient(180deg,var(--surface),var(--surface2));
  border-color:var(--edge-hi);box-shadow:0 0 40px -20px var(--glow)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:.9rem}
.cols>section{margin-bottom:0}
@media (max-width:1000px){.cols{grid-template-columns:1fr}}
@media (max-width:820px){.shell{grid-template-columns:1fr}
  .rail{position:static;height:auto;display:flex;flex-wrap:wrap;gap:.3rem}
  .brand{width:100%}}

.amap{position:relative;width:100%;max-width:960px;aspect-ratio:8/7;margin:.4rem auto 1.4rem;
  overflow:visible}
.wires{position:absolute;inset:0;width:100%;height:100%;overflow:visible;z-index:1}
.wires line{stroke:var(--edge-hi);stroke-width:2;stroke-dasharray:5 6;opacity:.75}
.wires marker path{fill:var(--edge-hi)}
.node{position:absolute;transform:translate(-50%,-50%);width:190px;z-index:2}
.node .acard{padding:.62rem .7rem}
.node .acard .task{font-size:.72rem}
.taskchip{position:absolute;transform:translate(-50%,-50%);z-index:3;white-space:nowrap;
  font-size:.6rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
  padding:.2rem .55rem;border-radius:20px;background:var(--surface);
  border:1px solid var(--glow);color:var(--glow);box-shadow:0 0 16px -4px var(--glow)}
.fan{position:absolute;transform:translate(-50%,-50%);z-index:3;font-size:.58rem;
  font-weight:700;color:var(--dim);background:var(--surface);border:1px solid var(--edge-hi);
  border-radius:10px;padding:.05rem .3rem}
.standby{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:1;
  text-align:center;font-size:.7rem;letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
.standby span{display:block;margin-top:.3rem;font-size:.68rem;letter-spacing:.04em;
  text-transform:none}
@media (max-width:760px){
  .amap{aspect-ratio:auto;max-width:none;display:grid;gap:.7rem}
  .amap .node{position:static;transform:none;width:auto}
  .wires,.taskchip,.standby{display:none}
}
.acard{background:linear-gradient(180deg,var(--surface2),var(--surface));
  border:1px solid var(--edge);border-radius:11px;padding:.85rem .9rem;
  position:relative;overflow:hidden;
  box-shadow:0 0 0 1px rgba(255,255,255,.02) inset}
.acard::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;
  background:linear-gradient(180deg,var(--acc),transparent)}
.acard::after{content:"";position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,var(--acc),transparent);
  background-size:220% 100%;opacity:.22}
.acard.cs-working,.acard.cs-active{
  box-shadow:0 0 26px -10px var(--acc),0 0 0 1px color-mix(in srgb,var(--acc) 38%,var(--edge)) inset}
.acard.cs-working::after,.acard.cs-active::after{opacity:.6;animation:scan 2.6s linear infinite}
.acard.cs-error{box-shadow:0 0 26px -10px var(--bad),
  0 0 0 1px color-mix(in srgb,var(--bad) 40%,var(--edge)) inset}
.acard.cs-error::after{background:linear-gradient(90deg,transparent,var(--bad),transparent);opacity:.55}
@keyframes scan{to{background-position:220% 0}}
.acard .top{display:flex;align-items:center;gap:.6rem;margin-bottom:.5rem}
.avatar{width:38px;height:38px;flex:none;border-radius:10px;display:grid;place-items:center;
  color:var(--acc);border:1px solid color-mix(in srgb,var(--acc) 40%,var(--edge-hi));
  background:radial-gradient(circle at 30% 28%,color-mix(in srgb,var(--acc) 26%,transparent),transparent 70%);
  box-shadow:0 0 18px -6px var(--acc)}
.avatar svg{width:21px;height:21px}
.acard .name{font-weight:600;font-size:.87rem}
.acard .role{font-size:.66rem;color:var(--dim);letter-spacing:.05em;text-transform:uppercase}
.acard .task{font-size:.78rem;color:var(--text);margin:.3rem 0 .5rem;min-height:1.1rem;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acard .task.none{color:var(--dim)}
.acard .meta{display:flex;flex-wrap:wrap;gap:.15rem .8rem;font-size:.68rem;color:var(--dim);
  border-top:1px solid var(--edge);padding-top:.45rem;margin-top:.55rem}
.acard .meta b{color:var(--text);font-weight:600;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

.pill{display:inline-flex;align-items:center;gap:.35rem;font-size:.64rem;font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;padding:.18rem .5rem;border-radius:20px;
  border:1px solid var(--edge-hi);color:var(--dim);background:rgba(0,0,0,.12)}
.dot{width:.5rem;height:.5rem;border-radius:50%;background:currentColor;flex:none;
  box-shadow:0 0 6px currentColor}
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
.bar{background:linear-gradient(90deg,var(--glow),color-mix(in srgb,var(--glow) 55%,var(--good)));
  height:.5rem;box-shadow:0 0 10px -2px var(--glow)}
.feed{max-height:20rem;overflow:auto}
.progress{margin:.5rem 0}
.progress .lbl{display:flex;justify-content:space-between;font-size:.72rem;color:var(--dim);
  margin-bottom:.25rem}
details{margin:.3rem 0}
summary{cursor:pointer;padding:.28rem 0}
details table{margin:.4rem 0 .7rem;max-width:460px}
h3{font-size:.58rem;letter-spacing:.15em;text-transform:uppercase;color:var(--dim);
  font-weight:600;margin:1.1rem 0 .5rem}
.rgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.5rem}
.ragent{display:flex;align-items:center;gap:.5rem;padding:.45rem .55rem;border-radius:9px;
  border:1px solid var(--edge);background:var(--surface2)}
.ragent .avatar{width:28px;height:28px;border-radius:8px;box-shadow:none}
.ragent .avatar svg{width:15px;height:15px}
.ragent.rplanned{opacity:.5}
.ragent.rlive{border-color:color-mix(in srgb,var(--good) 40%,var(--edge))}
.rname{font-size:.76rem;font-weight:600}
.rrole{font-size:.62rem;color:var(--dim);text-transform:uppercase;letter-spacing:.04em}
.rtag{margin-left:auto;font-size:.54rem;font-weight:700;letter-spacing:.06em;
  padding:.12rem .35rem;border-radius:12px;border:1px solid var(--edge-hi);color:var(--dim)}
.ragent.rlive .rtag{color:var(--good);border-color:color-mix(in srgb,var(--good) 45%,var(--edge))}
.cflow{display:flex;gap:.5rem;overflow-x:auto;margin:.2rem 0 1.2rem;padding-bottom:.3rem}
.cband{flex:1 1 0;min-width:150px;border:1px solid var(--edge);border-radius:10px;
  background:var(--surface2);padding:.5rem}
.cband-h{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  color:var(--dim);margin-bottom:.4rem;display:flex;justify-content:space-between}
.cnode{display:flex;align-items:center;gap:.4rem;padding:.32rem .4rem;border-radius:8px;
  border:1px solid var(--edge);background:var(--surface);margin:.26rem 0}
.cnode .avatar{width:22px;height:22px;border-radius:6px;box-shadow:none}
.cnode .avatar svg{width:12px;height:12px}
.cnode .cn{font-size:.66rem;font-weight:600;line-height:1.15}
.cnode .ct{margin-left:auto;font-size:.5rem;font-weight:700;letter-spacing:.05em;
  padding:.1rem .3rem;border-radius:10px;border:1px solid var(--edge-hi);color:var(--dim);
  white-space:nowrap}
.cnode.ran .ct{color:var(--good);border-color:color-mix(in srgb,var(--good) 45%,var(--edge))}
.cnode.gated .ct{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,var(--edge))}
.aout td.hl{color:var(--text)}
.fsm{width:1.4rem;text-align:center;font-weight:700}
.fsm-ok{color:var(--good)}
.fsm-warn{color:var(--warn)}
.fsm-off{color:var(--dim)}
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
    "finder": _svg('<path d="M4 5h16l-6 8v6l-4-2v-4z"/>'),
    "trendhunter": _svg('<path d="M3 17 9 11l4 4 8-8"/><path d="M17 4h4v4"/>'),
    "competitor": _svg('<circle cx="12" cy="12" r="8"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>'
                       '<circle cx="12" cy="12" r="2"/>'),
    "copywriter": _svg('<path d="M4 20h16"/><path d="m14 4 6 6-9 9H5v-6z"/>'),
    "analyst": _svg('<path d="M4 20h16"/><path d="M7 16v-5M12 16V8M17 16v-3"/>'),
    "content": _svg('<rect x="5" y="3" width="14" height="18" rx="1.5"/>'
                    '<path d="M9 8h6M9 12h6M9 16h3"/>'),
    "generic": _svg('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>'),
}
_ACCENT = {
    "operator": "#22d3ee", "discovery": "#38bdf8", "evaluator": "#a78bfa",
    "researcher": "#34d399", "planner": "#fbbf24", "offer": "#f472b6",
    "decision": "#f87171", "finder": "#5eead4", "trendhunter": "#c084fc",
    "competitor": "#fb923c", "copywriter": "#facc15", "analyst": "#4ade80",
    "content": "#38bdf8", "generic": "#94a3b8",
}

# llm_spend activity  ->  (node key, display name, role, Goal field, llm-mode value)
# Evaluator is the internal scoring engine; planner / offer / decision are
# internal steps not yet promoted to named roster agents.
_WORKERS = (
    ("evaluate", "evaluator", "Evaluator", "Scoring",         "evaluator",       "llm"),
    ("research", "researcher", "Product Researcher", "Due diligence", "research",     ("llm", "web")),
    ("competition", "competitor", "Competitor Analyzer", "Competition read", "competition", ("llm", "web")),
    ("plan",     "planner",    "Validation Planner", "Validation plan", "planner",    "llm"),
    ("offer",    "offer",      "Offer Builder", "First offer",    "proposer",         "llm"),
    ("copy",     "copywriter", "Copywriter AI", "Launch copy",    "copywriter",       "llm"),
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
        f"<div class='acard cs-{status}' style=\"--acc:{acc}\">"
        f"<div class='top'>{_avatar(key)}"
        f"<div><div class='name'>{_esc(name)}</div>"
        f"<div class='role'>{_esc(role)}</div></div></div>"
        f"<div class='task{task_cls}'>{task or 'No current task'}</div>"
        f"{_pill(status)}"
        f"<div class='meta'>{meta_html}</div>"
        "</div>"
    )


# ---------------------------------------------------------------------------
# orchestration map: deterministic role positions on a fixed logical canvas
# (5:4 so the SVG wire layer scales uniformly with the positioned nodes)
# ---------------------------------------------------------------------------

_MAP_W, _MAP_H = 800, 700
_MAP_POS = {
    "discovery":   (400, 70),
    "researcher":  (150, 232),
    "evaluator":   (650, 232),
    "trendhunter": (150, 400),
    "finder":      (650, 400),
    "competitor":  (650, 560),
    "copywriter":  (150, 560),
    "content":     (250, 660),
    "analyst":     (650, 660),
    "decision":    (120, 368),
    "operator":    (400, 358),
    "planner":     (400, 486),
    "offer":       (400, 582),
}


def _pct(role: str) -> tuple[float, float]:
    x, y = _MAP_POS[role]
    return round(100 * x / _MAP_W, 2), round(100 * y / _MAP_H, 2)


def _wires(edges: list[tuple[str, str]], last_edge, edge_counts=None) -> str:
    if not edges:
        wires = (
            "<svg viewBox='0 0 800 700' class='wires' preserveAspectRatio='xMidYMid meet'"
            " aria-hidden='true'></svg>"
            "<div class='standby'>No active task links"
            "<span>Agents are standing by.</span></div>"
        )
        return wires
    edge_counts = edge_counts or {}
    lines = "".join(
        "<line x1='{}' y1='{}' x2='{}' y2='{}' marker-end='url(#wa)'/>".format(
            *_MAP_POS[a], *_MAP_POS[b]
        )
        for a, b in edges
    )
    fanout = "".join(
        "<div class='fan' style='left:{}%;top:{}%'>&times;{}</div>".format(
            round(100 * (_MAP_POS[a][0] + _MAP_POS[b][0]) / 2 / _MAP_W, 2),
            round(100 * (_MAP_POS[a][1] + _MAP_POS[b][1]) / 2 / _MAP_H, 2),
            n,
        )
        for (a, b), n in edge_counts.items() if n > 1
    )
    chip = ""
    if last_edge:
        a, b, label = last_edge
        mx = round(100 * (_MAP_POS[a][0] + _MAP_POS[b][0]) / 2 / _MAP_W, 2)
        my = round(100 * (_MAP_POS[a][1] + _MAP_POS[b][1]) / 2 / _MAP_H, 2)
        chip = (f"<div class='taskchip' style='left:{mx}%;top:{my}%'>"
                f"{_esc(label)}</div>")
    return (
        "<svg viewBox='0 0 800 700' class='wires' preserveAspectRatio='xMidYMid meet'"
        " aria-hidden='true'>"
        "<defs><marker id='wa' viewBox='0 0 10 10' refX='8' refY='5' markerWidth='5'"
        " markerHeight='5' orient='auto'><path d='M0 0 L10 5 L0 10 z'/></marker></defs>"
        f"<g>{lines}</g></svg>{fanout}{chip}"
    )


def _agent_map(agent_log, spend_entries, goal, session, report,
               queue_open: bool = False, task_log=None) -> str:
    goal = goal or {}
    task_log = task_log or []
    decisions = [e for e in (agent_log or []) if e.get("action") not in _MARKERS]
    running = bool(session) and not session.get("ended_at")
    by_act: dict[str, list[dict]] = {}
    for e in (spend_entries or []):
        by_act.setdefault(e.get("activity"), []).append(e)
    task_by_node: dict[str, list[dict]] = {}
    for e in task_log:
        node = _TASK_AGENT_NODE.get(e.get("agent"))
        if node:
            task_by_node.setdefault(node, []).append(e)

    nodes: dict[str, dict] = {}

    # --- operator (CEO) ----------------------------------------------
    if decisions:
        last = decisions[-1]
        note = f"{_esc(last.get('action', ''))} &mdash; {_esc(last.get('reason', ''))}"
        if running:
            op_status = "working"
        elif last.get("action") == "stop" and queue_open:
            op_status = "waiting"
        else:
            op_status = "idle"
        op_meta = [("decisions", len(decisions)), ("last", _esc(last.get("ts", "")))]
    else:
        note, op_status, op_meta = "", "idle", [("decisions", 0)]
    nodes["operator"] = dict(key="operator", name="operator (CEO)", role="Coordinator",
                             status=op_status, task=note, meta=op_meta)

    # --- discovery-team agents (deterministic; status from the task log) --
    disc = [e for e in decisions if e.get("action") == "discover"]
    src = ", ".join(goal.get("sources", ["static"]))
    scans = task_by_node.get("discovery", [])
    nodes["discovery"] = dict(
        key="discovery", name="Market Scanner", role="Signal intake",
        status="working" if (running and scans) else ("active" if scans else "idle"),
        task=f"sources: {_esc(src)}",
        meta=[("runs", len(disc)),
              ("last", _esc(disc[-1]["ts"]) if disc else "&mdash;"),
              ("filter", "on" if goal.get("filter") else "off")],
    )

    finds = task_by_node.get("finder", [])
    last_find = finds[-1].get("summary", {}) if finds else {}
    nodes["finder"] = dict(
        key="finder", name="Opportunity Finder", role="Rank & shortlist",
        status="working" if (running and finds) else ("active" if finds else "idle"),
        task=(f"shortlisted {last_find.get('shortlist', 0)} of "
              f"{last_find.get('kept', 0)} kept" if finds else "awaiting scores"),
        meta=[("runs", len(finds))],
    )

    trends = task_by_node.get("trendhunter", [])
    last_trend = trends[-1].get("summary", {}) if trends else {}
    if goal.get("trend_hunter"):
        t_status = "working" if (running and trends) else ("active" if trends else "idle")
    else:
        t_status = "disabled"
    nodes["trendhunter"] = dict(
        key="trendhunter", name="Trend Hunter", role="Emerging demand",
        status=t_status,
        task=(f"{last_trend.get('keywords', 0)} keyword(s), "
              f"{last_trend.get('sources', 0)} source(s)" if trends
              else ("enabled" if goal.get("trend_hunter") else "off")),
        meta=[("runs", len(trends))],
    )

    ana = task_by_node.get("analyst", [])
    if goal.get("revenue_analyst"):
        a_status = "working" if (running and ana) else ("active" if ana else "idle")
    else:
        a_status = "disabled"
    nodes["analyst"] = dict(
        key="analyst", name="Revenue Analyst", role="ROI analysis",
        status=a_status,
        task=("portfolio analysed" if ana
              else ("enabled" if goal.get("revenue_analyst") else "off")),
        meta=[("runs", len(ana))],
    )

    pkg = task_by_node.get("content", [])
    if goal.get("content_creator"):
        c_status = "working" if (running and pkg) else ("active" if pkg else "idle")
    else:
        c_status = "disabled"
    nodes["content"] = dict(
        key="content", name="Content Creator", role="Launch page",
        status=c_status,
        task=("landing page packaged" if pkg
              else ("enabled" if goal.get("content_creator") else "off")),
        meta=[("runs", len(pkg))],
    )

    # --- configurable workers -------------------------------------
    for activity, key, name, role, gkey, on_val in _WORKERS:
        on_vals = on_val if isinstance(on_val, tuple) else (on_val,)
        entries = by_act.get(activity, [])
        runs = len(entries)
        mode = goal.get(gkey)
        ceiling = bool(entries and entries[-1].get("ceiling_hit"))
        if ceiling:
            status = "error"
        elif runs:
            status = "working" if running else "active"
        elif mode == "off":
            status = "disabled"
        elif mode is not None and mode not in on_vals:
            status = "deterministic"
        else:
            status = "idle"
        calls = sum(int(e.get("api_calls", 0)) for e in entries)
        cost = round(sum(float(e.get("cost_usd", 0.0)) for e in entries), 4)
        hits = sum(int(e.get("cache_hits", 0)) for e in entries)
        misses = sum(int(e.get("cache_misses", 0)) for e in entries)
        searches = sum(int(e.get("searches", 0)) for e in entries)
        task = f"mode: {_esc(mode)}" if mode is not None else ""
        if runs:
            task += f" &mdash; {calls} call(s)"
        meta = [("runs", runs)]
        if runs:
            meta += [("cost", f"${_num(cost)}"), ("cache", f"{hits}/{hits + misses}")]
        if searches:
            meta.append(("searches", searches))
        if mode is not None:
            meta.append(("enabled", "yes" if mode in on_vals else "no"))
        nodes[key] = dict(key=key, name=name, role=role,
                          status=status, task=task, meta=meta)

    # --- real relationships --------------------------------------------
    def took(action: str) -> bool:
        return any(e.get("action") == action for e in decisions)

    edge_counts: dict = {}
    if task_log:
        # genuine parent->child lineage from the dispatched tasks
        edges, edge_counts = _lineage_edges(task_log)
    else:
        # no task log yet: infer from the operator's own decisions
        edges = []
        if took("discover"):
            edges += [("operator", "discovery"), ("discovery", "evaluator")]
        if took("research"):
            edges.append(("operator", "researcher"))
    if took("investigate"):
        edges.append(("operator", "planner"))
    if took("prepare_launch"):
        edges.append(("operator", "offer"))
    if goal.get("decision_policy") == "llm" and by_act.get("decide"):
        edges.append(("decision", "operator"))
    edges = [e for e in dict.fromkeys(edges) if e[0] in _MAP_POS and e[1] in _MAP_POS]

    last_edge = None
    if decisions:
        tgt = _ACT_AGENT.get(decisions[-1].get("action"))
        if tgt and tgt != "operator" and ("operator", tgt) in edges:
            last_edge = ("operator", tgt, decisions[-1].get("action"))

    positioned = "".join(
        f"<div class='node' style='left:{_pct(r)[0]}%;top:{_pct(r)[1]}%'>"
        f"{_agent_card(**nodes[r])}</div>"
        for r in _MAP_POS if r in nodes
    )
    return (
        f"<div class='amap'>{_wires(edges, last_edge, edge_counts)}{positioned}</div>"
    )


def _roster_panel() -> str:
    """The full target roster from roster.py. Planned agents are shown but
    carry no status or metrics - they are not running. Live human-gated
    agents produce drafts only; the tag keeps that visible."""
    n_live = len(roster.live())
    n_planned = len(roster.AGENTS) - n_live
    blocks = ""
    for cluster in roster.CLUSTERS:
        rows = ""
        for a in (s for s in roster.AGENTS if s.cluster == cluster):
            live = a.status == "live"
            cls = "rlive" if live else "rplanned"
            if not live:
                tag = "planned"
            elif a.gate == "human":
                tag = "human-gated"
            else:
                tag = "live"
            rows += (
                f"<div class='ragent {cls}'>{_avatar(a.node)}"
                f"<div><div class='rname'>{_esc(a.name)}</div>"
                f"<div class='rrole'>{_esc(a.role)}</div></div>"
                f"<span class='rtag'>{tag}</span></div>"
            )
        blocks += (f"<h3>{_esc(cluster)}</h3><div class='rgrid'>{rows}</div>")
    tail = (" Planned agents are not running and report no activity."
            if n_planned else
            " Human-gated agents produce drafts only - a person runs, publishes, "
            "or funds anything money- or contact-related.")
    return (
        f"<p class='muted'>Target roster: {len(roster.AGENTS)} agents &middot; "
        f"{n_live} live &middot; {n_planned} planned.{tail}</p>"
        f"{blocks}"
    )


def _path_only(url: str) -> str:
    """The path of a URL, no scheme/host - so the dashboard stays free of
    external references while still showing which page is meant."""
    p = urlsplit(str(url or "")).path.rstrip("/")
    return p or "/"


def _acquisition_panel(acquisition: dict | None) -> str:
    """Phase 2 acquisition cluster - real counts only, straight from
    data/acquisition.json + data/outreach.json. No prospect URLs, titles,
    or invented status: the stores either hold these numbers or they do
    not. The system finds and drafts only; a person posts every reply.

    `acquisition` is {"leads": [...], "briefs": [...], "queue": [...]} or
    None. `queue` (Phase 2.4) is the de-duped review queue from
    autopilot.acquisition_queue.
    """
    data = acquisition if isinstance(acquisition, dict) else {}
    leads = [d for d in (data.get("leads") or []) if isinstance(d, dict)]
    briefs = [d for d in (data.get("briefs") or []) if isinstance(d, dict)]
    queue = [d for d in (data.get("queue") or []) if isinstance(d, dict)]
    if not leads and not briefs:
        return (
            "<p class='muted'>No acquisition run yet - "
            "<span class='mono'>data/acquisition.json</span> is empty. Run "
            "<span class='mono'>discover-free</span> to find current public "
            "posts from founders looking for their first customers. The system "
            "never posts, DMs, or emails - it drafts, a person sends.</p>"
        )

    def _c(items, key, val):
        return sum(1 for d in items if d.get(key) == val)

    q = {k: _c(leads, "prospect_quality", k)
         for k in ("high", "medium", "low", "none")}
    rev = {k: _c(leads, "human_review_status", k)
           for k in ("new", "reviewed", "rejected")}
    awaiting = sum(1 for b in briefs if b.get("status") in ("draft", "approved"))
    posted = _c(briefs, "status", "posted")
    llm_drafts = sum(1 for b in briefs
                     if isinstance((b.get("brief") or {}).get("draft_reply")
                                   or b.get("draft_reply"), dict)
                     and not ((b.get("brief") or {}).get("draft_reply")
                              or b.get("draft_reply") or {}).get("error"))

    rows = [
        ("prospects found", len(leads)),
        ("high / medium quality", q["high"] + q["medium"]),
        ("awaiting human review", rev["new"]),
        ("reviewed / rejected", f"{rev['reviewed']} / {rev['rejected']}"),
        ("outreach drafts prepared", len(briefs)),
        ("of those, with a tailored LLM draft", llm_drafts),
        ("awaiting a human post", awaiting),
        ("marked posted", posted),
        ("review queue (de-duped, still open)", len(queue)),
    ]
    body = "".join(
        f"<tr><td>{_esc(label)}</td><td class='hl'>{_esc(value)}</td></tr>"
        for label, value in rows
    )

    qhtml = ""
    if queue:
        qrows = ""
        for row in queue[:10]:
            age = ("age unknown" if row.get("age_days") is None
                   else f"{row['age_days']}d ({row.get('age_bucket', '?')})")
            qrows += (
                f"<tr><td>{_esc(row.get('prospect_quality', '?'))}</td>"
                f"<td>{_esc(row.get('stage', '?'))}</td>"
                f"<td>{_esc(row.get('brief_status', '?'))}</td>"
                f"<td>{_esc('promo:' + str(row.get('promo_allowed', '?')))}</td>"
                f"<td>{_esc(age)}</td>"
                f"<td class='mono'>{_esc(row.get('lead_id', ''))}</td></tr>"
            )
        more = (f"<tr><td colspan='6' class='muted'>&hellip; and "
                f"{len(queue) - 10} more</td></tr>" if len(queue) > 10 else "")
        qhtml = (
            "<p class='muted' style='margin-top:1rem'>Review queue - each row "
            "needs a person to review the draft and post their own reply "
            "(<span class='mono'>acquisition-queue</span>). No URL shown here.</p>"
            f"<div class='aout'><table><tr><th>quality</th><th>stage</th>"
            f"<th>brief</th><th>promo</th><th>age</th><th>lead id</th></tr>"
            f"{qrows}{more}</table></div>"
        )

    return (
        f"<div class='aout'><table>{body}</table></div>"
        f"<p class='muted'>Quality split: high {q['high']} &middot; medium "
        f"{q['medium']} &middot; low {q['low']} &middot; none {q['none']}. "
        "Straight from <span class='mono'>data/acquisition.json</span> + "
        "<span class='mono'>data/outreach.json</span> - no prospect is named "
        "here. The system finds and drafts only; a person reviews every "
        "prospect and posts every reply.</p>"
        f"{qhtml}"
    )


def _continuous_panel(acquisition: dict | None) -> str:
    """Phase 3: the continuous-loop session, straight from
    data/revenue_loop.json. No URLs, nothing inferred. The loop drives the
    existing supervisor - it passes no human gate and spends nothing."""
    data = acquisition if isinstance(acquisition, dict) else {}
    loop = data.get("loop") if isinstance(data.get("loop"), dict) else None
    if not loop:
        return ("<p class='muted'>No continuous session yet - "
                "<span class='mono'>data/revenue_loop.json</span> is absent. "
                "Start one with <span class='mono'>revenue-loop --watch</span> "
                "(bounded, resumable, deterministic, EUR 0).</p>")

    sess = loop.get("session") if isinstance(loop.get("session"), dict) else {}
    rows = [
        ("loop status", loop.get("status") or "idle"),
        ("session", (sess.get("end_reason") and f"ended: {sess['end_reason']}")
         or ("running" if sess.get("started_at") else "none")),
        ("ticks", sess.get("ticks", 0)),
        ("last tick", sess.get("last_tick_at") or "-"),
        ("steps taken (lifetime)", loop.get("steps_taken", 0)),
        ("last action", loop.get("last_action") or "-"),
    ]
    body = "".join(f"<tr><td>{_esc(k)}</td><td class='hl'>{_esc(v)}</td></tr>"
                   for k, v in rows)
    hq = [str(x) for x in (loop.get("human_queue") or []) if str(x).strip()]
    qhtml = ""
    if hq:
        items = "".join(f"<li>{_esc(x)}</li>" for x in hq[:8])
        qhtml = ("<p class='muted' style='margin-top:1rem'>Current human "
                 "queue (the loop stops here - it never acts on these):</p>"
                 f"<ul>{items}</ul>")
    return (f"<div class='aout'><table>{body}</table></div>"
            "<p class='muted'>Straight from "
            "<span class='mono'>data/revenue_loop.json</span>. The loop runs "
            "discovery + the pipeline deterministically and stops at every "
            "human gate - no posting, no spend, no API/PayPal call.</p>"
            f"{qhtml}")


def _experiments_panel(acquisition: dict | None) -> str:
    """Phase 3: the revenue-experiment ledger (data/experiments.json).
    Per-source rollup + per-experiment rows. No lead id, no URL - only
    source / price / status / age. Deterministic, read-only tracking."""
    data = acquisition if isinstance(acquisition, dict) else {}
    exp = data.get("experiments") if isinstance(data.get("experiments"), dict) else None
    roll = (exp or {}).get("rollup") if isinstance((exp or {}).get("rollup"), dict) else None
    if not roll or not roll.get("total"):
        return ("<p class='muted'>No experiments yet - "
                "<span class='mono'>data/experiments.json</span> is empty. One "
                "opens per prospect outreach attempt (drafted &rarr; posted "
                "&rarr; sale | no_sale). Deterministic, read-only.</p>")

    from .experiments import STATUSES
    o = roll["overall"]
    head = "".join(f"<th>{_esc(s)}</th>" for s in STATUSES)
    srows = ""
    for src, c in sorted(roll["by_source"].items()):
        cells = "".join(f"<td>{_esc(c.get(s, 0))}</td>" for s in STATUSES)
        srows += f"<tr><td class='hl'>{_esc(src)}</td>{cells}</tr>"
    total_cells = "".join(f"<td class='hl'>{_esc(o.get(s, 0))}</td>" for s in STATUSES)
    rollup_html = (
        f"<div class='aout'><table><tr><th>source</th>{head}</tr>{srows}"
        f"<tr><td class='hl'>all</td>{total_cells}</tr></table></div>")

    rrows = ""
    for row in roll["rows"][:15]:
        age = "age ?" if row.get("age_days") is None else f"{row['age_days']}d"
        rrows += (
            f"<tr><td>{_esc(row.get('source') or 'unknown')}</td>"
            f"<td>{_esc(row.get('offer_price'))} {_esc(row.get('currency') or 'EUR')}</td>"
            f"<td class='hl'>{_esc(row.get('status'))}</td>"
            f"<td>{_esc(age)}</td></tr>")
    more = (f"<tr><td colspan='4' class='muted'>&hellip; and "
            f"{len(roll['rows']) - 15} more</td></tr>"
            if len(roll["rows"]) > 15 else "")
    rows_html = (
        "<p class='muted' style='margin-top:1rem'>Experiments "
        f"({roll['open']} open, {roll['closed']} closed) - no prospect is "
        "named:</p>"
        f"<div class='aout'><table><tr><th>source</th><th>price</th>"
        f"<th>status</th><th>age</th></tr>{rrows}{more}</table></div>")
    return (rollup_html + rows_html
            + "<p class='muted'>From <span class='mono'>data/experiments.json"
            "</span>. Sales are correlated read-only from the intake + revenue "
            "ledger; PayPal is never called.</p>")


def _first_sale_panel(acquisition: dict | None) -> str:
    """A live readiness checklist for the first real sale, computed only
    from files on disk (the launched candidate, the acquisition + outreach
    stores, the revenue ledger, the LLM-spend log). It never calls an API,
    so it is explicit about what it cannot know (the live PayPal payment
    path). Every item is READY / CHECK / - with a one-line reason.
    """
    data = acquisition if isinstance(acquisition, dict) else {}
    r = data.get("readiness") if isinstance(data.get("readiness"), dict) else None
    if not r:
        return ("<p class='muted'>Readiness not computed - no launched, priced "
                "candidate found in <span class='mono'>data/candidates.json</span>.</p>")

    leads = [d for d in (data.get("leads") or []) if isinstance(d, dict)]
    queue = [d for d in (data.get("queue") or []) if isinstance(d, dict)]

    def mark(state):  # "ok" | "warn" | "off"
        return {"ok": "&#10003;", "warn": "&#9888;", "off": "&ndash;"}[state]

    items: list[tuple[str, str, str]] = []

    # 1. offer
    if r.get("candidate") and r.get("offer_price"):
        items.append(("ok", "Offer live",
                      f"{_esc(r.get('candidate'))} &middot; "
                      f"{_esc(r.get('offer_price'))} {_esc(r.get('offer_currency') or 'EUR')} "
                      f"&middot; {_esc(r.get('candidate_status') or '?')}"))
    else:
        items.append(("warn", "Offer live",
                      "no launched candidate with a price"))

    # 2. checkout page
    if r.get("checkout_built") and r.get("checkout_deployed"):
        items.append(("ok", "Checkout page",
                      f"built and deployed at "
                      f"<span class='mono'>{_esc(_path_only(r.get('candidate_public_url')))}</span>"))
    elif r.get("checkout_built"):
        items.append(("warn", "Checkout page",
                      "built but not deployed - run <span class='mono'>deploy-checkout</span>"))
    else:
        items.append(("warn", "Checkout page",
                      "not built - run <span class='mono'>build-checkout</span>"))

    # 3. outreach link points at the deployed checkout
    dep = _path_only(r.get("candidate_public_url"))
    out = _path_only(r.get("outreach_default_url"))
    if not r.get("candidate_public_url"):
        items.append(("off", "Outreach link", "no deployed checkout to compare"))
    elif dep.lower() == out.lower():
        items.append(("ok", "Outreach link",
                      f"briefs link to the deployed page "
                      f"(<span class='mono'>{_esc(dep)}</span>)"))
    else:
        items.append(("warn", "Outreach link",
                      f"briefs link to <span class='mono'>{_esc(out)}</span> but the "
                      f"checkout is deployed at <span class='mono'>{_esc(dep)}</span> "
                      "- a prospect who clicks the brief hits the wrong page "
                      "(pass <span class='mono'>--checkout-url</span> or fix the default)"))

    # 4. a fresh qualified lead
    qual = [d for d in leads
            if d.get("prospect_quality") in ("high", "medium")
            and d.get("human_review_status") != "rejected"]
    ages = [d["age_days"] for d in qual if isinstance(d.get("age_days"), int)]
    if not qual:
        items.append(("warn", "Actionable lead",
                      "no high/medium-quality prospect in the store - "
                      "run <span class='mono'>discover-free</span>"))
    elif ages and min(ages) > 14:
        items.append(("warn", "Actionable lead",
                      f"{len(qual)} qualified, but the freshest is {min(ages)}d old "
                      "- likely past a useful reply window"))
    else:
        fresh = f", freshest {min(ages)}d" if ages else ""
        items.append(("ok", "Actionable lead",
                      f"{len(qual)} high/medium-quality prospect(s){fresh}"))

    # 5. an outreach brief ready to post
    prepared = [d for d in queue if d.get("stage") == "prepared"]
    if prepared:
        items.append(("ok", "Outreach brief ready",
                      f"{len(prepared)} brief(s) drafted, awaiting your review + post"))
    elif queue:
        items.append(("warn", "Outreach brief ready",
                      f"{len(queue)} prospect(s) queued but no brief drafted yet - "
                      "run <span class='mono'>outreach-brief</span> / a cycle"))
    else:
        items.append(("warn", "Outreach brief ready", "nothing queued"))

    # 6. payment - honest about the API blind spot
    rev_eur = r.get("revenue_eur") or 0
    if rev_eur and float(rev_eur) > 0:
        items.append(("ok", "Payment received",
                      f"{_esc(rev_eur)} EUR booked in the revenue ledger"))
    else:
        items.append(("off", "Payment received",
                      "no sale yet. This view does NOT check the live PayPal "
                      "payment path (that needs an API call) - verify a real "
                      "payment can complete before relying on the funnel"))

    # 7. LLM opt-in paths
    calls = int(r.get("llm_api_calls") or 0)
    cost = r.get("llm_cost_usd") or 0.0
    if calls:
        items.append(("ok", "LLM opt-in paths",
                      f"{calls} metered API call(s) recorded (${_esc(cost)})"))
    else:
        items.append(("off", "LLM opt-in paths",
                      "no LLM call has succeeded here - <span class='mono'>--score llm</span>"
                      " / <span class='mono'>--draft llm</span> / "
                      "<span class='mono'>draft-launch-plan</span> are untested "
                      "(deterministic paths work at EUR 0)"))

    n_warn = sum(1 for s, *_ in items if s == "warn")
    rows = "".join(
        f"<tr><td class='fsm fsm-{s}'>{mark(s)}</td>"
        f"<td class='hl'>{_esc(label)}</td><td>{reason}</td></tr>"
        for s, label, reason in items
    )
    head = (f"<p class='muted'>{n_warn} item(s) need attention before a "
            "realistic first-sale attempt. Every row is computed from a file "
            "on disk - nothing here is inferred or live-checked.</p>"
            if n_warn else
            "<p class='muted'>All disk-checkable items look ready. The live "
            "PayPal payment path still needs a real verification (not done "
            "here).</p>")
    return head + f"<div class='aout'><table>{rows}</table></div>"


_CLUSTER_FLOW_LABEL = {
    "discovery": "1 &middot; Discovery", "build": "2 &middot; Build",
    "marketing": "3 &middot; Marketing", "acquisition": "4 &middot; Acquisition",
    "revenue": "5 &middot; Revenue", "support": "6 &middot; Support",
}


def _cluster_flow(agent_outputs: dict | None) -> str:
    """Every roster agent laid out by cluster (Discovery -> Build ->
    Marketing -> Acquisition -> Revenue -> Support). Status is real only:
    "ran" when agent_outputs.json holds a persisted output for that
    capability, otherwise "human-gated" or "ready". No metrics are
    invented."""
    outs = agent_outputs if isinstance(agent_outputs, dict) else {}
    bands = ""
    for cluster in roster.CLUSTERS:
        agents = [a for a in roster.AGENTS if a.cluster == cluster]
        ran = sum(1 for a in agents if a.capability in outs)
        rows = ""
        for a in agents:
            entry = outs.get(a.capability)
            if isinstance(entry, dict):
                cls = "ran"
                tag = "ran " + str(entry.get("ts", ""))[:10]
            elif a.gate == "human":
                cls, tag = "gated", "human-gated"
            else:
                cls, tag = "", "ready"
            rows += (
                f"<div class='cnode {cls}'>{_avatar(a.node)}"
                f"<span class='cn'>{_esc(a.name)}</span>"
                f"<span class='ct'>{_esc(tag)}</span></div>"
            )
        bands += (
            f"<div class='cband'><div class='cband-h'>"
            f"<span>{_CLUSTER_FLOW_LABEL[cluster]}</span>"
            f"<span>{ran}/{len(agents)}</span></div>{rows}</div>"
        )
    return (
        f"<p class='muted'>All {len(roster.AGENTS)} agents by cluster. "
        "&ldquo;ran&rdquo; means a real output is persisted in "
        "<span class='mono'>agent_outputs.json</span>; "
        "human-gated agents produce drafts only.</p>"
        f"<div class='cflow'>{bands}</div>"
    )


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
    llm_on = [
        n for a, k, n, r, g, o in _WORKERS
        if goal.get(g) in (o if isinstance(o, tuple) else (o,))
    ]
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
        + _metric("Agent tasks", str(len(decisions)), "recorded this session")
        + _metric("Awaiting you", str(len(queue)), q_sub)
        + _metric("LLM spend", spend_v, spend_s)
        + _metric("Revenue", rev_v, rev_s)
        + "</div>"
    )


def _attention(queue: list[dict], interactive: bool = False,
               csrf: str | None = None) -> str:
    if not queue:
        return ""
    if not interactive:
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
    forms = "".join(_gate_form(i, csrf) for i in queue)
    return (
        "<div class='attention'><div class='k'>Human action required</div>"
        f"{forms}</div>"
    )


def _gate_form(item: dict, csrf: str | None) -> str:
    name = _esc(item["name"])
    status = item.get("status")
    stale = " <span class='bad'>(stale)</span>" if item.get("stale") else ""
    head = (
        "<form class='gate-form' action='/action' method='post'>"
        f"<input type='hidden' name='csrf' value='{_esc(csrf or '')}'>"
        f"<input type='hidden' name='name' value='{name}'>"
        f"<span class='who'>{name}{stale}</span>"
    )
    if status == "shortlisted":
        body = (
            "<button class='gate-btn approve' name='action' value='approve'>Approve</button>"
            "<button class='gate-btn reject' name='action' value='reject'>Reject</button>"
        )
    elif status == "investigating":
        body = (
            "<input type='hidden' name='action' value='outcome'>"
            "<label>Metric <input name='metric' type='text' "
            "placeholder='e.g. 4 of 10 said yes'></label>"
            "<button class='gate-btn validated' name='result' value='validated'>Validated</button>"
            "<button class='gate-btn rejected' name='result' value='rejected'>Rejected</button>"
        )
    elif status == "validated":
        body = "<button class='gate-btn launch' name='action' value='launch'>Launch</button>"
    elif status in ("launched", "earning"):
        body = (
            "<label>Amount <input name='amount' type='number' step='0.01' min='0' "
            "required></label>"
            "<button class='gate-btn pay' name='action' value='payment'>Record payment</button>"
        )
    else:
        body = f"<span class='muted'>{_esc(item.get('next_action', ''))}</span>"
    return head + body + "</form>"


# ---------------------------------------------------------------------------
# lower panels
# ---------------------------------------------------------------------------

_ACT_AGENT = {
    "discover": "discovery", "investigate": "planner", "analyze_trends": "trendhunter",
    "prepare_launch": "offer", "research": "researcher",
    "analyze_competition": "competitor", "write_copy": "copywriter",
    "package_deliverable": "content",
    "analyze_revenue": "analyst", "stop": "operator",
}

# task_log agent name  ->  map node key
_TASK_AGENT_NODE = {
    "market_scanner": "discovery", "evaluator": "evaluator",
    "opportunity_finder": "finder", "product_researcher": "researcher",
    "competitor_analyzer": "competitor", "copywriter": "copywriter",
    "content_creator": "content",
    "revenue_analyst": "analyst", "trend_hunter": "trendhunter",
}


def _lineage_edges(task_log: list[dict]) -> tuple[list[tuple[str, str]], dict]:
    """Real parent->child agent edges from the task log, with a per-edge
    dispatch count. The operator is the parent of every root task."""
    by_id = {e.get("task_id"): e for e in task_log}
    edges: dict[tuple[str, str], int] = {}
    for e in task_log:
        child = _TASK_AGENT_NODE.get(e.get("agent"))
        if child is None:
            continue
        parent_entry = by_id.get(e.get("parent_id"))
        if parent_entry is None:
            parent = "operator"
        else:
            parent = _TASK_AGENT_NODE.get(parent_entry.get("agent"))
        if parent is None or parent == child:
            continue
        edges[(parent, child)] = edges.get((parent, child), 0) + 1
    return list(edges), edges


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


def _summary_text(summary: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in summary.items()) if summary else ""


def _activity(agent_log: list[dict], limit: int = 24, task_log=None) -> str:
    merged: list[tuple] = []
    for e in agent_log or []:
        action = e.get("action", "")
        who = _ACT_AGENT.get(action, "operator")
        merged.append((e.get("ts", ""), action, who, e.get("reason", ""),
                       action in _MARKERS))
    for e in task_log or []:
        merged.append((e.get("ts", ""), e.get("capability", ""),
                       e.get("agent", ""),
                       _summary_text(e.get("summary", {})) or e.get("error", "") or "",
                       False))
    if not merged:
        return "<p class='muted'>No agent decisions recorded.</p>"
    merged.sort(key=lambda r: r[0])
    rows = ""
    for ts, what, who, detail, marker in reversed(merged[-limit:]):
        cls = " class='marker'" if marker else ""
        rows += (
            f"<tr><td class='muted mono'>{_esc(ts)}</td>"
            f"<td{cls}>{_esc(what)}</td>"
            f"<td class='muted'>{_esc(who)}</td>"
            f"<td>{_esc(detail)}</td></tr>"
        )
    return (
        "<div class='feed'><table><tr><th>when</th><th>action</th><th>agent</th>"
        f"<th>detail</th></tr>{rows}</table></div>"
    )


def _trends(trend: dict | None) -> str:
    if not trend:
        return "<p class='muted'>No trend analysis yet.</p>"
    kw = trend.get("keywords") or []
    kws = "".join(
        f"<tr><td>{_esc(w)}</td><td class='num'>{_num(n)}</td></tr>"
        for w, n in kw[:12]
    ) or "<tr><td class='muted' colspan='2'>no keyword repeats yet</td></tr>"
    srcs = " &middot; ".join(
        f"{_esc(s)} <b>{_num(n)}</b>" for s, n in trend.get("sources", {}).items()
    )
    return (
        f"<p class='muted mono'>{_esc(trend.get('ts', ''))} &middot; "
        f"{_num(trend.get('count', 0))} candidate(s) over "
        f"{_num(trend.get('runs', 0))} run(s)</p>"
        f"<p>sources: {srcs or '&mdash;'}</p>"
        f"<table><tr><th>keyword</th><th>n</th></tr>{kws}</table>"
    )


def _revenue_analysis(a: dict | None) -> str:
    if not a:
        return "<p class='muted'>No revenue analysis yet.</p>"
    p = a.get("portfolio", {})
    rows = "".join(
        f"<tr><td>{_esc(r['name'])}</td><td>{_esc(r['status'])}</td>"
        f"<td class='num'>{_num(r['revenue'])}</td>"
        f"<td class='num'>{_num(r['spent'])}</td>"
        f"<td class='num'>{_num(r['net'])}</td></tr>"
        for r in a.get("per_candidate", [])
    ) or "<tr><td class='muted' colspan='5'>no candidates with revenue or spend</td></tr>"
    best = a.get("best")
    worst = a.get("worst")
    return (
        f"<p class='muted mono'>{_esc(a.get('ts', ''))}</p>"
        f"<p>{_esc(a.get('readout', ''))}</p>"
        f"<p class='muted'>portfolio net <b>{_num(p.get('net', 0))}</b>"
        + (f" &middot; efficiency {_num(a['spend_efficiency'])}"
           if a.get('spend_efficiency') is not None else "")
        + (f" &middot; best {_esc(best['name'])} (${_num(best['net'])})" if best else "")
        + (f" &middot; worst {_esc(worst['name'])} (${_num(worst['net'])})" if worst else "")
        + "</p>"
        f"<p class='muted'>outcome signal: {_esc(a.get('outcome_signal', ''))}</p>"
        f"<table><tr><th>candidate</th><th>status</th><th>revenue</th>"
        f"<th>spent</th><th>net</th></tr>{rows}</table>"
    )


_OUTPUT_HEADLINE_KEYS = (
    "qc_status", "implementation_status", "confidence", "priority",
    "human_gate_required", "launched", "spent", "paid_count", "net_profit",
    "research_needed", "daemon",
)


def _clip(v: object, n: int = 48) -> str:
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


def _output_headline(output: dict) -> str:
    """A few real top-level fields from an agent's output - never a
    derived or invented value, each clipped so a big blob cannot bloat
    the row."""
    if not isinstance(output, dict):
        return ""
    parts = []
    for k in _OUTPUT_HEADLINE_KEYS:
        if k in output and len(parts) < 4:
            v = output[k]
            if isinstance(v, (list, dict)):
                v = f"{len(v)} item(s)"
            parts.append(f"{k}={_clip(v)}")
    if not parts:
        scalars = [k for k, v in output.items()
                   if isinstance(v, (str, int, float, bool))][:3]
        parts = [f"{k}={_clip(output[k])}" for k in scalars]
    return ", ".join(parts) or f"{len(output)} field(s)"


def _agent_outputs_panel(data: dict | None) -> str:
    """The most recent persisted output per agent, straight from
    data/agent_outputs.json. Nothing is inferred; an empty / missing file
    renders an honest note."""
    entries = data if isinstance(data, dict) else {}
    rows_data = []
    for cap, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        spec = roster.by_capability(cap)
        rows_data.append((
            str(entry.get("ts", "")),
            spec.name if spec else str(cap),
            spec.cluster if spec else "",
            "human" if (spec and spec.gate == "human") else "auto",
            str(entry.get("objective", "")),
            _output_headline(entry.get("output", {})),
        ))
    if not rows_data:
        return ("<p class='muted'>No agent outputs yet. Deterministic roster "
                "agents persist here (<span class='mono'>data/agent_outputs.json</span>) "
                "when dispatched through <span class='mono'>run_agent</span>.</p>")
    rows_data.sort(key=lambda r: r[0], reverse=True)
    rows = "".join(
        f"<tr><td>{_esc(name)}</td><td>{_esc(cluster)}</td><td>{_esc(gate)}</td>"
        f"<td class='mono'>{_esc(ts[:19].replace('T', ' '))}</td>"
        f"<td>{_esc(obj)}</td><td class='hl'>{_esc(hl)}</td></tr>"
        for ts, name, cluster, gate, obj, hl in rows_data
    )
    return (
        f"<p class='muted'>{len(rows_data)} agent(s) with a persisted output, "
        "newest first. Straight from agent_outputs.json - nothing inferred.</p>"
        "<div class='aout'><table><tr><th>agent</th><th>cluster</th><th>gate</th>"
        f"<th>when</th><th>objective</th><th>result</th></tr>{rows}</table></div>"
    )


_PIPE_STATUS_CLS = {"ok": "good", "failed": "bad", "blocked": "bad",
                    "skipped": "muted", "prepared": "good", "running": "warn",
                    "pending": "muted"}


def _agent_pipeline_panel(pipe: dict | None) -> str:
    """The real one-cycle agent pipeline for a qualified candidate, read
    from data/pipeline.json. Nothing is inferred; no run -> honest note."""
    from .pipeline import STEP_ORDER

    if not isinstance(pipe, dict) or not pipe.get("candidate"):
        return ("<p class='muted'>No pipeline run yet. "
                "<span class='mono'>revenue_os pipeline run &lt;candidate&gt;</span> "
                "chains Opportunity Finder &rarr; Product Researcher &rarr; Competitor "
                "Analyzer &rarr; Supplier Finder &rarr; Copywriter &rarr; Content Creator "
                "&rarr; Designer &rarr; Store Builder &rarr; Quality Control &rarr; human "
                "gate.</p>")
    steps = pipe.get("steps", {}) or {}
    rows = ""
    for i, cap in enumerate(STEP_ORDER, 1):
        spec = roster.by_capability(cap)
        entry = steps.get(cap, {"status": "pending"})
        stt = entry.get("status", "pending")
        cls = _PIPE_STATUS_CLS.get(stt, "dim")
        gate = " &middot; human-gated" if spec and spec.gate == "human" else ""
        raw_detail = entry.get("reason", "") or _summary_text(entry.get("summary", {}))
        detail = _esc(_clip(raw_detail, 140))
        rows += (
            f"<tr><td class='num'>{i}</td>"
            f"<td>{_esc(spec.name if spec else cap)}{gate}</td>"
            f"<td class='{cls}'>{_esc(stt)}</td>"
            f"<td class='muted'>{detail}</td></tr>"
        )
    hg = pipe.get("human_gate") or {}
    status = _esc(pipe.get("status", ""))
    gate_html = ""
    if hg:
        bullets = "".join(
            f"<li>{_esc(x)}</li>" for x in
            (hg.get("blocking_issues") or hg.get("human_gated_next") or [])
        )
        gate_html = (
            f"<p class='muted'><b>Human gate:</b> {_esc(hg.get('reason', ''))}</p>"
            + (f"<ul>{bullets}</ul>" if bullets else "")
        )
    err = pipe.get("error")
    err_html = f"<p class='bad'>error: {_esc(err)}</p>" if err else ""
    return (
        f"<p class='muted'>candidate <b>{_esc(pipe.get('candidate'))}</b> &middot; "
        f"status <b>{status}</b> &middot; updated "
        f"<span class='mono'>{_esc(str(pipe.get('updated_at', ''))[:19])}</span>. "
        "Straight from data/pipeline.json - nothing inferred.</p>"
        f"{err_html}"
        "<table><tr><th>#</th><th>agent</th><th>state</th><th>detail</th></tr>"
        f"{rows}</table>{gate_html}"
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
    searches = spend.get("total_searches", 0)
    search_txt = f" &middot; {_num(searches)} web search(es)" if searches else ""
    return (
        f"<p>total <b>${_num(spend['total_cost_usd'])}</b> &middot; "
        f"{_num(spend['runs'])} run(s) &middot; "
        f"{_num(spend['total_api_calls'])} api call(s){search_txt}</p>{budget}"
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


def _sources(sources) -> str:
    """Render a web-note's sources as scheme-stripped host + title text
    (no live links, so the no-'://' invariant holds)."""
    if not sources:
        return ""
    items = "".join(
        f"<li><span class='mono'>{_esc(str(s.get('url', '')).split('://')[-1][:60])}</span> "
        f"&mdash; {_esc(s.get('title', ''))}</li>"
        for s in sources[:6]
    )
    return f"<ul class='muted' style='font-size:.72rem'>{items}</ul>"


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
                + _sources(research.get("sources"))
            )
        comp = c.get("competition") or {}
        cmp_html = ""
        if comp.get("verdict"):
            cmp_html = (
                f"<p><b>competition: {_esc(comp['verdict'])}</b> &mdash; "
                f"{_esc(comp.get('rationale', ''))} "
                f"<span class='muted'>({_esc(comp.get('basis', ''))})</span></p>"
                + _sources(comp.get("sources"))
            )
        draft = c.get("launch_draft") or {}
        draft_html = ""
        if draft.get("headline"):
            draft_html = (
                f"<p><b>launch copy:</b> {_esc(draft['headline'])} "
                f"<span class='muted'>&mdash; CTA: {_esc(draft.get('primary_cta', ''))} "
                f"({_esc(draft.get('basis', ''))})</span></p>"
            )
        deliv = c.get("deliverable") or {}
        deliv_html = ""
        if deliv.get("dir"):
            deliv_html = (
                f"<p><b>deliverable:</b> <span class='mono'>{_esc(deliv['dir'])}/"
                f"landing.html</span> <span class='muted'>(not published)</span></p>"
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
            f"<details><summary>{summary}</summary>{rat}{res}{cmp_html}"
            f"{draft_html}{deliv_html}{budget}{off}"
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
    ("First sale", "#first-sale", "evaluator"),
    ("Agents", "#agents", "generic"),
    ("Acquisition", "#acquisition", "finder"),
    ("Loop", "#continuous", "operator"),
    ("Experiments", "#experiments", "evaluator"),
    ("Pipeline", "#pipeline", "finder"),
    ("Outputs", "#agent-outputs", "content"),
    ("Tasks", "#tasks", "planner"),
    ("Opportunities", "#opportunities", "discovery"),
    ("Finances", "#finances", "offer"),
    ("Automations", None, "decision"),
    ("Logs", "#activity", "researcher"),
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
        "<nav class='rail'><div class='brand'><b>Revenue&nbsp;OS</b>"
        "<span class='brand-sub'>multi-agent runtime</span></div>"
        f"<div class='nav'>{items}</div></nav>"
    )


def render_html(report: dict, generated_at: str, *,
                agent_log: list[dict] | None = None,
                session: dict | None = None,
                spend_entries: list[dict] | None = None,
                goal: dict | None = None,
                task_log: list[dict] | None = None,
                trend: dict | None = None,
                revenue_analysis: dict | None = None,
                agent_outputs: dict | None = None,
                pipeline: dict | None = None,
                acquisition: dict | None = None,
                interactive: bool = False,
                flash: str | None = None,
                csrf: str | None = None) -> str:
    """Build the full standalone command-center HTML document.

    interactive=True renders the human gates as POST forms to /action
    (used by `dashboard-serve`); the default renders the read-only view.
    """
    queue = report["action_queue"]
    flash_html = ""
    if flash:
        cls = " err" if flash.lower().startswith("error") else ""
        flash_html = f"<div class='flash{cls}'>{_esc(flash)}</div>"
    body = (
        _sidebar()
        + "<main id='top'>"
        + f"<p class='muted mono' style='margin:.2rem 0 .8rem'>generated {_esc(generated_at)}</p>"
        + flash_html
        + _topbar(report, session, agent_log, spend_entries, goal)
        + _attention(queue, interactive, csrf)
        + "<section id='first-sale'><h2>First sale readiness</h2>"
        + _first_sale_panel(acquisition) + "</section>"
        + "<section id='agents'><h2>Agents</h2>"
        + _agent_map(agent_log or [], spend_entries, goal, session, report,
                     bool(queue), task_log or [])
        + f"<h3>All {len(roster.AGENTS)} agents</h3>"
        + _cluster_flow(agent_outputs)
        + "<h3>Target roster</h3>"
        + _roster_panel()
        + "</section>"
        + "<section id='acquisition'><h2>Customer acquisition</h2>"
        + _acquisition_panel(acquisition) + "</section>"
        + "<section id='continuous'><h2>Continuous revenue loop</h2>"
        + _continuous_panel(acquisition) + "</section>"
        + "<section id='experiments'><h2>Revenue experiments</h2>"
        + _experiments_panel(acquisition) + "</section>"
        + "<section id='pipeline'><h2>Agent pipeline</h2>"
        + _agent_pipeline_panel(pipeline) + "</section>"
        + "<section id='agent-outputs'><h2>Agent outputs</h2>"
        + _agent_outputs_panel(agent_outputs) + "</section>"
        + "<div class='cols'>"
        + "<section id='tasks'><h2>Task queue</h2>"
        + _task_queue(report["action_queue"]) + "</section>"
        + "<section id='activity'><h2>Recent activity</h2>"
        + _activity(agent_log or [], task_log=task_log or []) + "</section>"
        + "</div>"
        + "<div class='cols'>"
        + "<section id='opportunities'><h2>Pipeline</h2>"
        + _pipeline(report["status_counts"]) + "</section>"
        + "<section><h2>Trends</h2>"
        + _trends(trend) + "</section>"
        + "</div>"
        + "<div class='cols'>"
        + "<section id='finances'><h2>LLM spend</h2>"
        + _spend(report.get("llm_spend")) + "</section>"
        + "<section><h2>Outcomes</h2>"
        + _outcomes(report.get("outcomes")) + "</section>"
        + "</div>"
        + "<section><h2>Last discovery</h2>"
        + _last_discovery(report.get("last_discovery")) + "</section>"
        + "<section><h2>Revenue / ROI</h2>"
        + _roi(report["roi"], report, goal) + "</section>"
        + "<section><h2>Revenue analysis</h2>"
        + _revenue_analysis(revenue_analysis) + "</section>"
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
