# Setup — Exact Manual Steps + Install

## 1. Install (no cost)

```bash
pip install -e .
```

No dependency in `pyproject.toml`'s base install costs money.
`pip install anthropic` is optional and only needed if you deliberately
add an LLM-assisted step later — the pipeline built in this pass never
calls one.

## 2. Human setup steps — do these yourself; the fleet never can

Every step below is a real account/credential a human must create.
Nothing here can be automated safely, and nothing here was bypassed.

| # | What | Where | Cost | Notes |
|---|---|---|---|---|
| 1 | A GitHub Personal Access Token (fine-grained, Contents: read+write on this repo) | github.com/settings/tokens | Free | Set as `GITHUB_TOKEN` in `.env` (gitignored) or your shell env. Never commit it. |
| 2 | A GitHub Pages-enabled repo for the deployed site | this repo's Settings → Pages | Free | Set `GITHUB_PAGES_REPO=<owner>/<repo>` |
| 3 | At least one affiliate program: Amazon Associates, Awin, or CJ Affiliate | each network's own signup | Free | See `ecosystem/affiliate_model.NETWORK_POLICY` for the exact per-network setup steps and required env vars (e.g. `AWIN_DATAFEED_API_KEY`). Awin may require a real, refundable publisher deposit — a human money-approval decision, never automatic. |
| 4 | (Optional, secondary path) A Gumroad or Payhip account | gumroad.com / payhip.com | Free to create | Only needed if you want the digital-product upsell to actually go live; the affiliate pipeline works fully without it. |
| 5 | A free Pinterest account | pinterest.com | Free | For posting the drafts the system prepares. No card, no ID needed. |

None of steps 1–5 require a credit card or ID verification to *start* —
tax/bank details are only ever requested by these platforms themselves,
at real payout, and are never touched by this codebase.

## 3. `.env` file (gitignored)

```
GITHUB_TOKEN=<your fine-grained PAT>
GITHUB_PAGES_REPO=<owner>/<repo>
# once approved by the network (see the "Affiliate offer discovery" block
# in .env.example for CJ / Awin / Wondershare / systeme.io):
AWIN_DATAFEED_API_KEY=<if using Awin>
AWIN_ADVERTISER_IDS=<comma-separated, approved only>
```

## 4. Get your first real affiliate offer into the pipeline

You have two routes. Both end at the same fail-closed schema gate
(`ecosystem/affiliate_sources.ingest_affiliate_offer`) — never fabricate a
commission rate, price, link, or approval; enter only what the program's
own dashboard/terms state.

### 4a. Assisted: offer discovery → complete a candidate

Once **any one** offer-source network is configured in `.env` (see the
"Affiliate offer discovery" block in `.env.example` — CJ Affiliate,
Awin datafeed, Wondershare/Awin link, or systeme.io link), the pipeline
searches that network's real product API for each discovered demand
signal and stages the results as **candidates**:

```bash
revenue_os affiliate-offer-candidates --discover --data-dir data
```

Networks with no credentials are never contacted. A candidate is **not**
a usable offer — a product search cannot state your commission or your
membership. Review what each still needs, then complete one with your
real, evidenced terms:

```bash
revenue_os affiliate-complete-offer <candidate_id> --data-dir data \
    --program-name "<the program you joined>" \
    --commission-kind <fixed|percent|recurring_percent> \
    --commission-rate <the rate stated on your dashboard, e.g. 0.30> \
    --commission-evidence "<paste the exact wording from your affiliate dashboard/terms>" \
    --confirm-joined
```

`--confirm-joined` is mandatory and means *a human has already been
accepted into this program*. The fleet never joins a program itself.

### 4b. Manual: ingest a full offer JSON

```bash
revenue_os affiliate-ingest-offer path/to/offer.json --data-dir data
```

(See `ecosystem/affiliate_sources.ingest_affiliate_offer` / existing
tests for the exact JSON shape.)

## 5. Run the pipeline

```bash
revenue_os pipeline-cycle --data-dir data
```

See `OPERATIONS.md` for what happens next and how to run this on a
schedule.

## 6. Product images (currently blocked — needs an Amazon API)

Public product pages show a neutral icon placeholder instead of a real
photo. This is **not a bug** — it is the fail-safe: nothing in this
project fetches, scrapes, screenshots or guesses a product image.

The **only** Amazon-sanctioned source of product image URLs is the
Product Advertising API (PA-API 5.0) / its **Creators API** successor,
which returns image URLs on Amazon's own media CDN
(`m.media-amazon.com`, `*.ssl-images-amazon.com`). As of 2026:

* PA-API 5.0 is deprecated in favour of the Creators API.
* Creators API access requires **10 qualifying affiliate sales in the
  trailing 30 days** — this account currently has 0.
* SiteStripe's image / "Text+Image" link types were retired by Amazon in
  2024, so a logged-in Associate can no longer generate image links
  there either.

**What you need to provide once available:** Creators API (or PA-API)
credentials for the `airevenue-21` account, then a small credentialed
connector can populate `AffiliateOffer.image_urls` for each ASIN.

**Manual population in the meantime:** if you obtain a compliant Amazon
image URL through a permitted mechanism, attach it yourself:

```bash
revenue_os affiliate-set-image <offer_id> "https://m.media-amazon.com/images/I/....jpg" --data-dir data
```

The command **rejects** any URL that is not an HTTPS image on an Amazon
media CDN (Google Images, third-party hosts, placeholders, screenshots
are all refused). Everything downstream — product cards, the `/product/`
index, product detail pages (with a small accessible gallery if you
supply more than one image), related-product cards and the `Product`
JSON-LD — renders the real image automatically once `image_urls` is
populated. Nothing else needs to change.
