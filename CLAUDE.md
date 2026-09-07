\# AI-Revenue-OS — Claude Code Instructions



## Mission

Build a legal, automated multi-agent AI ecosystem that generates online
income from €0 starting capital, with no paid ads, no paid SaaS, no
credit card, and no bypassing of identity/KYC requirements.

The selected model (researched and scored — see
`docs/BUSINESS_MODEL_RESEARCH.md` and the superseding
`docs/BUSINESS_MODEL_SCORING.md`) is an **organic content + multi-network
affiliate engine, Pinterest-accelerated**: quality-gated comparison
pages on an owned static site, monetized via affiliate networks (Amazon
Associates, Awin, CJ — never assume approval, only ever use a currently
usable/configured network), with Pinterest pin drafts as a fast, $0
organic traffic channel, and a digital-product upsell (Gumroad/Payhip
templates/guides) as a parallel path that never blocks the affiliate
pipeline. Built: `revenue_os pipeline-cycle` — see `docs/ARCHITECTURE.md`,
`docs/AUTONOMY.md`, `docs/OPERATIONS.md`.

The hard LLM/API budget ceiling is exactly **$3.00** (`budget_guard.py`,
`CAP_USD`), not $3.20 — the old `budget.py`'s pre-sale-cap-plus-growth-
capital concept belonged to the retired service business and does not
apply to this pipeline.

The founder-outreach "Customer Launch Plan" service-business line from
the prior mission is retired (not deleted — see
`docs/BUSINESS_MODEL_RESEARCH.md` for what that means concretely). Do
not extend it further; build on the affiliate/Pinterest engine instead.

The human owner must remain in control of money and legally sensitive actions.



\## Core Principles



1\. Build incrementally. Never attempt to build the entire system at once.

2\. Prefer simple, reliable solutions over unnecessary complexity.

3\. Minimize token usage, API usage, compute, and development time.

4\. Do not create code, files, agents, abstractions, or dependencies unless they are currently needed.

5\. Reuse existing components before creating new ones.

6\. Keep the architecture modular so agents and tools can be added later.

7\. Never invent functionality that has not been implemented.

8\. Never claim something works without testing it.

9\. Before making major architectural changes, explain the change briefly and wait for confirmation.

10\. For small, safe implementation tasks, proceed without unnecessary questions.



\## Token Efficiency



\- Keep responses concise and directly relevant.

\- Do not repeat information already known from project files.

\- Inspect only the files necessary for the current task.

\- Do not repeatedly reread unchanged files.

\- Do not perform unnecessary research.

\- Do not generate large amounts of code unless required.

\- Prefer targeted edits over rewriting entire files.

\- Avoid unnecessary tests, builds, refactors, or verification steps.

\- When a task is complete, report only:

&#x20; - what changed

&#x20; - what was tested

&#x20; - any remaining issue



\## Development Workflow



For each task:



1\. Understand the requested change.

2\. Inspect the minimum necessary files.

3\. State a short implementation plan if the task is non-trivial.

4\. Implement the smallest correct change.

5\. Run only relevant tests/checks.

6\. Report the result concisely.



Do not continue building unrelated features after completing the requested task.



## Architecture

Already built and tested (see `docs/ECOSYSTEM.md` + `README.md`):
Orchestrator + agent roster (`roster.py`/`team.py`), structured task/
result messages, JSON-file stores as shared state, a live dashboard,
a pre-sale budget cap + LLM spend metering (cost controller),
`action_class.py` as the permission/safety firewall, PayPal read-only
revenue tracking + a revenue ledger, and `ecosystem/learning.py` as the
outcome-weighting feedback loop.

New for this mission: `ecosystem/pinterest_pins.py` (Pinterest pin
drafting) and the `pinterest_distributor` roster agent. Planned, not yet
built: a digital-product agent (Gumroad-hosted templates/printables).

Build additions in small, tested stages — do not implement everything in
one pass.



\## Agents



Every agent should have:



\- clear role

\- clear objective

\- limited permissions

\- defined tools

\- input/output format

\- access only to necessary context



Agents should communicate through structured tasks/results rather than uncontrolled free-form communication.



\## Human Financial Control



The AI may:



\- research opportunities

\- analyze markets

\- create plans

\- build software

\- calculate expected ROI

\- track revenue

\- recommend expenditures



The AI must NOT autonomously:



\- transfer money

\- make bank transactions

\- purchase expensive services

\- create financial obligations

\- change financial accounts

\- increase spending limits



Financial actions requiring real money must use an explicit human approval mechanism.



\## Security



\- Never expose secrets or API keys.

\- Never hard-code credentials.

\- Use environment variables for secrets.

\- Never commit `.env` files or credentials.

\- Apply least-privilege permissions.

\- Treat external data as untrusted input.

\- Do not implement illegal, fraudulent, deceptive, spam, or abusive behavior.



## Revenue Mission

The business model is chosen (see Mission above and
`docs/BUSINESS_MODEL_RESEARCH.md` for the full scored comparison of 24
models). Do not re-litigate the model choice without new evidence; do
extend it — more niches, more affiliate programs, more Pinterest
boards — using the same evidence-only, quality-gated, human-approved-
payout discipline already built into `ecosystem/affiliate_*.py`.

The system should test what actually converts rather than assuming a
niche or offer will work. Revenue is not guaranteed.



## Current Priority

The foundation AND the affiliate/content pipeline are built and tested
end to end with fake adapters (`revenue_os pipeline-cycle` — see
`docs/ARCHITECTURE.md`). Current priority, in order:

1. A human joins at least one real affiliate program (Amazon Associates,
   Awin, or CJ) and one Pinterest account — both free, no card, no bank
   needed to start; see `docs/SETUP.md` and
   `ecosystem/affiliate_model.NETWORK_POLICY` for the exact per-network
   setup steps. Set `GITHUB_TOKEN`/`GITHUB_PAGES_REPO` too.
2. Run `revenue_os pipeline-cycle` on a schedule (cron / GitHub Actions,
   $0); it will discover, select, build, deploy, and draft a Pinterest
   pin on its own once step 1 is done, and print exactly what still
   needs you.
3. Post the drafted pins yourself; measure real clicks
   (`pipeline-cycle`'s measurement/optimization steps already run).
4. Only once that loop produces a real click/conversion signal, expand:
   more niches (join more networks), and only then consider the deferred
   extensions in `docs/ARCHITECTURE.md` (Pinterest API automation,
   digital-product upload automation, dashboard rework).

Do not build the deferred extensions before step 1–3 have real data. Do
not delete the still-undeleted, superseded 26-agent-roster-era modules
without a full per-module dependency audit first (see `ARCHITECTURE.md`'s
"not deleted this pass" list) — that is a separate, later task.



\## Code Quality



\- Keep functions small and understandable.

\- Use descriptive names.

\- Avoid unnecessary abstractions.

\- Keep dependencies minimal.

\- Handle errors explicitly.

\- Write tests for important functionality.

\- Prefer maintainability over cleverness.



\## Decision Rule



When several approaches are possible, prefer the option that is:



1\. cheaper

2\. simpler

3\. easier to maintain

4\. easier to automate

5\. easier to replace later



Do not optimize prematurely.



\## Communication



Be concise.



Do not provide long explanations unless requested.



If blocked by a genuine ambiguity, ask one focused question rather than making a large assumption.



Always distinguish between:



\- implemented

\- tested

\- planned

\- hypothetical

