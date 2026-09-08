"""Social affiliate distribution (Reddit + Pinterest).

Goal: turn a REAL organic demand signal on a social platform into a
genuinely helpful piece of content that reaches an interested person, so
they can get to the product through our real, human-registered Amazon
affiliate link (tag=airevenue-21).

Hard rules (enforced, not aspirational):
  * No spam, no mass posting, no copy-paste, no fake experience, no fake
    account, no karma manipulation, no rule circumvention, no CAPTCHA /
    2FA / login bypass.
  * A platform / community is auto-posted to ONLY when its own published
    rules allow automated affiliate content AND the operator has
    explicitly attested that (`social/compliance.py`). Otherwise the flow
    returns HUMAN_REQUIRED with a finished, ready-to-review draft.
  * Every recommendation is preceded by the "does this person actually
    have a problem / buying intent this product fits?" gate
    (`social/intent.py`). No -> reject, no content is produced.
  * Affiliate disclosure is included wherever the platform or the law
    requires it, in the same text the recommendation lives in.
  * Only offers actually ingested and marked usable=True are ever used.
  * No fabricated clicks, impressions, conversions or revenue - revenue
    is booked only from real Amazon PartnerNet data via the existing
    `affiliate-record-commission` path.

This subpackage is self-contained and additive: it imports from the
existing ecosystem / affiliate modules but modifies none of them, and it
does not touch the work-in-progress `ecosystem/browser_*.py` files.
"""
