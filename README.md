# AI-Revenue-OS
Autonomous multi-agent AI revenue ecosystem

## Acquisition Agent (`discover-opportunities`)

Finds **public** posts where someone is explicitly asking how to get their
first paying customers/clients/users - potential buyers of the Customer
Launch Plan.

```
python -m revenue_os discover-opportunities --data-dir data \
  [--source hn-algolia|reddit|both|file|static] [--query "phrase" ...] \
  [--limit 15] [--min-score 0] [--dry-run] [--json]
```

- `acquisition_sources.py` isolates the I/O: `hn-algolia` and `reddit` hit
  free, keyless public search APIs; `file`/`static` are offline. Default
  source: `hn-algolia`.
- `acquisition.py` scores each **real** post deterministically (no LLM, no
  cost): intent-phrase weights + title bonus + recency -> `fit_score`
  0-100, `buying_intent`, and an advisory `promo_allowed`
  (`no|caution|likely|unknown`) with the reason.
- Every stored field is copied verbatim from the API record or computed
  from it - nothing is synthesised (missing author/timestamp stay empty).
  Quality checks drop leads with no real URL, no title, a placeholder
  host, or no intent match.
- Leads are stored in `data/acquisition.json`, de-duplicated by canonical
  URL, ranked by `fit_score`. Re-runs add only new threads.

**It never posts, replies, or contacts anyone.** `promo_allowed` is a
conservative hint; always read the community's own rules before replying.

## PayPal

Read-only integration (`src/revenue_os/paypal.py`): it books payments PayPal
has already captured into the RevenueLedger. It cannot move money, change an
account, or create an obligation.

Setup: copy `.env.example` to `.env` and fill in `PAYPAL_CLIENT_ID`,
`PAYPAL_CLIENT_SECRET`, `PAYPAL_ENV` (`sandbox` or `live`). Nothing loads
`.env` automatically -- export the vars into the shell before running.

Commands:

- `revenue_os paypal-verify <candidate> <order-id>` -- verify one order and
  book it.
- `revenue_os paypal-sync [--days N] [--dry-run]` -- book recent payments,
  matched to candidates by the order's `custom_id`. Needs the "Transaction
  Search" feature enabled on the PayPal app.
- `revenue_os build-checkout <candidate> --price 29.90 [--currency EUR]
  [--what NAME --promise LINE --delivery-note LINE --disclaimer LINE
  --include "bullet" ...]` -- write a self-contained
  `deliverables/<candidate>/checkout.html` with a real PayPal JS SDK button.
  The order it creates sets `custom_id` to the exact candidate name. Requires
  `PAYPAL_ENV=live` and `PAYPAL_CLIENT_ID` in the environment, and the
  candidate to be `launched`/`earning`. Persists the offer (price, promise,
  "what you get" bullets, disclaimer) on the candidate. The page never records
  revenue -- reconcile the real payment afterwards with `paypal-sync`.

Payments are tied to a candidate by setting the order's `custom_id` to the
candidate name. Booking still requires the candidate to be `launched` or
`earning`.

### Buyer intake (Customer Launch Plan)

`build-checkout` also writes `intake.html` and embeds the same form in
`checkout.html` (revealed after payment, with the order + capture id filled
in). Pass `--form-action <url>` to point the form at a form provider
(Formspree, Getform, Netlify Forms, a Google Apps Script -- your choice); a
visible placeholder is shown if you omit it. Revenue OS runs no server and
holds no form secrets.

Flow: buyer pays -> submits the form -> you export the submissions from the
provider as **CSV or JSON** -> `revenue_os intake-import <export.csv>`
(`csv.DictReader`; extra provider columns are ignored). A row is stored
only if its `capture_id` matches a booked `paypal:<id>` payment for the
candidate (run `paypal-sync` first) - the same gate for CSV and JSON. Then
`intake-list`,
`intake-show <order-id>`, and `intake-review <order-id>` (the human gate
before the plan is written and the PDF delivered).

`data/intake.json` holds customer-supplied personal data. `data/` is
gitignored -- do not commit or share it.

### Drafting the Customer Launch Plan

Once an intake is `reviewed`:

- `revenue_os draft-launch-plan <order-id> [--mode web|llm] [--max-cost 1.5]`
  -- one web-grounded Claude call (`launch_plan.py`, reuses the evaluator's
  cost meter / cache / cumulative `llm-budget` cap) drafts all sections;
  a deterministic quality-control pass (`qc_plan`) checks the shape (14
  days, 5-10 opportunities, 2-3 templates, sources, no promise language)
  before the draft is stored. `--mode web` requires real sources or it
  fails. Re-runs are served from `llm_launch_plan_cache.json`.
- `revenue_os intake-show <order-id>` -- review the draft + QC + sources.
- `revenue_os plan-approve <order-id>` -- the human gate before delivery.
- `revenue_os plan-render <order-id>` -- writes
  `deliverables/<candidate>/plan-<order-id>.md` (approved plans only).
  Convert to PDF yourself; **nothing is sent to the customer
  automatically**.

The draft gate re-checks that the order's `capture_id` is still a booked
`paypal:` payment. No candidate status changes; no money moves.

Moving from sandbox to live: see [docs/paypal-live-switch.md](docs/paypal-live-switch.md).
