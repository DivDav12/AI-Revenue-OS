"""Quality Control (#21, support cluster) - the final validation layer.

Runs deterministic checks over the offer, copy, landing page, launch
plan, build artifacts and upstream agent results. It can BLOCK: a
`qc_status` of "block" is the signal a coordinator must honour before
anything proceeds. It never passes something just because it exists -
`pass` requires the core checks to have actually run with no failures.
"""

from __future__ import annotations

import re

from .agent import Agent
from .messages import Result, Task

_URL = re.compile(r"^https?://[^\s]+$")
# a price token, not preceded by another digit/dot so "29.0" is one number
# (not also "0"); 1-2 decimals accepted, then normalised to 2dp for compare
_MONEY_IN_TEXT = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d{1,2})?)\s*(?:eur|usd|€|\$)", re.I)
_PROHIBITED = (
    ("launched", True, "an upstream agent reports launched=True (ads must be human-run)"),
    ("auto_sent", True, "an upstream agent reports auto_sent=True (no auto contact)"),
    ("authorizes_spend", True, "an upstream agent claims spend authority"),
    ("unlocks_growth_capital", True, "an upstream agent claims to unlock Growth Capital"),
)


def _prices_in(text: str) -> set[str]:
    out = set()
    for m in _MONEY_IN_TEXT.finditer(str(text or "")):
        try:
            out.add(f"{float(m.group(1).replace(',', '.')):.2f}")
        except ValueError:
            pass
    return out


def run_quality_checks(*, offer=None, copy=None, landing_page="", launch_plan=None,
                       build_artifacts=None, agent_results=None,
                       expected_business_email="") -> dict:
    offer = offer or {}
    copy = copy or {}
    launch_plan = launch_plan or {}
    agent_results = agent_results or []

    passed, failed, warnings, blocking = [], [], [], []

    # --- required fields ------------------------------------------------
    for field in ("price", "currency", "what_is_sold"):
        (passed if offer.get(field) not in (None, "") else failed).append(
            f"offer.{field} present")
    (passed if str(copy.get("headline") or "").strip() else warnings).append(
        "copy.headline present")

    ran_core = bool(offer) and (bool(copy) or bool(landing_page))

    # --- pricing consistency -----------------------------------------
    price = offer.get("price")
    if price is not None:
        want = f"{float(price):.2f}"
        for label, blob in (("page", landing_page), ("copy", copy.get("body", ""))):
            bad = sorted(p for p in _prices_in(blob) if p != want)
            if bad:
                failed.append(f"pricing mismatch: offer {want} vs {label} {bad}")
            elif _prices_in(blob):
                passed.append(f"pricing consistent between offer and {label}")
        plan_price = launch_plan.get("price")
        if plan_price is not None and f"{float(plan_price):.2f}" != want:
            failed.append(f"pricing mismatch: offer {want} vs launch_plan {plan_price}")

    # --- contact email consistency ---------------------------------
    if expected_business_email:
        if expected_business_email in str(landing_page):
            passed.append("business contact email present on the page")
        else:
            warnings.append("expected business email not found on the landing page")

    # --- broken links / config -------------------------------------
    fa = (offer.get("form_action") or "").strip()
    if fa and not _URL.match(fa):
        failed.append(f"form_action is not a URL: {fa!r}")
    if landing_page:
        if "paypal-button-container" in landing_page and "paypal.com/sdk/js" not in landing_page:
            failed.append("checkout page has a PayPal container but no SDK script")
        for m in re.finditer(r'href=[\'"]([^\'"]+)[\'"]', landing_page):
            if m.group(1).startswith("http") and not _URL.match(m.group(1)):
                failed.append(f"broken link: {m.group(1)!r}")

    # --- prohibited autonomous actions ----------------------------
    for res in agent_results:
        out = res.get("output", res) if isinstance(res, dict) else {}
        for key, bad, msg in _PROHIBITED:
            if out.get(key) is bad:
                blocking.append(msg)
        spent = out.get("spent") or out.get("spend")
        if isinstance(spent, (int, float)) and spent > 0:
            blocking.append("an upstream agent reports spent > 0")

    if not ran_core:
        blocking.append("not enough artefacts to validate - refuse to pass")

    if blocking:
        status = "block"
    elif failed:
        status = "block"
    elif warnings:
        status = "warn"
    else:
        status = "pass"

    return {
        "qc_status": status,
        "passed_checks": passed,
        "failed_checks": failed,
        "warnings": warnings,
        "blocking_issues": blocking,
        "can_block_downstream": True,
        "core_checks_ran": ran_core,
        "note": "pass requires core checks to have run with zero failures - "
                "existence alone is never approval",
    }


class QualityControlAgent(Agent):
    role = "quality_control"
    objective = "Validate downstream outputs; block on any failure."
    capabilities = ("quality_check",)

    def run(self, task: Task) -> Result:
        p = task.payload
        if not any(k in p for k in ("offer", "copy", "landing_page", "launch_plan",
                                    "build_artifacts", "agent_results")):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload needs at least one artefact to validate")
        for key in ("offer", "copy", "launch_plan"):
            if key in p and not isinstance(p[key], dict):
                return Result(task_id=task.id, agent=self.name, status="error",
                              error=f"payload['{key}'] must be a dict when given")
        if "agent_results" in p and not isinstance(p["agent_results"], list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['agent_results'] must be a list when given")
        out = run_quality_checks(
            offer=p.get("offer"), copy=p.get("copy"),
            landing_page=str(p.get("landing_page", "")),
            launch_plan=p.get("launch_plan"),
            build_artifacts=p.get("build_artifacts"),
            agent_results=p.get("agent_results"),
            expected_business_email=str(p.get("expected_business_email", "")),
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
