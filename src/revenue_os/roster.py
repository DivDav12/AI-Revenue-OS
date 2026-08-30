"""The agent roster - the single source of truth for which agents exist.

Phase 0 of the multi-agent build-out. An AgentSpec is metadata only; it
runs nothing. `live` specs map to an implemented worker and its Goal
knob; `planned` specs are on the roadmap and render as PLANNED on the
dashboard - never as active, never with fabricated metrics.

`depends_on` names the roster ids whose output an agent consumes. A
planned agent is only promoted to `live` once its implementation and
tests pass AND every id in `depends_on` is itself `live` - see
`unmet_dependencies` / `blocked`.

The Operator/CEO is the coordinator and is deliberately not in the
roster. The internal "decision policy" helper is not an agent either,
nor are the current validation-plan / offer steps - those fold into the
build cluster in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass

CLUSTERS = ("discovery", "build", "marketing", "acquisition", "revenue", "support")


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
    depends_on: tuple[str, ...] = ()  # roster ids this agent consumes


AGENTS: tuple[AgentSpec, ...] = (
    # --- discovery cluster ------------------------------------------------
    AgentSpec("market_scanner", "Market Scanner", "discovery", "Signal intake",
              "discover", node="discovery", status="live"),
    AgentSpec("opportunity_finder", "Opportunity Finder", "discovery",
              "Rank & shortlist", "select", node="finder", status="live",
              depends_on=("market_scanner",)),
    AgentSpec("product_researcher", "Product Researcher", "discovery", "Due diligence",
              "research", node="researcher", mode_field="research",
              on_value="llm", off_value="off", spend_activity="research",
              status="live", depends_on=("market_scanner",)),
    AgentSpec("trend_hunter", "Trend Hunter", "discovery", "Emerging demand",
              "analyze_trends", node="trendhunter", status="live",
              depends_on=("market_scanner",)),
    AgentSpec("competitor_analyzer", "Competitor Analyzer", "discovery",
              "Competition read", "analyze_competition", node="competitor",
              mode_field="competition", on_value="llm", off_value="off",
              spend_activity="competition", status="live",
              depends_on=("market_scanner",)),
    AgentSpec("supplier_finder", "Supplier Finder", "discovery", "Sourcing feasibility",
              "find_suppliers", node="discovery", status="live",
              depends_on=("opportunity_finder",)),
    # --- build cluster --------------------------------------------------
    AgentSpec("content_creator", "Content Creator", "build", "Launch page",
              "package_deliverable", node="content", status="live",
              depends_on=("opportunity_finder",)),
    AgentSpec("copywriter", "Copywriter AI", "build", "Launch copy",
              "write_copy", node="copywriter", mode_field="copywriter",
              on_value="llm", off_value="off", spend_activity="copy",
              status="live", depends_on=("opportunity_finder",)),
    AgentSpec("designer", "Designer AI", "build", "Visual assets",
              "design_assets", node="offer", status="live",
              depends_on=("opportunity_finder",)),
    AgentSpec("store_builder", "Store Builder", "build", "Storefront",
              "build_store", node="planner", gate="human", status="live",
              depends_on=("opportunity_finder",)),
    AgentSpec("developer", "Developer AI", "build", "Product build",
              "develop", node="planner", gate="human", status="live",
              depends_on=("opportunity_finder",)),
    AgentSpec("automation_engineer", "Automation Engineer", "build", "Ops automation",
              "automate", node="decision", gate="human", status="live",
              depends_on=("opportunity_finder",)),
    # --- acquisition cluster (Phase 2: autonomous customer acquisition) ---
    # Finds public "how do I get my first customers" posts, scores them, and
    # drafts a human-review outreach reply. The system never posts, DMs, or
    # emails - `outreach_drafter` is human-gated: a person posts every reply.
    # The LLM relevance knob for the scorer is CLI-side (`discover-* --score
    # llm`), not an operator Goal field, so no mode_field here.
    AgentSpec("prospect_scout", "Prospect Scout", "acquisition",
              "Find public asks", "scout_prospects", node="discovery",
              status="live"),
    AgentSpec("opportunity_scorer", "Opportunity Scorer", "acquisition",
              "Score & rank prospects", "score_prospects", node="finder",
              status="live", depends_on=("prospect_scout",)),
    AgentSpec("outreach_drafter", "Outreach Drafter", "acquisition",
              "Draft the reply", "draft_outreach", node="copywriter",
              gate="human", status="live", depends_on=("opportunity_scorer",)),
    # --- marketing cluster (all human-gated: real ad spend) -----------
    AgentSpec("ads_manager", "Ads Manager", "marketing", "Campaigns",
              "run_ads", node="offer", gate="human", status="live",
              depends_on=("store_builder",)),
    AgentSpec("campaign_optimizer", "Campaign Optimizer", "marketing", "Optimization",
              "optimize_campaigns", node="evaluator", gate="human", status="live",
              depends_on=("store_builder",)),
    AgentSpec("budget_allocator", "Budget Allocator", "marketing", "Spend allocation",
              "allocate_budget", node="decision", gate="human", status="live",
              depends_on=("store_builder",)),
    # --- revenue cluster ---------------------------------------------
    AgentSpec("sales_tracker", "Sales Tracker", "revenue", "Sales ledger",
              "track_sales", node="evaluator", status="live"),
    AgentSpec("profit_master", "Profit Master", "revenue", "Margin control",
              "manage_profit", node="evaluator", status="live"),
    AgentSpec("revenue_analyst", "Revenue Analyst", "revenue", "ROI analysis",
              "analyze_revenue", node="analyst", status="live"),
    # --- support / quality cluster ---------------------------------
    AgentSpec("customer_support", "Customer Support", "support", "Customer help",
              "support_customers", node="generic", status="live"),
    AgentSpec("review_manager", "Review Manager", "support", "Reputation",
              "manage_reviews", node="generic", status="live"),
    AgentSpec("quality_control", "Quality Control", "support", "QA",
              "quality_check", node="generic", status="live",
              depends_on=("store_builder", "developer", "sales_tracker",
                          "profit_master", "customer_support", "review_manager")),
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


def unmet_dependencies(spec: AgentSpec) -> tuple[str, ...]:
    """Ids in spec.depends_on that are not themselves live."""
    return tuple(d for d in spec.depends_on
                 if (_BY_ID.get(d) is None or _BY_ID[d].status != "live"))


def blocked() -> tuple[AgentSpec, ...]:
    """Agents that are not live and have at least one unmet dependency."""
    return tuple(a for a in AGENTS
                 if a.status != "live" and unmet_dependencies(a))
