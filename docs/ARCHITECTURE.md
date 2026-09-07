# Architecture — Affiliate/Content Pipeline (V2)

Implements the model selected in `BUSINESS_MODEL_SCORING.md`: organic
content + multi-network affiliate + Pinterest distribution + a digital-
product upsell. This document describes what was actually built, not an
aspirational design — see `AUTONOMY.md` for the honest automatic/manual
breakdown.

## Pipeline

```
Opportunity (real discovery -> select)
  -> Affiliate Chain (content -> QC -> link -> GitHub Pages deploy)
  -> Distribution (Pinterest pin draft, rate-limited, human posts)
  -> Digital Product (parallel, best-effort, never blocks the chain)
  -> Measurement -> Optimization
```

One bounded cycle (`revenue_os pipeline-cycle`, backed by
`affiliate_cycle.run_cycle()`) — not a persistent daemon. Safe to invoke
repeatedly on a schedule (cron / GitHub Actions / a human running the
CLI) at $0. Every step above `SAFE_AUTONOMOUS` stops cleanly and is
reported in `human_actions`, never silently skipped, never crashing the
rest of the cycle.

## Agents (5, plus the pre-existing `pinterest_distributor`)

Phase 5 originally proposed 6; Content and Deployment were merged during
implementation once it became clear the existing, tested
`ecosystem.affiliate_pipeline.run_affiliate_chain()` already implements
match -> build -> QC -> link -> deploy as one coupled, fail-closed
function with no independent decision point between "build" and
"deploy" — splitting them would mean either calling the same function
twice or risky surgery on already-correct code for zero benefit.

| Agent | Role | Input | Output | Class |
|---|---|---|---|---|
| `opportunity_agent` | Real public-signal discovery (HN/RemoteOK/curated file) -> deterministic score/select against a currently-usable affiliate offer | seed sources, `AffiliateOfferStore` | SELECTED/REJECTED opportunity | `SAFE_AUTONOMOUS` |
| `affiliate_chain_agent` | Content -> QC -> link -> GitHub Pages deploy, as one atomic step | a SELECTED opportunity | a live asset + tracked link, or a precise `human_required` reason | `SAFE_AUTONOMOUS` (deploy only if a GitHub credential already resolves) |
| `pinterest_distributor` | Pin drafting for an already-deployed asset, rate-limited | a live `AffiliateAsset` | a `PinterestPinDraft` | drafting `SAFE_AUTONOMOUS`; posting is human-only, always |
| `measurement_agent` | Click aggregation (self-hosted) + commission rollup (human-fed) | opportunity/link records | a measurement report | `SAFE_AUTONOMOUS` |
| `optimization_agent` | Deterministic priority-weight recompute + zero-traffic flagging (never deletes) | settled outcomes, click data | weights + a flagged list | `SAFE_AUTONOMOUS` |

Digital-product generation is *not* a roster agent — it's a standalone,
deliberately separate function (`ecosystem.digital_products.
generate_product_draft()`) called directly by the cycle orchestrator,
because its only job is template generation with zero decision logic of
its own, and its defining property (never blocks or is blocked by the
affiliate chain) is best enforced by NOT coupling it into the same
call graph at all.

## What was kept, modified, replaced, or deleted from the prior 26-agent roster

See `BUSINESS_MODEL_SCORING.md`'s predecessor decision and the commit
history for the full per-cluster breakdown. In this implementation pass,
specifically:

- **Kept unmodified**: the safety firewall (`action_class.py`), the
  orchestrator/roster/team pattern, GitHub Pages deployment
  (`deployment.py`), the entire affiliate pipeline
  (`ecosystem/affiliate_*.py`), PayPal read-only revenue tracking.
- **Kept and reused directly** (discovered during implementation to
  already fully solve the "opportunity -> live asset" gap flagged in an
  earlier architecture pass): `ecosystem/discovery.py`,
  `ecosystem/verification.py`, `ecosystem/content_opportunity.py`,
  `ecosystem/affiliate_pipeline.py`, `ecosystem/offer_sources.py`
  (with real Awin/CJ connectors that activate only when actually
  authorized).
- **Modified**: `ecosystem/pinterest_pins.py` (added rate limiting),
  `roster.py`/`team.py` (added 4 new agents, additive).
- **Added, new**: `budget_guard.py`, `opportunity_agent.py`,
  `affiliate_chain_agent.py`, `measurement_agent.py`,
  `optimization_agent.py`, `affiliate_cycle.py`,
  `ecosystem/digital_products.py`.
- **Not deleted this pass** (audited, deferred — see below): the prior
  26-agent roster's service-business-specific modules (`outreach*.py`,
  `launch_plan.py`, `intake.py`, `acceptance.py`, `ads_manager.py`,
  `campaign_optimizer.py`, `budget_allocator.py`, `store_builder.py`,
  `developer.py`, `automation_engineer.py`, `supplier_finder.py`,
  `customer_support.py`, `review_manager.py`, `quality_control.py`,
  `acquisition*.py`, `autopilot.py`, `revenue_loop.py`, the old
  `budget.py`). A real dependency audit (grep-based reference counts)
  showed 4–47 external referrers per module across the existing,
  currently-passing test suite (roster/team/dashboard/jarvis/operator
  tests, CLI commands, and each other) — deleting them safely means
  migrating every one of those consumers and their tests, a large,
  separate effort disproportionate to what this pass needed to prove
  (a working, tested, live pipeline). Per the explicit "no blind bulk
  deletion" requirement, this was deferred rather than rushed; they
  remain on disk, unused by the new pipeline, and orphaned-but-harmless.

## MVP vs. deferred extensions

**Built and tested (MVP):** everything above, end to end, with fake
adapters proving the full chain (see `tests/test_affiliate_cycle.py`).

**Explicitly deferred, not built, not assumed:**
- Pinterest API integration for actual autonomous posting (needs
  Pinterest's own app review first — a human/legal step).
- Browser automation for any platform login or status check (none
  exists in this repo; see `SAFETY.md`).
- Digital-product marketplace upload automation (Gumroad/Payhip - always
  human, per their own onboarding).
- Dashboard/JARVIS visual rework beyond the minimal fix needed to keep
  it rendering all 30 roster agents (`_ECO_SLOTS` expanded from 6 to 7
  per cluster).
- Deletion of the superseded 26-agent-roster-era modules (see above).
