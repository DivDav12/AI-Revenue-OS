# Costs

## Startup cost: €0

- Hosting: GitHub Pages (free, no card).
- Affiliate networks: Amazon Associates, Awin, CJ — free to join (Awin
  may involve a real, refundable publisher deposit — a human
  money-approval decision, never automatic; see `SAFETY.md`).
- Pinterest: free account, no card, no ID.
- Digital-product platforms (optional): Gumroad/Payhip — free to create.
- Software: Python standard library only for the base install
  (`pyproject.toml`: `dependencies = []`). No paid API is required for
  any part of the pipeline built in this pass.

## Ongoing cost: €0 (+ optional domain)

- GitHub Pages hosting stays free at the traffic levels this system will
  realistically reach early on (documented assumption, not a guarantee —
  see `RISKS.md`).
- A custom domain is optional, not required (~€10/year if you want one;
  GitHub Pages subdomains work at €0).
- No paid ads anywhere in this pipeline, by design.
- No paid SaaS/automation platform anywhere in this pipeline.

## LLM/API budget: hard $3.00 ceiling, currently $0 spent

`budget_guard.py` enforces an absolute $3.00 cap (see `SAFETY.md` for
the fail-closed mechanics). As built, the entire pipeline — discovery,
scoring, content generation, pin drafting, digital-product generation —
is deterministic and template-based; **no LLM call is made anywhere in
this pass**, so current recorded spend is $0.00 of the $3.00 budget.

Check current spend at any time:

```bash
python3 -c "from revenue_os import budget_guard; print(budget_guard.spent('data'), '/', budget_guard.CAP_USD)"
```

## What could cost money later (all human-gated)

- A custom domain, if you choose one (~€10/year) — a human purchase, not
  automatic.
- Awin's refundable publisher deposit, if that network requires it for
  your chosen advertisers — a human money-approval decision.
- Any future LLM-assisted step (e.g. AI-varied prose) would be
  budget-gated against the same $3.00 ceiling and never exceed it.
