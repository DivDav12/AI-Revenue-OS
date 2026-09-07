# Operations — Running the Pipeline

## The daily loop

```bash
revenue_os pipeline-cycle --data-dir data
```

One bounded cycle: real discovery → select → build → QC → deploy → pin
draft → digital-product draft → measure → optimize. Not a daemon — safe
to run repeatedly (cron, a GitHub Actions scheduled workflow at $0, or
just running it yourself). Prints what happened and, under "Needs you:",
exactly what requires a human action this cycle — never silent, never
fabricated as done.

`--source hn,remoteok` (default) uses real, keyless public APIs.
`--source file --source-path path/to/signals.json` uses a curated,
human-vouched list instead (or in addition — pass `--source` multiple
times).

## What you'll be asked to do, and how

- **`PINTEREST: review and post draft <id>`** → `revenue_os
  pinterest-pending` to see it, post it yourself on Pinterest, then
  `revenue_os pinterest-mark-posted <id> posted`.
- **`DIGITAL PRODUCT: generated, but no Gumroad/Payhip account
  confirmed`** → create a free account if you want this channel, upload
  the generated file at `data/digital_products.json`'s referenced
  content yourself, then set `GUMROAD_ACCOUNT_CONFIRMED=1` (or
  `PAYHIP_ACCOUNT_CONFIRMED=1`) so future cycles stop reminding you.
- **`DEPLOYMENT: no GitHub Pages credential configured`** → see
  `SETUP.md` step 1–2.
- **`OPPORTUNITY: no usable affiliate offer`** → see `SETUP.md` step 3.

## Checking status without changing anything

```bash
revenue_os pipeline-cycle --data-dir data --json     # full machine-readable report
revenue_os pinterest-pending --data-dir data          # pin drafts waiting on you
revenue_os dashboard-serve --data-dir data            # live status of all 30 agents
```

## Scaling up

Once the first real click/conversion signal exists, the correct next
step (per `optimize`'s priority weights, which need ≥5 settled outcomes
before they mean anything) is: more niches through the same
`pipeline-cycle`, more affiliate networks approved, not a new
architecture. Do not build the deferred extensions in `ARCHITECTURE.md`
before that signal exists.

## Recording a real commission

No network gives a live conversion feed (see `SAFETY.md`). Read your
network's own dashboard periodically and record what it says:

```bash
revenue_os affiliate-record-commission --link-id <link-id> --confirm --ref <network-ref> --data-dir data
```

(See `ecosystem/affiliate_revenue.py` for the full lifecycle: PENDING →
CONFIRMED/REVERSED → PAID.)
