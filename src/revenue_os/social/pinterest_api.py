"""Pinterest access through the official API v5 - standard library only.

Auth: OAuth2 with a stored refresh token. The one-time
authorization-code exchange (which needs an interactive login + redirect)
is a human step; after that the fleet refreshes its own access token.

  PINTEREST_CLIENT_ID       app id                       (required)
  PINTEREST_CLIENT_SECRET   app secret                   (required)
  PINTEREST_REFRESH_TOKEN   from the one-time OAuth grant (or stored in
                            data/oauth_tokens.json by `social-pinterest-connect`)

Fail closed: no credentials / no refresh token -> `available = False`.
No scraping, no anti-bot workaround, no fake impressions. A create only
happens through the documented POST /v5/pins endpoint on a business
account with an approved app.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import oauth_store

_API = "https://api.pinterest.com/v5"
_TOKEN_URL = f"{_API}/oauth/token"
_TIMEOUT = 15.0
PLATFORM = "pinterest"


class PinterestUnavailable(RuntimeError):
    """Pinterest API cannot be used. Never carries a secret."""


@dataclass
class PinterestConfig:
    client_id: str
    client_secret: str
    refresh_token: str = ""

    @classmethod
    def resolve(cls, data_dir=None, environ=None) -> "PinterestConfig | None":
        env = environ if environ is not None else os.environ
        cid = (env.get("PINTEREST_CLIENT_ID") or "").strip()
        sec = (env.get("PINTEREST_CLIENT_SECRET") or "").strip()
        if not (cid and sec):
            return None
        rt = (env.get("PINTEREST_REFRESH_TOKEN") or "").strip()
        if not rt and data_dir is not None:
            rt = str(oauth_store.get(data_dir, PLATFORM).get("refresh_token") or "")
        return cls(client_id=cid, client_secret=sec, refresh_token=rt)


class PinterestClient:
    def __init__(self, config: PinterestConfig | None = None, *, data_dir=None,
                 environ=None, opener=None) -> None:
        self._data_dir = data_dir
        self._config = config or PinterestConfig.resolve(data_dir, environ)
        self._opener = opener or urllib.request.build_opener()
        self._token = ""
        self._token_exp = 0.0
        if data_dir is not None:
            row = oauth_store.get(data_dir, PLATFORM)
            if oauth_store.access_token_valid(row):
                self._token = row["access_token"]
                self._token_exp = float(row["access_token_expires_at"])

    @property
    def available(self) -> bool:
        return self._config is not None and bool(self._config.refresh_token)

    def _require(self) -> PinterestConfig:
        if self._config is None:
            raise PinterestUnavailable(
                "Pinterest API not configured - set PINTEREST_CLIENT_ID and "
                "PINTEREST_CLIENT_SECRET (create an app at "
                "developers.pinterest.com)")
        if not self._config.refresh_token:
            raise PinterestUnavailable(
                "Pinterest refresh token missing - run the one-time OAuth "
                "grant (see `revenue_os social-pinterest-connect --help`)")
        return self._config

    def _refresh(self) -> None:
        cfg = self._require()
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": cfg.refresh_token,
        }).encode("utf-8")
        basic = base64.b64encode(
            f"{cfg.client_id}:{cfg.client_secret}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(_TOKEN_URL, data=body, method="POST", headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with self._opener.open(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise PinterestUnavailable(
                f"Pinterest token refresh failed (HTTP {exc.code}) - the "
                "refresh token may be expired; re-run the OAuth grant") from None
        except urllib.error.URLError as exc:
            raise PinterestUnavailable(f"Pinterest unreachable: {exc.reason}") from None
        tok = payload.get("access_token")
        if not tok:
            raise PinterestUnavailable("Pinterest token response had no access_token")
        self._token = tok
        self._token_exp = time.monotonic() + float(payload.get("expires_in", 3600)) - 60
        if self._data_dir is not None:
            oauth_store.put(self._data_dir, PLATFORM, access_token=tok,
                            expires_in=float(payload.get("expires_in", 3600)),
                            scope=payload.get("scope"))
            if payload.get("refresh_token"):
                oauth_store.put(self._data_dir, PLATFORM,
                                refresh_token=payload["refresh_token"])

    def _token_now(self) -> str:
        if not self._token or time.monotonic() >= self._token_exp:
            self._refresh()
        return self._token

    def _call(self, method: str, path: str, *, body: dict | None = None,
              params: dict | None = None) -> dict:
        token = self._token_now()
        url = f"{_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        try:
            with self._opener.open(req, timeout=_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise PinterestUnavailable(
                f"Pinterest {method} {path} -> HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise PinterestUnavailable(f"Pinterest unreachable: {exc.reason}") from None

    # --- boards ---------------------------------------------------------
    def list_boards(self) -> list[dict]:
        out = self._call("GET", "/boards", params={"page_size": 25})
        return list(out.get("items", []) or [])

    def resolve_board_id(self, name_or_id: str) -> str:
        """Accept a board id as-is, or resolve a board by (case-insensitive)
        name. Empty if not found."""
        name_or_id = (name_or_id or "").strip()
        if not name_or_id:
            return ""
        for b in self.list_boards():
            if str(b.get("id")) == name_or_id:
                return name_or_id
        lower = name_or_id.lower()
        for b in self.list_boards():
            if str(b.get("name", "")).strip().lower() == lower:
                return str(b.get("id"))
        return ""

    # --- create --------------------------------------------------------
    def create_pin(self, *, board_id: str, title: str, description: str,
                   link: str, alt_text: str = "", image_url: str = "",
                   image_base64: str = "", i_have_read_the_rules: bool = False) -> dict:
        if not i_have_read_the_rules:
            raise PinterestUnavailable(
                "create_pin refused - caller must pass i_have_read_the_rules="
                "True (the compliance layer clears this)")
        if not board_id:
            raise PinterestUnavailable("create_pin needs a board_id")
        if image_url:
            media = {"source_type": "image_url", "url": image_url}
        elif image_base64:
            media = {"source_type": "image_base64", "content_type": "image/png",
                     "data": image_base64}
        else:
            raise PinterestUnavailable(
                "create_pin needs a creative - image_url or image_base64")
        body = {
            "board_id": board_id, "title": title[:100],
            "description": description[:500], "alt_text": alt_text[:500],
            "link": link, "media_source": media,
        }
        out = self._call("POST", "/pins", body=body)
        pid = out.get("id", "")
        return {"created": bool(pid), "pin_id": pid,
                "url": f"https://www.pinterest.com/pin/{pid}/" if pid else "",
                "raw": out}

    # --- measurement --------------------------------------------------
    def pin_analytics(self, pin_id: str, *, start_date: str, end_date: str,
                      metrics: str = "IMPRESSION,PIN_CLICK,OUTBOUND_CLICK") -> dict:
        return self._call("GET", f"/pins/{pin_id}/analytics", params={
            "start_date": start_date, "end_date": end_date,
            "metric_types": metrics})
