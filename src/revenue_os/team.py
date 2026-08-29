"""The Operator's agent team - one Orchestrator with a persistent
registry of the Phase 1 agents.

The team is rebuilt per Operator cycle (store / goal / source state
changes between cycles); only task_log.json persists. The optional sink
is the TaskLog recorder, so every dispatched task is logged with its
lineage.
"""

from __future__ import annotations

from .agent import DiscoveryAgent, EvaluatorAgent
from .normalize import to_opportunity
from .opportunity_finder import OpportunityFinderAgent
from .orchestrator import Orchestrator
from .registry import AgentRegistry
from .research import ResearchAgent
from .trend import TrendHunterAgent

# roster id -> registered agent name (kept in sync with roster.py nodes)
MARKET_SCANNER = "market_scanner"
EVALUATOR = "evaluator"
OPPORTUNITY_FINDER = "opportunity_finder"
PRODUCT_RESEARCHER = "product_researcher"
TREND_HUNTER = "trend_hunter"


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
    registry.register(TrendHunterAgent(name=TREND_HUNTER))
    return Orchestrator(registry=registry, sink=sink)
