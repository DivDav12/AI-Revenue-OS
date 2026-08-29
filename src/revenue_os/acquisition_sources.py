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

import html as _html
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_USER_AGENT = "AI-Revenue-OS/0.1 (acquisition research; contact via repo)"
_TIMEOUT = 8.0
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class AcqRecord:
    """A verbatim record of one real public post."""

    title: str
    url: str = ""
    text: str = ""
    author: str = ""
    posted_at: str = ""      # ISO 8601, "" if the API did not provide one
    platform: str = ""
    source: str = ""
    query: str = ""


def _plain(value: object) -> str:
    return _html.unescape(_TAG_RE.sub(" ", str(value or ""))).strip()


def _http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


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

    def search(self, query: str, limit: int) -> list[AcqRecord]:
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

    def search(self, query: str, limit: int) -> list[AcqRecord]:
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
    """Hacker News full-text search via the free, keyless Algolia API."""

    name = "hn-algolia"
    _URL = "https://hn.algolia.com/api/v1/search"

    def search(self, query: str, limit: int) -> list[AcqRecord]:
        params = urllib.parse.urlencode({
            "query": query, "tags": "story",
            "hitsPerPage": max(1, min(limit, 50)),
        })
        body = _http_json(f"{self._URL}?{params}")
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
    """Reddit link search via the free, keyless search.json endpoint."""

    name = "reddit"
    _URL = "https://www.reddit.com/search.json"

    def search(self, query: str, limit: int) -> list[AcqRecord]:
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


class CompositeAcqSource:
    """Fans a query out to several sources. One dead source (e.g. a 403
    from an API that now needs auth) is logged and recorded in
    `self.errors`; the others still return their results."""

    name = "both"

    def __init__(self, sources) -> None:
        self._sources = list(sources)
        self.errors: list[tuple[str, str]] = []

    def search(self, query: str, limit: int) -> list[AcqRecord]:
        out: list[AcqRecord] = []
        for src in self._sources:
            try:
                out.extend(src.search(query, limit))
            except Exception as exc:  # a dead sub-source must not kill the rest
                logger.warning("source %r failed for %r: %s",
                               src.name, query, exc)
                self.errors.append((src.name, str(exc)))
        return out


def build_acquisition_source(name: str, path=None):
    if name == "static":
        return StaticAcqSource()
    if name == "file":
        if not path:
            raise ValueError("source 'file' requires --source-path")
        return FileAcqSource(path)
    if name == "hn-algolia":
        return HNAlgoliaSource()
    if name == "reddit":
        return RedditSearchSource()
    if name == "both":
        return CompositeAcqSource([HNAlgoliaSource(), RedditSearchSource()])
    raise ValueError(
        f"unknown source: {name!r} "
        "(expected hn-algolia, reddit, both, file, or static)")
