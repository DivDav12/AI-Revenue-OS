# Safety — Gates, Credentials, Budget, Rate Limits

## The action-classification firewall

Every outward action in this codebase is classified by `action_class.py`
into exactly one of five classes before it can run:

- `SAFE_AUTONOMOUS` — the fleet may do it on its own
- `MONEY_APPROVAL_REQUIRED` — moves or commits money; human approval only
- `IDENTITY_APPROVAL_REQUIRED` — needs the owner's personal/legal identity
- `LEGAL_APPROVAL_REQUIRED` — a binding legal act in the owner's name
- `SAFETY_BLOCKED` — never allowed

An **unrecognized action kind fails closed to `SAFETY_BLOCKED`** — there
is no default-allow path. `autonomous_context()` is a thread-local guard
that hard-refuses money/PayPal/e-mail/paid-LLM calls whenever the
autonomous loop is active, checked at the actual call sites
(`budget.py`/`paypal.py`/`delivery.py`/`llm_normalize.py`), not just at
the classifier.

`posting_permitted(platform)` fails closed for every platform that is
not an explicitly owned channel (GitHub Pages, this repo). Pinterest,
Gumroad, Payhip, Awin, Amazon, and CJ are all third parties — the fleet
never posts, logs in, or uploads to any of them.

## GitHub credential handling (mandatory correction #1)

- `deployment.GitHubPagesDeploymentAdapter.authorized` is a pure,
  no-network check: it resolves `GITHUB_TOKEN` + `GITHUB_PAGES_REPO`
  from the environment and returns `True`/`False` — never prints,
  logs, or exposes the token value.
- `deploy.py`'s `_redact()` scrubs the token out of every possible error
  message before it can appear in an exception, a log line, or CLI
  output — verified by this codebase's own existing tests
  (`tests/test_deploy.py`).
- `affiliate_cycle.run_cycle()` checks `adapter.authorized` **before**
  attempting a deploy and reports a precise `HUMAN SETUP REQUIRED` line
  when it's False — deployment is never silently skipped or fabricated
  as successful, and is never described as "credential-free."
- Nothing in this codebase ever requests, generates, prints, or
  autonomously rotates a GitHub token. Setting it is a one-time human
  action (`SETUP.md`).

## Hard $3.00 LLM/API budget (mandatory correction: exactly $3.00)

`budget_guard.py` — a single constant, `CAP_USD = 3.00`, replacing the
prior mission's dual-cap concept (a pre-sale cap plus a separately
locked "growth capital" — that distinction belonged to the retired
service business and doesn't apply here).

- `guard(data_dir, estimated_cost_usd=...)` **fails closed on two
  conditions**: the cap would be exceeded, OR the estimate itself is
  missing, non-numeric, NaN, or negative. A caller with no verifiable
  cost estimate is refused, never allowed to guess "probably free."
- No code path raises `CAP_USD` at runtime. No auto-reload. No override
  function exists (`tests/test_budget_guard.py` asserts this directly:
  no public name in the module contains "override"/"reload"/"raise_cap").
  Changing the cap means a human edits the constant and redeploys — a
  visible, reviewed change.
- The entire affiliate/content pipeline built in this pass is fully
  deterministic and **never calls an LLM at all** — discovery, scoring,
  content rendering, pin drafting, and digital-product generation are
  all template/rule-based. The budget guard exists and is tested for
  when an LLM step is deliberately added later; it is not currently
  spending anything.

## Pinterest rate limiting (mandatory correction #4)

`ecosystem/pinterest_pins.MAX_PINS_DRAFTED_PER_DAY = 10` — our own,
internal, conservative pacing policy, informed by third-party 2026
reports of an observed safe range of roughly 5–15 pins/day for automated
posting on a similarly-sized account. **This is explicitly not, and must
never be described as, an official Pinterest-published limit** —
Pinterest publishes no such number, and its actual enforcement behavior
could change at any time without notice. `PinRateLimitExceeded` is
raised (never silently dropped) when the daily cap is reached; re-
fetching an already-drafted pin is never rate-limited (idempotency is
unaffected). A test (`test_rate_limit_is_a_documented_internal_policy_
not_pinterests`) asserts the module's own docstring makes this
disclaimer explicit.

Posting itself is never automated regardless of this limiter — no
Pinterest API integration or browser automation exists in this repo
(see below); a human posts every pin from their own account.

## Browser/computer-use honesty (mandatory correction #7)

This repository has **no Playwright/Selenium/browser-automation
dependency**. No agent claims to log into Pinterest, Gumroad, Payhip,
Awin, Amazon, or CJ via a browser. Every step requiring a platform login
is a human action, full stop. A future extension wanting browser
automation would be a new, separately implemented, tested, and reviewed
capability — never assumed to exist.

## Digital-product independence (mandatory correction #3)

`ecosystem/digital_products.py` is structurally self-contained: it does
not import from `affiliate_pipeline.py` or `affiliate_assets.py`, and
neither of those modules imports from it. `affiliate_cycle.run_cycle()`
calls `generate_product_draft()` in its own `try/except` block, whose
failure is recorded in `report["digital_product_error"]` without ever
touching `report["chain"]`. Verified directly:
`tests/test_affiliate_cycle.py::test_full_pipeline_reaches_a_live_
asset_with_a_pin_draft` asserts a successful chain result and a
successful digital-product result exist independently in the same
report.

## Multi-network affiliate safety (mandatory correction #3, "never assume
approval")

`AffiliateOffer.usable` (`active AND status == POLICY_OK`) is the only
gate the Opportunity Agent ever checks. Every network in
`NETWORK_POLICY` defaults to `POLICY_HUMAN_SETUP_REQUIRED` — Amazon
Associates permanently so (no real connector exists; its own API
requires 10 prior sales, a documented chicken-and-egg limitation), Awin
and CJ only flip to usable once a human's real, authorized credential
resolves from the environment (`AwinOfferSource`/`CjOfferSource`
`.authorized` checks, no network call). Zero usable offers produces an
explicit `REJECTED — HUMAN SETUP REQUIRED` result, never a fabricated
match.

## What every test suite run in this pass checked

- No credential value appears in any test fixture, log capture, or
  assertion string (`tests/test_deploy.py`, existing).
- No test ever sets a real `GITHUB_TOKEN`/`ANTHROPIC_API_KEY` to a real
  value; fake/local adapters stand in throughout
  (`deployment.FakeDeploymentAdapter`).
- Budget-exhaustion and unverifiable-cost-estimate paths are directly
  tested (`tests/test_budget_guard.py`).
- No test or code path can trigger a real payment, subscription, or
  purchase — none of the modules built in this pass touch `paypal.py`'s
  write paths or any payment API at all.
