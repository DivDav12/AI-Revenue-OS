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
# once approved by the network:
AWIN_DATAFEED_API_KEY=<if using Awin>
AWIN_ADVERTISER_IDS=<comma-separated, approved only>
```

## 4. Ingest your first real affiliate offer

Once you've joined a program and have its real terms in hand:

```bash
revenue_os affiliate-ingest-offer path/to/offer.json --data-dir data
```

(See `ecosystem/affiliate_sources.ingest_affiliate_offer` / existing
tests for the exact JSON shape — never fabricate a commission rate or
price; only enter what the program's own dashboard states.)

## 5. Run the pipeline

```bash
revenue_os pipeline-cycle --data-dir data
```

See `OPERATIONS.md` for what happens next and how to run this on a
schedule.
