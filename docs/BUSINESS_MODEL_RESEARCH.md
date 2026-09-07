# Business Model Research — Phase 1 & 2 (2026-09)

Mission pivot: the prior "any business model, founder-outreach + Customer
Launch Plan service" direction is retired. This document is the audit
trail for the researched, scored, and selected replacement: a €0-start,
organic-traffic content + affiliate engine, extended with Pinterest
distribution.

Hard constraints (from the mission brief) that immediately disqualify a
model: requires paid ads, a paid SaaS subscription, a credit card, a bank
account or ID verification **before any real activity can even start**,
or platform ToS violations. Tax-interview / payout-verification steps
that occur only once real money is about to move (Amazon Associates,
AdSense, Gumroad, ...) are NOT disqualifying — those are real, one-time,
human-only steps and are exactly what the Phase 4 Safety Gate
(`action_class.py`) already routes to a human. They are marked "human
step at payout" below, not "blocked".

## Scoring model

Weighted 0–10 per dimension, 100 pts total:

| dimension | weight | rationale |
|---|---|---|
| Start cost = €0 | 15 | absolute priority |
| Ongoing cost | 10 | no recurring spend |
| Verification / KYC burden | 20 | hard constraint area |
| Automatability | 20 | mission is agent-driven |
| Organic traffic fit | 15 | no paid ads allowed |
| Platform-dependency risk (10=low) | 10 | one policy change shouldn't kill it |
| Time to first realistic revenue | 10 | momentum matters |

## Full comparison (≥20 models evaluated)

| # | model | start cost | ongoing cost | accounts needed | KYC/verify | traffic source | automatable | platform risk | time to $1 | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Niche SEO/affiliate content site (Amazon Associates + Awin/CJ + display ads) | €0 (GitHub Pages) | €0–10/yr domain | Associates, Awin, CJ, AdSense/Ezoic | tax interview at payout only | Google organic search | high (content/SEO scriptable) | medium (Google algo) | 2–6 months | **shortlist** |
| 2 | Pinterest-driven digital products/affiliate (Gumroad/Payhip) | €0 | €0 | Pinterest, Gumroad | none to post; payout at $10 threshold | Pinterest search/organic | medium-high | medium (single platform) | 2–6 weeks | **shortlist** |
| 3 | Faceless YouTube Shorts (AdSense + affiliate) | €0 | €0 (local TTS/render) | Google/YouTube, AdSense | AdSense identity+address verify at payout | YouTube/Shorts organic | medium (video pipeline) | high (YPP tier gate: 500 subs+3M views tier 1 / 1,000+4,000h tier 2) | 3–6 months | shortlist (3rd) |
| 4 | Niche newsletter (beehiiv/Substack) + affiliate | €0 | €0 | beehiiv/Substack | none | cross-promo, SEO, social | medium | low-medium | 2–4 months | viable, slower |
| 5 | Amazon KDP low-content books | €0 | €0 | KDP | tax/bank at payout | Amazon internal search | medium (no official API) | high (saturated, policy crackdowns) | 1–3 months | viable, weak moat |
| 6 | Redbubble/TeePublic POD | €0 | €0 | Redbubble | none upfront | platform search + social | medium-high | medium | 1–2 months | viable, thin margins |
| 7 | Etsy POD (Printful/Printify) | ~€15 listing fee (card) | 5%+ fees | Etsy | **mandatory gov-ID + banking to unlock payouts, before first real sale** | Etsy search + Pinterest | high | high | 1–2 months | **discarded** — ID/bank gate + card-required fee up front |
| 8 | Shopify dropshipping | Shopify subscription | monthly SaaS + txn fees | Shopify + payment processor | merchant KYC on processor | needs paid ads in practice | medium | high | weeks (if ad-funded) | **discarded** — paid SaaS + typically paid ads |
| 9 | Fiverr/Upwork freelance automation | €0 | €0 | Fiverr/Upwork | ID verification for payout in most regions; ongoing bespoke human labor | platform-internal | low (service, not scalable) | high | weeks | **discarded** — service model, poor automation fit, ID-gated |
| 10 | Stock content (Adobe Stock/Shutterstock/Freepik) | €0 | €0 | stock platforms | tax/bank at payout | platform-internal search | medium | high (AI-content restrictions, saturation) | 2–4 months | deprioritized |
| 11 | Open source + GitHub Sponsors | €0 | €0 (hosting free tiers) | GitHub | none | GitHub/HN/dev communities | low (needs real engineering) | low | very slow, uncertain | deprioritized |
| 12 | Notion/Canva template marketplace | €0 | €0 | Gumroad/Canva Creator | none/approval-gated | Pinterest/SEO (same as #2) | medium-high | medium | 4–8 weeks | folded into #2 |
| 13 | Quora/Medium partner content | €0 | €0 | Quora/Medium | none | platform-internal | medium | high (programs unstable/discontinued in many regions) | uncertain | deprioritized |
| 14 | Reddit organic + affiliate links | €0 | €0 | Reddit | none | Reddit | medium | very high (self-promo rules, ban risk) | — | **discarded** — high ToS/ban risk for automated posting |
| 15 | X/Twitter content + affiliate bio link | €0 | needs paid subscription for monetization | X | payment method required for creator payouts | X organic (degraded for non-paying accounts) | medium | high | — | **discarded** — monetization requires a paid tier |
| 16 | Chrome/Firefox extension + affiliate/ads | €0 | one-time $5 Chrome dev fee (card) | Chrome Web Store | card required for the one-time fee | organic search/word of mouth | medium | medium | slow | **discarded** — mandatory card payment to publish |
| 17 | Instagram/TikTok Shorts organic → affiliate bio link | €0 | €0 | Instagram/TikTok | none to post | organic reach | medium (video pipeline) | high (reach algorithm volatile) | weeks–months | folded into #3 as a distribution add-on |
| 18 | TikTok Creator Rewards Program | €0 | €0 | TikTok | 10k followers + 100k 30-day views to even qualify | TikTok organic | medium | high (high bar to enter, payout ~$0.40–1/1k views) | months (to reach the bar) | deprioritized — high entry bar |
| 19 | Local/city-interest content site + display ads | €0 | €0 | AdSense/Ezoic | tax/bank at payout | local SEO | high | medium | 2–6 months | folded into #1 (same mechanics) |
| 20 | AI-tool/SaaS directory site (listing + affiliate) | €0 | €0 | Awin/Associates + PayPal for paid listings | none to start | SEO + backlinks | high | medium | 2–4 months | folded into #1 as a content vertical |
| 21 | Deal-aggregator newsletter/site | €0 | €0 | Awin/Associates | none to start | SEO + Pinterest | high | medium | 1–3 months | folded into #1/#2 |
| 22 | Print-on-demand via own Gumroad-hosted files (no marketplace) | €0 | €0 | Gumroad | none to start | needs its own traffic (Pinterest/SEO) | high | low | 4–8 weeks | folded into #2 |
| 23 | Systeme.io affiliate program promotion | €0 | €0 | systeme.io | none to start | SEO/content | medium (single curated offer, no product API) | medium | 2–4 months | already integrated as one offer source in #1 |
| 24 | Facebook Instant Articles / Meta content monetization | €0 | €0 | Facebook | region/eligibility gated, inconsistent | Meta organic (degraded reach) | medium | high (program availability shifts) | uncertain | deprioritized |

## Weighted scores (top candidates)

| rank | model | start | ongoing | verify | automat. | organic | platform | time | **total/100** |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Pinterest → digital products/affiliate | 10 | 10 | 9 | 7 | 9 | 6 | 7 | **83.5** |
| 2 | SEO/affiliate content site | 10 | 10 | 8 | 8 | 9 | 7 | 5 | **82.5** |
| 3 | Niche newsletter + affiliate | 10 | 10 | 9 | 6 | 6 | 8 | 4 | 76.0 |
| 4 | Faceless YouTube Shorts | 10 | 9 | 7 | 7 | 8 | 5 | 4 | 73.0 |
| 5 | Redbubble/TeePublic POD | 10 | 10 | 8 | 7 | 5 | 5 | 4 | 71.5 |
| 6 | Amazon KDP low-content books | 10 | 10 | 7 | 7 | 6 | 4 | 5 | 71.0 |
| 7 | Open source + GitHub Sponsors | 10 | 10 | 9 | 4 | 5 | 7 | 2 | 67.5 |
| 8 | Stock content marketplaces | 10 | 10 | 7 | 6 | 4 | 4 | 4 | 65.0 |

#1 and #2 are close enough, and complementary rather than competing (same
affiliate networks, same "own page" destination, different traffic
engine and different time-to-first-click), that the selected model
**combines both** instead of picking one exclusively.

## Selected model: Organic Content + Affiliate Engine, Pinterest-accelerated

**What it is:** deploy quality-gated, evidence-only "problem → affiliate
offer" comparison pages on an owned static site (existing
`ecosystem/site.py` + `ecosystem/affiliate_assets.py`), monetized through
Amazon Associates / Awin / CJ / systeme.io / Wondershare-style affiliate
programs (all already modeled in `ecosystem/affiliate_model.py`), and
accelerated with Pinterest pin drafts pointing at those same live pages
(new: `ecosystem/pinterest_pins.py`) so early traffic doesn't have to
wait out Google's months-long indexing/ranking curve. A digital-product
upsell (Gumroad-hosted templates/printables in the same niches) is the
next planned increment once the content base exists — not built yet, so
it is **not** claimed as implemented.

**Why it won:**
- Highest combined weighted score, and ~70% of the required
  infrastructure already exists and is tested (affiliate offer model,
  quality-gated page rendering, GitHub Pages deploy, PayPal read-only
  revenue booking, budget caps, the whole `action_class.py` safety
  firewall) — reusing it is both cheaper and lower-risk than building a
  new model from zero, per the project's own "reuse existing components"
  rule.
- Every verification requirement in the winning model is a one-time,
  human-only step at real payout (tax interview, PayPal/bank details) —
  never a blocker to starting, drafting, or deploying content.
- Two independent, genuinely organic traffic sources (Google SEO,
  Pinterest) instead of one, which lowers single-platform risk relative
  to YouTube-only or newsletter-only models.
- No paid ads, no paid SaaS, no credit card, at any step.

## What "not continued" means concretely

The founder-outreach / "Customer Launch Plan" service-business line
(`outreach.py`, `launch_plan.py`, `intake.py`, the €29.90 consulting
product, the general-purpose "any business type" opportunity-discovery
ecosystem) is **not** being extended further. It is not deleted — it is
real, tested code and deleting it destroys working history for no
benefit — but it is no longer the active mission and should not be
built on. The safety/financial fabric it shares with the new mission
(`action_class.py`, `approvals.py`, `budget.py`, `paypal.py`,
`deployment.py`, the roster/orchestrator pattern) is kept as-is; it was
already built to exactly the discipline this mission needs.

## Sources consulted (2026)

- [Amazon Affiliate Program Requirements 2026](https://getaawp.com/blog/amazon-affiliate-program-requirements/)
- [AdSense payment thresholds](https://support.google.com/adsense/answer/1709871?hl=en)
- [YouTube Partner Program Requirements 2026](https://air.io/en/monetization/youtube-partner-program-requirements-2026-the-complete-guide)
- [Awin partner types overview](https://help.awin.com/docs/partner-types-overview)
- [TikTok Creator Rewards Program 2026](https://www.shortsync.app/resources/tiktok-creator-rewards-program-2026)
- [Google Scaled Content Abuse crackdown](https://www.digitalapplied.com/blog/scaled-content-abuse-google-march-update-ai-pages-decimated)
- [Etsy print-on-demand ID/banking verification 2026](https://www.insightagent.app/guides/print-on-demand-etsy-rules)
- [Gumroad/Payhip/Ko-fi payout comparison 2026](https://delivvo.io/blog/gumroad-vs-payhip-vs-kofi-sell-digital-2026)
