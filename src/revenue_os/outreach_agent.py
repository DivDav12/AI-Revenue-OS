"""Roster `outreach_drafter` - the Agent wrapper around `outreach_brief`.

A thin adapter so the drafter is registry-routable exactly like the other
roster agents (capability `draft_outreach`). It does no work of its own -
it delegates to `outreach.outreach_brief`, which builds a HUMAN-REVIEW
draft only.

Human-gated: this agent NEVER posts, comments, DMs, or emails. Its output
is a draft a person edits and posts themselves, after checking the
community's own self-promotion rules. It invents no facts about the lead.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Result, Task
from .outreach import DEFAULT_CHECKOUT_URL, outreach_brief


class OutreachDrafterAgent(Agent):
    role = "outreach_drafter"
    objective = "Turn a scored lead into a human-review outreach draft; never posts."
    capabilities = ("draft_outreach",)

    def run(self, task: Task) -> Result:
        lead = task.payload.get("lead")
        if not isinstance(lead, dict) or not lead.get("lead_id"):
            return Result(
                task_id=task.id, agent=self.name, status="error",
                error="payload['lead'] must be a lead dict carrying a lead_id",
            )
        checkout_url = str(task.payload.get("checkout_url") or DEFAULT_CHECKOUT_URL)
        brief = outreach_brief(lead, checkout_url=checkout_url)
        return Result(task_id=task.id, agent=self.name, status="ok", output=brief)
