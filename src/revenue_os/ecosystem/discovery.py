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
from . import model, verification
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
    }

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
        for rec in store.all():
            d = rec.get("discovery") or {}
            if d.get("source") and d.get("source_id"):
                existing_by_srcid[f"{d['source']}:{d['source_id']}"] = rec["id"]

        drafts: list[OpportunityDraft] = []
        for src in self.sources:
            meta = getattr(src, "meta", None)
            try:
                found = src.discover(limit_per_source)
            except Exception as exc:            # noqa: BLE001 - one bad source never kills the run
                report.errors.append(f"{meta.source if meta else '?'}: {exc!r}")
                continue
            drafts.extend(found)
        report.raw = len(drafts)

        for draft in drafts:
            key = draft.dedup_key()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            report.deduped += 1

            verdict = verification.verify(draft)
            opp = _draft_to_opportunity(draft, verdict)

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
