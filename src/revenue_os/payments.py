"""Payment provider abstraction - INCOMING payments only.

  adapter.poll(opportunity_id) -> PaymentPollResult      (read the provider)
  process_payment_event(ledger, event) -> PaymentResult  (book ONE, idempotent)

A "payment" here is money a customer has ALREADY paid, that a provider has
ALREADY settled. Processing it books a row in the existing RevenueLedger
(revenue.record_opportunity_payment) - it moves no money, captures nothing,
authorises no spend. INCOMING PAYMENT != OUTGOING SPEND.

Adapters:
  * NullPaymentAdapter - no provider wired (fail-closed default)
  * FakePaymentAdapter - deterministic, offline, for tests

A real PayPal adapter can be added later against this same interface. The
existing read-only paypal.py (Candidate-centric) is left untouched.

Idempotency is anchored on the provider's stable reference
(capture_id / transaction_id): ledger ref = "<provider>:<reference>".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaymentEvent:
    reference: str                 # stable provider ref (capture_id / txn_id)
    amount: float
    currency: str = "EUR"
    opportunity_id: str = ""
    customer_ref: str = ""         # buyer email / name, if the provider gives it
    provider: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PaymentPollResult:
    ok: bool
    provider: str
    events: list = field(default_factory=list)   # list[PaymentEvent]
    blocked: bool = False          # True = no provider / no credentials
    error: str = ""


@dataclass
class PaymentResult:
    success: bool
    provider: str
    reference: str = ""            # provider reference
    payment_id: str = ""          # our ledger ref ("<provider>:<reference>")
    amount: float = 0.0
    currency: str = "EUR"
    opportunity_id: str = ""
    customer_ref: str = ""
    already_booked: bool = False   # idempotency: this ref was already in the ledger
    error: str = ""
    blocked: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success, "provider": self.provider,
            "reference": self.reference, "payment_id": self.payment_id,
            "amount": self.amount, "currency": self.currency,
            "opportunity_id": self.opportunity_id,
            "customer_ref": self.customer_ref,
            "already_booked": self.already_booked, "error": self.error,
            "blocked": self.blocked,
        }


class PaymentAdapter:
    provider = "base"

    def poll(self, *, opportunity_id: str) -> PaymentPollResult:  # pragma: no cover
        raise NotImplementedError


class NullPaymentAdapter(PaymentAdapter):
    """No provider connected. Fail-closed: reports blocked, returns nothing."""

    provider = "none"

    def poll(self, *, opportunity_id: str) -> PaymentPollResult:
        return PaymentPollResult(
            ok=False, provider=self.provider, blocked=True,
            error="no opportunity payment provider is configured - the payment "
                  "path is ready, a real provider adapter must be wired")


class FakePaymentAdapter(PaymentAdapter):
    """Deterministic, offline. Simulates a provider poll only - never a
    capture, transfer, or spend."""

    provider = "fake"

    def __init__(self, *, events: list | None = None, fail: bool = False,
                 blocked: bool = False, error: str = "") -> None:
        self._events = list(events or [])
        self.fail = fail
        self.blocked = blocked
        self.error = error
        self.polls = 0

    def poll(self, *, opportunity_id: str) -> PaymentPollResult:
        self.polls += 1
        if self.blocked:
            return PaymentPollResult(ok=False, provider=self.provider,
                                     blocked=True,
                                     error=self.error or "fake: no credentials")
        if self.fail:
            return PaymentPollResult(ok=False, provider=self.provider,
                                     error=self.error or "fake: provider error")
        evs = [e for e in self._events
               if not e.opportunity_id or e.opportunity_id == opportunity_id]
        for e in evs:
            e.provider = e.provider or self.provider
        return PaymentPollResult(ok=True, provider=self.provider,
                                 events=list(evs))


def ledger_ref(event: PaymentEvent) -> str:
    return f"{event.provider or 'payment'}:{event.reference}" if event.reference else ""


def process_payment_event(ledger, event: PaymentEvent, *,
                          actor: str = "payment") -> PaymentResult:
    """Book ONE confirmed incoming payment into the revenue ledger.
    Idempotent by the provider reference. Rejects invalid events. Moves no
    money."""
    from .revenue import record_opportunity_payment

    if not event.reference:
        return PaymentResult(success=False, provider=event.provider or "payment",
                             opportunity_id=event.opportunity_id,
                             error="payment has no provider reference - "
                                   "cannot dedupe, refusing to book")
    try:
        amount = float(event.amount)
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0:
        return PaymentResult(success=False, provider=event.provider or "payment",
                             reference=event.reference,
                             opportunity_id=event.opportunity_id,
                             error=f"invalid payment amount {event.amount!r}")
    if not event.opportunity_id:
        return PaymentResult(success=False, provider=event.provider or "payment",
                             reference=event.reference,
                             error="payment is not attributable to an opportunity")

    ref = ledger_ref(event)
    outcome = record_opportunity_payment(
        ledger, opportunity_id=event.opportunity_id, amount=amount, ref=ref,
        currency=event.currency or "EUR", customer_ref=event.customer_ref,
        actor=actor, note=f"{event.provider or 'payment'} {event.reference}")
    return PaymentResult(
        success=True, provider=event.provider or "payment",
        reference=event.reference, payment_id=ref, amount=round(amount, 2),
        currency=event.currency or "EUR", opportunity_id=event.opportunity_id,
        customer_ref=event.customer_ref,
        already_booked=(outcome["outcome"] == "already_booked"))


def default_payment_adapter() -> PaymentAdapter:
    return NullPaymentAdapter()
