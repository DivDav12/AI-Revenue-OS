"""Phase 11-real P0-1: a real, read-only PayPalPaymentAdapter.

Turns PayPal's authenticated, read-only Transaction Search into
`payments.PaymentEvent`s for the CHECK_REVENUE / PaymentAdapter pipeline:

  CHECK_REVENUE -> PayPalPaymentAdapter.poll(opportunity_id)
    -> PayPalClient.search_transactions() (read-only, existing paypal.py)
    -> strict opportunity attribution + amount/currency verification
    -> payments.PaymentEvent
    -> payments.process_payment_event() (existing, unchanged)
    -> revenue.record_opportunity_payment() (existing, unchanged)

No second PayPal HTTP client, no second auth path, no second revenue
ledger: this module only reuses `paypal.PayPalClient` /
`paypal.PayPalConfig` / `paypal._txn_row` and the existing PaymentAdapter
contract. It never calls a PayPal write endpoint (no create/capture/
refund/payout) - only `search_transactions` (and the config/oauth_token
reads that call needs).

Attribution is strict and non-heuristic: a transaction is only ever
turned into a PaymentEvent when its PayPal custom_id/custom_field is
EXACTLY the opportunity_id being polled, that id is a syntactically
valid opportunity id, AND it exists in the persistent OpportunityStore.
No email / amount / URL / recency / "only one candidate" fallback.

The expected price is the OFFER FROZEN by the opportunity's own
successful PLAN task (`task.output["offer"]`) - never a freshly computed
estimate. A transaction must match that amount and currency EXACTLY
(rounded to 2dp, exact currency string) to become a PaymentEvent.

Fail-closed: missing/invalid PayPal credentials, an HTTP/auth failure, or
a malformed Transaction Search response never fabricates revenue - they
report `ok=False` (blocked for missing config, retryable for a transient
provider error) with zero events, exactly like every other PaymentAdapter
failure mode already handled by CheckRevenueAdapter.

Read-only inside the autonomous loop: `guard_paypal()` (Phase 11-real
P0-2) already restricts every PayPal call this module reaches
(config / oauth_token / search_transactions) to a `with
paypal_read_context():` block inside `autonomous_context()`. This module
opens that scope itself around its own PayPal calls - it does not widen
`action_class.PAYPAL_READONLY_OPS` and does not touch
`guard_no_money_in_autonomy`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import opportunity_state as ostate
from .action_class import paypal_read_context
from .execution import load_tasks
from .measurement import KEEP_MEASURING_STATES
from .opportunity_store import load_opportunities
from .payments import PaymentAdapter, PaymentEvent, PaymentPollResult
from .paypal import PayPalClient, PayPalConfig, _txn_row

#: the ONLY accepted opportunity id shape (opportunity_store._oid())
_OID_RE = re.compile(r"^opp_[0-9a-f]{12}$")


class _PayPalNotConfigured(RuntimeError):
    """Internal signal: PayPal credentials could not be resolved. Caught by
    `poll()` and reported as a blocked (fail-closed) PaymentPollResult -
    never raised out of the adapter."""


class PayPalPaymentAdapter(PaymentAdapter):
    """Real PayPal read-only PaymentAdapter for CHECK_REVENUE.

    Not wired as the default payment adapter (see `payments.
    default_payment_adapter`) - an operator opts in explicitly by passing
    an instance to `task_adapters.CheckRevenueAdapter(payment_adapter=...)`.
    """

    provider = "paypal"

    def __init__(self, data_dir, *, lookback_days: int = 8,
                 client: PayPalClient | None = None,
                 config_factory=None, now: datetime | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.lookback_days = max(1, min(int(lookback_days), 31))
        self._client = client
        self._config_factory = config_factory or PayPalConfig.from_env
        self._now = now

    # --- internals ---------------------------------------------------
    def _client_for(self) -> PayPalClient:
        if self._client is not None:
            return self._client
        try:
            cfg = self._config_factory()
        except ValueError as exc:
            raise _PayPalNotConfigured(str(exc)) from exc
        return PayPalClient(cfg)

    def _frozen_offer(self, opportunity_id: str) -> tuple[float, str] | None:
        """The price/currency frozen by this opportunity's successful PLAN
        task - the only accepted source of an "expected amount". Never a
        freshly computed estimate."""
        tasks = load_tasks(self.data_dir).by_opportunity(opportunity_id)
        plan = next((t for t in tasks
                     if t.task_type == "PLAN" and t.status == "SUCCEEDED"), None)
        if plan is None:
            return None
        offer = (plan.output or {}).get("offer")
        if not isinstance(offer, dict):
            return None
        currency = str(offer.get("currency") or "").strip().upper()
        if not currency:
            return None
        try:
            price = round(float(offer.get("price")), 2)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return price, currency

    # --- PaymentAdapter contract --------------------------------------
    def poll(self, *, opportunity_id: str) -> PaymentPollResult:
        oid = (opportunity_id or "").strip()

        # 1. the id itself must be a syntactically valid opportunity id
        if not _OID_RE.match(oid):
            return PaymentPollResult(ok=True, provider=self.provider, events=[])

        # 2. it must exist in the persistent store
        opp = load_opportunities(self.data_dir).get(oid)
        if opp is None:
            return PaymentPollResult(ok=True, provider=self.provider, events=[])

        # 3. it must be in a state where incoming revenue is plausible
        if (opp.get("state") or ostate.INITIAL) not in KEEP_MEASURING_STATES:
            return PaymentPollResult(ok=True, provider=self.provider, events=[])

        # 4. a frozen offer (successful PLAN output) is required to know
        #    the expected amount/currency - no fallback estimate.
        offer = self._frozen_offer(oid)
        if offer is None:
            return PaymentPollResult(ok=True, provider=self.provider, events=[])
        expected_amount, expected_currency = offer

        # 5. authenticated, read-only PayPal Transaction Search. Fail
        #    closed on any credential / transport / API problem - never
        #    fabricate revenue.
        try:
            with paypal_read_context():
                client = self._client_for()
                end = self._now or datetime.now(timezone.utc)
                start = end - timedelta(days=self.lookback_days)
                details = client.search_transactions(start, end)
        except _PayPalNotConfigured as exc:
            return PaymentPollResult(ok=False, provider=self.provider,
                                     blocked=True,
                                     error=f"PayPal is not configured: {exc}")
        except Exception as exc:   # noqa: BLE001 - never let a provider
            # failure of any shape (HTTP error, malformed JSON, timeout,
            # unexpected schema) be interpreted as a successful poll.
            return PaymentPollResult(ok=False, provider=self.provider,
                                     error=f"PayPal Transaction Search failed: {exc}")

        # 6. strict, non-heuristic attribution + amount/currency match
        events: list[PaymentEvent] = []
        for detail in details:
            row = _txn_row(detail)          # status == "S", amount > 0, else None
            if row is None:
                continue
            custom_id = str(row.get("custom_id") or "").strip()
            if custom_id != oid:            # exact match only - no fallback
                continue
            currency = str(row.get("currency") or "").strip().upper()
            try:
                amount = round(float(row.get("amount")), 2)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            if amount != expected_amount or currency != expected_currency:
                continue
            capture_id = str(row.get("capture_id") or "").strip()
            if not capture_id:
                continue
            events.append(PaymentEvent(
                reference=capture_id, amount=amount, currency=currency,
                opportunity_id=oid, customer_ref="", provider=self.provider,
                raw=dict(detail)))

        return PaymentPollResult(ok=True, provider=self.provider, events=events)
