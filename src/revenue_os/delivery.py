"""Customer Launch Plan delivery: render a PDF and email it to the buyer.

Two stages, so the real send stays behind a human gate:

  stage_delivery(order_id)  - render the approved plan to a real PDF on
                              disk (no send). Safe to re-run.
  send_delivery(order_id)   - email that PDF to the buyer. Refuses to
                              send twice for the same order.

Gates that must already have been passed (unchanged):
  * the intake is `reviewed`            (intake-review, human)
  * the plan is `approved`              (plan-approve, human)
  * the order's capture_id is still a booked `paypal:` payment for the
    candidate                           (real-revenue rule)

The actual send is only performed by `send_delivery` / `plan-deliver
--send`; `plan-deliver` alone just stages the PDF.

Email transport is standard-library smtplib; credentials come from the
environment (.env), never code:
  SMTP_HOST      (required)          SMTP_USER      (required)
  SMTP_PASSWORD  (required)          SMTP_PORT      (default 587)
  SMTP_FROM      (default: $BUSINESS_EMAIL)
  SMTP_STARTTLS  (default: true)     SMTP_TIMEOUT   (default 30)

Nothing here moves money or touches PayPal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import tempfile
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .deliverable import render_launch_plan_md
from .intake import IntakeStore, _booked_paypal
from .pdf import render_markdown_pdf
from .revenue import RevenueLedger
from .store import now_iso

logger = logging.getLogger(__name__)

_PRODUCT = "Customer Launch Plan"


class DeliveryError(RuntimeError):
    """A delivery could not be staged or sent. Never contains a password."""


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

class DeliveryStore:
    """One JSON list at data/deliveries.json, atomically written, keyed by
    order_id. Records what was rendered and whether it was sent."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_order: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "DeliveryStore":
        store = cls(path)
        if not store.path.exists():
            return store
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DeliveryError(f"corrupt delivery store {store.path}: {exc}") from None
        if not isinstance(raw, list):
            raise DeliveryError(f"delivery store {store.path} must be a JSON list")
        for entry in raw:
            store._by_order[str(entry["order_id"])] = dict(entry)
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(list(self._by_order.values()), indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, order_id: str) -> dict | None:
        return self._by_order.get(str(order_id))

    def all(self) -> list[dict]:
        return list(self._by_order.values())

    def put(self, entry: dict) -> None:
        self._by_order[str(entry["order_id"])] = dict(entry)


# ---------------------------------------------------------------------------
# email config + transport
# ---------------------------------------------------------------------------

@dataclass
class EmailConfig:
    host: str
    user: str
    password: str
    port: int = 587
    sender: str = ""
    starttls: bool = True
    timeout: int = 30

    @classmethod
    def from_env(cls, environ=None) -> "EmailConfig":
        env = environ if environ is not None else os.environ
        host = (env.get("SMTP_HOST") or "").strip()
        user = (env.get("SMTP_USER") or "").strip()
        password = env.get("SMTP_PASSWORD") or ""
        sender = (env.get("SMTP_FROM") or env.get("BUSINESS_EMAIL") or "").strip()
        missing = [n for n, v in (("SMTP_HOST", host), ("SMTP_USER", user),
                                  ("SMTP_PASSWORD", password)) if not v]
        if missing:
            raise DeliveryError(
                f"set {', '.join(missing)} in the environment (.env) to send "
                "the plan by email")
        try:
            port = int(env.get("SMTP_PORT") or 587)
        except ValueError:
            raise DeliveryError("SMTP_PORT must be an integer") from None
        starttls = (env.get("SMTP_STARTTLS") or "true").strip().lower() not in (
            "0", "false", "no")
        try:
            timeout = int(env.get("SMTP_TIMEOUT") or 30)
        except ValueError:
            timeout = 30
        return cls(host=host, user=user, password=password, port=port,
                   sender=sender or user, starttls=starttls, timeout=timeout)


def _smtp_send(config: EmailConfig, msg: EmailMessage) -> str:
    """Default transport. Returns the Message-ID. Raises DeliveryError on
    any SMTP failure - the message never contains the password."""
    try:
        with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as s:
            s.ehlo()
            if config.starttls:
                s.starttls()
                s.ehlo()
            s.login(config.user, config.password)
            s.send_message(msg)
    except smtplib.SMTPException as exc:
        raise DeliveryError(f"SMTP send failed: {exc.__class__.__name__}: {exc}") from None
    except OSError as exc:
        raise DeliveryError(f"SMTP connection failed: {exc}") from None
    return msg["Message-ID"]


# ---------------------------------------------------------------------------
# stage / send
# ---------------------------------------------------------------------------

def _load_gated_entry(data_dir: Path, order_id: str) -> dict:
    intake = IntakeStore.load(data_dir / "intake.json")
    entry = intake.get(order_id)
    if entry is None:
        raise DeliveryError(f"no intake for order {order_id!r}")
    plan = entry.get("plan") or {}
    if entry.get("status") != "reviewed":
        raise DeliveryError(
            f"order {order_id!r} is {entry.get('status')!r}; run `intake-review` first")
    if plan.get("status") != "approved":
        raise DeliveryError(
            f"order {order_id!r} plan is {plan.get('status')!r}; run "
            "`plan-approve` first (human gate before delivery)")
    cap = str(entry.get("capture_id", ""))
    booked = _booked_paypal(RevenueLedger.load(data_dir / "revenue.json"))
    if cap not in booked or booked[cap] != entry.get("candidate"):
        raise DeliveryError(
            f"order {order_id!r}: capture {cap!r} is not a booked payment for "
            f"{entry.get('candidate')!r}; nothing was rendered")
    return entry


def stage_delivery(data_dir, order_id: str) -> dict:
    """Render the approved plan to a PDF on disk. No email is sent."""
    data_dir = Path(data_dir)
    entry = _load_gated_entry(data_dir, order_id)
    fields = entry.get("fields") or {}

    md = render_launch_plan_md(entry)
    pdf_bytes = render_markdown_pdf(md, title=f"{_PRODUCT} - {entry['candidate']}")
    out_dir = data_dir / "deliverables" / entry["candidate"]
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"plan-{order_id}.pdf"
    pdf_path.write_bytes(pdf_bytes)

    store = DeliveryStore.load(data_dir / "deliveries.json")
    prev = store.get(order_id) or {}
    record = {
        "order_id": str(order_id),
        "candidate": entry["candidate"],
        "to_email": fields.get("email", ""),
        "to_name": fields.get("name", ""),
        "subject": f"Your {_PRODUCT} (order {order_id})",
        "pdf_path": str(pdf_path),
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pdf_bytes": len(pdf_bytes),
        "status": prev.get("status", "staged") if prev.get("status") == "sent"
        else "staged",
        "staged_at": now_iso(),
        "sent_at": prev.get("sent_at"),
        "message_id": prev.get("message_id"),
    }
    store.put(record)
    store.save()
    return {k: v for k, v in record.items()}


def send_delivery(data_dir, order_id: str, *, mailer=None, force: bool = False,
                  config: EmailConfig | None = None) -> dict:
    """Email the staged PDF to the buyer. Refuses a second send unless
    force=True. Auto-stages if the PDF is missing."""
    data_dir = Path(data_dir)
    store = DeliveryStore.load(data_dir / "deliveries.json")
    record = store.get(order_id)
    if record is None or not Path(record.get("pdf_path", "")).is_file():
        record = stage_delivery(data_dir, order_id)
        store = DeliveryStore.load(data_dir / "deliveries.json")

    if record.get("status") == "sent" and not force:
        raise DeliveryError(
            f"order {order_id!r} was already delivered at {record.get('sent_at')} "
            f"(message {record.get('message_id')}); pass --force to resend")

    to_email = (record.get("to_email") or "").strip()
    if "@" not in to_email:
        raise DeliveryError(
            f"order {order_id!r} has no usable buyer email in the intake")

    cfg = config or EmailConfig.from_env()
    pdf_path = Path(record["pdf_path"])

    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = to_email
    msg["Subject"] = record["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1] or None)
    name = (record.get("to_name") or "").split(" ")[0] or "there"
    msg.set_content(
        f"Hi {name},\n\n"
        f"Your {_PRODUCT} is attached as a PDF. It is a personalised research "
        f"and strategy document - not a guarantee of customers or revenue.\n\n"
        f"If anything is unclear, just reply to this email.\n\n"
        f"Order reference: {order_id}\n"
    )
    msg.add_attachment(pdf_path.read_bytes(), maintype="application",
                       subtype="pdf",
                       filename=f"Customer-Launch-Plan-{order_id}.pdf")

    send = mailer or _smtp_send
    message_id = send(cfg, msg) or msg["Message-ID"]

    record = dict(record)
    record["status"] = "sent"
    record["sent_at"] = now_iso()
    record["message_id"] = message_id
    record["to_email"] = to_email
    store.put(record)
    store.save()
    logger.info("delivered order %s to a buyer address (message %s)",
                order_id, message_id)
    return {k: v for k, v in record.items()}


def delivery_status(data_dir, order_id: str | None = None) -> dict:
    store = DeliveryStore.load(Path(data_dir) / "deliveries.json")
    if order_id is not None:
        rec = store.get(order_id)
        return rec or {"order_id": order_id, "status": "none"}
    entries = store.all()
    return {
        "total": len(entries),
        "staged": sum(1 for e in entries if e.get("status") == "staged"),
        "sent": sum(1 for e in entries if e.get("status") == "sent"),
        "entries": entries,
    }
