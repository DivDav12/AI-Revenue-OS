"""Localhost-only interactive dashboard server (Phase 5).

Serves the live command-center HTML and accepts POSTs for exactly the
five existing human gates. Every action is routed to the same tested
domain function the CLI uses - no lifecycle or financial logic lives
here. No JavaScript, no file writes from the browser, no remote access.

Routes:
  GET  /         -> the live dashboard (interactive forms)
  POST /action   -> one allowlisted human gate, then 303 -> /
  anything else  -> 404
"""

from __future__ import annotations

import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .approval import record_decision
from .revenue import mark_launched, record_payment
from .store import CandidateStore
from .revenue import RevenueLedger
from .spend import SpendLedger
from .validation import record_validation_outcome

_ACTIONS = ("approve", "reject", "outcome", "launch", "payment")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_MAX_BODY = 64 * 1024


def _load(data_dir: Path):
    return (
        CandidateStore.load(data_dir / "candidates.json"),
        RevenueLedger.load(data_dir / "revenue.json"),
        SpendLedger.load(data_dir / "spend.json"),
    )


def apply_action(data_dir: Path, actor: str, form: dict) -> str:
    """Run one allowlisted human gate through the existing domain
    functions. Returns a flash message. Never raises - domain errors
    become an 'error: ...' string and nothing is written."""
    action = (form.get("action") or [""])[0]
    name = (form.get("name") or [""])[0]
    if action not in _ACTIONS:
        return f"error: unknown action {action!r}"
    if not name:
        return "error: missing candidate name"
    try:
        store, revenue_ledger, _ = _load(data_dir)
        if action == "approve":
            out = record_decision(store, name, "approve", approver=actor)
        elif action == "reject":
            out = record_decision(store, name, "reject", approver=actor)
        elif action == "outcome":
            result = (form.get("result") or [""])[0]
            if result not in ("validated", "rejected"):
                return "error: outcome must be validated or rejected"
            metric = (form.get("metric") or [""])[0]
            out = record_validation_outcome(
                store, name, result, metric_value=metric, actor=actor
            )
        elif action == "launch":
            out = mark_launched(store, name, actor=actor)
        else:  # payment
            raw = (form.get("amount") or [""])[0]
            try:
                amount = float(raw)
            except (TypeError, ValueError):
                return f"error: invalid amount {raw!r}"
            out = record_payment(store, revenue_ledger, name, amount, actor=actor)
        return f"ok: {out.name} -> {out.status}"
    except (ValueError, FileNotFoundError) as exc:
        return f"error: {exc}"


def _make_handler(data_dir: Path, actor: str, csrf: str, render, allowed_origins):

    class Handler(BaseHTTPRequestHandler):
        server_version = "RevenueOS-dash"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep stdout clean
            pass

        def _send(self, code: int, body: bytes = b"", ctype="text/html; charset=utf-8",
                  extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}):
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _same_origin(self) -> bool:
            for h in ("Origin", "Referer"):
                val = self.headers.get(h)
                if val:
                    host = urlsplit(val).netloc.split("@")[-1].lower()
                    if host not in allowed_origins:
                        return False
            return True

        def do_GET(self):
            if urlsplit(self.path).path != "/":
                self._send(404, b"not found")
                return
            flash = self.server._flash
            self.server._flash = None
            html = render(flash=flash, csrf=csrf).encode("utf-8")
            self._send(200, html)

        def do_POST(self):
            if urlsplit(self.path).path != "/action":
                self._send(404, b"not found")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0 or length > _MAX_BODY:
                self._send(400, b"bad request")
                return
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            if (form.get("csrf") or [""])[0] != csrf:
                self._send(403, b"bad csrf token")
                return
            if not self._same_origin():
                self._send(403, b"cross-origin blocked")
                return
            self.server._flash = apply_action(data_dir, actor, form)
            self._send(303, b"", extra=[("Location", "/")])

    return Handler


def serve(data_dir, host: str = "127.0.0.1", port: int = 8787,
          actor: str = "dashboard") -> None:
    data_dir = Path(data_dir)
    if host not in _LOOPBACK:
        raise ValueError(
            f"refusing to bind to non-loopback host {host!r}; "
            "the dashboard server is localhost-only"
        )
    csrf = secrets.token_urlsafe(24)

    def _render(flash=None, csrf=None):
        from .cli import build_dashboard_html
        return build_dashboard_html(
            data_dir, interactive=True, flash=flash, csrf=csrf,
        )

    allowed_origins = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    handler = _make_handler(data_dir, actor, csrf, _render, allowed_origins)
    bind = "127.0.0.1" if host == "localhost" else host
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd._flash = None
    print(f"dashboard: http://localhost:{port}/  (actor={actor}, Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
