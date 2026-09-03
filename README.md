# AI-Revenue-OS
Autonomous multi-agent AI revenue ecosystem

## Autonomous Revenue Ecosystem (`discover` / `evaluate` / `select-strategy` / `simulate`)

Real opportunity discovery → deterministic profitability evaluation →
monetisation-strategy selection → an executable task chain that reuses the
existing PayPal checkout / SMTP delivery / revenue-ledger stack.

```
revenue_os discover --source synthetic|hn|remoteok|file[,...] [--limit N]
revenue_os evaluate <OPP_ID>            # profitability projection (every number is an ESTIMATE)
revenue_os select-strategy <OPP_ID>     # TASK|PRODUCT|AFFILIATE|ECOMMERCE|SERVICE|OTHER (service is never the default)
revenue_os plan-strategy <OPP_ID>       # PRODUCT -> acceptance chain; other -> prepared, HUMAN_REQUIRED
revenue_os simulate --n 10000 --seed 42 # whole-loop simulation, zero external side effects
revenue_os ecosystem-status
```

`hn` and `remoteok` are real, keyless, read-only public APIs. Sources that
need an account (`upwork`, `fiverr`, `amazon_associates`, `shopify`) report
`HUMAN_SETUP_REQUIRED` and yield nothing - the fleet never self-provisions
credentials, logs in, or posts. See **[docs/ECOSYSTEM.md](docs/ECOSYSTEM.md)**.

## Acquisition Agent (`discover-opportunities` / `top-opportunities`)

Finds **current, real** public posts from founders who are actively
struggling to get their first paying customers - people the EUR 29.90
Customer Launch Plan could help right now.

```
# free-first: only keyless sources, $0, never calls Anthropic
python -m revenue_os discover-free --data-dir data \
  [--source hn-algolia] [--source stackexchange] \
  [--query "phrase" ...] [--limit 15] [--min-score 0] [--max-age-days 30] \
  [--dry-run] [--json]

# same, plus the optional paid --source web (Anthropic web_search, budget-gated)
python -m revenue_os discover-opportunities --data-dir data [--source web ...] \
  [--score deterministic|llm] [--max-cost 1.0] [--refresh] ...

python -m revenue_os top-opportunities [--limit 10] [--min-score 60] \
  [--max-age-days 30] [--all] [--json]

python -m revenue_os review-opportunity <lead-id> --approve|--reject
```

**Sources (free-first).** `--source` is repeatable; default =
`hn-algolia` + `stackexchange` + `lobsters` + `lemmy` (all keyless, $0).
The CLI groups sources by tier in its output:

| tier | source | note |
|---|---|---|
| FREE | `hn-algolia` | HN search via Algolia; `--max-age-days` switches to date-sorted `search_by_date` incl. Ask HN |
| FREE | `stackexchange` | SE API, keyless; `freelancing` + `webmasters` (there is no live "startups" SE). Spaces its calls, honours the `backoff` field, retries one `429` |
| FREE | `lobsters` | `lobste.rs/newest.json` recent-story feed, keyword-filtered client-side (lobsters has no keyless search API); catches a *fresh* on-topic post, not a back-catalogue |
| FREE | `lemmy` | `lemmy.world /api/v3/search`, keyless; federated founder Q&A - recovers some of the Reddit-style discussion without touching Reddit |
| UNAVAILABLE (auth) | `reddit` | `robots.txt` `Disallow: /`; JSON API 403 - left failure-isolated, not bypassed |
| UNAVAILABLE (auth) | `bluesky` | `searchPosts` now auth/edge gated (401/403) - failure-isolated, not bypassed |
| PAID | `web` | Anthropic `web_search` - needs API credits; `discover-opportunities` only, budget-gated |

`acquisition-rescore` re-derives every stored lead's score with the
current deterministic model (no network, no spend) - run it after a
scorer change so old records stop showing a stale `prospect_quality`.

One dead source never kills the run: `sources_status` reports it and the others still return.

**Scoring** (`acquisition.py`, deterministic by default - no LLM, no cost):
`classify()` scans four signal groups - ASK ("how do I get customers",
"struggling to get customers"), FIRST_CUSTOMER ("0 customers", "first
sale"), SELF_SITUATION ("my SaaS", "just launched"), plus a question /
Ask-HN bonus - against NEGATIVE groups: STORY (retrospective case studies,
"how I got 10k customers", "from 0 to ...") and MISC (tutorials, news,
"someone should build"). A title match counts far more than a body match.
It produces `relevance_score` 0-100, `prospect_type`
(active_problem / seeking_advice / founder_building / success_story /
educational / irrelevant / unknown), `active_problem`, and `buying_intent`.

It also produces **`prospect_quality`** (`high` / `medium` / `low` /
`none`) - the one "worth a human's time?" verdict - and **`why`**, a list
of observable reasons each tied to real matched text or metadata (never
invented). `matched_queries` records every search that surfaced the same
thread (merged into one lead).

**Recency (7-tier "reply window")**: `age_bucket` = `extremely_fresh`
(<=3d) / `fresh` (<=7d) / `recent` (<=14d) / `aging` (<=30d) / `stale`
(<=60d) / `very_stale` (<=90d) / `archive` (>90d) / `unknown`. A missing
timestamp is `unknown`, never guessed - down-weighted, not deleted. Old
records are kept but a bumped `scorer_version` re-derives them on the next
run, and they can never outrank a scored current lead.

**`final_score` = relevance x recency x problem x intent x solved**
(weights in `acquisition.py`). Posts older than `--max-age-days` hit an
explicit recency cliff, so a 2-day-old genuine ask beats a 90-day-old
perfect case study.

**`--score llm`** adds one metered Claude call per lead
(`acquisition_llm.py`) that judges active-problem vs case-study (examples
A-F baked into the prompt) and returns `relevance_score`,
`is_active_problem`, `prospect_type`, `reason`, `recommended_fit`. It
reuses `budget_gate` + `CostMeter` + `record_llm_spend` + `LlmCache` (a
lead already scored is never re-charged) and the post text is fenced as
UNTRUSTED. The model is instructed never to claim the person will buy.

**Sources** (`--source` is repeatable; default = the four keyless ones):

| name | API | key? | freshness | notes |
|---|---|---|---|---|
| `stackexchange` | api.stackexchange.com | no | `fromdate` | `freelancing` + `webmasters`; every result is a real question; `meta` carries answer stats -> already-answered questions are down-ranked; call spacing + `backoff` + one `429` retry |
| `lobsters` | lobste.rs/newest.json | no | client-side on `created_at` | recent-feed + keyword filter (no keyless search API); stores the `comments_url` link; read-only |
| `lemmy` | lemmy.world /api/v3/search | no | client-side on `published` | federated founder Q&A; stores the canonical `ap_id`; read-only, never authenticates |
| `bluesky` | public.api.bsky.app (AppView) | no | `since` + `sort=latest` | auth/edge gated now (401/403); kept failure-isolated, no auth |
| `hn-algolia` | hn.algolia.com | no | `numericFilters` | HN stories/Ask HN |
| `web` | Anthropic `web_search` tool | `ANTHROPIC_API_KEY` | search `page_age` | **opt-in** (`--source web`), budget-gated + cached; reaches indexed Reddit/IH/forum threads without touching those sites; keeps ONLY URLs that appear in real search results |
| `reddit` | reddit.com/search.json | - | - | returns HTTP 403 unauthenticated; kept failure-isolated, no OAuth |
| `free` / `all` | - | - | - | `free` = hn-algolia+stackexchange+lobsters+lemmy; `all` = free + web |

Every source is failure-isolated: one dead API is reported in
`sources_status` and the others still return.

**Solved-signal**: `_SOLVED` phrases ("solved it", "update: solved",
"thanks everyone", `[solved]` prefixes) and Stack Exchange
`accepted`/`answer_count>=2` mark a post `solved` -> `final_score x 0.20`.
Old records are never deleted from the store but a missing/low
`final_score` sinks them below current opportunities.

**Store** (`data/acquisition.json`, dashboard-ready): keyed + de-duped by
canonical URL. A re-found lead is *merged* - the better score wins, the
`human_review_status` (`new` / `reviewed` / `rejected`) and original
`discovered_at` are preserved.

**It never posts, replies, DMs, emails, or contacts anyone.**
`review-opportunity --approve` only records a human verdict.
`promo_allowed` is a conservative hint - always read the community's own
rules first.

## Autopilot (`autopilot`) + pre-sale budget cap

`autopilot` is a thin orchestrator (no daemon) that runs **one funnel
cycle per invocation** and stops at every human gate. It reuses the
existing components - it adds no parallel system.

```
python -m revenue_os autopilot start   --data-dir data [--allow-web] [--max-age-days 14] [--limit 15]
python -m revenue_os autopilot cycle   --data-dir data           # one cycle, same as start
python -m revenue_os autopilot status  --data-dir data [--json]  # capital / leads / sales / cost, no secrets
python -m revenue_os autopilot pause   --data-dir data --reason "..."
python -m revenue_os autopilot resume  --data-dir data
python -m revenue_os autopilot stop    --data-dir data           # state preserved, `start` resumes
```

One cycle does, in order: **free discovery** (`discover-free` sources,
$0) -> **rank + qualify** -> **prepare outreach briefs** for high/medium
quality leads (draft only) -> **PayPal check** (books any captured
payment) -> **intake/plan funnel status**. Every step that needs a human
is emitted as a `HUMAN: ...` action; the system never posts, messages, or
spends past the cap. State lives in `data/autopilot.json` and survives a
restart (`start` resumes, no duplicated work).

**Pre-sale hard budget cap** (`src/revenue_os/budget.py`). Before the
first real sale the system may spend **at most EUR 3.00 total**
(`PRESALE_CAP_USD = 3.20`, metered in USD via the existing
`LlmSpendLog`). `budget.guard()` runs *before* every paid LLM call inside
`budget_gate`; if `recorded_spend + estimate > cap` it raises
`BudgetBlocked` and nothing is spent - no auto-override, no fallback to
another paid API. The reserved **EUR 17.00** growth capital stays locked
until `RevenueLedger.total() > 0`; the first booked sale flips
`presale_active` to `False` and releases it (it is still never spent
automatically). `autopilot status -> capital` shows the full picture.

## Revenue supervisor (`revenue-step` / `revenue-loop`)

`revenue-loop` is the OBSERVE -> DECIDE -> ACT loop that ties the existing
pieces together for the first sale. Each step it reads the real
candidate / pipeline / payment / intake / delivery state, runs **one**
action that is allowed **without a human**, persists, and repeats until
only human-gated actions remain - then it prints a concrete action
queue and stops.

```
python -m revenue_os revenue-loop  --data-dir data [--max-steps 25] [--no-discovery]
python -m revenue_os revenue-step  --data-dir data        # a single action
python -m revenue_os revenue-status --data-dir data       # last state + human queue
```

Actions it takes on its own (all pre-existing, all safe - no money, no
messages, no PayPal writes):

| action | what it does |
|---|---|
| `stage_delivery` | render an approved plan to a PDF on disk (no send) |
| `run_pipeline` | `select -> ... -> QC -> deploy` the checkout page |
| `sync_payments` | read-only PayPal booking (only if credentials are set) |
| `discover` | one free autopilot cycle (discovery + outreach drafts) |

Everything else - approve, launch, `build-checkout`, post outreach,
`intake-review`, `draft-launch-plan` (costs money), `plan-approve`,
`plan-deliver --send` - stays a human gate and is surfaced, never done.
State lives in `data/revenue_loop.json` and is restart-safe.

## Deploying the checkout page (`deploy-checkout <candidate>`)

`build-checkout` writes `deliverables/<candidate>/checkout.html` +
`intake.html` to disk. `deploy-checkout` publishes them to **GitHub
Pages** via the Contents API and stores the live URL on the candidate.

```
python -m revenue_os deploy-checkout <candidate> --data-dir data
python -m revenue_os deploy-status   <candidate> --data-dir data
```

One-time setup (in `.env`, gitignored):

```
GITHUB_TOKEN=<fine-grained PAT: Contents read+write on the repo>
GITHUB_PAGES_REPO=<owner>/<repo>          # e.g. divdav12/customer-launch-plan
GITHUB_PAGES_BRANCH=main                  # optional
GITHUB_PAGES_SUBDIR=                      # optional path prefix inside the repo
```

The repo must exist and have **Pages** enabled (Settings -> Pages ->
Deploy from branch). Deploys are idempotent (an unchanged file is not
re-committed) and the token never appears in output or logs. The
pipeline runs this as its final `deploy` step; a missing token just
records `deploy: skipped` and the pipeline still finishes at `prepared`.

## Delivering the plan as a PDF (`plan-deliver <order-id>`)

Once a plan is `approved`:

- `revenue_os plan-deliver <order-id>` renders the approved plan to a
  real PDF at `deliverables/<candidate>/plan-<order-id>.pdf` (a small
  dependency-free PDF writer - no pandoc, no conversion step) and
  records it in `data/deliveries.json` as `staged`. **Nothing is sent.**
- `revenue_os plan-deliver <order-id> --send` emails that PDF to the
  buyer (address from the intake) via SMTP. This is the human gate for
  the actual send; it refuses to send twice for the same order
  (`--force` overrides).
- `revenue_os plan-deliver <order-id> --status` shows the delivery record.

SMTP config (in `.env`): `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`,
optional `SMTP_PORT` (587), `SMTP_FROM` (defaults to `BUSINESS_EMAIL`),
`SMTP_STARTTLS` (true). For Gmail use `smtp.gmail.com` + an App Password.
The send re-checks that the order's `capture_id` is still a booked
`paypal:` payment for the candidate. No money moves.

## `.env` loading

`python -m revenue_os` now auto-loads `.env` from the working directory
(or `$REVENUE_OS_ENV_FILE`) on startup - values already set in the real
shell environment always win, and only key **names** are logged, never
values. `.env` stays gitignored.

## Outreach brief (`outreach-brief <lead-id>`)

```
python -m revenue_os outreach-brief <lead-id> --data-dir data [--checkout-url URL] [--json]
  [--draft template|llm] [--model ...] [--max-cost 0.10] [--refresh]
```

Turns one qualified lead into a **draft** answer plan: the lead's own
words (verbatim), why it is relevant, a help-first answer angle, generic
talking points, the community's self-promotion policy, and an *optional*
last-line CTA with a tracked checkout link (`?lead=<id>`). It makes no
claim about the lead's business beyond their own text, and it **never
posts** - a human rewrites and publishes. Drafts persist in
`data/outreach.json` (`draft` -> `approved` -> `posted` / `skipped`).

**`--draft llm`** (opt-in, paid, budget-gated) adds one metered Claude
call (`outreach_llm.py`) that writes a *tailored* reply referencing the
prospect's actual post, attached as `draft_reply`. Same machinery as
`--score llm`: `budget_gate` (cumulative cap + EUR 3 pre-sale limit),
per-run `--max-cost`, `CostMeter`, and `LlmCache` (a lead already drafted
is never re-charged). The prompt forbids any purchase claim / guarantee /
fabricated result or anecdote, the post is fenced as untrusted, and a
regex flags promise language for the human. Still a draft - a person
edits and posts it.

**Lead -> sale tracking.** The tracked link carries `?lead=<id>`; the
generated `checkout.html` / `intake.html` copy it into a hidden
`lead_id` field, and `intake-import` stores it on the intake entry. This
is additive - the PayPal `custom_id` (candidate name) and the payment
capture-id gate are unchanged.

## Dashboard (`dashboard` / `dashboard-serve`)

A single self-contained HTML command center. No JavaScript, no external
requests, no images: every value is traceable to a file on disk, and an
agent with no record on disk reads IDLE / no activity rather than being
animated into looking busy.

Top to bottom:

- **Mission control** -- the primary objective (FIRST REAL CUSTOMER), the
  secondary loop, real revenue booked, the launched offer, LLM spend, and
  the candidate lifecycle rail (`discovered -> shortlisted -> approved ->
  investigating -> validated -> launched -> earning`, plus `rejected`) with
  the real counts from the candidate store. The stages whose transition is
  a human decision are marked HUMAN.
- **First sale readiness** -- unchanged; a per-item checklist computed only
  from disk, explicit about the live PayPal path it cannot check.
- **Agents** -- the orchestration map (a link is drawn only where a real
  parent->child task lineage exists) beside the coordination readout, then
  the **24-agent fleet grid** in six clusters. Each cell shows the agent's
  name, cluster, real status (RUNNING / WAITING / IDLE / DISABLED /
  HUMAN-GATED / BLOCKED / PLANNED), its recorded run count from
  `task_log.json`, its latest real activity, its current task if one is in
  flight, and a progress bar **only** if the agent itself persisted a
  progress value.
- **Customer acquisition** -- the Phase 2.3 chain Prospect Scout ->
  Opportunity Scorer -> Outreach Drafter with the real store counts. The
  drafter is human-gated and the gate is stated on the page: the system
  drafts and stops; a person posts every reply.
- Pipeline, agent outputs, task queue, activity, opportunities, trends,
  LLM spend, outcomes, last discovery, revenue / ROI, revenue analysis and
  candidates -- all unchanged.

Revenue means payments booked in the revenue ledger. Until one is booked it
reads `0` -- never an estimate, a projection, or a placeholder.

### Blockers (`blockers`)

Some real blockers leave no trace on disk -- a payment-account restriction,
missing API credits, a provider outage. `data/blockers.json` is a
human-maintained register for exactly those, rendered in Mission control.
Nothing detects a blocker automatically, so an **empty register is reported
as empty, not as all-clear**.

```
revenue_os blockers                       # open blockers (--all includes resolved)
revenue_os blockers add paypal-payee-restricted \
    --title "PayPal checkout blocked: PAYEE_ACCOUNT_RESTRICTED" \
    --detail "The live payment path returns PAYEE_ACCOUNT_RESTRICTED." \
    --area payment --severity critical
revenue_os blockers resolve paypal-payee-restricted
```

`--severity` is `critical | warning | info`. Re-adding a known id updates it
in place and reopens it.

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
holds no form secrets. Make sure the form provider delivers submissions to
your **business email** -- that is a provider-side account setting, not a
repo setting.

Pass `--business-email <addr>` (or set `BUSINESS_EMAIL` in `.env`) to print
a real contact address on both pages -- the page footer, the post-payment
message, and the "if the form does not send" fallback. Without it the pages
use the generic "the address that sold you this plan" wording. This is
unrelated to PayPal.

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
