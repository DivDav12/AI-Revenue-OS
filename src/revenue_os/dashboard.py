"""Static HTML snapshot of the pipeline.

render_html() is pure: a report dict (from report.pipeline_report) and a
timestamp string in, a self-contained HTML document out. Inline CSS,
no JavaScript, no external requests. All candidate-derived text is
HTML-escaped: titles originate from an untrusted external source.
"""

from __future__ import annotations

from html import escape

from . import lifecycle
from .opportunity import CRITERIA
from .report import NEUTRAL_SCORE

_STYLE = """
body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
.generated { color: #666; margin-bottom: 1.5rem; }
h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; }
table { border-collapse: collapse; width: 100%; max-width: 900px; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #ddd; }
th { background: #f4f4f4; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.low { color: #b00; font-weight: bold; }
.muted { color: #888; }
details { margin: .25rem 0; max-width: 900px; }
summary { cursor: pointer; padding: .2rem 0; }
details table { margin: .4rem 0 .8rem; max-width: 460px; }
""".strip()


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def _num(value: object) -> str:
    return _esc(value)


def _status_table(status_counts: dict) -> str:
    rows = "".join(
        f"<tr><td>{_esc(status)}</td>"
        f"<td class='num'>{_num(status_counts.get(status, 0))}</td></tr>"
        for status in lifecycle.STATUSES
    )
    return f"<table><tr><th>status</th><th>count</th></tr>{rows}</table>"


def _action_queue(queue: list[dict]) -> str:
    if not queue:
        return "<p class='muted'>Nothing awaiting a human.</p>"
    rows = "".join(
        f"<tr><td>{_esc(item['name'])}</td>"
        f"<td>{_esc(item['status'])}</td>"
        f"<td class='num'>{_num(item.get('age_days', 0))}d</td>"
        + (
            "<td class='low'>stale</td>" if item.get("stale")
            else "<td></td>"
        )
        + f"<td>{_esc(item['next_action'])}</td></tr>"
        for item in queue
    )
    n_stale = sum(1 for i in queue if i.get("stale"))
    caption = f"<p>{len(queue)} awaiting a human"
    caption += f", {n_stale} stale</p>" if n_stale else "</p>"
    return caption + (
        "<table><tr><th>candidate</th><th>status</th><th>age</th>"
        f"<th></th><th>next action</th></tr>{rows}</table>"
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
        budget_html = ""
        if c.get("plan_needs_budget"):
            budget_html = (
                f"<p><strong>validation needs a budget decision: "
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
            f"<details><summary>{summary}</summary>{rationale_html}{budget_html}"
            f"{offer_html}{_breakdown_table(c.get('breakdown', {}))}</details>"
        )
    return blocks


_DISCOVERY_FIELDS = (
    "source", "limit", "fetched", "filtered_out", "dropped_below_score",
    "evaluated", "kept", "new", "refreshed", "shortlisted",
    "evaluator", "est_cost_usd", "actual_cost_usd", "cost_ceiling_hit",
    "eval_cache_hits", "eval_cache_misses",
)


def _last_discovery(entry: dict | None) -> str:
    if not entry:
        return "<p class='muted'>No discovery run recorded.</p>"
    rows = "".join(
        f"<tr><td>{_esc(field)}</td>"
        f"<td class='num'>{_esc(entry.get(field, ''))}</td></tr>"
        for field in _DISCOVERY_FIELDS
    )
    return (
        f"<p class='generated'>{_esc(entry.get('ts', ''))}</p>"
        f"<table><tr><th>field</th><th>value</th></tr>{rows}</table>"
    )


def _llm_spend_section(spend: dict | None) -> str:
    if not spend or spend.get("runs", 0) == 0:
        return "<p class='muted'>No LLM runs recorded.</p>"
    by = spend["by_activity"]
    cap_html = ""
    if "cap_usd" in spend:
        cap_html = (
            f"<p>cap <strong>${_num(spend['cap_usd'])}</strong> &nbsp; "
            f"remaining <strong>${_num(spend['remaining_usd'])}</strong></p>"
        )
    return (
        f"<p>total <strong>${_num(spend['total_cost_usd'])}</strong> over "
        f"{_num(spend['runs'])} run(s), {_num(spend['total_api_calls'])} api call(s)</p>"
        f"{cap_html}"
        f"<table><tr><th>activity</th><th>cost</th></tr>"
        f"<tr><td>evaluate</td><td class='num'>${_num(by['evaluate'])}</td></tr>"
        f"<tr><td>plan</td><td class='num'>${_num(by['plan'])}</td></tr>"
        f"<tr><td>offer</td><td class='num'>${_num(by['offer'])}</td></tr></table>"
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
    rows = ""
    for name in CRITERIA:
        c = retro["by_criterion"][name]
        rows += (
            f"<tr><td>{_esc(name)}</td>"
            f"<td class='num'>{_num(c['validated_avg'])}</td>"
            f"<td class='num'>{_num(c['rejected_avg'])}</td>"
            f"<td class='num'>{_num(c['gap'])}</td></tr>"
        )
    return head + (
        "<table><tr><th>criterion</th><th>validated</th>"
        f"<th>rejected</th><th>gap</th></tr>{rows}</table>"
    )


def _roi_section(roi: dict) -> str:
    totals = (
        f"<p>revenue <strong>{_num(roi['grand_revenue'])}</strong> &nbsp; "
        f"spent <strong>{_num(roi['grand_spent'])}</strong> &nbsp; "
        f"net <strong>{_num(roi['grand_net'])}</strong></p>"
    )
    per = roi.get("candidates", {})
    if not per:
        return totals + "<p class='muted'>No per-candidate revenue or spend yet.</p>"
    rows = "".join(
        f"<tr><td>{_esc(name)}</td>"
        f"<td>{_esc(row['status'])}</td>"
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


def render_html(report: dict, generated_at: str) -> str:
    """Build the full standalone HTML document for a pipeline report."""
    body = "".join(
        [
            "<h1>AI-Revenue-OS &mdash; pipeline</h1>",
            f"<p class='generated'>generated {_esc(generated_at)}</p>",
            "<h2>Status</h2>",
            _status_table(report["status_counts"]),
            "<h2>Action queue</h2>",
            _action_queue(report["action_queue"]),
            "<h2>Last discovery</h2>",
            _last_discovery(report.get("last_discovery")),
            "<h2>LLM spend</h2>",
            _llm_spend_section(report.get("llm_spend")),
            "<h2>Candidates</h2>",
            _candidate_blocks(report.get("candidates", [])),
            "<h2>Outcomes</h2>",
            _outcomes_section(report.get("outcomes")),
            "<h2>ROI</h2>",
            _roi_section(report["roi"]),
        ]
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>AI-Revenue-OS pipeline</title>\n"
        f"<style>\n{_STYLE}\n</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )
