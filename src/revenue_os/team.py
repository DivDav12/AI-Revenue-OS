"""The Operator's agent team - one Orchestrator with a persistent
registry of the Phase 1 agents.

The team is rebuilt per Operator cycle (store / goal / source state
changes between cycles); only task_log.json persists. The optional sink
is the TaskLog recorder, so every dispatched task is logged with its
lineage.
"""

from __future__ import annotations

from .acquisition import AcquisitionAgent
from .agent import DiscoveryAgent, EvaluatorAgent
from .ads_manager import AdsManagerAgent
from .automation_engineer import AutomationEngineerAgent
from .budget_allocator import BudgetAllocatorAgent
from .campaign_optimizer import CampaignOptimizerAgent
from .competition import CompetitorAnalyzerAgent
from .copywriter import CopywriterAgent
from .customer_support import CustomerSupportAgent
from .deliverable import DeliverablePackagerAgent
from .designer import DesignerAgent
from .developer import DeveloperAgent
from .distribution import DistributionAgent
from .normalize import to_opportunity
from .opportunity_finder import OpportunityFinderAgent
from .orchestrator import Orchestrator
from .outreach_agent import OutreachDrafterAgent
from .profit_master import ProfitMasterAgent
from .quality_control import QualityControlAgent
from .registry import AgentRegistry
from .research import ResearchAgent
from .review_manager import ReviewManagerAgent
from .revenue_analyst import RevenueAnalystAgent
from .sales_tracker import SalesTrackerAgent
from .store_builder import StoreBuilderAgent
from .supplier_finder import SupplierFinderAgent
from .trend import TrendHunterAgent

# roster id -> registered agent name (kept in sync with roster.py nodes)
MARKET_SCANNER = "market_scanner"
EVALUATOR = "evaluator"
OPPORTUNITY_FINDER = "opportunity_finder"
PRODUCT_RESEARCHER = "product_researcher"
COMPETITOR_ANALYZER = "competitor_analyzer"
COPYWRITER = "copywriter"
CONTENT_CREATOR = "content_creator"
REVENUE_ANALYST = "revenue_analyst"
TREND_HUNTER = "trend_hunter"

# Deterministic roster agents added from Phase A onward. Registered by
# roster id; each has a unique capability so registry routing is exact.
# (prospect_scout is NOT here - like market_scanner it needs a live search
# source, so the acquisition workflow builds it per run.)
_ROSTER_AGENT_CLASSES = {
    "opportunity_scorer": AcquisitionAgent,
    "outreach_drafter": OutreachDrafterAgent,
    "distribution_strategist": DistributionAgent,
    "supplier_finder": SupplierFinderAgent,
    "designer": DesignerAgent,
    "store_builder": StoreBuilderAgent,
    "developer": DeveloperAgent,
    "automation_engineer": AutomationEngineerAgent,
    "ads_manager": AdsManagerAgent,
    "campaign_optimizer": CampaignOptimizerAgent,
    "budget_allocator": BudgetAllocatorAgent,
    "sales_tracker": SalesTrackerAgent,
    "profit_master": ProfitMasterAgent,
    "customer_support": CustomerSupportAgent,
    "review_manager": ReviewManagerAgent,
    "quality_control": QualityControlAgent,
}


def build_team(*, source=None, normalizer=to_opportunity, sink=None) -> Orchestrator:
    """A fresh team. A DiscoveryAgent is registered only when a source is
    supplied (the operator builds one team per source)."""
    registry = AgentRegistry()
    if source is not None:
        registry.register(
            DiscoveryAgent(source, name=MARKET_SCANNER, normalizer=normalizer)
        )
    registry.register(EvaluatorAgent(name=EVALUATOR))
    registry.register(OpportunityFinderAgent(name=OPPORTUNITY_FINDER))
    registry.register(ResearchAgent(name=PRODUCT_RESEARCHER))
    registry.register(CompetitorAnalyzerAgent(name=COMPETITOR_ANALYZER))
    registry.register(CopywriterAgent(name=COPYWRITER))
    registry.register(DeliverablePackagerAgent(name=CONTENT_CREATOR))
    registry.register(RevenueAnalystAgent(name=REVENUE_ANALYST))
    registry.register(TrendHunterAgent(name=TREND_HUNTER))
    for agent_id, cls in _ROSTER_AGENT_CLASSES.items():
        registry.register(cls(name=agent_id))
    return Orchestrator(registry=registry, sink=sink)
