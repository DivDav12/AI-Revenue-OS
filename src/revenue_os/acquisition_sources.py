"""Public search sources for the Acquisition Agent.

Each source turns one search query into a list of AcqRecord - a raw,
verbatim record of a real public post. The external-I/O risk is isolated
here, exactly like sources.py:

  - StaticAcqSource / FileAcqSource are fully offline and deterministic
    (the only ones used in tests).
  - HNAlgoliaSource and RedditSearchSource hit free, keyless public
    APIs. They are opt-in (`--source`) and never used by default in
    tests.

No source ever invents a record: a record is only emitted for a hit the
API actually returned, and a hit with no usable URL is dropped here.

Standard library only (json, urllib, html, re).
"""

from __future__ import annotations

import gzip
import html as _html
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_USER_AGENT = "AI-Revenue-OS/0.1 (acquisition research; contact via repo)"
_TIMEOUT = 10.0
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class AcqRecord:
    """A verbatim record of one real public post.

    `meta` carries source-specific structured signals (Stack Exchange
    answer counts, etc.) that the scorer factors in when present. It is
    optional and defaults to {} so every existing caller keeps working.
    """

    title: str
    url: str = ""
    text: str = ""
    author: str = ""
    posted_at: str = ""      # ISO 8601, "" if the API did not provide one
    platform: str = ""
    source: str = ""
    query: str = ""
    meta: dict = field(default_factory=dict)


def _plain(value: object) -> str:
    return _html.unescape(_TAG_RE.sub(" ", str(value or ""))).strip()


def _http_json(url: str, *, headers: dict | None = None):
    h = {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _iso(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


# What each source needs, so the CLI can group sources by tier and the
# operator can see at a glance what runs for free.
#   tier: "free" | "paid" | "authenticated"
SOURCE_REGISTRY: dict[str, dict] = {
    "hn-algolia": {"tier": "free", "auth": False,
                   "note": "Hacker News search (Algolia), keyless"},
    "stackexchange": {"tier": "free", "auth": False,
                      "note": "Stack Exchange API, keyless (~300 req/day/IP)"},
    "bluesky": {"tier": "authenticated", "auth": True,
                "note": "post search is now auth/edge gated (401/403)"},
    "reddit": {"tier": "authenticated", "auth": True,
               "note": "robots.txt Disallow: / ; JSON API 403 unauthenticated"},
    "web": {"tier": "paid", "auth": True,
            "note": "Anthropic web_search - needs API credits (ANTHROPIC_API_KEY)"},
    "static": {"tier": "free", "auth": False, "note": "built-in samples (offline)"},
    "file": {"tier": "free", "auth": False, "note": "local JSON file (offline)"},
}
# free-first default set (Reddit/Bluesky excluded - unavailable; web excluded - paid)
FREE_SOURCES = ("hn-algolia", "stackexchange")


def _epoch(since_ts) -> int | None:
    """since_ts may be an int epoch, an ISO string, or None."""
    if since_ts in (None, ""):
        return None
    try:
        return int(since_ts)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(since_ts).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return None


# --- offline sources (used by tests / --source static|file) -----------

class StaticAcqSource:
    name = "static"

    _SAMPLE = [
        {"title": "Ask HN: How do I get my first paying customer?",
         "url": "https://news.ycombinator.com/item?id=40000001",
         "text": "Launched my SaaS 3 months ago. 200 signups, 0 paying customers. "
                 "What worked for you?",
         "author": "founder_a", "posted_at": "2026-08-01T10:00:00+00:00",
         "platform": "Hacker News", "source": "static"},
        {"title": "Nobody is buying my product after launch",
         "url": "https://www.reddit.com/r/SaaS/comments/abc123/nobody_is_buying/",
         "text": "Two weeks post-launch, lots of traffic, no sales. How did you "
                 "find your first clients?",
         "author": "u/dev_b", "posted_at": "2026-08-20T12:00:00+00:00",
         "platform": "r/SaaS", "source": "static"},
        {"title": "Show HN: My new time-tracking app",
         "url": "https://news.ycombinator.com/item?id=40000009",
         "text": "Built this over the weekend, feedback welcome.",
         "author": "founder_c", "posted_at": "2026-08-25T09:00:00+00:00",
         "platform": "Hacker News", "source": "static"},
    ]

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        return [
            AcqRecord(query=query, **{k: d.get(k, "") for k in (
                "title", "url", "text", "author", "posted_at", "platform",
                "source")})
            for d in self._SAMPLE[: max(0, limit)]
        ]


class FileAcqSource:
    """Reads a JSON list of record dicts from disk. Offline, deterministic."""

    name = "file"

    def __init__(self, path) -> None:
        from pathlib import Path

        self.path = Path(path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"record file not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed record file {self.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"record file {self.path} must contain a JSON list")
        self._records = raw

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        out = []
        for d in self._records[: max(0, limit)]:
            if not isinstance(d, dict):
                continue
            out.append(AcqRecord(
                title=str(d.get("title", "")).strip(),
                url=str(d.get("url", "")).strip(),
                text=str(d.get("text", "")).strip(),
                author=str(d.get("author", "")).strip(),
                posted_at=str(d.get("posted_at", "")).strip(),
                platform=str(d.get("platform", "")).strip(),
                source="file",
                query=query,
            ))
        return out


# --- real sources (opt-in, keyless) ----------------------------------

class HNAlgoliaSource:
    """Hacker News full-text search via the free, keyless Algolia API.

    When `since_ts` is given, only stories created after it are fetched
    (numericFilters), so a `--max-age-days` run gets current threads
    instead of decade-old articles."""

    name = "hn-algolia"
    _SEARCH = "https://hn.algolia.com/api/v1/search"
    _BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        epoch = _epoch(since_ts)
        # With a freshness window: sort by DATE and include Ask HN, so real
        # recent questions surface instead of being buried under Show HN.
        # Without one: relevance ranking over all stories.
        if epoch is not None:
            url = self._BY_DATE
            params = {"query": query, "tags": "(story,ask_hn)",
                      "numericFilters": f"created_at_i>{epoch}",
                      "hitsPerPage": max(1, min(limit, 50))}
        else:
            url = self._SEARCH
            params = {"query": query, "tags": "story",
                      "hitsPerPage": max(1, min(limit, 50))}
        body = _http_json(f"{url}?{urllib.parse.urlencode(params)}")
        out: list[AcqRecord] = []
        for hit in body.get("hits", []) or []:
            oid = str(hit.get("objectID", "")).strip()
            if not oid:
                continue
            title = _plain(hit.get("title"))
            if not title:
                continue
            out.append(AcqRecord(
                title=title,
                # the discussion (where a reply would go) is the item page
                url=f"https://news.ycombinator.com/item?id={oid}",
                text=_plain(hit.get("story_text")),
                author=str(hit.get("author", "")).strip(),
                posted_at=str(hit.get("created_at", "")).strip(),
                platform="Hacker News",
                source=self.name,
                query=query,
            ))
        return out


class RedditSearchSource:
    """Reddit link search via the keyless search.json endpoint.

    NOTE: Reddit now returns HTTP 403 for unauthenticated non-browser
    requests, so this source is effectively unavailable. It is kept for
    completeness and stays failure-isolated (CompositeAcqSource /
    workflow record the error and HN still works). A future OAuth-based
    source would plug in here with the same `search(query, limit,
    since_ts=)` signature - do not add rule-violating workarounds."""

    name = "reddit"
    _URL = "https://www.reddit.com/search.json"

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        params = urllib.parse.urlencode({
            "q": query, "sort": "new", "type": "link",
            "limit": max(1, min(limit, 50)),
        })
        body = _http_json(f"{self._URL}?{params}")
        out: list[AcqRecord] = []
        for child in (body.get("data", {}) or {}).get("children", []) or []:
            d = child.get("data", {}) or {}
            permalink = str(d.get("permalink", "")).strip()
            title = _plain(d.get("title"))
            if not permalink or not title:
                continue
            author = str(d.get("author", "")).strip()
            if author in ("", "[deleted]"):
                author = ""
            posted_at = ""
            try:
                posted_at = datetime.fromtimestamp(
                    float(d.get("created_utc")), tz=timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
            sub = str(d.get("subreddit", "")).strip()
            out.append(AcqRecord(
                title=title,
                url=f"https://www.reddit.com{permalink}",
                text=_plain(d.get("selftext")),
                author=author,
                posted_at=posted_at,
                platform=f"r/{sub}" if sub else "Reddit",
                source=self.name,
                query=query,
            ))
        return out


class StackExchangeSource:
    """Stack Exchange questions via the documented, public, keyless API
    (https://api.stackexchange.com/, ~300 req/day/IP without a key).

    Queries several sites (default: freelancing, webmasters - there is no
    live "startups" SE site) - every result is a question someone posted
    because they were stuck, so the signal is clean. `since_ts` ->
    `fromdate`; results are sorted newest-first. `meta` carries answered /
    answer_count / score / accepted so the scorer can down-rank
    already-solved problems.

    No HTML scraping: the API returns everything we need (title, link,
    creation_date, owner, body, answer stats).
    """

    name = "stackexchange"
    _URL = "https://api.stackexchange.com/2.3/search/advanced"
    _DEFAULT_SITES = ("freelancing", "webmasters")

    def __init__(self, sites=None) -> None:
        self.sites = tuple(sites) if sites else self._DEFAULT_SITES
        self.requests = 0

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        per_site = max(1, min(limit, 30))
        epoch = _epoch(since_ts)
        out: list[AcqRecord] = []
        for site in self.sites:
            params = {
                "site": site, "q": query, "sort": "creation", "order": "desc",
                "pagesize": per_site, "filter": "withbody",
            }
            if epoch is not None:
                params["fromdate"] = epoch
            self.requests += 1
            body = _http_json(f"{self._URL}?{urllib.parse.urlencode(params)}")
            for item in body.get("items", []) or []:
                link = str(item.get("link", "")).strip()
                title = _plain(item.get("title"))
                if not link or not title:
                    continue
                answer_count = int(item.get("answer_count", 0) or 0)
                accepted = bool(item.get("accepted_answer_id"))
                out.append(AcqRecord(
                    title=title,
                    url=link,
                    text=_plain(item.get("body"))[:1500],
                    author=str((item.get("owner") or {}).get("display_name", "")).strip(),
                    posted_at=_iso(item.get("creation_date")),
                    platform=f"{site}.stackexchange.com",
                    source=self.name,
                    query=query,
                    meta={
                        "answered": bool(item.get("is_answered")),
                        "answer_count": answer_count,
                        "score": int(item.get("score", 0) or 0),
                        "accepted": accepted,
                    },
                ))
        return out


_BSKY_URI = re.compile(r"^at://([^/]+)/app\.bsky\.feed\.post/([A-Za-z0-9]+)$")


class BlueskySource:
    """Bluesky post search via the AppView
    (https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts).

    `sort=latest` + `since` (ISO) give real freshness; AT URIs are
    converted to canonical bsky.app URLs. This source only ever READS
    public posts - it never authenticates, posts, or interacts.

    NOTE: Bluesky now gates the `searchPosts` endpoint (401 'Authentication
    Required' on bsky.social; 403 from many server IPs on the public
    AppView). When that happens this source stays failure-isolated - the
    other sources still run and `sources_status` records it - exactly like
    Reddit. We do NOT add authentication or a scraping workaround; the
    `web` source reaches Bluesky threads through indexed search instead.
    """

    name = "bluesky"
    _URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        params = {"q": query, "sort": "latest", "limit": max(1, min(limit, 100))}
        epoch = _epoch(since_ts)
        if epoch is not None:
            params["since"] = _iso(epoch)
        body = _http_json(f"{self._URL}?{urllib.parse.urlencode(params)}")
        out: list[AcqRecord] = []
        for post in body.get("posts", []) or []:
            m = _BSKY_URI.match(str(post.get("uri", "")))
            author = post.get("author") or {}
            handle = str(author.get("handle", "")).strip()
            if not m or not handle:
                continue
            rkey = m.group(2)
            record = post.get("record") or {}
            text = _plain(record.get("text"))
            if not text:
                continue
            out.append(AcqRecord(
                title=text[:120],
                url=f"https://bsky.app/profile/{handle}/post/{rkey}",
                text=text,
                author=str(author.get("displayName") or handle).strip(),
                posted_at=str(record.get("createdAt", "")).strip(),
                platform="Bluesky",
                source=self.name,
                query=query,
                meta={"reply_count": int(post.get("replyCount", 0) or 0),
                      "like_count": int(post.get("likeCount", 0) or 0)},
            ))
        return out


class CompositeAcqSource:
    """Fans a query out to several sources. One dead source (e.g. a 403
    from an API that now needs auth) is logged and recorded in
    `self.errors`; the others still return their results."""

    name = "both"

    def __init__(self, sources) -> None:
        self._sources = list(sources)
        self.errors: list[tuple[str, str]] = []

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        out: list[AcqRecord] = []
        for src in self._sources:
            try:
                out.extend(src.search(query, limit, since_ts=since_ts))
            except Exception as exc:  # a dead sub-source must not kill the rest
                logger.warning("source %r failed for %r: %s",
                               src.name, query, exc)
                self.errors.append((src.name, str(exc)))
        return out


_SIMPLE = {
    "static": StaticAcqSource,
    "hn-algolia": HNAlgoliaSource,
    "reddit": RedditSearchSource,
    "stackexchange": StackExchangeSource,
    "bluesky": BlueskySource,
}


def _one_source(name: str, path=None, web_source=None):
    if name == "file":
        if not path:
            raise ValueError("source 'file' requires --source-path")
        return FileAcqSource(path)
    if name == "web":
        if web_source is None:
            raise ValueError("source 'web' must be built with a client (see CLI)")
        return web_source
    if name in _SIMPLE:
        return _SIMPLE[name]()
    raise ValueError(
        f"unknown source: {name!r} (expected one of "
        f"{', '.join(sorted(list(_SIMPLE) + ['file', 'web', 'free', 'all', 'both']))})")


def build_acquisition_source(names, path=None, web_source=None):
    """Build one source or a CompositeAcqSource from a list of names.

    `free` -> hn-algolia + stackexchange (keyless, no credentials).
    `all`  -> free + web.  `both` is a back-compat alias for `free`.
    (Reddit and Bluesky are NOT in `free` - both are currently
    unavailable without auth; select them explicitly to try anyway.)
    A single name returns that source directly; several return a
    CompositeAcqSource (failure-isolated).
    """
    if isinstance(names, str):
        names = [names]
    expanded: list[str] = []
    for n in names:
        if n in ("free", "both"):
            expanded += list(FREE_SOURCES)
        elif n == "all":
            expanded += list(FREE_SOURCES) + ["web"]
        else:
            expanded.append(n)
    seen: list[str] = []
    for n in expanded:
        if n not in seen:
            seen.append(n)
    if ("file" in seen or "static" in seen) and len(seen) > 1:
        raise ValueError("'file' / 'static' cannot be combined with other sources")
    built = [_one_source(n, path=path, web_source=web_source) for n in seen]
    return built[0] if len(built) == 1 else CompositeAcqSource(built)
