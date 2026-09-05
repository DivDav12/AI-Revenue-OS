"""Affiliate click-tracking redirect server (spec section 6).

A minimal, real HTTP server: `GET /go/<tracking_id>` records one click
(`affiliate_links.record_click`) and 302-redirects to the link's real
external `target_url`. Nothing else is served. Same localhost-only,
no-JS, no-file-write pattern as `dashboard_server.py`.

Reaching real visitors requires a human to put this behind a public
domain / reverse proxy (TLS termination, DNS) - that is genuinely a
human infrastructure decision, not something the fleet may provision
itself (it would mean registering a domain / spending money - an
`action_class.MONEY_APPROVAL_REQUIRED` action). Until then this server
still runs correctly for local testing and for a human-operated reverse
proxy pointed at `127.0.0.1`.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..store import now_iso
from .affiliate_links import record_click

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _make_handler(data_dir: Path):

    class Handler(BaseHTTPRequestHandler):
        server_version = "RevenueOS-affiliate-tracker"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler convention
            if self.client_address[0] not in _LOOPBACK:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            path = urlsplit(self.path).path
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "go" and parts[1]:
                tracking_id = parts[1]
                channel = "" .join(self.headers.get("X-Distribution-Channel", "").split())
                out = record_click(data_dir, tracking_id=tracking_id,
                                   channel=channel, now_iso=now_iso())
                if out.get("recorded") and out.get("target_url"):
                    self.send_response(302)
                    self.send_header("Location", out["target_url"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def serve(data_dir, *, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Start the redirect server and return it (caller controls its
    lifecycle - `.shutdown()` + a background thread `.join()`, exactly
    like the existing dashboard/jarvis servers' own test fixtures)."""
    server = ThreadingHTTPServer((host, port), _make_handler(Path(data_dir)))
    return server
