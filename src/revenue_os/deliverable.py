"""Deliverable Packager (Content Creator) - assembles a publishable
landing page from a validated candidate's offer + draft copy.

Deterministic: no LLM, no I/O of its own. The agent returns the rendered
files; workflow.package_deliverables writes them and attaches a
`deliverable` note to the candidate.

Honesty constraints: the page captures nothing (the waitlist form is a
labelled placeholder the human wires to a real provider), carries no
payment flow, impersonates nothing, and states plainly that it is a
pre-launch demand test.
"""

from __future__ import annotations

from html import escape

from .agent import Agent
from .messages import Result, Task
from .store import now_iso


def _esc(v: object) -> str:
    return escape(str(v), quote=True)


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in str(body).split("\n\n") if p.strip()]


_STYLE = """
*{box-sizing:border-box}
body{margin:0;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  Helvetica,Arial,sans-serif;color:#1b2430;background:#f7f8fa}
.wrap{max-width:640px;margin:0 auto;padding:3rem 1.25rem 4rem}
h1{font-size:2rem;line-height:1.25;margin:0 0 .6rem}
.sub{font-size:1.15rem;color:#4a5568;margin:0 0 2rem}
p{margin:0 0 1rem}
ul{margin:0 0 2rem;padding-left:1.25rem}
li{margin:.35rem 0}
.offer{border:1px solid #d9dee6;border-radius:12px;padding:1.25rem 1.4rem;
  background:#fff;margin:2rem 0}
.offer .what{font-weight:600}
.offer .price{font-size:1.4rem;font-weight:700;margin:.3rem 0}
.offer .est{font-size:.85rem;color:#718096;font-weight:400}
.cta{display:inline-block;margin-top:.5rem;font-weight:600}
.waitlist{border:1px dashed #b7c0cc;border-radius:12px;padding:1.25rem 1.4rem;
  background:#fff;margin:2rem 0}
.waitlist .note{font-size:.85rem;color:#a0562a;margin-bottom:.6rem}
.waitlist input,.waitlist button{font:inherit;padding:.55rem .8rem;border-radius:8px;
  border:1px solid #cdd4de}
.waitlist input{width:60%}
.waitlist button{background:#1b2430;color:#fff;border-color:#1b2430;cursor:not-allowed;
  opacity:.6}
details{border-top:1px solid #e2e6ec;padding:.7rem 0}
summary{cursor:pointer;font-weight:600}
details p{margin:.5rem 0 0;color:#4a5568}
.foot{margin-top:2.5rem;font-size:.85rem;color:#718096;border-top:1px solid #e2e6ec;
  padding-top:1rem}
""".strip()


def render_landing_html(candidate: dict, offer: dict, draft: dict,
                        plan: dict) -> str:
    offer = offer or {}
    draft = draft or {}
    plan = plan or {}

    headline = (draft.get("headline") or offer.get("what_is_sold")
                or candidate.get("description") or candidate.get("name", ""))
    sub = draft.get("subheadline") or offer.get("positioning") or ""
    paras = _paragraphs(draft.get("body", "")) or (
        [plan["hypothesis"]] if plan.get("hypothesis") else [])

    bullets = ""
    what = offer.get("what_is_sold", "")
    if what:
        bullets = f"<ul><li>{_esc(what)}</li></ul>"

    price = offer.get("price")
    currency = offer.get("currency", "USD")
    est = " <span class='est'>(estimated - you set the real price)</span>" \
        if offer.get("price_is_estimate") else ""
    cta = draft.get("primary_cta") or offer.get("call_to_action") or "Join the waitlist"
    offer_box = ""
    if what or price is not None:
        offer_box = (
            "<div class='offer'>"
            + (f"<div class='what'>{_esc(what)}</div>" if what else "")
            + (f"<div class='price'>{_esc(price)} {_esc(currency)}{est}</div>"
               if price is not None else "")
            + f"<div class='cta'>&rarr; {_esc(cta)}</div>"
            + "</div>"
        )

    faq = ""
    for item in draft.get("faq", []) or []:
        q, a = _esc(item.get("question", "")), _esc(item.get("answer", ""))
        if q and a:
            faq += f"<details><summary>{q}</summary><p>{a}</p></details>"

    body_html = "".join(f"<p>{_esc(p)}</p>" for p in paras)
    metric = plan.get("success_metric", "")

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(headline)}</title>\n<style>\n{_STYLE}\n</style>\n</head>\n<body>\n"
        "<div class='wrap'>\n"
        f"<h1>{_esc(headline)}</h1>\n"
        + (f"<p class='sub'>{_esc(sub)}</p>\n" if sub else "")
        + body_html + bullets + offer_box
        + "<div class='waitlist'>\n"
        "<!-- WAITLIST FORM PLACEHOLDER - this page captures nothing. "
        "Replace this block with a real form embed (Tally / Carrd / Google Form). -->\n"
        "<div class='note'>Placeholder - wire this to a real form provider before publishing.</div>\n"
        "<input type='email' placeholder='you@example.com' disabled> "
        "<button type='button' disabled>Join the waitlist</button>\n"
        "</div>\n"
        + faq
        + "<div class='foot'>Early access - nothing is charged. This is a "
        "pre-launch demand test"
        + (f" (goal: {_esc(metric)})" if metric else "") + ".</div>\n"
        "</div>\n</body>\n</html>\n"
    )


def render_readme(candidate: dict, plan: dict, files: list[str]) -> str:
    plan = plan or {}
    metric = plan.get("success_metric", "(see the validation plan)")
    name = candidate.get("name", "")
    return (
        f"Deliverable for: {name}\n"
        f"Files: {', '.join(files)}\n\n"
        "Assembled from the offer + draft copy. It captures nothing on its own.\n\n"
        "TO RUN THE REAL VALIDATION TEST\n"
        "1. Open landing.html, find \"WAITLIST FORM PLACEHOLDER\".\n"
        "2. Replace that block with a real form embed:\n"
        "     - Tally (tally.so, free) - emails land in Tally / a Google Sheet\n"
        "     - Carrd (carrd.co, free) - built-in form -> Mailchimp / Sheet\n"
        "     - Google Forms - simplest\n"
        "3. Publish landing.html for free:\n"
        "     - Carrd, GitHub Pages, Netlify Drop, Cloudflare Pages\n"
        "4. Share the URL where your audience already is and it is on-topic.\n"
        "     Use one UTM-tagged link per channel so you know what worked.\n"
        f"5. Measure for the validation window. Success metric: {metric}\n"
        "     Count signups; sanity-check the signup / visitor rate.\n"
        "6. Only then record the outcome with the REAL number:\n"
        f"     revenue_os outcome \"{name}\" validated --metric \"N signups in D days\"\n"
        "     (or 'rejected' if it misses). Do NOT record before you have a number.\n"
    )


class DeliverablePackagerAgent(Agent):
    role = "content_creator"
    objective = "Assemble a publishable landing page from the offer and copy."
    capabilities = ("package_deliverable",)

    def run(self, task: Task) -> Result:
        candidate = task.payload.get("candidate")
        offer = task.payload.get("offer")
        if not isinstance(candidate, dict) or not isinstance(offer, dict) or not offer:
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload needs a candidate dict and a non-empty offer dict",
            )
        draft = task.payload.get("draft") or {}
        plan = task.payload.get("plan") or {}
        files = ["landing.html", "README.txt"]
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={
                "candidate_name": candidate["name"],
                "landing_html": render_landing_html(candidate, offer, draft, plan),
                "readme": render_readme(candidate, plan, files),
                "deliverable": {
                    "dir": f"deliverables/{candidate['name']}",
                    "files": files,
                    "has_copy": bool(draft),
                    "packaged_at": now_iso(),
                    "basis": "assembled from offer + draft, not published",
                },
            },
        )
