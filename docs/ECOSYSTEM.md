# Autonomous AI Revenue Ecosystem

`src/revenue_os/ecosystem/` is the "brain" that turns **real, discovered
opportunities** into an **executable task chain**, reusing the existing
execution stack (opportunity_store, opportunity_state, execution queue,
worker, acceptance, PayPal payment, SMTP delivery, revenue ledger)
unchanged.

```
real sources ─▶ DiscoveryEngine ─▶ Opportunity(origin="real")
                     │
                     ▼
              verification.verify()   ─▶ DISCOVERED..QUALIFIED / REJECTED / HUMAN_REQUIRED / BLOCKED
                     │
                     ▼
            profitability.evaluate()  ─▶ expected profit / profit-per-hour / risk   (every number ESTIMATE)
                     │
                     ▼
              strategy.select()       ─▶ TASK | PRODUCT | AFFILIATE | ECOMMERCE | SERVICE | OTHER
                     │                    (SERVICE is never the default - spec §29)
                     ▼
              pipeline.plan()         ─▶ PRODUCT -> the existing acceptance chain
                                         other   -> a prepared plan, HUMAN_REQUIRED for the external step
```

Nothing here spends money, posts, contacts anyone, logs in, or creates an
account. Every real action still flows through
`action_class` / `autonomous_context()` / `approvals`.

## Data model

New namespaces on each `opportunity_store` record (all optional,
back-filled on load):

| field | meaning |
|---|---|
| `origin` | `"synthetic"` (test data) or `"real"` (discovered from a source). Ratchets synthetic→real, never back. |
| `discovery` | `{source, source_url, source_id, access_method, policy_status, evidence[], opportunity_type, verification:{status,reasons,checks}, demand_hint, ...}` |
| `evaluation` | the deterministic profitability projection; carries `is_estimate: true` |
| `strategy` | `{recommended, options:[{strategy,score,...}], reason, plan:{...}}` |

Store writers: `record_discovery` (merge), `record_evaluation`,
`record_strategy` (replace).

## Sources (`ecosystem/sources.py`)

Every source declares `SourceMeta` (spec §6): `source`, `source_type`,
`source_url`, `access_method`, `automation_allowed`, `requires_login`,
`requires_human`, `policy_status`.

| name | real? | access | notes |
|---|---|---|---|
| `synthetic` | no | generated | reuses `opportunity_engine` archetypes; deterministic; stays `origin="synthetic"` |
| `hn` / `hackernews` | **yes** | official keyless API | HN demand threads (Ask HN / hiring / "I will pay …"); read-only; the fleet never posts to HN |
| `remoteok` | **yes** | official public JSON API | remote job / gig listings → real freelance demand; read-only, descriptive UA |
| `file` | yes | curated local JSON | a human-vouched signal list; fully offline |
| `upwork`, `fiverr`, `amazon_associates`, `shopify` | — | — | **`HUMAN_SETUP_REQUIRED`**: yield nothing until a human wires an account / API key. The fleet never self-provisions. |

Real sources take an injectable `fetch_json` callable (tests replace it -
no network in the suite), same pattern as `paypal.py` / `deploy.py`.

## Verification (`ecosystem/verification.py`)

Pure gate. Fail closed. Checks provenance, policy status, evidence,
title, type + fleet capability, pay realism. Only a `QUALIFIED`
opportunity can be planned into a real chain.

- `POLICY_BLOCKED` → `BLOCKED`
- `HUMAN_SETUP_REQUIRED`, or a login-gated external task → `HUMAN_REQUIRED`
- no evidence / unknown type / pay below floor / unknown policy → `REJECTED`

## Profitability (`ecosystem/profitability.py`)

Deterministic projection. **Every output carries `is_estimate: true`.**
Headline comparator: `decision_value = profit_per_hour × success_prob ×
(1 − 0.5·risk) × (0.5 + 0.5·automation)`. A small fast likely task can
out-score a big slow uncertain service (spec §9 example).

## Strategy (`ecosystem/strategy.py`)

Scores every viable strategy on: capital-light, speed, automatable,
low-platform-risk, repeatable, scalable (spec §28 weighting) plus the
economic term and a demand lift. **SERVICE carries a 0.80 handicap**
(spec §29) - it can still win, but only clearly. A non-positive projected
profit → no recommendation.

## Distribution channel classes (`strategy` reads them; execution unchanged)

`OWNED / SEARCH / MARKETPLACE / AFFILIATE / ADS / COMMUNITY / DIRECT /
PARTNER / OTHER` - real distribution still runs through the existing
`distribution.py` (Null adapter by default; owned-web only).

## Autonomy levels (`ecosystem/autonomy.py`)

Presentation over `action_class` (no new policy). Maps an activity to a
class (`READ_ONLY / RESEARCH / BUILD / DRAFT / PUBLISH / CONTACT / SELL /
DELIVER / BUY / PAY / ADVERTISE`) and a verdict (`AUTONOMOUS_ALLOWED /
HUMAN_APPROVAL_REQUIRED / HUMAN_REQUIRED / BLOCKED`). Unknown → BLOCKED.

## Learning (`ecosystem/learning.py`)

`OutcomeStore` (`data/ecosystem_outcomes.json`, append-only). After an
opportunity settles: `{strategy, source, category, type, channel, time,
cost, revenue, success, failure_reason}`. `aggregate()` rolls it up;
`priority_weights()` = win-rate ÷ overall-win-rate, clamped [0.5, 1.6],
only once ≥ 5 outcomes have settled. Plain ratios - not ML.

## Simulation (`ecosystem/simulation.py`)

`simulate(n, seed)` runs the whole loop over N synthetic opportunities
with **zero external side effects** (no money, network, messages, ads,
orders, accounts, or writes to real stores). Deterministic:
`(n, seed)` → byte-identical report. Reports discovery / verification /
strategy mix / executed / successes / failures / simulated revenue +
profit + per-category analytics.

## ExecutionTask integration (spec §24)

New task types `DISCOVER`, `VERIFY`, `EVALUATE`, `SELECT_STRATEGY` (all
`SAFE_AUTONOMOUS` via `task_class`). Adapters in
`ecosystem/task_adapters.py`, registered into `default_registry()`. The
worker runs them inside `autonomous_context()` like any other task.

## CLI

```
revenue_os discover [--source synthetic|hn|remoteok|file|<setup-name>[,...]] [--limit N] [--source-path P]
revenue_os evaluate <OPP_ID>
revenue_os select-strategy <OPP_ID>
revenue_os plan-strategy <OPP_ID>          # PRODUCT -> acceptance chain; other -> prepared/HUMAN_REQUIRED
revenue_os simulate [--n 1000] [--seed 42]
revenue_os ecosystem-status
```

## Safety invariants (unchanged)

- `action_class` firewall: only additive kinds - existing gates untouched.
- `NullPaymentAdapter` / `NullDeliveryAdapter` / `NullDistributionAdapter` /
  `NullMeasurementAdapter` stay the defaults.
- DEPLOY still born `BLOCKED_APPROVAL`; SMTP still refuses in
  `autonomous_context()`.
- PayPal read-only; one revenue ledger; EUR-3 pre-sale budget cap.
- No source logs in, solves a CAPTCHA, posts, or creates an account.
- Real ads / supplier orders / affiliate-network fees are
  `MONEY_APPROVAL_REQUIRED`.

## Not yet built (next phases)

- Fully autonomous chains for TASK / AFFILIATE / ECOMMERCE strategies
  (spec §11, §13, §14) - today they produce a prepared, human-gated plan.
- Ad Strategy experiment loop with a real test budget (spec §17) - the
  autonomy layer already classes it `HUMAN_APPROVAL_REQUIRED`.
- Dashboard / JARVIS panels for the ecosystem read model
  (`ecosystem/intel.py` is the data layer, wired to `ecosystem-status`).
