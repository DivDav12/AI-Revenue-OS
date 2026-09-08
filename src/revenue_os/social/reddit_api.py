"""Reddit access through the official OAuth2 API - standard library only.

Read path (search public posts): app-only OAuth token
(`grant_type=client_credentials`) against a registered app. Enough to
discover real buyer-intent threads.

Write path (submit a comment): requires a USER-context token
(`grant_type=password` for a "script" app, or a stored refresh token).
`submit_comment()` refuses unless such a token is available AND the caller
passes `i_have_read_the_rules=True` - the flow layer only ever calls it
after `social/compliance.py` cleared an auto-post, which for Reddit
currently never happens (HUMAN_REQUIRED), so in practice the fleet drafts
and a human posts.

Fail closed: missing credentials -> `available = False`, every call
raises `RedditUnavailable`. No scraping, no `.json` endpoint workaround,
no browser, no CAPTCHA/login bypass. A required, descriptive User-Agent
is sent on every request (Reddit API rule).

Env:
  REDDIT_CLIENT_ID       registered app id            (required)
  REDDIT_CLIENT_SECRET   registered app secret        (required)
  REDDIT_USER_AGENT      e.g. "ai-revenue-os/0.1 by u/<name>"  (required)
  REDDIT_USERNAME        only for the write path (script app)
  REDDIT_PASSWORD        only for the write path (script app)
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

from ..acquisition_sources import AcqRecord, _plain

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API = "https://oauth.reddit.com"
_TIMEOUT = 12.0


class RedditUnavailable(RuntimeError):
    """Reddit API cannot be used (missing credentials / auth failed).
    Never carries a secret in its message."""


@dataclass
class RedditConfig:
    client_id: str
    client_secret: str
    user_agent: str
    username: str = ""
    password: str = ""

    @classmethod
    def from_env(cls, environ=None) -> "RedditConfig | None":
        env = environ if environ is not None else os.environ
        cid = (env.get("REDDIT_CLIENT_ID") or "").strip()
        sec = (env.get("REDDIT_CLIENT_SECRET") or "").strip()
        ua = (env.get("REDDIT_USER_AGENT") or "").strip()
        if not (cid and sec and ua):
            return None
        return cls(client_id=cid, client_secret=sec, user_agent=ua,
                   username=(env.get("REDDIT_USERNAME") or "").strip(),
                   password=(env.get("REDDIT_PASSWORD") or "").strip())


class RedditClient:
    def __init__(self, config: RedditConfig | None = None, *, environ=None,
                 opener=None) -> None:
        self._config = config or RedditConfig.from_env(environ)
        self._opener = opener or urllib.request.build_opener()
        self._token = ""
        self._token_exp = 0.0
        self._token_scope = ""   # "read" | "user"

    @property
    def available(self) -> bool:
        return self._config is not None

    @property
    def can_write(self) -> bool:
        return bool(self._config and self._config.username and self._config.password)

    def _require(self) -> RedditConfig:
        if self._config is None:
            raise RedditUnavailable(
                "Reddit API not configured - set REDDIT_CLIENT_ID, "
                "REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT (register an app "
                "at reddit.com/prefs/apps)")
        return self._config

    # --- auth ------------------------------------------------------------
    def _fetch_token(self, *, want_user: bool) -> None:
        cfg = self._require()
        if want_user and not self.can_write:
            raise RedditUnavailable(
                "Reddit write access needs a user-context token - set "
                "REDDIT_USERNAME + REDDIT_PASSWORD on a 'script' app, or wire "
                "a stored refresh token")
        if want_user:
            data = {"grant_type": "password", "username": cfg.username,
                    "password": cfg.password}
            scope = "user"
        else:
            data = {"grant_type": "client_credentials"}
            scope = "read"
        body = urllib.parse.urlencode(data).encode("utf-8")
        basic = base64.b64encode(
            f"{cfg.client_id}:{cfg.client_secret}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(_TOKEN_URL, data=body, method="POST", headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": cfg.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with self._opener.open(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RedditUnavailable(
                f"Reddit token request failed (HTTP {exc.code}) - check the "
                "app credentials / account") from None
        except urllib.error.URLError as exc:
            raise RedditUnavailable(f"Reddit unreachable: {exc.reason}") from None
        tok = payload.get("access_token")
        if not tok:
            raise RedditUnavailable("Reddit token response had no access_token")
        self._token = tok
        self._token_exp = time.monotonic() + float(payload.get("expires_in", 3600)) - 60
        self._token_scope = scope

    def _token_for(self, *, want_user: bool) -> str:
        need = "user" if want_user else "read"
        if (not self._token or time.monotonic() >= self._token_exp
                or (want_user and self._token_scope != "user")):
            self._fetch_token(want_user=want_user)
        return self._token

    def _get(self, path: str, params: dict, *, want_user: bool = False) -> dict:
        cfg = self._require()
        token = self._token_for(want_user=want_user)
        url = f"{_API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"bearer {token}", "User-Agent": cfg.user_agent})
        try:
            with self._opener.open(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RedditUnavailable("Reddit rate limit (HTTP 429) - back off") from None
            raise RedditUnavailable(f"Reddit API GET {path} -> HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise RedditUnavailable(f"Reddit unreachable: {exc.reason}") from None

    # --- read: search --------------------------------------------------
    def search(self, query: str, *, subreddit: str = "", limit: int = 10,
               sort: str = "new", time_filter: str = "month") -> list[AcqRecord]:
        """Search real posts. `subreddit=""` -> site-wide search."""
        limit = max(1, min(int(limit), 25))
        params = {"q": query, "limit": limit, "sort": sort, "t": time_filter,
                  "type": "link", "raw_json": 1}
        if subreddit:
            params["restrict_sr"] = "true"
            path = f"/r/{subreddit}/search"
        else:
            path = "/search"
        body = self._get(path, params)
        out: list[AcqRecord] = []
        for child in (body.get("data", {}) or {}).get("children", []) or []:
            d = child.get("data", {}) or {}
            perm = str(d.get("permalink", "")).strip()
            title = _plain(d.get("title"))
            if not perm or not title:
                continue
            author = str(d.get("author", "")).strip()
            if author in ("", "[deleted]", "[removed]"):
                author = ""
            posted_at = ""
            try:
                from datetime import datetime, timezone
                posted_at = datetime.fromtimestamp(
                    float(d.get("created_utc")), tz=timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
            sub = str(d.get("subreddit", "")).strip()
            out.append(AcqRecord(
                title=title, url=f"https://www.reddit.com{perm}",
                text=_plain(d.get("selftext"))[:2000], author=author,
                posted_at=posted_at, platform=f"r/{sub}" if sub else "reddit",
                source="reddit", query=query,
                meta={"fullname": d.get("name", ""), "subreddit": sub,
                      "num_comments": d.get("num_comments", 0),
                      "over_18": bool(d.get("over_18")),
                      "link_flair_text": d.get("link_flair_text") or ""}))
        return out

    # --- write: comment (guarded, rarely used) ------------------------
    def submit_comment(self, *, thing_fullname: str, text: str,
                       i_have_read_the_rules: bool = False) -> dict:
        if not i_have_read_the_rules:
            raise RedditUnavailable(
                "submit_comment refused - caller must pass "
                "i_have_read_the_rules=True (compliance layer clears this)")
        cfg = self._require()
        token = self._token_for(want_user=True)
        body = urllib.parse.urlencode(
            {"api_type": "json", "thing_id": thing_fullname, "text": text}
        ).encode("utf-8")
        req = urllib.request.Request(f"{_API}/api/comment", data=body, method="POST",
                                     headers={"Authorization": f"bearer {token}",
                                              "User-Agent": cfg.user_agent,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._opener.open(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RedditUnavailable(f"Reddit comment POST -> HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise RedditUnavailable(f"Reddit unreachable: {exc.reason}") from None
        errs = (((payload or {}).get("json") or {}).get("errors") or [])
        if errs:
            raise RedditUnavailable(f"Reddit rejected the comment: {errs}")
        things = (((payload or {}).get("json") or {}).get("data") or {}).get("things") or []
        url = ""
        if things:
            cdata = (things[0].get("data") or {})
            pl = cdata.get("permalink") or ""
            url = f"https://www.reddit.com{pl}" if pl else ""
        return {"posted": True, "url": url, "raw": payload}
