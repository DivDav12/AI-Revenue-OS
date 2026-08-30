"""Supplier Finder (#9, discovery cluster) - deterministic sourcing read.

Compares supplier / fulfillment options for a validated opportunity from
data the caller supplies. It has NO network access and NEVER invents a
supplier, a price, an MOQ or a shipping term: every field in the output
traces back to a `known_suppliers` entry the caller passed in. When no
verified suppliers are supplied it returns an empty candidate list plus
generic channels to research by hand - it does not fill the gap with
guesses.

No purchases. No supplier contact. Research structuring only.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task
from .store import now_iso

# Generic B2B sourcing channels to search by hand - directories, not
# suppliers, and carrying no pricing/MOQ claims.
_RESEARCH_CHANNELS = (
    "manufacturer / wholesale directories",
    "trade-show exhibitor lists for the category",
    "industry association member lists",
    "existing-competitor 'made by' / packaging attributions",
    "local fulfillment / 3PL provider listings",
)

_SUPPLIER_FIELDS = ("name", "url", "pricing", "moq", "shipping", "lead_time", "notes")


def _clean_supplier(raw: dict) -> dict | None:
    """Keep only the fields the caller actually provided. Missing stays missing."""
    if not isinstance(raw, dict):
        return None
    out = {k: raw[k] for k in _SUPPLIER_FIELDS
           if k in raw and str(raw[k]).strip() != ""}
    return out or None


def build_supplier_report(opportunity: dict, *, product_requirements: dict | None = None,
                          target_market: str = "", constraints: dict | None = None,
                          known_suppliers: list | None = None,
                          now: str | None = None) -> dict:
    opportunity = opportunity or {}
    candidates = [c for c in (_clean_supplier(s) for s in (known_suppliers or [])) if c]

    source_urls = sorted({c["url"] for c in candidates if c.get("url")})
    pricing = {c["name"]: c["pricing"] for c in candidates
               if c.get("name") and "pricing" in c}
    moq = {c["name"]: c["moq"] for c in candidates
           if c.get("name") and "moq" in c}
    shipping = {c["name"]: c["shipping"] for c in candidates
                if c.get("name") and "shipping" in c}

    with_url_and_price = sum(1 for c in candidates if c.get("url") and c.get("pricing"))
    if with_url_and_price >= 3:
        confidence = "high"
    elif any(c.get("url") for c in candidates):
        confidence = "medium"
    elif candidates:
        confidence = "low"
    else:
        confidence = "none"

    return {
        "opportunity": opportunity.get("name") or opportunity.get("title") or "",
        "target_market": target_market,
        "product_requirements": dict(product_requirements or {}),
        "constraints": dict(constraints or {}),
        "supplier_candidates": candidates,
        "source_urls": source_urls,
        "pricing_information": pricing,
        "moq": moq,
        "shipping_information": shipping,
        "confidence": confidence,
        "research_needed": not candidates,
        "research_channels": list(_RESEARCH_CHANNELS) if not candidates else [],
        "no_contact_note": "supplier research only - no supplier was contacted, "
                           "nothing was purchased",
        "research_timestamp": now or now_iso(),
    }


class SupplierFinderAgent(Agent):
    role = "supplier_finder"
    objective = "Compare supplier / fulfillment options from caller-supplied data."
    capabilities = ("find_suppliers",)

    def run(self, task: Task) -> Result:
        opp = task.payload.get("opportunity")
        if not isinstance(opp, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['opportunity'] must be a dict")
        ks = task.payload.get("known_suppliers")
        if ks is not None and not isinstance(ks, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['known_suppliers'] must be a list when given")
        report = build_supplier_report(
            opp,
            product_requirements=task.payload.get("product_requirements"),
            target_market=str(task.payload.get("target_market", "")),
            constraints=task.payload.get("constraints"),
            known_suppliers=ks,
            now=task.payload.get("now"),
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=report)
