"""The Discovery Engine (spec sections 4 + 5 + 16).

    sources ─▶ normalize ─▶ dedupe ─▶ verify ─▶ persist as Opportunity

It reuses the existing `OpportunityStore` (the execution stack's currency)
and `opportunity_state` machine - a discovered real opportunity is just an
`Opportunity` with `origin="real"` and a populated `discovery` namespace.

No network here: the engine calls whatever sources it is given. The real
network sources live in `ecosystem.sources` behind injectable fetchers.

Deterministic given fixed source output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..discovery_log import DiscoveryLog
from ..opportunity_store import Opportunity, load_opportunities
from ..store import now_iso
from . import model, task_signal, verification
from .model import OpportunityDraft
from .sources import OpportunitySource, default_sources

_DISCOVERY_LOG = "ecosystem_discovery.json"

# opportunity_type -> opportunity_store category (best-fit)
_TYPE_TO_CATEGORY = {
    model.TYPE_TASK: "freelancing",
    model.TYPE_DIGITAL_PRODUCT: "digital_product",
    model.TYPE_SOFTWARE_TOOL: "developer_tool",
    model.TYPE_AFFILIATE: "affiliate",
    model.TYPE_ECOMMERCE: "ecommerce",
    model.TYPE_DROPSHIPPING: "ecommerce",
    model.TYPE_SERVICE: "b2b_service",
    model.TYPE_CONTENT: "content_business",
    model.TYPE_OTHER: "other",
}


@dataclass
class DiscoveryReport:
    run_at: str
    sources: list = field(default_factory=list)
    raw: int = 0
    deduped: int = 0
    new: int = 0
    refreshed: int = 0
    by_verification: dict = field(default_factory=dict)
    by_origin: dict = field(default_factory=dict)
    qualified_ids: list = field(default_factory=list)
    human_required_ids: list = field(default_factory=list)
    blocked_ids: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_at": self.run_at, "sources": list(self.sources),
            "raw": self.raw, "deduped": self.deduped,
            "new": self.new, "refreshed": self.refreshed,
            "by_verification": dict(self.by_verification),
            "by_origin": dict(self.by_origin),
            "qualified": list(self.qualified_ids),
            "human_required": list(self.human_required_ids),
            "blocked": list(self.blocked_ids),
            "errors": list(self.errors),
        }


def _draft_to_opportunity(draft: OpportunityDraft, verdict) -> Opportunity:
    meta = draft.source_meta
    is_real = bool(meta) and meta.access_method != model.ACCESS_SYNTHETIC
    category = draft.category if draft.category and draft.category != "other" \
        else _TYPE_TO_CATEGORY.get(draft.opportunity_type, "other")

    discovery_ns = {
        "opportunity_type": draft.opportunity_type,
        "source": meta.source if meta else "",
        "source_type": meta.source_type if meta else "",
        "source_url": draft.source_url or (meta.source_url if meta else ""),
        "source_id": draft.source_id,
        "discovered_at": draft.discovered_at or now_iso(),
        "access_method": meta.access_method if meta else "",
        "automation_allowed": bool(meta.automation_allowed) if meta else False,
        "requires_login": bool(meta.requires_login) if meta else False,
        "requires_human": bool(meta.requires_human) if meta else False,
        "policy_status": meta.policy_status if meta else model.POLICY_BLOCKED,
        "evidence": list(draft.evidence or []),
        "origin": model.ORIGIN_REAL if is_real else model.ORIGIN_SYNTHETIC,
        "verification": verdict.to_dict(),
        "demand_hint": float(draft.demand_hint or 0.0),
        "est_pay_eur": float(draft.est_pay_eur or 0.0),
        "est_time_minutes": float(draft.est_time_minutes or 0.0),
        # discovery quality layer - persist the structured evidence itself,
        # not just the classification derived from it, so pipeline.py's
        # draft_from_record() can reconstruct it later (PLAN_TASK needs the
        # real deadline/deliverable facts, not just task_kind).
        "payment_evidence": draft.payment_evidence.to_dict(),
        "submission_evidence": draft.submission_evidence.to_dict(),
    }

    # Demand Quality Layer (spec: Demand-to-Revenue plan, Step 3) - only
    # present for drafts built by ecosystem.demand_sources; every other
    # source's `draft.raw` never carries these keys, so this is a no-op
    # for HN/RemoteOK/synthetic/curated/TASK sources. Without this, the
    # evidence/score/provenance ecosystem.demand_sources computed would be
    # silently dropped at persistence time - demand_hint alone (below)
    # does not carry the FACT/ESTIMATED/UNKNOWN breakdown or the reasons.
    _raw = draft.raw or {}
    if "demand_evidence" in _raw:
        discovery_ns["demand_evidence"] = _raw["demand_evidence"]
        discovery_ns["demand_quality"] = _raw.get("demand_quality")
        discovery_ns["demand_provenance"] = _raw.get("demand_provenance")
        # Demand Ranking Layer (spec: Decision-/Ranking-Design step,
        # additive Read-Model integration) - advisory-only buyer/problem
        # confidence scores, `.get()`-guarded so a draft built before this
        # field existed (or by anything other than ecosystem.demand_sources)
        # never breaks this read-model assembly. Display-only: nothing in
        # this file, or anywhere downstream of it (verification already ran
        # BEFORE this function is even called - see DiscoveryEngine.run()),
        # reads these two keys to accept/reject/prioritize anything.
        discovery_ns["buyer_confidence"] = _raw.get("buyer_confidence")
        discovery_ns["problem_confidence"] = _raw.get("problem_confidence")
        # Product Intent (Demand-First Affiliate architecture, Step 1,
        # additive Read-Model integration) - same `.get()`-guarded,
        # display-only pattern as buyer/problem confidence above: nothing
        # here or downstream reads this to accept/reject/prioritize
        # anything, and it is a no-op for any draft that never carried it.
        discovery_ns["product_intent"] = _raw.get("product_intent")

    return Opportunity(
        title=draft.title[:200] or "untitled opportunity",
        category=category,
        target_customer=str(draft.raw.get("target_customer", "")) if draft.raw else "",
        est_revenue_eur=float(draft.est_pay_eur or 0.0),
        required_work=(draft.description or draft.title)[:400],
        source=f"discovery:{meta.source}" if meta else "discovery",
        origin=model.ORIGIN_REAL if is_real else model.ORIGIN_SYNTHETIC,
        discovery=discovery_ns,
        legal_platform_risk="low" if (meta and meta.policy_status == model.POLICY_OK)
        else "medium",
        probability=max(0.02, min(0.9, float(draft.demand_hint or 0.12))),
    )


class DiscoveryEngine:
    def __init__(self, data_dir, sources: list[OpportunitySource] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.sources = list(sources) if sources is not None else default_sources()

    def run(self, *, limit_per_source: int = 25) -> DiscoveryReport:
        report = DiscoveryReport(run_at=now_iso(),
                                 sources=[getattr(s, "meta", None).source
                                          if getattr(s, "meta", None) else "?"
                                          for s in self.sources])
        store = load_opportunities(self.data_dir)
        seen_keys: set[str] = set()
        # pre-index existing records by (source, source_id) so a re-run refreshes
        existing_by_srcid: dict[str, str] = {}
        # pre-index existing TASK-type records by their stable fingerprint -
        # robust to a re-scrape changing the URL/source_id/timestamp (spec:
        # TASK dedupe). See task_signal.task_fingerprint().
        existing_by_fingerprint: dict[str, str] = {}
        for rec in store.all():
            d = rec.get("discovery") or {}
            if d.get("source") and d.get("source_id"):
                existing_by_srcid[f"{d['source']}:{d['source_id']}"] = rec["id"]
            fp = d.get("task_fingerprint")
            if fp:
                existing_by_fingerprint[fp] = rec["id"]

        drafts: list[OpportunityDraft] = []
        for src in self.sources:
            meta = getattr(src, "meta", None)
            try:
                found = src.discover(limit_per_source)
            except Exception as exc:            # noqa: BLE001 - one bad source never kills the run
                report.errors.append(f"{meta.source if meta else '?'}: {exc!r}")
                continue
            drafts.extend(found)
            # a source may have swallowed non-fatal, per-query failures of
            # its own (e.g. DemandDiscoverySource - one bad query out of
            # several) - surface those too instead of silently losing them.
            # `getattr(..., None) or []` is a no-op for every existing
            # source, which has no `last_errors` attribute at all.
            for err in getattr(src, "last_errors", None) or []:
                report.errors.append(f"{meta.source if meta else '?'}: {err}")
        report.raw = len(drafts)

        for draft in drafts:
            key = draft.dedup_key()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            report.deduped += 1

            verdict = verification.verify(draft)
            opp = _draft_to_opportunity(draft, verdict)

            # TASK dedupe (existing) + demand-signal dedupe (Step 3, spec:
            # "bestehende Fingerprints/Dedupe respektiert werden") - the
            # SAME fingerprint utility, only widened to also cover
            # TYPE_DIGITAL_PRODUCT drafts that came from a demand_signal
            # source (never for a synthetic/curated/other-sourced
            # DIGITAL_PRODUCT - those keep today's title-hash dedupe
            # unchanged).
            is_demand_product = (
                draft.opportunity_type == model.TYPE_DIGITAL_PRODUCT
                and draft.source_meta is not None
                and draft.source_meta.source_type == "demand_signal")
            if draft.opportunity_type == model.TYPE_TASK or is_demand_product:
                fp = task_signal.task_fingerprint(draft)
                opp.discovery["task_fingerprint"] = fp
                dup_id = existing_by_fingerprint.get(fp)
                if dup_id and dup_id != opp.id:
                    # same underlying task offer re-scraped under a fresh
                    # url/source_id/timestamp - refresh the EXISTING record
                    # instead of spawning a duplicate opportunity.
                    opp.id = dup_id
                existing_by_fingerprint[fp] = opp.id

            srcid = (f"{draft.source_meta.source}:{draft.source_id}"
                     if draft.source_meta and draft.source_id else "")
            was_known = srcid in existing_by_srcid or store.get(opp.id) is not None

            rec = store.upsert(opp)
            store.record_discovery(rec["id"], opp.discovery)

            if was_known:
                report.refreshed += 1
            else:
                report.new += 1

            vstatus = verdict.status
            report.by_verification[vstatus] = report.by_verification.get(vstatus, 0) + 1
            o = "real" if opp.origin == model.ORIGIN_REAL else "synthetic"
            report.by_origin[o] = report.by_origin.get(o, 0) + 1
            if vstatus == model.V_QUALIFIED:
                report.qualified_ids.append(rec["id"])
            elif vstatus == model.V_HUMAN_REQUIRED:
                report.human_required_ids.append(rec["id"])
            elif vstatus == model.V_BLOCKED:
                report.blocked_ids.append(rec["id"])

        store.save()

        log = DiscoveryLog.load(self.data_dir / _DISCOVERY_LOG)
        log.add(report.to_dict())
        log.save()
        return report


def latest_discovery(data_dir) -> dict | None:
    return DiscoveryLog.load(Path(data_dir) / _DISCOVERY_LOG).latest()
