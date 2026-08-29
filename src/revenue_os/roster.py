"""The agent roster - the single source of truth for which agents exist.

Phase 0 of the multi-agent build-out. An AgentSpec is metadata only; it
runs nothing. `live` specs map to an implemented worker and its Goal
knob; `planned` specs are on the roadmap and render as PLANNED on the
dashboard - never as active, never with fabricated metrics.

The Operator/CEO is the coordinator and is deliberately not in the
roster. The internal "decision policy" helper is not an agent either,
nor are the current validation-plan / offer steps - those fold into the
build cluster in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass

CLUSTERS = ("discovery", "build", "marketing", "revenue", "support")


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    cluster: str                     # one of CLUSTERS
    role: str                        # short human label
    capability: str                  # registry routing key / operator action
    node: str = "generic"            # dashboard avatar + map-position key
    gate: str = "autonomous"         # autonomous | human (money / legal)
    mode_field: str | None = None    # Goal field that enables it
    on_value: str | None = None      # mode_field value meaning "LLM on"
    off_value: str | None = None     # mode_field value meaning "deterministic / off"
    spend_activity: str | None = None  # llm_spend.json activity key
    status: str = "planned"          # live | planned


AGENTS: tuple[AgentSpec, ...] = (
    # --- discovery cluster ------------------------------------------------
    AgentSpec("market_scanner", "Market Scanner", "discovery", "Signal intake",
              "discover", node="discovery", status="live"),
    AgentSpec("opportunity_finder", "Opportunity Finder", "discovery",
              "Rank & shortlist", "select", node="finder", status="live"),
    AgentSpec("product_researcher", "Product Researcher", "discovery", "Due diligence",
              "research", node="researcher", mode_field="research",
              on_value="llm", off_value="off", spend_activity="research",
              status="live"),
    AgentSpec("trend_hunter", "Trend Hunter", "discovery", "Emerging demand",
              "analyze_trends", node="trendhunter", status="live"),
    AgentSpec("competitor_analyzer", "Competitor Analyzer", "discovery",
              "Competition read", "analyze_competition", node="competitor",
              mode_field="competition", on_value="llm", off_value="off",
              spend_activity="competition", status="live"),
    AgentSpec("supplier_finder", "Supplier Finder", "discovery", "Sourcing feasibility",
              "find_suppliers", node="discovery"),
    # --- build cluster --------------------------------------------------
    AgentSpec("content_creator", "Content Creator", "build", "Launch content",
              "create_content", node="offer"),
    AgentSpec("copywriter", "Copywriter AI", "build", "Sales copy",
              "write_copy", node="offer"),
    AgentSpec("designer", "Designer AI", "build", "Visual assets",
              "design_assets", node="offer"),
    AgentSpec("store_builder", "Store Builder", "build", "Storefront",
              "build_store", node="planner", gate="human"),
    AgentSpec("developer", "Developer AI", "build", "Product build",
              "develop", node="planner", gate="human"),
    AgentSpec("automation_engineer", "Automation Engineer", "build", "Ops automation",
              "automate", node="decision", gate="human"),
    # --- marketing cluster (all human-gated: real ad spend) -----------
    AgentSpec("ads_manager", "Ads Manager", "marketing", "Campaigns",
              "run_ads", node="offer", gate="human"),
    AgentSpec("campaign_optimizer", "Campaign Optimizer", "marketing", "Optimization",
              "optimize_campaigns", node="evaluator", gate="human"),
    AgentSpec("budget_allocator", "Budget Allocator", "marketing", "Spend allocation",
              "allocate_budget", node="decision", gate="human"),
    # --- revenue cluster ---------------------------------------------
    AgentSpec("sales_tracker", "Sales Tracker", "revenue", "Sales ledger",
              "track_sales", node="evaluator"),
    AgentSpec("profit_master", "Profit Master", "revenue", "Margin control",
              "manage_profit", node="evaluator"),
    AgentSpec("revenue_analyst", "Revenue Analyst", "revenue", "ROI analysis",
              "analyze_revenue", node="evaluator"),
    # --- support / quality cluster ---------------------------------
    AgentSpec("customer_support", "Customer Support", "support", "Customer help",
              "support_customers", node="generic"),
    AgentSpec("review_manager", "Review Manager", "support", "Reputation",
              "manage_reviews", node="generic"),
    AgentSpec("quality_control", "Quality Control", "support", "QA",
              "quality_check", node="generic"),
)

_BY_ID = {a.id: a for a in AGENTS}
_BY_CAP = {a.capability: a for a in AGENTS}


def get(agent_id: str) -> AgentSpec | None:
    return _BY_ID.get(agent_id)


def by_capability(capability: str) -> AgentSpec | None:
    return _BY_CAP.get(capability)


def live() -> tuple[AgentSpec, ...]:
    return tuple(a for a in AGENTS if a.status == "live")


def planned() -> tuple[AgentSpec, ...]:
    return tuple(a for a in AGENTS if a.status == "planned")


def by_cluster() -> dict[str, list[AgentSpec]]:
    out: dict[str, list[AgentSpec]] = {c: [] for c in CLUSTERS}
    for a in AGENTS:
        out.setdefault(a.cluster, []).append(a)
    return out
