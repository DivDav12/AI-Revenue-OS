"""Store Builder (#11, build cluster, HUMAN-GATED) - landing-page /
store specification.

Assembles a page specification from the validated opportunity, offer,
copy and design. It writes NOTHING and publishes NOTHING: the real page
is produced by the existing `revenue_os build-checkout` command, which
this spec points at. It must not - and does not - touch PayPal
credentials or configuration, the BUSINESS_EMAIL behaviour, or the
Formspree endpoint; it only records that those are preserved.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task

_PRESERVE = (
    "PayPal LIVE credentials and configuration (never altered)",
    "custom_id = exact candidate name",
    "price / currency from the persisted offer",
    "BUSINESS_EMAIL contact behaviour (env / --business-email)",
    "Formspree form endpoint (--form-action)",
)

_MUST_NOT = (
    "alter PayPal client id / secret / environment",
    "change the Formspree endpoint",
    "publish or deploy without a human gate",
)


def build_store_spec(opportunity: dict, offer: dict, *, copy: dict | None = None,
                     design: dict | None = None,
                     form_action: str = "", business_email: str = "") -> dict:
    opportunity = opportunity or {}
    offer = offer or {}
    copy = copy or {}
    design = design or {}
    name = str(opportunity.get("name") or opportunity.get("title") or "")

    layout_sections = (design.get("page_layout") or {}).get("sections")
    includes = [str(i).strip() for i in (offer.get("includes") or []) if str(i).strip()]
    sections = layout_sections or (
        ["header", "hero"]
        + (["feature_list"] if includes else [])
        + (["faq"] if copy.get("faq") else [])
        + ["pricing_cta", "footer"]
    )

    cta = {
        "label": str(offer.get("call_to_action") or copy.get("primary_cta") or "Pay now"),
        "action": "PayPal JS SDK button (from build-checkout)",
        "price": offer.get("price"),
        "currency": offer.get("currency") or "EUR",
    }

    integration = {
        "generator_command": (
            f"revenue_os build-checkout {name} "
            "--form-action <formspree-endpoint> --business-email $BUSINESS_EMAIL"
        ).strip(),
        "checkout_page": "checkout.html (PayPal button + hidden post-payment intake form)",
        "intake_page": "intake.html (standalone, ?order=&capture=&lead=)",
        "form_action": form_action or "<unchanged: existing Formspree endpoint>",
        "business_email": business_email or "<unchanged: $BUSINESS_EMAIL behaviour>",
        "preserves": list(_PRESERVE),
        "must_not": list(_MUST_NOT),
    }

    return {
        "opportunity": name,
        "page_structure": {"sections": sections, "max_content_width": "640px",
                           "self_contained": True, "external_origins": ["paypal.com", "formspree.io"]},
        "sections": sections,
        "cta_specification": cta,
        "checkout_intake_integration_spec": integration,
        "build_artifacts": [],
        "build_artifacts_note": "none produced here - run the generator command "
                                "(human-reviewed) to create the real pages",
        "human_gate_required": True,
        "publish_blocked_until": "human approval",
    }


class StoreBuilderAgent(Agent):
    role = "store_builder"
    objective = "Specify the landing page / store; never publish."
    capabilities = ("build_store",)

    def run(self, task: Task) -> Result:
        opp = task.payload.get("opportunity")
        offer = task.payload.get("offer")
        if not isinstance(opp, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['opportunity'] must be a dict")
        if not isinstance(offer, dict) or not offer:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['offer'] must be a non-empty dict")
        spec = build_store_spec(
            opp, offer,
            copy=task.payload.get("copy") if isinstance(task.payload.get("copy"), dict) else None,
            design=task.payload.get("design") if isinstance(task.payload.get("design"), dict) else None,
            form_action=str(task.payload.get("form_action", "")),
            business_email=str(task.payload.get("business_email", "")),
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=spec)
