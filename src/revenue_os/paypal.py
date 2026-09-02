"""PayPal integration - read-only.

Books payments that PayPal has ALREADY processed into the existing
RevenueLedger via revenue.record_payment(). It cannot send money, change
an account, or create an obligation - the Client ID / Secret only grant
read access to orders and the Transaction Search API.

Every call here is a GET (or the client-credentials token fetch a GET
needs). Each is gated by `action_class.guard_paypal(<op>)`: outside the
autonomous loop it is a no-op; inside the autonomous loop it is permitted
ONLY within an explicit `with action_class.paypal_read_context():` block
and only for the three known read operations. Any other / future PayPal
operation is blocked inside the autonomous loop, fail-closed.

Credentials come from the environment, never from code or the store:
  PAYPAL_CLIENT_ID       (required)
  PAYPAL_CLIENT_SECRET   (required)
  PAYPAL_ENV             sandbox (default) | live

A PayPal payment is tied to a candidate through the order's `custom_id`
field (set it to the candidate name when you create the button/invoice).
Every booked payment carries ref = "paypal:<capture-id>", so re-running
verify / sync never double-books.

Standard library only (urllib + json + base64).
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .revenue import RevenueLedger, record_payment
from .store import CandidateStore

_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}
_TIMEOUT = 15
_MAX_DAYS = 31  # PayPal Transaction Search caps the range at 31 days


def _http_json(method: str, url: str, *, headers: dict, data: bytes | None = None):
    """One urllib wrapper (patched in tests). Returns the parsed JSON body;
    raises ValueError with the status text on a non-2xx response."""
    # Fail-closed backstop: the ONLY non-GET this module ever issues is the
    # client-credentials token POST. Any other write verb reaching PayPal
    # from inside the autonomous loop - now or in future code - is blocked,
    # regardless of paypal_read_context().
    if method.upper() != "GET" and not url.endswith("/v1/oauth2/token"):
        from .action_class import guard_paypal
        guard_paypal(f"http_{method.lower()}")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ValueError(f"PayPal API {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"PayPal API unreachable: {exc.reason}") from exc


@dataclass
class PayPalConfig:
    client_id: str
    client_secret: str
    env: str = "sandbox"

    @property
    def base_url(self) -> str:
        return _BASE[self.env]

    @classmethod
    def from_env(cls, environ=None) -> "PayPalConfig":
        import os

        # Read-only by construction: every call in this module is a GET (or
        # the token fetch a GET needs). Inside the autonomous loop this is
        # permitted ONLY inside an explicit `paypal_read_context()` - see
        # action_class.guard_paypal. Money-moving calls stay blocked.
        from .action_class import guard_paypal
        guard_paypal("config")

        environ = environ if environ is not None else os.environ
        cid = environ.get("PAYPAL_CLIENT_ID", "").strip()
        secret = environ.get("PAYPAL_CLIENT_SECRET", "").strip()
        env = environ.get("PAYPAL_ENV", "sandbox").strip().lower() or "sandbox"
        if env not in _BASE:
            raise ValueError("PAYPAL_ENV must be 'sandbox' or 'live'")
        if not cid or not secret:
            raise ValueError(
                "set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in the environment"
            )
        return cls(client_id=cid, client_secret=secret, env=env)


@dataclass
class PayPalClient:
    config: PayPalConfig
    _token: str = ""
    _token_expiry: float = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        from .action_class import guard_paypal
        guard_paypal("oauth_token")
        basic = base64.b64encode(
            f"{self.config.client_id}:{self.config.client_secret}".encode()
        ).decode()
        body = _http_json(
            "POST", f"{self.config.base_url}/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=b"grant_type=client_credentials",
        )
        self._token = str(body.get("access_token", ""))
        if not self._token:
            raise ValueError("PayPal did not return an access token")
        self._token_expiry = time.time() + float(body.get("expires_in", 300))
        return self._token

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.config.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _http_json(
            "GET", url,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Content-Type": "application/json"},
        )

    def get_order(self, order_id: str) -> dict:
        from .action_class import guard_paypal
        guard_paypal("get_order")
        return self._get(f"/v2/checkout/orders/{urllib.parse.quote(order_id)}")

    def search_transactions(self, start: datetime, end: datetime) -> list[dict]:
        from .action_class import guard_paypal
        guard_paypal("search_transactions")
        out: list[dict] = []
        page = 1
        while True:
            body = self._get("/v1/reporting/transactions", {
                "start_date": start.strftime("%Y-%m-%dT%H:%M:%S-0000"),
                "end_date": end.strftime("%Y-%m-%dT%H:%M:%S-0000"),
                "fields": "transaction_info",
                "page_size": 100, "page": page,
            })
            out.extend(body.get("transaction_details", []) or [])
            if page >= int(body.get("total_pages", 1) or 1):
                return out
            page += 1


# --- pure extraction -----------------------------------------------------

def extract_capture(order: dict) -> dict:
    """Pull the completed capture from an order dict. Raises ValueError if
    the order is not a completed payment."""
    status = str(order.get("status", "")).upper()
    if status != "COMPLETED":
        raise ValueError(f"order status is {status or 'unknown'}, not COMPLETED")
    units = order.get("purchase_units") or []
    if not units:
        raise ValueError("order has no purchase_units")
    unit = units[0]
    captures = ((unit.get("payments") or {}).get("captures")) or []
    done = [c for c in captures if str(c.get("status", "")).upper() == "COMPLETED"]
    if not done:
        raise ValueError("order has no completed capture")
    cap = done[0]
    amt = cap.get("amount") or {}
    try:
        value = float(amt.get("value"))
    except (TypeError, ValueError) as exc:
        raise ValueError("capture amount is not a number") from exc
    if value <= 0:
        raise ValueError("capture amount is not positive")
    return {
        "capture_id": str(cap.get("id", "")),
        "amount": round(value, 2),
        "currency": str(amt.get("currency_code", "USD")),
        "custom_id": str(cap.get("custom_id") or unit.get("custom_id") or "").strip(),
        "created_at": str(cap.get("create_time") or order.get("create_time") or ""),
    }


def _txn_row(detail: dict) -> dict | None:
    info = detail.get("transaction_info") or {}
    if str(info.get("transaction_status", "")).upper() != "S":  # S = success
        return None
    amt = info.get("transaction_amount") or {}
    try:
        value = round(float(amt.get("value")), 2)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return {
        "capture_id": str(info.get("transaction_id", "")),
        "amount": value,
        "currency": str(amt.get("currency_code", "USD")),
        "custom_id": str(info.get("custom_field") or "").strip(),
        "created_at": str(info.get("transaction_initiation_date") or ""),
    }


# --- booking -----------------------------------------------------------

def _book(store, ledger, cap: dict, *, candidate: str, actor: str,
          source_note: str) -> str:
    ref = f"paypal:{cap['capture_id']}"
    if ledger.has_ref(ref):
        return "already booked"
    record_payment(
        store, ledger, candidate, cap["amount"], actor=actor,
        currency=cap["currency"], note=source_note, ref=ref,
        received_at=cap["created_at"] or None,
    )
    return "booked"


def verify_and_book_order(store: CandidateStore, ledger: RevenueLedger, *,
                          candidate: str, order_id: str, actor: str = "paypal",
                          force: bool = False, client: PayPalClient | None = None) -> dict:
    """Fetch one order, verify it is a completed payment, and book it
    against `candidate` through record_payment (which still requires the
    candidate to be launched/earning)."""
    client = client or PayPalClient(PayPalConfig.from_env())
    cap = extract_capture(client.get_order(order_id))
    if cap["custom_id"] and cap["custom_id"] != candidate and not force:
        raise ValueError(
            f"order custom_id is {cap['custom_id']!r} but you passed "
            f"{candidate!r}; re-run with --force to override"
        )
    outcome = _book(store, ledger, cap, candidate=candidate, actor=actor,
                    source_note=f"paypal order {order_id}")
    return {"outcome": outcome, "candidate": candidate, **cap}


def sync_transactions(store: CandidateStore, ledger: RevenueLedger, *,
                      days: int = _MAX_DAYS, actor: str = "paypal",
                      dry_run: bool = False,
                      client: PayPalClient | None = None,
                      now: datetime | None = None) -> dict:
    """List successful PayPal transactions in the last `days`, map each to a
    candidate via its custom_field, and book the ones that fit and are not
    already recorded. Books nothing when dry_run is True."""
    client = client or PayPalClient(PayPalConfig.from_env())
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=min(max(1, days), _MAX_DAYS))

    booked: list[dict] = []
    skipped: list[dict] = []
    for detail in client.search_transactions(start, end):
        row = _txn_row(detail)
        if row is None:
            continue
        name = row["custom_id"]
        ref = f"paypal:{row['capture_id']}"
        if not name:
            skipped.append({**row, "reason": "no custom_field (candidate) on the transaction"})
            continue
        if ledger.has_ref(ref):
            continue  # already booked - silent
        cand = store.get(name)
        if cand is None:
            skipped.append({**row, "reason": f"unknown candidate {name!r}"})
            continue
        if cand.status not in ("launched", "earning"):
            skipped.append({**row, "reason": f"{name!r} is {cand.status}, not launched/earning"})
            continue
        if dry_run:
            booked.append({**row, "candidate": name, "outcome": "would book"})
            continue
        try:
            _book(store, ledger, row, candidate=name, actor=actor,
                  source_note="paypal sync")
            booked.append({**row, "candidate": name, "outcome": "booked"})
        except ValueError as exc:
            skipped.append({**row, "reason": str(exc)})

    return {
        "booked": booked,
        "skipped": skipped,
        "total_booked": round(sum(b["amount"] for b in booked), 2),
        "range_days": (end - start).days,
        "dry_run": dry_run,
    }
