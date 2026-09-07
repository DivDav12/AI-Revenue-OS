# Risks

Carried forward from the adversarial review in `BUSINESS_MODEL_SCORING.md`,
plus implementation-specific risks found while building. Revenue is not
guaranteed at any point in this chain.

## Platform risks

- **Amazon Associates' 180-day/3-sales cliff**: an account can be closed
  for inactivity before it has real traffic. Mitigated (not eliminated)
  by requiring multi-network diversification (Awin/CJ) from day one —
  built into `usable_offers()`'s network-agnostic selection, not
  optional.
- **Pinterest policy/enforcement changes**: the 5–15 pins/day pacing is
  third-party-observed, not official, and could be wrong or change
  without notice. The rate limiter (`MAX_PINS_DRAFTED_PER_DAY = 10`) is
  a conservative guess, not a guarantee of safety.
- **Awin/CJ approval is not guaranteed**: both run real compliance
  review; a rejected application means that network contributes zero
  usable offers, correctly reported as `HUMAN SETUP REQUIRED`, never
  silently worked around.
- **Google ranking timeline for a new domain is genuinely slow** (3–8
  months per 2026 sources) and not something this system can
  accelerate — the architecture deliberately does not depend on it for
  first revenue (Pinterest carries the early months).

## Traffic risks

- Content creation costs real time (agent compute + human review), even
  at $0 cash cost — traffic is not literally free.
- Niche competition in broad categories (electronics comparisons, deal
  aggregation) is real; the mitigation is niche selection, not a
  different architecture.
- Zero-traffic assets are flagged for human review
  (`optimization_agent.zero_traffic_assets`) but never auto-deleted —
  the system will keep a genuinely dead page up until a human acts.

## Monetization risks

- No affiliate network in this codebase provides a live conversion
  feed — Amazon's own Creators API requires 10 prior qualifying sales
  (a documented chicken-and-egg limitation). Commission figures depend
  entirely on a human periodically reading their network's dashboard and
  recording what it says (`measurement_agent` reports this honestly
  rather than estimating a number).
- Digital-product revenue depends on the human actually creating a
  Gumroad/Payhip account and uploading the generated file — the pipeline
  will keep generating drafts regardless, but they produce $0 until that
  one-time setup happens.

## Technical risks

- The prior 26-agent roster's service-business-specific modules remain
  on disk, unused but undeleted (see `ARCHITECTURE.md`) — a future
  contributor could accidentally re-wire one of them; they are not
  actively maintained or tested against the new pipeline.
- `content_opportunity.find_content_opportunities()`'s relevance
  scoring is keyword/category-overlap based, not semantic — a real
  demand signal phrased very differently from the offer's own keywords
  may be missed (a false negative, never a fabricated match).
- GitHub Pages' own rate limits/outages are a real, if rare, deployment
  risk — `run_affiliate_chain` reports `human_required` on any deploy
  failure rather than fabricating success.

## Legal/ToS risks

- Every affiliate page includes the required disclosure text
  (`affiliate_assets.DISCLOSURE_TEXT`) and every Pinterest pin includes
  `PIN_DISCLOSURE` — both checked by automated tests, not just written
  once and forgotten.
- No content in this pipeline is ever posted to a platform whose own
  rules forbid automated posting (`action_class.posting_permitted()`
  fails closed for every non-owned channel) — this was a hard gate in
  Phase 1's model selection (Reddit/X/Chrome-extension-fee models were
  discarded specifically for this reason).
- Digital-product generation reproduces only the caller's own supplied
  evidence/facts — it does not scrape or reproduce third-party
  copyrighted material.
