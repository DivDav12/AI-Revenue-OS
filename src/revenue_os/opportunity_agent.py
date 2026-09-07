"""Roster `opportunity_agent` - real discovery -> research -> score ->
select for the affiliate/content pipeline.

Real flow (never a static "seed -> offer -> score" shortcut):

    real public signals (Hacker News / RemoteOK / an optional curated
    file - all keyless, $0, already built in ecosystem/sources.py)
      -> ecosystem.discovery.DiscoveryEngine (dedupes each draft,
         verifies it with ecosystem.verification.verify(), persists it)
      -> ecosystem.content_opportunity.find_content_opportunities()
         scores every REAL, VERIFIED, EVIDENCED opportunity against
         every currently-USABLE affiliate offer (never a
         HUMAN_SETUP_REQUIRED network) using the existing, unmodified
         relevance/demand engine
      -> the single best recommend_page=True candidate is SELECTED;
         an empty result is an explicit REJECTED with the real reason
         (no usable offer, or nothing clears the content bar yet)

This module adds no new discovery or scoring logic of its own - it
composes three already-real, already-tested subsystems. The source list
is a parameter precisely so a stronger public-signal source can be
added later without changing this module's contract; the MVP default
(HN + RemoteOK) is real public research, not a placeholder.
"""

from __future__ import annotations

from .agent import Agent
from .ecosystem import content_opportunity as co
from .ecosystem import sources as eco_sources
from .ecosystem.affiliate_model import AffiliateOfferStore
from .ecosystem.discovery import DiscoveryEngine
from .messages import Result, Task

#: real, keyless, $0 public-signal sources used by default. "file" is
#: opt-in (needs a path=) - a human-curated supplement, never the only
#: source (see module docstring: this is real public research first,
#: not "seed -> offer -> score").
DEFAULT_REAL_SOURCES = ("hn", "remoteok")


def usable_offers(data_dir) -> list:
    """Only offers a human has actually joined and confirmed
    (`status == POLICY_OK`) - a HUMAN_SETUP_REQUIRED network contributes
    nothing here, silently and correctly, never a fabricated offer."""
    return [o for o in AffiliateOfferStore.load(data_dir).all() if o.usable]


def discover_real_signals(data_dir, *, source_names=DEFAULT_REAL_SOURCES,
                          limit_per_source: int = 25, source_kwargs: dict | None = None):
    """Real public research step. Returns the `DiscoveryReport`
    (new/refreshed/qualified counts, per-source errors) - one dead
    source never kills the run (existing, tested convention)."""
    kwargs = source_kwargs or {}
    built = [eco_sources.build_source(n, **kwargs.get(n, {})) for n in source_names]
    return DiscoveryEngine(data_dir, sources=built).run(limit_per_source=limit_per_source)


def select_opportunity(data_dir) -> dict:
    """Candidate generation -> affiliate-offer matching -> deterministic
    scoring -> selection/rejection, across every currently usable offer."""
    offers = usable_offers(data_dir)
    if not offers:
        return {
            "status": "REJECTED",
            "reason": ("no usable affiliate offer - HUMAN SETUP REQUIRED: join "
                      "Amazon Associates, Awin, or CJ and confirm the offer "
                      "(see ecosystem.affiliate_model.NETWORK_POLICY)"),
            "selected": None,
        }

    best = None  # (ContentOpportunity, AffiliateOffer)
    for offer in offers:
        for cand in co.find_content_opportunities(data_dir, offer_id=offer.offer_id):
            if not cand.recommend_page:
                continue
            if best is None or cand.content_opportunity_score > best[0].content_opportunity_score:
                best = (cand, offer)

    if best is None:
        return {
            "status": "REJECTED",
            "reason": ("no real, evidenced opportunity currently clears the content "
                      "bar against a usable offer - this is a report of current "
                      "real data, not a permanent verdict; discover more and re-run"),
            "selected": None,
        }

    cand, offer = best
    return {
        "status": "SELECTED", "reason": "",
        "selected": {
            "opportunity_id": cand.opportunity_id, "offer_id": offer.offer_id,
            "score": cand.content_opportunity_score, "title": cand.title,
        },
    }


class OpportunityAgent(Agent):
    role = "opportunity_agent"
    objective = ("Run real public-signal discovery, then match/score/select the best "
                "opportunity against a currently-usable affiliate offer; never "
                "invents an offer and never selects a HUMAN_SETUP_REQUIRED network.")
    capabilities = ("discover_and_select_opportunity",)

    def run(self, task: Task) -> Result:
        payload = task.payload or {}
        data_dir = payload.get("data_dir")
        if not data_dir:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['data_dir'] is required")
        source_names = tuple(payload.get("source_names") or DEFAULT_REAL_SOURCES)
        limit_per_source = int(payload.get("limit_per_source", 25))
        source_kwargs = payload.get("source_kwargs") or {}

        # This agent is fully deterministic and $0 - no LLM call anywhere
        # in discovery or scoring. If a future change adds an LLM-assisted
        # step here, it MUST go through budget_guard.guard() first.
        try:
            report = discover_real_signals(data_dir, source_names=source_names,
                                           limit_per_source=limit_per_source,
                                           source_kwargs=source_kwargs)
        except Exception as exc:  # noqa: BLE001 - one bad cycle never crashes the agent
            return Result(task_id=task.id, agent=self.name, status="error",
                          error=f"discovery failed: {exc!r}")

        selection = select_opportunity(data_dir)
        return Result(task_id=task.id, agent=self.name, status="ok",
                      output={"discovery": report.to_dict(), "selection": selection})
