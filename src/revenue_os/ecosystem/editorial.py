"""Editorial Pick - a human-authorized, proactive affiliate guide topic.

NOT a discovered demand signal. Every other opportunity in this ecosystem
traces back to something a real, independent third party actually said
(a forum post, a search-API result, a human-fed real listing). An
editorial pick is different and is never allowed to be confused with
those: a human explicitly decided "this real product's category is
worth an evergreen buying guide right now" - a completely ordinary,
legitimate affiliate-marketing practice, but it is OUR OWN judgement
call, not observed demand.

Reuses the existing architecture exactly like `human_fed.py` does: one
pre-built `OpportunityDraft`, wrapped in a minimal `OpportunitySource`,
run through the SAME, unmodified `DiscoveryEngine`/`verification.verify()`
pipeline. No parallel persistence path, no new verification rule.

Kept honest end to end:
  - `SourceMeta.source_type = "editorial_pick"` (not "demand_signal",
    not "human_fed") - `affiliate_intel.affiliate_funnel_status()` reads
    this to report `demand_basis` correctly, and `affiliate_assets.
    render_comparison_page()` reads `draft.raw['editorial_pick']` to
    NEVER frame our own editorial note as "people have said, in their
    own words" - it gets the same neutral "this is a common need"
    framing an opportunity with no evidence at all already gets.
  - `evidence` still must be non-empty (verification.verify()'s existing,
    unmodified gate) - it holds OUR OWN stated reason for the pick, never
    a fabricated quote attributed to a stranger.
"""

from __future__ import annotations

from . import model
from .model import OpportunityDraft, SourceMeta

SOURCE_TYPE_EDITORIAL_PICK = "editorial_pick"


class EditorialError(ValueError):
    """The editorial pick's own required fields are missing - never
    silently defaulted."""


class _EditorialSource:
    """`OpportunitySource` wrapping a single, already-built editorial
    draft - no network, no re-fetch, mirrors `human_fed.HumanFedTaskSource`."""

    def __init__(self, draft: OpportunityDraft) -> None:
        self._draft = draft
        self.meta = draft.source_meta

    def discover(self, limit: int) -> list[OpportunityDraft]:
        return [self._draft] if limit > 0 else []


def build_editorial_draft(*, title: str, description: str, note: str,
                          category: str = "other", actor: str = "human") -> OpportunityDraft:
    """`note` is OUR OWN stated reason this topic was picked (e.g. "this
    product category is commonly searched by people starting an online
    business") - never a quote attributed to anyone else. Fails closed on
    any missing required text."""
    title = (title or "").strip()
    description = (description or "").strip()
    note = (note or "").strip()
    if not title:
        raise EditorialError("title must be non-empty")
    if not description:
        raise EditorialError("description must be non-empty")
    if not note:
        raise EditorialError("note (our own stated reason for the pick) must be non-empty")

    meta = SourceMeta(
        source="editorial", source_type=SOURCE_TYPE_EDITORIAL_PICK,
        access_method=model.ACCESS_CURATED_FILE,
        automation_allowed=False, requires_login=False, requires_human=True,
        policy_status=model.POLICY_OK)

    return OpportunityDraft(
        title=title[:200], description=description[:800],
        opportunity_type=model.TYPE_AFFILIATE,
        evidence=[note],
        source_meta=meta, source_id="", source_url="",
        demand_hint=0.0, category=category or "other",
        raw={"editorial_pick": True, "editorial_note": note, "authorized_by": actor},
    )


def ingest_editorial_pick(data_dir, *, title: str, description: str, note: str,
                          category: str = "other", actor: str = "human") -> dict:
    """Validate + ingest one editorial pick via the existing
    `DiscoveryEngine`/`verification.verify()` pipeline, unchanged.
    Idempotent: re-running the same title resolves to the same
    opportunity (the same title/dedupe rule every other source already
    gets - no new dedupe mechanism)."""
    from .discovery import DiscoveryEngine
    from ..opportunity_store import load_opportunities

    draft = build_editorial_draft(title=title, description=description, note=note,
                                  category=category, actor=actor)
    DiscoveryEngine(data_dir, sources=[_EditorialSource(draft)]).run(limit_per_source=1)

    store = load_opportunities(data_dir)
    rec = next((r for r in store.all()
               if r.get("title") == draft.title
               and (r.get("discovery") or {}).get("source_type") == SOURCE_TYPE_EDITORIAL_PICK),
              None)
    if rec is None:
        raise EditorialError(
            "internal: could not locate the ingested editorial opportunity after discovery - "
            "this should never happen")

    v = (rec.get("discovery") or {}).get("verification") or {}
    return {"opportunity_id": rec["id"], "title": rec["title"],
            "verification_status": v.get("status", ""),
            "qualified": v.get("status") in model.PLANNABLE}
