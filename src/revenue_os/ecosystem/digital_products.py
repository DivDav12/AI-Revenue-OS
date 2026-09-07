"""Digital-product secondary path (business-model research: a templates/
guides upsell in the same niches as the affiliate content).

Wholly separate from, and never blocking, the primary affiliate pipeline
(mandatory correction #3): if no digital-product platform is configured,
or generation fails for any reason, the affiliate pipeline in
`affiliate_pipeline.py` continues exactly as if this module did not
exist - nothing here is imported or called by that module, and nothing
in that module is required by this one.

Same `NETWORK_POLICY`-style discipline as `affiliate_model.py`: every
platform defaults to `HUMAN_SETUP_REQUIRED` until a human confirms an
account exists. No Gumroad/Payhip API integration exists in this
codebase, so uploading a listing is always a human action - this module
only ever generates the product FILE and stops there.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import model

PLATFORM_GUMROAD = "gumroad"
PLATFORM_PAYHIP = "payhip"

#: every digital-product platform this codebase knows the NAME of -
#: never a live integration. Mirrors affiliate_model.NETWORK_POLICY's
#: "no fake connector is built" discipline exactly.
DIGITAL_PRODUCT_PLATFORM_POLICY: dict[str, dict] = {
    PLATFORM_GUMROAD: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Create a free Gumroad account (no credit card required to sign up).",
            "Complete Gumroad's own payout setup (bank/PayPal, tax info) - this "
            "codebase never touches that; it happens entirely on Gumroad.",
            "Upload the generated product file yourself - no Gumroad API "
            "integration exists here.",
        ],
        "note": "No Gumroad API credential is read, requested, or assumed anywhere "
                "in this codebase - uploading a listing is always a human action.",
    },
    PLATFORM_PAYHIP: {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": [
            "Create a free Payhip account.",
            "Complete Payhip's own payout setup (Stripe/PayPal).",
            "Upload the generated product file yourself.",
        ],
        "note": "No Payhip API credential is read, requested, or assumed anywhere "
                "in this codebase.",
    },
}


def platform_policy(platform: str) -> dict:
    """Fail closed: an unknown platform is HUMAN_SETUP_REQUIRED, never OK."""
    return DIGITAL_PRODUCT_PLATFORM_POLICY.get((platform or "").strip().lower(), {
        "status": model.POLICY_HUMAN_SETUP_REQUIRED,
        "setup_steps": ["Unknown platform - a human must confirm how it is accessed "
                        "before anything is uploaded there."],
        "note": "unknown platform - failing closed",
    })


def any_platform_configured() -> bool:
    """True only if a human has confirmed (via env var) that they have
    already created an account - never assumed true by default."""
    return bool(os.environ.get("GUMROAD_ACCOUNT_CONFIRMED")
               or os.environ.get("PAYHIP_ACCOUNT_CONFIRMED"))


@dataclass
class DigitalProductDraft:
    product_id: str
    opportunity_id: str
    title: str
    body_markdown: str
    platform: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id, "opportunity_id": self.opportunity_id,
            "title": self.title, "body_markdown": self.body_markdown,
            "platform": self.platform, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DigitalProductDraft":
        d = dict(d or {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class DigitalProductStore:
    """Deliberately self-contained (does not import affiliate_model's
    internal store base) - this path's independence from the affiliate
    pipeline is structural, not just a convention."""

    _FILENAME = "digital_products.json"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._rows = [dict(r) for r in raw] if isinstance(raw, list) else []
            except json.JSONDecodeError:
                self._rows = []

    @classmethod
    def load(cls, data_dir) -> "DigitalProductStore":
        return cls(Path(data_dir) / cls._FILENAME)

    def all(self) -> list[DigitalProductDraft]:
        return [DigitalProductDraft.from_dict(r) for r in self._rows]

    def by_opportunity(self, opportunity_id: str) -> DigitalProductDraft | None:
        for r in self._rows:
            if r.get("opportunity_id") == opportunity_id:
                return DigitalProductDraft.from_dict(r)
        return None

    def upsert(self, draft: DigitalProductDraft) -> None:
        for i, r in enumerate(self._rows):
            if r.get("product_id") == draft.product_id:
                self._rows[i] = draft.to_dict()
                return
        self._rows.append(draft.to_dict())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._rows, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def _new_id() -> str:
    return f"dp-{uuid.uuid4().hex[:12]}"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def generate_product_draft(data_dir, *, opportunity_id: str, topic: str,
                           evidence: tuple = (), category: str = "",
                           now_iso: str = "") -> DigitalProductDraft:
    """Deterministic, $0, template-only checklist generation - the same
    "never invent a fact" discipline as
    `affiliate_assets.render_comparison_page()`. Idempotent per
    opportunity_id. Never calls an LLM, never requires network access,
    never depends on the affiliate chain having run for this opportunity
    (this is the parallel path, not a follow-on step)."""
    store = DigitalProductStore.load(data_dir)
    existing = store.by_opportunity(opportunity_id)
    if existing is not None:
        return existing

    topic = (topic or "").strip() or "diese Kategorie"
    cat = (category or "").strip()
    real_points = [str(e).strip() for e in evidence if str(e).strip()]
    checklist_items = "\n".join(f"- {p}" for p in real_points) or (
        "- (keine weiteren belegten Punkte vorhanden)")

    body = (
        f"# Checkliste: {topic}\n\n"
        f"Kategorie: {cat or 'allgemein'}\n\n"
        "## Worauf du achten solltest\n\n"
        f"{checklist_items}\n\n"
        "Hinweis: Diese Checkliste fasst nur real belegte Punkte zusammen "
        "- nichts hier ist erfunden oder getestet.\n"
    )

    draft = DigitalProductDraft(
        product_id=_new_id(), opportunity_id=opportunity_id,
        title=f"Checkliste: {topic}", body_markdown=body, created_at=now_iso)
    store.upsert(draft)
    store.save()
    return draft
