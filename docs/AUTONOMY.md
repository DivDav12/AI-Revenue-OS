# Autonomy — What "Autonomous" Actually Means Here

"Autonomous" never means "an agent exists for it." It means: this
specific action is classified `SAFE_AUTONOMOUS` by `action_class.py`,
and no human step sits between decision and outcome. Everything else is
one of: a one-time human setup, a recurring human action, or something
never automated by design regardless of future capability.

## Process table (Phase 14 format)

| Process | Automatic | Human needed | Why |
|---|---|---|---|
| Public-signal discovery (HN/RemoteOK/curated file) | ✅ fully | — | keyless public APIs, read-only |
| Verification (evidence/provenance/policy check) | ✅ fully | — | deterministic gate, no network |
| Content-opportunity scoring against a usable offer | ✅ fully | — | deterministic, no LLM |
| Selecting/rejecting an opportunity | ✅ fully | — | deterministic |
| Page content generation | ✅ fully | — | template-only, $0 |
| Quality gate (word count, disclosure, CTA, evidence) | ✅ fully | — | deterministic function, not an agent |
| Affiliate link creation (our own tracked redirect) | ✅ fully | — | no money moves |
| GitHub Pages deployment | ✅ **only if** GITHUB_TOKEN + GITHUB_PAGES_REPO already configured | one-time: set those two secrets | owned channel, but the fleet never requests/prints/rotates the credential |
| Joining an affiliate network (Amazon/Awin/CJ) | ❌ never | every network, once | accepting a binding publisher agreement is a legal act |
| Awin's refundable publisher deposit | ❌ never | if/when required | real money, even if refundable |
| Pinterest pin drafting | ✅ fully | — | template-only, rate-limited, $0 |
| Pinterest pin **posting** | ❌ never (no capability exists) | every pin | no Pinterest API integration or browser automation in this repo |
| Creating a Pinterest account | ❌ never | once | login/identity |
| Digital-product file generation | ✅ fully | — | template-only, $0, parallel path |
| Creating a Gumroad/Payhip account | ❌ never | once, only if using this path | login/identity/payout onboarding |
| Uploading a digital product listing | ❌ never (no capability exists) | every product | no upload API integration in this repo |
| Click counting (self-hosted redirect) | ✅ fully | — | our own domain, no PII |
| Reading a commission figure off a network dashboard | ❌ never | periodically, if desired | no live conversion feed for any supported network exists (Amazon's Creators API needs 10 prior sales - documented chicken-and-egg) |
| Recording a human-reported commission number | ✅ fully (once told) | the human tells it the number | recording a fact ≠ discovering one |
| Priority-weight recompute | ✅ fully | — | plain ratios over settled outcomes |
| Zero-traffic asset flagging | ✅ fully (the flag) | reviewing/acting on the flag | recommendation only, never auto-deletes |
| LLM/API budget enforcement | ✅ fully | raising the $3.00 cap (never happens automatically) | hard-coded constant, no runtime override path exists |
| Tax/bank details at real payout | ❌ never | once per network/platform, at first real payout | KYC/financial onboarding is the network's own process |

## Never automated, by design, regardless of future capability

Accepting any platform's legal terms; entering payment or bank details;
KYC/ID verification; 2FA; CAPTCHA-gated actions; anything that would
spend real money; anything that would create a financial obligation.

## Do not overstate this

- "GitHub Pages deployment is autonomous" is true **only when** a human
  already configured the credential — the system checks this before
  attempting to deploy and reports a precise `HUMAN SETUP REQUIRED`
  otherwise. It never claims deployment is "credential-free."
- "Pinterest drafting is rate-limited" describes **our own** conservative
  policy (see `SAFETY.md`), never an official Pinterest limit.
- "Measurement" only ever reports what it can actually observe (clicks)
  or what a human actually told it (commissions) — it never estimates a
  conversion that didn't happen.
