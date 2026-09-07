"""Roster `affiliate_chain_agent` - Content Creation -> Quality Control ->
Link Creation -> Deployment, as ONE atomic, idempotent step.

Phase 5 originally proposed separate Content and Deployment agents. This
merges them because the existing, tested `ecosystem.affiliate_pipeline.
run_affiliate_chain()` already implements match -> build_asset -> QC ->
create_link -> deploy as one coupled, fail-closed function with no
independent decision point between "build" and "deploy" - splitting it
into two agents would mean either calling the same function twice or
performing risky surgery on already-correct, tested code for zero
benefit. Same "don't create an agent just because a diagram has a box
for it" judgment Phase 5 already applied to Quality Control and Link
Creation.

Deployment here is GitHub Pages ONLY (the owned channel - autonomous, no
login, no cost, credential-gated by the existing deployment adapter -
see its own module for the "never print/expose/rotate a credential"
guarantee). This agent NEVER attempts to create or log into a Gumroad/
Payhip account for the digital-product path - see digital_products.py,
a wholly separate, optional, parallel path that never blocks or is
required by this one (mandatory correction #3).
"""

from __future__ import annotations

from .agent import Agent
from .ecosystem.affiliate_pipeline import run_affiliate_chain
from .ecosystem.pipeline import draft_from_record
from .messages import Result, Task
from .opportunity_store import load_opportunities


class AffiliateChainAgent(Agent):
    role = "affiliate_chain_agent"
    objective = ("Turn a SELECTED opportunity + usable offer into a deployed, live "
                "GitHub Pages asset with a valid tracked affiliate link - or an "
                "explicit HUMAN_REQUIRED reason at the exact step that blocked it. "
                "Never touches a third-party account or login.")
    capabilities = ("run_affiliate_chain",)

    def run(self, task: Task) -> Result:
        payload = task.payload or {}
        data_dir = payload.get("data_dir")
        opportunity_id = payload.get("opportunity_id")
        if not data_dir or not opportunity_id:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload requires 'data_dir' and 'opportunity_id'")

        rec = load_opportunities(data_dir).get(opportunity_id)
        if rec is None:
            return Result(task_id=task.id, agent=self.name, status="error",
                          error=f"no opportunity record with id {opportunity_id!r}")

        draft = draft_from_record(rec)
        out = run_affiliate_chain(
            data_dir, opportunity_id=opportunity_id, draft=draft,
            now_iso=str(payload.get("now_iso") or ""),
            source=str(payload.get("source") or "own_site"),
            deployment_adapter=payload.get("deployment_adapter"),
            guide_title=str(payload.get("guide_title") or ""))
        # run_affiliate_chain never raises for an ordinary blocked step - it
        # returns a structured human_required result. Result.status="ok"
        # here means "the agent completed its own work without error", NOT
        # "the asset went live" - callers must check
        # output["status"]/["next_step_class"] for that.
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
