# Business Model Scoring — Phase 2, 3, 4 (2026-09, V2 pass)

Supersedes the scoring section of `BUSINESS_MODEL_RESEARCH.md` (that
document's Phase 1 model table and sources remain valid and are reused
here) with a stricter, explicitly-weighted rubric, an adversarial review
of the top pick, and a documented Top 3 / final winner.

## Weighting (0–100, as specified for this pass)

| dimension | weight |
|---|---|
| €0 start cost | 25 |
| Automatability | 20 |
| Organic traffic fit | 15 |
| Time-to-first-revenue | 10 |
| Low ongoing cost | 10 |
| Low KYC/verification burden | 5 |
| Low platform dependency | 5 |
| Scalability | 5 |
| Technical feasibility | 5 |

Gate 0 (checked before scoring, per the mission's non-negotiable
criteria): a model requiring KYC/bank/credit-card/paid-ads/paid-SaaS as
a blocker *before any real activity can start* is discarded outright,
never scored. Eight of the 33 evaluated models were discarded at this
gate (Etsy POD, Shopify dropshipping, Fiverr/Upwork, Reddit automated
promotion, X monetization, Chrome Web Store publishing, B2B cold-outreach
lead-gen, local lead-gen reselling) — see `BUSINESS_MODEL_RESEARCH.md`
for the full 33-model table and reasons.

## Scored ranking (top 12 of 25 gate-surviving models)

| model | €0(25) | Automat.(20) | Organic(15) | Time(10) | LowCost(10) | LowKYC(5) | PlatformIndep(5) | Scale(5) | TechFeas(5) | **Total** |
|---|---|---|---|---|---|---|---|---|---|---|
| Multi-platform syndication (SEO+Pinterest+affiliate, combined) | 25 | 16 | 15 | 6 | 10 | 5 | 5 | 5 | 4 | **91** |
| Pinterest + digital products/affiliate (no SEO leg) | 25 | 17 | 15 | 8 | 10 | 5 | 3 | 4 | 4 | 91 |
| SEO/affiliate content site alone | 25 | 15 | 15 | 4 | 9 | 4 | 3 | 5 | 5 | 85 |
| Deal/coupon aggregator | 25 | 16 | 13 | 6 | 10 | 4 | 3 | 4 | 4 | 85 |
| Education/guide digital product | 25 | 16 | 12 | 6 | 10 | 5 | 3 | 3 | 4 | 84 |
| Tool/SaaS directory site | 25 | 15 | 13 | 5 | 10 | 4 | 3 | 4 | 4 | 83 |
| Digital downloads via own site | 25 | 15 | 10 | 5 | 10 | 5 | 4 | 3 | 4 | 81 |
| Niche newsletter + affiliate | 25 | 13 | 9 | 4 | 10 | 5 | 4 | 3 | 4 | 77 |
| Notion/Canva template marketplace | 25 | 14 | 10 | 6 | 9 | 4 | 2 | 3 | 3 | 76 |
| Job/opportunity board aggregator | 25 | 15 | 10 | 5 | 8 | 4 | 2 | 3 | 3 | 75 |
| Redbubble/TeePublic POD | 25 | 14 | 7 | 6 | 9 | 4 | 2 | 3 | 3 | 73 |
| Faceless YouTube Shorts | 25 | 12 | 11 | 3 | 8 | 3 | 2 | 4 | 2 | 70 |

## Adversarial review of the provisional #1

Actively attacked with the required question list; findings that
**changed the design**, not just confirmed it:

1. **Amazon Associates' 180-day/3-sales cliff is real** — a brand-new
   site risks losing the program before it has traffic. → Multi-network
   diversification (Awin + CJ alongside Associates) is now a **binding
   requirement from day one** in the architecture, not a later add-on.
2. **Pinterest automation is only safe within a paced envelope** (2026
   third-party reports: 5–15 pins/day; a new account bursting gets
   flagged) — this is our own conservative policy, never an official
   Pinterest number. → A rate limiter (`MAX_PINS_DRAFTED_PER_DAY = 10`)
   is now a binding implementation requirement, explicitly documented as
   internal, not Pinterest's.
3. **Google's "site reputation abuse" policy is narrower than first
   assumed** — it targets parasite SEO on someone else's high-authority
   domain, not a small first-party site — but a brand-new own domain
   still ranks slowly regardless ("rented reputation is fragile...
   [first-party content is] more sustainable — but still slow", 2026
   sources). → Revenue timeline is stated as a range (Pinterest: weeks;
   Google: 3–8 months), never a single optimistic number, and the
   architecture never depends on Google traffic for the first revenue.
4. **Traffic is not literally free** — content creation costs agent time
   and human review time, even at $0 cash cost. Documented, not hidden.
5. **A genuinely competing alternative exists** (Pinterest + digital
   products alone, dropping the SEO leg entirely) and scores identically
   (91) — see the tie-break below.

No finding discarded the pick; two findings became binding
implementation requirements (multi-network from day one, Pinterest rate
limiting) that are now built and tested — see `SAFETY.md`.

## Top 3

1. **Multi-platform syndication: organic content + multi-network
   affiliate + Pinterest + digital-product upsell** — 91/100
2. **Pinterest + digital products/affiliate (no SEO leg)** — 91/100
3. **Deal/coupon aggregator site + newsletter** — 85/100

## Final winner and why

**#1**, resolved on tie-break against #2: #1 strictly contains #2's
entire mechanism (Pinterest, digital products) *plus* a second, slower
but durable traffic leg (Google/SEO) that doesn't depend on any single
platform's policy mood. Sequencing answers the adversarial finding about
Google's slow ranking — Pinterest carries months 1–3, SEO compounds
after — rather than dropping the channel for no corresponding gain in
€0-ness, automatability, or safety.

**#3 lost** mainly on organic-traffic durability (deal content churns
faster and ranks/pins less durably than evergreen comparison guides) and
faces entrenched incumbents (RetailMeNot, Slickdeals); it remains a
legitimate *future content vertical* inside the winning model, not a
separate business.

## Assumptions carried into the architecture

- Amazon Associates / Awin / CJ publisher applications will actually be
  approved for the chosen niches (not guaranteed — Awin runs a real
  compliance review).
- The 5–15 pins/day pacing is third-party-reported, not an official
  Pinterest limit, and could change without notice.
- "3–8 months to organic Google revenue" is a range from 2026 sources
  for a brand-new domain in a competitive niche — could be faster in a
  genuinely underserved niche, or never happen if the niche is wrong.
- No paid tool is assumed anywhere in the pipeline; free-tier hosting
  (GitHub Pages) is assumed to remain free at the traffic levels this
  system will realistically reach early on.

## Risks carried forward (bounded, not eliminated)

See `RISKS.md` for the full, current list.
