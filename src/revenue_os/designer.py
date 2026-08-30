"""Designer AI (#10, build cluster) - deterministic design spec + asset briefs.

Turns the offer + copy into a structured design specification and a set
of asset briefs. It generates NO images and claims none exist:
`assets_exist` is always False and every image slot is a written brief,
not a file. No external publishing.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task

_BASE_ASSETS = (
    ("hero_image", "image/svg+xml or webp", "1600x900", "top-of-page visual"),
    ("og_image", "image/png", "1200x630", "social share preview"),
    ("favicon", "image/png", "512x512", "browser tab / bookmark icon"),
    ("cta_button", "css", "n/a", "primary action styling"),
)

_A11Y_REQUIREMENTS = (
    "text contrast >= WCAG AA (4.5:1 body, 3:1 large)",
    "single H1, semantic heading order",
    "all imagery has descriptive alt text",
    "layout reflows to a 360px viewport without horizontal scroll",
    "focus states visible on every interactive element",
)


def _tone(positioning: str) -> str:
    p = (positioning or "").lower()
    if any(w in p for w in ("premium", "luxury", "enterprise")):
        return "refined, high-trust, restrained"
    if any(w in p for w in ("fun", "playful", "creator", "community")):
        return "warm, energetic, approachable"
    return "clear, credible, low-friction"


def build_design_spec(opportunity: dict, offer: dict, *, copy: dict | None = None,
                      brand: dict | None = None) -> dict:
    opportunity = opportunity or {}
    offer = offer or {}
    copy = copy or {}
    brand = brand or {}

    positioning = str(offer.get("positioning") or copy.get("subheadline") or "")
    includes = [str(i).strip() for i in (offer.get("includes") or []) if str(i).strip()]

    sections = ["header"]
    if copy.get("headline") or offer.get("what_is_sold"):
        sections.append("hero")
    if includes:
        sections.append("feature_list")
    if copy.get("body"):
        sections.append("narrative")
    if copy.get("faq"):
        sections.append("faq")
    sections += ["pricing_cta", "footer"]

    image_briefs = [
        {"slot": "hero_image",
         "brief": f"Convey: {positioning or 'the core value proposition'}. "
                  "No stock-photo clichés; abstract or product-focused.",
         "exists": False},
        {"slot": "og_image",
         "brief": f"Title text '{copy.get('headline') or offer.get('what_is_sold') or ''}' "
                  "on a solid brand ground, legible at thumbnail size.",
         "exists": False},
    ]

    return {
        "opportunity": opportunity.get("name") or opportunity.get("title") or "",
        "visual_direction": {
            "tone": _tone(positioning),
            "palette_roles": ["ground", "ink", "accent", "muted", "positive", "warning"],
            "typography_scale": ["1rem body", "1.15rem lead", "1.8rem h2", "2.4rem h1"],
            "brand_inputs_used": sorted(brand.keys()),
        },
        "asset_specs": [
            {"name": n, "format": fmt, "dimensions": dim, "purpose": purpose}
            for n, fmt, dim, purpose in _BASE_ASSETS
        ],
        "page_layout": {"sections": sections, "max_content_width": "640px",
                        "grid": "single-column, fl* for feature_list"},
        "image_briefs": image_briefs,
        "design_requirements": list(_A11Y_REQUIREMENTS),
        "assets_exist": False,
        "publishing": "none - specification only",
    }


class DesignerAgent(Agent):
    role = "designer"
    objective = "Produce a design specification and asset briefs for the offer."
    capabilities = ("design_assets",)

    def run(self, task: Task) -> Result:
        opp = task.payload.get("opportunity")
        offer = task.payload.get("offer")
        if not isinstance(opp, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['opportunity'] must be a dict")
        if not isinstance(offer, dict) or not offer:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['offer'] must be a non-empty dict")
        spec = build_design_spec(
            opp, offer,
            copy=task.payload.get("copy") if isinstance(task.payload.get("copy"), dict) else None,
            brand=task.payload.get("brand") if isinstance(task.payload.get("brand"), dict) else None,
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=spec)
