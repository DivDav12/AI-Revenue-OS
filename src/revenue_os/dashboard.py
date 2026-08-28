"""Static HTML snapshot of the pipeline.

render_html() is pure: a report dict (from report.pipeline_report) and a
timestamp string in, a self-contained HTML document out. Inline CSS,
no JavaScript, no external requests. All candidate-derived text is
HTML-escaped: titles originate from an untrusted external source.
"""

from __future__ import annotations

from html import escape

from . import lifecycle

_STYLE = """
body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
.generated { color: #666; margin-bottom: 1.5rem; }
h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; }
table { border-collapse: collapse; width: 100%; max-width: 900px; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #ddd; }
th { background: #f4f4f4; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: #888; }
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
        f"<td>{_esc(item['next_action'])}</td></tr>"
        for item in queue
    )
    return (
        "<table><tr><th>candidate</th><th>status</th>"
        f"<th>next action</th></tr>{rows}</table>"
    )


def _candidate_table(candidates: list[dict]) -> str:
    if not candidates:
        return "<p class='muted'>No candidates.</p>"
    rows = "".join(
        f"<tr><td>{_esc(c['name'])}</td>"
        f"<td>{_esc(c['status'])}</td>"
        f"<td class='num'>{_num(c['score'])}</td>"
        f"<td>{_esc(c['verdict'])}</td></tr>"
        for c in candidates
    )
    return (
        "<table><tr><th>candidate</th><th>status</th>"
        f"<th>score</th><th>verdict</th></tr>{rows}</table>"
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
            "<h2>Candidates</h2>",
            _candidate_table(report.get("candidates", [])),
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
