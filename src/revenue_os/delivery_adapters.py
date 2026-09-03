"""Delivery adapters - deliver a purchased digital product to the buyer.

  adapter.deliver(DeliveryArtifact, DeliveryRecipient) -> DeliveryResult

Same shape as deployment.py / payments.py. Three implementations:

  * NullDeliveryAdapter  - no provider wired (fail-closed default)
  * FakeDeliveryAdapter  - deterministic, offline, for tests
  * SmtpDeliveryAdapter  - real: a plain digital-delivery e-mail via the
                           existing delivery.py SMTP transport; fail-closed
                           when SMTP_* is not configured, and refuses to run
                           inside autonomous_context() (Phase 11-real P1-3).

The existing order-based delivery.py (PDF render + gated send for the
Customer-Launch-Plan product) is left untouched - SmtpDeliveryAdapter only
reuses its EmailConfig + _smtp_send transport.

Nothing here moves money, captures a payment, or spends. Delivering a
digital good the customer already paid for is not an outgoing-money action -
but SENDING it is still e-mail, one of the four leak paths the autonomous
loop's money/identity firewall refuses (see guard_no_money_in_autonomy on
SmtpDeliveryAdapter.deliver()). `default_delivery_adapter()` still returns
NullDeliveryAdapter; SmtpDeliveryAdapter is not wired anywhere by this
phase - only guarded, so a future explicit wiring cannot accidentally send
autonomously.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from .store import now_iso


@dataclass
class DeliveryArtifact:
    opportunity_id: str
    product_name: str
    live_url: str = ""
    body: str = ""
    files: dict = field(default_factory=dict)   # {filename: bytes} - optional

    def content_key(self) -> str:
        h = hashlib.sha256()
        h.update(self.product_name.encode("utf-8"))
        h.update(b"\0")
        h.update(self.live_url.encode("utf-8"))
        for name in sorted(self.files):
            h.update(name.encode("utf-8"))
        return h.hexdigest()[:16]


@dataclass
class DeliveryRecipient:
    reference: str                 # e-mail / handle / customer id
    opportunity_id: str = ""
    name: str = ""


@dataclass
class DeliveryResult:
    success: bool
    provider: str
    delivery_id: str = ""          # our stable id for this delivery
    reference: str = ""            # provider message id / receipt
    recipient: str = ""
    opportunity_id: str = ""
    error: str = ""
    blocked: bool = False          # True = missing provider / credentials
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success, "provider": self.provider,
            "delivery_id": self.delivery_id, "reference": self.reference,
            "recipient": self.recipient, "opportunity_id": self.opportunity_id,
            "error": self.error, "blocked": self.blocked,
            "details": dict(self.details),
        }


class DeliveryAdapter:
    provider = "base"

    def deliver(self, artifact: DeliveryArtifact,
                recipient: DeliveryRecipient) -> DeliveryResult:  # pragma: no cover
        raise NotImplementedError


class NullDeliveryAdapter(DeliveryAdapter):
    provider = "none"

    def deliver(self, artifact, recipient) -> DeliveryResult:
        return DeliveryResult(
            success=False, blocked=True, provider=self.provider,
            opportunity_id=artifact.opportunity_id,
            recipient=recipient.reference,
            error="no delivery provider is configured - the delivery path is "
                  "ready, a real provider adapter must be wired")


class FakeDeliveryAdapter(DeliveryAdapter):
    """Deterministic, offline. Never opens an SMTP connection."""

    provider = "fake"

    def __init__(self, *, fail: bool = False, blocked: bool = False,
                 error: str = "") -> None:
        self.fail = fail
        self.blocked = blocked
        self.error = error
        self.calls = 0
        self._sent: dict[tuple, DeliveryResult] = {}

    def deliver(self, artifact, recipient) -> DeliveryResult:
        self.calls += 1
        if self.blocked:
            return DeliveryResult(success=False, blocked=True,
                                  provider=self.provider,
                                  opportunity_id=artifact.opportunity_id,
                                  recipient=recipient.reference,
                                  error=self.error or "fake: no credentials")
        if self.fail:
            return DeliveryResult(success=False, provider=self.provider,
                                  opportunity_id=artifact.opportunity_id,
                                  recipient=recipient.reference,
                                  error=self.error or "fake: send failed")
        if not recipient.reference:
            return DeliveryResult(success=False, provider=self.provider,
                                  opportunity_id=artifact.opportunity_id,
                                  error="fake: no recipient reference")

        key = (artifact.opportunity_id, recipient.reference, artifact.content_key())
        prior = self._sent.get(key)
        if prior is not None:
            return replace(prior, details={**prior.details,
                                           "duplicate_suppressed": True})

        digest = hashlib.sha256(
            f"{key}".encode("utf-8")).hexdigest()[:10]
        res = DeliveryResult(
            success=True, provider=self.provider,
            delivery_id=f"fake-del-{digest}",
            reference=f"fake-msg-{digest}",
            recipient=recipient.reference,
            opportunity_id=artifact.opportunity_id,
            details={"product": artifact.product_name,
                     "live_url": artifact.live_url,
                     "delivered_at": now_iso()})
        self._sent[key] = res
        return res


class SmtpDeliveryAdapter(DeliveryAdapter):
    """Real: send a plain digital-delivery e-mail through delivery.py's SMTP
    transport. Fail-closed when SMTP is not configured.

    Phase 11-real P1-3: `deliver()` is also one of the four documented
    autonomous "leak paths" (see `action_class.guard_no_money_in_autonomy`,
    alongside budget.py / paypal.py / delivery.py's own send_delivery /
    llm_normalize.py) and refuses to run inside `autonomous_context()` -
    the same guard `delivery.py`'s candidate-flow `send_delivery()` already
    uses. This is checked BEFORE any SMTP config is resolved or transport
    touched, so a fully-configured adapter still sends nothing when the
    worker runs it (as it always does, inside `autonomous_context()`).
    Outside that context (a human running this directly) it is a no-op and
    behaviour is unchanged."""

    provider = "smtp"

    def __init__(self, *, config=None, mailer=None, environ=None) -> None:
        self._config = config
        self._mailer = mailer
        self._environ = environ

    def deliver(self, artifact, recipient) -> DeliveryResult:
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid

        from .action_class import guard_no_money_in_autonomy
        from .delivery import DeliveryError, EmailConfig, _smtp_send

        guard_no_money_in_autonomy("send customer e-mail")

        try:
            cfg = self._config or EmailConfig.from_env(self._environ)
        except DeliveryError as exc:
            return DeliveryResult(success=False, blocked=True,
                                  provider=self.provider,
                                  opportunity_id=artifact.opportunity_id,
                                  error=str(exc))
        if "@" not in (recipient.reference or ""):
            return DeliveryResult(success=False, provider=self.provider,
                                  opportunity_id=artifact.opportunity_id,
                                  error="recipient reference is not an e-mail address")

        msg = EmailMessage()
        msg["From"] = cfg.sender
        msg["To"] = recipient.reference
        msg["Subject"] = f"Your {artifact.product_name}"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=(cfg.sender.split("@")[-1] or None))
        msg.set_content(artifact.body or (
            f"Thank you for your purchase of \"{artifact.product_name}\".\n\n"
            + (f"Access it here: {artifact.live_url}\n" if artifact.live_url else "")
            + "\nThis is an automated delivery.\n"))
        for name, data in artifact.files.items():
            msg.add_attachment(data if isinstance(data, bytes)
                               else str(data).encode("utf-8"),
                               maintype="application", subtype="octet-stream",
                               filename=name)
        send = self._mailer or _smtp_send
        try:
            mid = send(cfg, msg) or msg["Message-ID"]
        except DeliveryError as exc:
            return DeliveryResult(success=False, provider=self.provider,
                                  opportunity_id=artifact.opportunity_id,
                                  recipient=recipient.reference, error=str(exc))
        return DeliveryResult(
            success=True, provider=self.provider, delivery_id=str(mid),
            reference=str(mid), recipient=recipient.reference,
            opportunity_id=artifact.opportunity_id,
            details={"product": artifact.product_name})


def default_delivery_adapter() -> DeliveryAdapter:
    return NullDeliveryAdapter()
