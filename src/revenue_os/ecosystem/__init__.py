"""AI Revenue OS - the autonomous revenue ecosystem.

This package is the "brain" that connects real opportunity discovery to the
existing execution stack (opportunity_store / opportunity_state / execution
queue / worker / acceptance / PayPal payment / SMTP delivery / revenue
ledger). It never rewrites that stack - it feeds it.

    real sources ─▶ DiscoveryEngine ─▶ Opportunity (origin="real")
                          │
                          ▼
                    verification.verify()   ─▶ DISCOVERED..QUALIFIED/REJECTED/HUMAN_REQUIRED/BLOCKED
                          │
                          ▼
                 profitability.evaluate()   ─▶ expected profit / profit-per-hour / risk  (ESTIMATE)
                          │
                          ▼
                    strategy.select()       ─▶ TASK | PRODUCT | AFFILIATE | ECOMMERCE | SERVICE | OTHER
                          │
                          ▼
                    pipeline.plan()         ─▶ an executable ExecutionTask chain (reuses acceptance.py)

Everything here is pure / deterministic unless a module name says otherwise
(only `sources` does real network I/O, and only through injectable fetchers
that tests replace). No module spends money, sends a message, posts
anywhere, or takes an identity/legal action - the existing action_class /
autonomous_context / approvals firewall still governs every real action.

`simulation` runs the whole loop over N synthetic opportunities with zero
external side effects; `learning` turns settled outcomes into deterministic
discovery/strategy priority weights (no ML).
"""

from __future__ import annotations
