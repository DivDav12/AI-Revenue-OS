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

import json
from html import escape
from urllib.parse import quote

from .agent import Agent
from .intake import INTAKE_FIELDS
from .messages import Result, Task
from .store import now_iso

_INTAKE_PLACEHOLDER = "REPLACE_WITH_YOUR_FORM_ENDPOINT"
_INTAKE_TEXTAREA = {"sells", "target_audience", "customer_situation",
                    "previous_attempts", "biggest_problem"}
_INTAKE_REQUIRED = {"name", "email", "sells"}


def _esc(v: object) -> str:
    return escape(str(v), quote=True)


def _js(v: object) -> str:
    """A safe JS string literal: JSON-encoded, then neutralise the three
    characters that could break out of a <script> element."""
    return (json.dumps(str(v))
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


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


_CHECKOUT_STYLE = """
*{box-sizing:border-box}
body{margin:0;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  Helvetica,Arial,sans-serif;color:#1b2430;background:#f7f8fa}
.wrap{max-width:560px;margin:0 auto;padding:3rem 1.25rem 4rem}
h1{font-size:1.8rem;line-height:1.25;margin:0 0 .6rem}
.sub{font-size:1.1rem;color:#4a5568;margin:0 0 1.75rem}
p{margin:0 0 1rem}
.card{border:1px solid #d9dee6;border-radius:12px;padding:1.4rem;background:#fff;
  margin:1.75rem 0}
.price{font-size:1.7rem;font-weight:700;margin:.2rem 0 .1rem}
.what{font-weight:600;margin-bottom:.4rem}
ul{margin:0 0 1.5rem;padding-left:1.2rem}
li{margin:.4rem 0}
.disclaimer{font-size:.9rem;color:#8a4b1f;background:#fdf4ec;border:1px solid #f0dcc8;
  border-radius:8px;padding:.7rem .9rem;margin:.2rem 0 1rem}
#paypal-button-container{margin-top:1rem;min-height:45px}
#pay-result{margin-top:1rem;padding:.9rem 1rem;border-radius:8px;background:#eef6ee;
  border:1px solid #cfe6cf;display:none}
#pay-result.err{background:#fdeeee;border-color:#e6cfcf}
#pay-result code{font-size:.9rem;word-break:break-all}
.terms{font-size:.9rem;color:#4a5568}
.foot{margin-top:2rem;font-size:.85rem;color:#718096;border-top:1px solid #e2e6ec;
  padding-top:1rem}
h2{font-size:1.3rem;margin:2rem 0 .5rem}
.hidden{display:none}
form.intake{margin:1.25rem 0}
form.intake label{display:block;margin:.8rem 0;font-weight:600;font-size:.95rem}
form.intake input,form.intake textarea{width:100%;font:inherit;font-weight:400;
  padding:.5rem .6rem;border:1px solid #cdd4de;border-radius:8px}
form.intake button{margin-top:1rem;font:inherit;font-weight:600;padding:.6rem 1.1rem;
  background:#1b2430;color:#fff;border:1px solid #1b2430;border-radius:8px;cursor:pointer}
.intake-note{font-size:.9rem;color:#4a5568;margin-top:.6rem}
""".strip()


def render_intake_fields() -> str:
    """The buyer-intake controls, one definition shared by the checkout
    page and the standalone intake page."""
    rows = []
    for key, label in INTAKE_FIELDS:
        req = " required" if key in _INTAKE_REQUIRED else ""
        star = " *" if key in _INTAKE_REQUIRED else ""
        if key in _INTAKE_TEXTAREA:
            control = f"<textarea name='{key}' rows='3'{req}></textarea>"
        else:
            typ = "email" if key == "email" else "text"
            control = f"<input type='{typ}' name='{key}'{req}>"
        rows.append(
            f"<label>{_esc(label)}{star}<br>{control}</label>"
        )
    return "\n".join(rows)


def _intake_form(candidate: str, *, form_action: str, form_id: str,
                 submit: str) -> str:
    action = form_action or _INTAKE_PLACEHOLDER
    return (
        f"<form id='{form_id}' class='intake' method='post' "
        f"action='{_esc(action)}'>\n"
        f"<input type='hidden' name='candidate' value='{_esc(candidate)}'>\n"
        "<input type='hidden' name='order_id' value=''>\n"
        "<input type='hidden' name='capture_id' value=''>\n"
        "<input type='hidden' name='lead_id' value=''>\n"
        + render_intake_fields()
        + f"\n<button type='submit'>{_esc(submit)}</button>\n</form>\n"
    )


def render_checkout_html(candidate: dict, offer: dict, *,
                         client_id: str, currency: str = "EUR",
                         form_action: str = "") -> str:
    """A real one-item PayPal checkout page for a human-priced offer.

    Self-contained except the PayPal JS SDK. The order it creates sets
    purchase_units[].custom_id to the exact candidate name, so a later
    `paypal-sync` / `paypal-verify` books the payment against this
    candidate. This page never records revenue itself.
    """
    offer = offer or {}
    name = str(candidate.get("name", ""))
    if not name:
        raise ValueError("candidate has no name")
    if not client_id:
        raise ValueError("client_id is required")
    try:
        price = round(float(offer["price"]), 2)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("offer needs a numeric price") from exc
    if price <= 0:
        raise ValueError("offer price must be positive")

    currency = (offer.get("currency") or currency or "EUR").upper()
    amount = f"{price:.2f}"
    what = str(offer.get("what_is_sold") or candidate.get("description") or name)
    positioning = str(offer.get("positioning") or "")
    cta = str(offer.get("call_to_action") or "Pay now")
    delivery = str(offer.get("delivery") or "manual")
    includes = [str(i).strip() for i in (offer.get("includes") or []) if str(i).strip()]
    delivery_note = str(offer.get("delivery_note") or "")
    disclaimer = str(offer.get("disclaimer") or "")

    sdk_url = (
        "https://www.paypal.com/sdk/js?client-id="
        + quote(client_id, safe="")
        + "&currency=" + quote(currency, safe="")
        + "&intent=capture"
    )
    # JS string literals - escaped so nothing can break out of <script>.
    js_custom_id = _js(name)
    js_amount = _js(amount)
    js_currency = _js(currency)
    js_desc = _js(what[:127])

    script = (
        "paypal.Buttons({\n"
        "  createOrder: function(data, actions) {\n"
        "    return actions.order.create({\n"
        "      intent: 'CAPTURE',\n"
        "      purchase_units: [{\n"
        f"        amount: {{ value: {js_amount}, currency_code: {js_currency} }},\n"
        f"        custom_id: {js_custom_id},\n"
        f"        description: {js_desc}\n"
        "      }]\n"
        "    });\n"
        "  },\n"
        "  onApprove: function(data, actions) {\n"
        "    return actions.order.capture().then(function(details) {\n"
        "      var cap = '';\n"
        "      try { cap = details.purchase_units[0].payments.captures[0].id; }\n"
        "      catch (e) {}\n"
        "      var box = document.getElementById('pay-result');\n"
        "      box.style.display = 'block';\n"
        "      var oid = String(data.orderID).replace(/[<>&]/g, '');\n"
        "      var form = document.getElementById('intake-form');\n"
        "      if (form) {\n"
        "        form.querySelector(\"[name='order_id']\").value = data.orderID;\n"
        "        form.querySelector(\"[name='capture_id']\").value = cap;\n"
        "        var lf = form.querySelector(\"[name='lead_id']\");\n"
        "        if (lf) lf.value = (new URLSearchParams(location.search).get('lead')"
        " || '').replace(/[^A-Za-z0-9_-]/g, '');\n"
        "        document.getElementById('intake').classList.remove('hidden');\n"
        "        document.getElementById('intake').scrollIntoView();\n"
        "        box.innerHTML = 'Payment received (order <code>' + oid + '</code>)."
        " Please complete the short form below so we can build your plan.';\n"
        "      } else {\n"
        "        box.innerHTML = 'Payment received. Your order ID is <code>' + oid +"
        " '</code>. You will be contacted at your PayPal email to arrange"
        " delivery.';\n"
        "      }\n"
        "    });\n"
        "  },\n"
        "  onError: function(err) {\n"
        "    var box = document.getElementById('pay-result');\n"
        "    box.className = 'err'; box.style.display = 'block';\n"
        "    box.textContent = 'Payment could not be completed. Nothing was charged.';\n"
        "  }\n"
        "}).render('#paypal-button-container');"
    )

    est_note = ""  # a paid_offer is never an estimate; guard anyway
    if offer.get("price_is_estimate"):
        est_note = " <span class='terms'>(indicative price)</span>"

    includes_html = ""
    if includes:
        items = "".join(f"<li>{_esc(i)}</li>" for i in includes)
        includes_html = f"<p class='what'>What you get</p>\n<ul>{items}</ul>\n"

    delivery_line = (
        _esc(delivery_note) if delivery_note
        else f"Delivery: {_esc(delivery)}. {_esc(cta)}"
    )
    disclaimer_html = (
        f"<p class='disclaimer'>{_esc(disclaimer)}</p>\n" if disclaimer else ""
    )

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(what)}</title>\n<style>\n{_CHECKOUT_STYLE}\n</style>\n"
        f"<script src=\"{_esc(sdk_url)}\"></script>\n"
        "</head>\n<body>\n<div class='wrap'>\n"
        f"<h1>{_esc(what)}</h1>\n"
        + (f"<p class='sub'>{_esc(positioning)}</p>\n" if positioning else "")
        + includes_html
        + "<div class='card'>\n"
        f"<div class='what'>{_esc(what)}</div>\n"
        f"<div class='price'>{_esc(amount)} {_esc(currency)}{est_note}</div>\n"
        f"<p class='terms'>{delivery_line}</p>\n"
        + disclaimer_html
        + "<div id='paypal-button-container'></div>\n"
        "<div id='pay-result'></div>\n"
        "</div>\n"
        "<div id='intake' class='hidden'>\n"
        "<h2>Your business details</h2>\n"
        "<p class='intake-note'>Payment complete. Tell us about your business so "
        "we can build your personalised plan. Fields marked * are required.</p>\n"
        + _intake_form(name, form_action=form_action, form_id="intake-form",
                       submit="Send my details")
        + "<p class='intake-note'>If the form does not send, email your answers "
        "and your PayPal order ID to the address that sold you this plan.</p>\n"
        "</div>\n"
        "<p class='terms'>Payment is handled entirely by PayPal. If the plan "
        "cannot be delivered, the payment is refunded in full via PayPal.</p>\n"
        f"<div class='foot'>Sold by the operator of this page. Order reference: "
        f"<code>{_esc(name)}</code>.</div>\n"
        "</div>\n"
        f"<script>\n{script}\n</script>\n"
        "</body>\n</html>\n"
    )


_INTAKE_FILL_SCRIPT = (
    "(function(){\n"
    "  var q = new URLSearchParams(location.search);\n"
    "  var map = {order: 'order_id', capture: 'capture_id', lead: 'lead_id'};\n"
    "  Object.keys(map).forEach(function(k){\n"
    "    var v = (q.get(k) || '').replace(/[^A-Za-z0-9_-]/g, '');\n"
    "    var el = document.querySelector(\"[name='\" + map[k] + \"']\");\n"
    "    if (el) el.value = v;\n"
    "  });\n"
    "})();"
)


def render_intake_html(candidate: str, *, form_action: str = "",
                       product: str = "Customer Launch Plan") -> str:
    """Standalone intake form for a buyer who already paid.

    The order / capture id come from the query string
    (?order=...&capture=...), so the operator can email this link after
    a payment shows up in `paypal-sync`. Posts to the same operator-
    configured endpoint as the checkout page's inline form.
    """
    if not str(candidate).strip():
        raise ValueError("candidate is required")
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(product)} - your details</title>\n"
        f"<style>\n{_CHECKOUT_STYLE}\n</style>\n</head>\n<body>\n<div class='wrap'>\n"
        f"<h1>{_esc(product)} - your details</h1>\n"
        "<p>You have completed payment. Tell us about your business so we can "
        "build your personalised plan. Fields marked * are required.</p>\n"
        + _intake_form(candidate, form_action=form_action, form_id="intake-form",
                       submit="Send my details")
        + "<p class='intake-note'>If the form does not send, email your answers "
        "and your PayPal order ID to the address that sold you this plan.</p>\n"
        "</div>\n"
        f"<script>\n{_INTAKE_FILL_SCRIPT}\n</script>\n"
        "</body>\n</html>\n"
    )


def render_launch_plan_md(intake_entry: dict) -> str:
    """Deterministic Markdown for a drafted/approved Customer Launch Plan.

    No LLM. Renders what launch_plan already produced plus the buyer's
    business context. PDF conversion is a manual step (pandoc /
    print-to-PDF); nothing here is sent to the customer.
    """
    entry = intake_entry or {}
    plan = entry.get("plan") or {}
    if not plan:
        raise ValueError("intake entry has no plan to render")
    fields = entry.get("fields") or {}

    def esc(v):
        return str(v).replace("\r", "").strip()

    out: list[str] = ["# Customer Launch Plan", ""]
    out.append(f"Prepared for **{esc(fields.get('name') or 'the customer')}**"
               + (f" ({esc(fields.get('business'))})" if fields.get("business") else "")
               + ".")
    out.append("")
    out.append("_This is a personalised research and strategy document. It is not "
               "a guarantee of customers, revenue, or results._")
    out.append("")

    ba = plan.get("business_analysis", {})
    out += ["## 1. Business & product analysis", "",
            f"- **What you sell:** {esc(ba.get('what_sold'))}",
            f"- **Problem it solves:** {esc(ba.get('problem_solved'))}",
            f"- **Core value proposition:** {esc(ba.get('value_proposition'))}", ""]

    ic = plan.get("ideal_customer", {})
    out += ["## 2. Ideal customer profile", "",
            f"- **Most likely customer:** {esc(ic.get('profile'))}",
            f"- **Relevant characteristics:** {esc(ic.get('characteristics'))}",
            f"- **Where to reach them:** {esc(ic.get('where_to_reach'))}", ""]

    out += ["## 3. Customer acquisition opportunities", ""]
    for i, o in enumerate(plan.get("acquisition_opportunities", []), 1):
        out += [f"### {i}. {esc(o.get('name'))}",
                f"- **Channel:** {esc(o.get('channel'))}",
                f"- **Why it fits you:** {esc(o.get('why_relevant'))}",
                f"- **First step:** {esc(o.get('first_step'))}", ""]

    ps = plan.get("prioritized_strategy", {})
    out += ["## 4. Prioritised acquisition strategy", ""]
    for i, name in enumerate(ps.get("ranking", []), 1):
        out.append(f"{i}. {esc(name)}")
    out += ["",
            f"**Start with:** {esc(ps.get('start_with'))}", "",
            f"**Reasoning:** {esc(ps.get('reasoning'))}", ""]

    out += ["## 5. 14-day action plan", ""]
    for d in sorted(plan.get("action_plan_14_day", []), key=lambda x: x.get("day", 0)):
        out += [f"**Day {esc(d.get('day'))} - {esc(d.get('focus'))}**",
                f"{esc(d.get('actions'))}", ""]

    out += ["## 6. Ready-to-use templates", ""]
    for t in plan.get("outreach_templates", []):
        out += [f"### {esc(t.get('name'))}"]
        if t.get("context"):
            out.append(f"_{esc(t.get('context'))}_")
        out += ["", "```", esc(t.get("body")), "```", ""]

    out += ["## 7. Next steps", ""]
    for s in plan.get("next_steps", []):
        out.append(f"- [ ] {esc(s)}")
    out.append("")

    sources = plan.get("sources") or []
    if sources:
        out += ["## Sources", ""]
        for s in sources:
            out.append(f"- {esc(s.get('title'))} - {esc(s.get('url'))}")
        out.append("")

    qc = plan.get("qc") or {}
    out += ["---", "",
            f"_Basis: {esc(plan.get('basis'))}. Drafted {esc(plan.get('drafted_at'))} "
            f"with {esc(plan.get('model'))}. "
            f"QC: {'passed' if qc.get('passed') else 'not run'}._"]
    return "\n".join(out) + "\n"


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
