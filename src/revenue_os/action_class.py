"""The CENTRAL action classifier and runtime firewall.

Every side-effecting or outward-facing action in the system is described
by a stable *kind* string. `classify()` maps a kind to exactly one class:

  SAFE_AUTONOMOUS          - the fleet may do it on its own
  MONEY_APPROVAL_REQUIRED  - moves / commits money; a human must approve
  IDENTITY_APPROVAL_REQUIRED - needs the owner's personal / legal identity
  LEGAL_APPROVAL_REQUIRED  - a binding legal act in the owner's name
  SAFETY_BLOCKED           - never allowed (fraud, spam, TOS bypass, ...)

Individual agents cannot bypass this: the autonomous loop routes every
outward step through `check()`, and the four real "leak" paths (paying,
PayPal, e-mail, paid LLM) additionally refuse to run inside an
`autonomous_context()` - see `guard_no_money_in_autonomy()` and the
call-site hooks in budget.py / paypal.py / delivery.py / llm_normalize.py.

Pure logic + one thread-local. No I/O, no network, deterministic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum


class ActionClass(str, Enum):
    SAFE_AUTONOMOUS = "SAFE_AUTONOMOUS"
    MONEY_APPROVAL_REQUIRED = "MONEY_APPROVAL_REQUIRED"
    IDENTITY_APPROVAL_REQUIRED = "IDENTITY_APPROVAL_REQUIRED"
    LEGAL_APPROVAL_REQUIRED = "LEGAL_APPROVAL_REQUIRED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"


class ActionBlocked(RuntimeError):
    """Raised when a non-autonomous action is attempted without approval."""


@dataclass(frozen=True)
class Verdict:
    kind: str
    action_class: ActionClass
    reason: str

    @property
    def autonomous(self) -> bool:
        return self.action_class is ActionClass.SAFE_AUTONOMOUS

    @property
    def approval_kind(self) -> str | None:
        return {
            ActionClass.MONEY_APPROVAL_REQUIRED: "money",
            ActionClass.IDENTITY_APPROVAL_REQUIRED: "identity",
            ActionClass.LEGAL_APPROVAL_REQUIRED: "legal",
        }.get(self.action_class)


# ---------------------------------------------------------------------------
# the rule table - every kind maps to exactly one class
# ---------------------------------------------------------------------------

_SAFE = frozenset({
    # research / analysis
    "research", "browse_web", "discover_opportunity", "market_analysis",
    "competitor_analysis", "product_research", "customer_research",
    "lead_discovery", "research_distribution", "analytics", "monitor", "report",
    # ecosystem: real opportunity discovery -> evaluation -> strategy (all
    # read-only projection; nothing here spends, posts, or contacts anyone)
    "verify_opportunity", "evaluate_profitability", "select_strategy",
    "simulate_revenue", "demand_discovery", "affiliate_offer_research",
    "commission_analysis", "supplier_research", "margin_analysis",
    "traffic_strategy", "prepare_task_solution", "prepare_store_listing",
    "ad_strategy_analysis", "learn_from_outcomes",
    # affiliate revenue pipeline - matching/asset/link creation is
    # non-financial, non-identity administrative + content work (spec:
    # Affiliate Revenue Pipeline section 15); joining a program itself
    # stays a CONTACT/HUMAN_REQUIRED activity - see ecosystem/autonomy.py
    "match_affiliate_offer", "create_affiliate_link", "record_click",
    # build (assets, code, design, copy) - nothing is shipped that binds money
    "write_code", "build_website", "build_landing_page", "build_product_page",
    "create_digital_product", "create_design", "write_copy", "seo_work",
    "prepare_listing", "prepare_marketplace_asset", "agent_spec_draft",
    "create_documentation", "create_social_draft", "draft_outreach_message",
    # publish - non-financial, non-identity
    "publish_website", "publish_public_content", "publish_github_repo",
    "publish_docs", "deploy_nonfinancial_infra", "publish_seo_page",
    # experiment / strategy - no spend
    "experiment_no_spend", "ab_test_no_spend", "optimize_nonfinancial",
    "change_strategy", "create_business_experiment", "abandon_experiment",
    "select_experiment", "score_opportunity", "run_deterministic_agent",
})

# kinds whose NAME unambiguously means money is spent, moved, or committed -
# always MONEY, regardless of any nominal amount.
_MONEY_ALWAYS = frozenset({
    "spend_money", "authorize_spend", "allocate_real_budget", "buy_api_credits",
    "buy_ads", "launch_paid_ad_campaign", "purchase_domain", "purchase_software",
    "subscribe_service", "start_subscription", "renew_subscription",
    "financial_transfer", "withdraw_money", "deposit_money", "increase_budget",
    "enable_paid_api", "real_llm_call", "issue_refund_with_money_movement",
    "record_real_payment", "book_revenue", "commit_future_spend",
    "incur_processor_fees", "activate_revenue_share", "pay_listing_fee",
    "pay_platform_commission", "activate_paid_checkout", "publish_paid_checkout",
    # ecosystem strategy execution that would move real money
    "place_supplier_order", "fund_ad_test", "pay_affiliate_network_fee",
    "order_inventory", "fund_dropship_order",
})

# kinds that are usually FREE but can carry a real financial effect - MONEY
# only when `has_financial_effect(context)` is true (a real cost, fees, a
# subscription, a card requirement, or a future / third-party obligation).
# A nominal amount of 0 with none of those -> SAFE_AUTONOMOUS.
_MONEY_IF_EFFECT = frozenset({
    "use_external_service", "sign_up_service", "create_service_account",
    "register_on_platform", "use_external_api", "publish_to_marketplace",
    "list_product_for_sale", "deploy_infra", "provision_hosting",
    "change_price", "enter_binding_financial_agreement",
    "optimize_paid_campaign", "connect_payment_provider",
})

_MONEY = _MONEY_ALWAYS | _MONEY_IF_EFFECT
MONEY = ActionClass.MONEY_APPROVAL_REQUIRED   # shorthand used below

_IDENTITY = frozenset({
    "kyc", "identity_verification", "age_verification",
    "account_ownership_verification", "bank_verification",
    "paypal_identity_action", "create_personal_account",
    "personal_identity_action", "recover_account_identity",
})

_LEGAL = frozenset({
    "sign_contract", "tax_submission", "legal_signature",
    "binding_legal_agreement", "publish_identity_dependent_content",
    "accept_terms_binding",
})

_SAFETY_BLOCKED = frozenset({
    "solve_captcha", "bypass_bot_detection", "bypass_authentication",
    "spam", "mass_unsolicited_message", "repeat_identical_post",
    "fabricate_customer", "fabricate_revenue", "fabricate_payment",
    "fabricate_review", "fabricate_testimonial", "fabricate_credential",
    "fabricate_identity", "fabricate_business_result", "deceive_user",
    "scrape_behind_auth", "automated_post_tos_forbidden",
    "exceed_rate_limit",
})

_ALL = {
    ActionClass.SAFE_AUTONOMOUS: _SAFE,
    ActionClass.IDENTITY_APPROVAL_REQUIRED: _IDENTITY,
    ActionClass.LEGAL_APPROVAL_REQUIRED: _LEGAL,
    ActionClass.SAFETY_BLOCKED: _SAFETY_BLOCKED,
}


# ---------------------------------------------------------------------------
# financial-effect detection - the core of "0 EUR runs autonomously"
# ---------------------------------------------------------------------------

_FREE_HINTS = frozenset({"free", "free_tier", "free_plan", "no_cost", "$0",
                         "0", "0.0", "eur 0", "no_card"})


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def has_financial_effect(context: dict | None) -> tuple[bool, str]:
    """True + reason when an action ACTUALLY costs money, moves money, incurs
    fees, starts a subscription, needs a payment method, or creates a
    future / third-party financial obligation.

    A nominal amount of 0 (or 'free') with none of the above -> (False, '').
    Also catches hidden / indirect costs: trials that convert to paid,
    usage / metered billing, listing fees, revenue-share commissions.
    """
    c = context or {}

    for k in ("amount", "cost_eur", "cost_usd", "price", "monthly_cost",
              "setup_fee", "min_spend"):
        if _num(c.get(k)) > 0:
            return True, f"{k}={c.get(k)} (real cost)"

    if c.get("recurring") or c.get("subscription") or c.get("ongoing_cost") \
            or c.get("monthly") or c.get("annual"):
        return True, "recurring / subscription cost"
    if c.get("fees") or c.get("processor_fees") or c.get("transaction_fees") \
            or c.get("commission") or c.get("revenue_share") or c.get("listing_fee"):
        return True, "third-party fees / commission apply"
    if c.get("creates_payment_obligation") or c.get("creates_obligation") \
            or c.get("activates_financial_obligation") \
            or c.get("third_party_payment_obligation"):
        return True, "activates a financial obligation to third parties"
    if c.get("future_commitment") or c.get("committed_spend") \
            or c.get("contractual_spend"):
        return True, "future financial commitment"
    if c.get("requires_payment_method") or c.get("requires_card") \
            or c.get("card_on_file"):
        return True, "requires a payment method on file (chargeable)"
    if c.get("free_trial_converts_to_paid") or c.get("trial_to_paid") \
            or c.get("auto_upgrades_to_paid"):
        return True, "free trial that auto-converts to a paid plan"
    if c.get("metered") or c.get("usage_billing") or c.get("pay_as_you_go"):
        return True, "usage / metered billing"

    return False, ""


# context-sensitive kinds: kind -> predicate(context) -> ActionClass
_CONTEXT_RULES = {
    # deploying a page is safe UNLESS it takes payment / has a real cost
    "deploy_page": lambda c: (ActionClass.MONEY_APPROVAL_REQUIRED
                              if (c or {}).get("has_checkout")
                              or (c or {}).get("collects_payment")
                              or has_financial_effect(c)[0]
                              else ActionClass.SAFE_AUTONOMOUS),
    # posting to a public platform: safe only where automation is permitted
    "post_public_reply": lambda c: (ActionClass.SAFE_AUTONOMOUS
                                    if posting_permitted((c or {}).get("platform", ""))
                                    else ActionClass.SAFETY_BLOCKED),
    "post_public_content": lambda c: (ActionClass.SAFE_AUTONOMOUS
                                      if posting_permitted((c or {}).get("platform", ""))
                                      else ActionClass.SAFETY_BLOCKED),
}

# platforms that forbid unattended/automated posting in their rules.
_NO_AUTO_POST = frozenset({
    "hacker news", "hackernews", "hn", "reddit", "lobsters", "lemmy",
    "indie hackers", "indiehackers", "twitter", "x", "linkedin",
    "facebook", "instagram", "discord", "slack",
})
# channels the owner controls where automated publishing is fine.
_OWNED_CHANNELS = frozenset({
    "own_site", "own_blog", "github", "github_pages", "static_site",
    "gist", "own_docs",
})


def posting_permitted(platform: str) -> bool:
    """True only where unattended automated posting is allowed - i.e. a
    channel the owner controls. Third-party communities are never
    auto-posted (respect their rules); the fleet drafts, a human posts."""
    p = (platform or "").strip().lower()
    if not p:
        return False
    if p in _OWNED_CHANNELS:
        return True
    return False


_UNKNOWN_DEFAULT = ActionClass.SAFETY_BLOCKED   # fail closed


def classify(kind: str, context: dict | None = None) -> Verdict:
    k = (kind or "").strip()

    if k in _CONTEXT_RULES:
        cls = _CONTEXT_RULES[k](context)
        return Verdict(k, cls, _reason(k, cls, context))

    if k in _MONEY_ALWAYS:
        return Verdict(k, ActionClass.MONEY_APPROVAL_REQUIRED, _reason(k, MONEY, context))

    if k in _MONEY_IF_EFFECT:
        eff, why = has_financial_effect(context)
        if eff:
            return Verdict(k, MONEY,
                           f"'{k}' has a real financial effect: {why} - "
                           "human money approval required")
        return Verdict(k, ActionClass.SAFE_AUTONOMOUS,
                       f"'{k}' costs EUR 0 and creates no fee / subscription / "
                       "obligation - the fleet may do it")

    for cls, names in _ALL.items():
        if k in names:
            return Verdict(k, cls, _reason(k, cls, context))

    return Verdict(k or "<empty>", _UNKNOWN_DEFAULT,
                   f"unknown action kind {k!r} - failing closed (safety)")


def _reason(kind: str, cls: ActionClass, context: dict | None) -> str:
    if cls is ActionClass.SAFE_AUTONOMOUS:
        return f"'{kind}' is non-financial, non-identity work - the fleet may do it"
    if cls is ActionClass.MONEY_APPROVAL_REQUIRED:
        return f"'{kind}' moves or commits money - human money approval required"
    if cls is ActionClass.IDENTITY_APPROVAL_REQUIRED:
        return f"'{kind}' needs the owner's personal identity - human identity approval required"
    if cls is ActionClass.LEGAL_APPROVAL_REQUIRED:
        return f"'{kind}' is a binding legal act - human legal approval required"
    if kind == "post_public_reply" or kind == "post_public_content":
        plat = (context or {}).get("platform", "that platform")
        return (f"automated posting to {plat} is not permitted by its rules - "
                "the fleet drafts, a human posts, or the fleet uses an owned channel")
    return f"'{kind}' is never allowed (fraud / spam / TOS bypass / fabrication)"


def check(kind: str, context: dict | None = None):
    """Alias returning the Verdict - the loop's primary entry point."""
    return classify(kind, context)


def is_autonomous(kind: str, context: dict | None = None) -> bool:
    return classify(kind, context).autonomous


# ---------------------------------------------------------------------------
# runtime firewall - a thread-local "we are running autonomously" flag that
# the four real money/identity leak paths check and refuse.
# ---------------------------------------------------------------------------

_local = threading.local()


class autonomous_context:
    """`with autonomous_context(): ...` - inside this block the money /
    PayPal / e-mail / paid-LLM call sites hard-refuse."""

    def __enter__(self):
        _local.active = getattr(_local, "depth", 0)
        _local.depth = getattr(_local, "depth", 0) + 1
        return self

    def __exit__(self, *exc):
        _local.depth = max(0, getattr(_local, "depth", 1) - 1)
        return False


def in_autonomous_context() -> bool:
    return getattr(_local, "depth", 0) > 0


def guard_no_money_in_autonomy(what: str) -> None:
    """Call-site hook for budget.py / paypal.py / delivery.py /
    llm_normalize.py. Raises if reached while the autonomous loop is on."""
    if in_autonomous_context():
        raise ActionBlocked(
            f"BLOCKED: '{what}' cannot run inside the autonomous loop. "
            "Money / PayPal / e-mail / paid-LLM actions require an explicit "
            "human approval performed OUTSIDE autonomous mode."
        )


# ---------------------------------------------------------------------------
# Phase 11-real P0-2: a NARROW read-only-PayPal exception.
#
# Reading a payment PayPal has ALREADY settled (looking up one order,
# searching completed transactions) moves no money and mutates nothing - so
# a payment-verification adapter may do it inside autonomous_context(). It
# is gated twice, fail-closed:
#   1. it must run inside an explicit `with paypal_read_context():` block
#      (a caller opting in to "everything I do here is read-only PayPal"),
#   2. AND the individual operation must be one of the three known
#      read-only calls below. Anything else - an unknown operation name, a
#      read op outside the scope, or any money-moving call - raises.
#
# There is deliberately no way to widen this by string matching or by HTTP
# method: the allow-list is these exact operation names and nothing else.
# ---------------------------------------------------------------------------

#: the ONLY PayPal operations permitted inside autonomous_context() - each
#: is a pure GET (or the client-credentials token fetch that read GETs
#: require). No capture / refund / payout / void / order-create.
PAYPAL_READONLY_OPS: frozenset[str] = frozenset({
    "config",               # resolve read-only credentials for the session
    "oauth_token",          # POST /v1/oauth2/token - client_credentials, read scope
    "get_order",            # GET  /v2/checkout/orders/{id}
    "search_transactions",  # GET  /v1/reporting/transactions
})


class paypal_read_context:
    """`with paypal_read_context(): ...` - the caller asserts that every
    PayPal API call inside this block is strictly read-only (order lookup /
    transaction search). Only then does `guard_paypal()` permit those calls
    inside `autonomous_context()`. Money-moving PayPal calls stay blocked
    regardless of this scope. Nestable; a no-op outside autonomous_context()."""

    def __enter__(self):
        _local.ppr_depth = getattr(_local, "ppr_depth", 0) + 1
        return self

    def __exit__(self, *exc):
        _local.ppr_depth = max(0, getattr(_local, "ppr_depth", 1) - 1)
        return False


def in_paypal_read_context() -> bool:
    return getattr(_local, "ppr_depth", 0) > 0


def guard_paypal(operation: str) -> None:
    """Call-site hook for paypal.py, one call per PayPal operation.

    Outside autonomous_context(): a no-op (the human-driven candidate flow
    is unaffected).

    Inside autonomous_context(): raises ActionBlocked unless BOTH
      * the call is inside an explicit paypal_read_context(), AND
      * `operation` is one of PAYPAL_READONLY_OPS.
    Fail closed - an unknown operation, or any money-moving operation, is
    blocked even inside a read context.
    """
    if not in_autonomous_context():
        return
    op = (operation or "").strip()
    if not in_paypal_read_context():
        raise ActionBlocked(
            f"BLOCKED: PayPal operation {op or '<unknown>'!r} inside the "
            "autonomous loop is only allowed inside an explicit "
            "paypal_read_context() (read-only verification). A money-moving "
            "PayPal action requires an explicit human approval OUTSIDE "
            "autonomous mode."
        )
    if op not in PAYPAL_READONLY_OPS:
        raise ActionBlocked(
            f"BLOCKED: PayPal operation {op or '<unknown>'!r} is not a "
            "permitted read-only operation - it may move money or mutate a "
            "PayPal resource. Only order lookup / transaction search are "
            "allowed inside the autonomous loop."
        )
