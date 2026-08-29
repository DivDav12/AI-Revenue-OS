"""Acquisition Agent - discovers public posts where someone is explicitly
trying to get their first paying customers / clients / users.

Each discovered post becomes an AcquisitionLead: a real public thread we
*could* (a human decides) reply to, offering the Customer Launch Plan.

Hard rules baked in here:
  - Never invent a source, person, thread, or URL. Every field is copied
    verbatim from a real API record (acquisition_sources.AcqRecord) or
    computed deterministically from it. Missing author/timestamp stay "".
  - Deterministic scoring - no LLM, no cost, fully reproducible.
  - Never contacts or posts to anyone. `promo_allowed` is advisory and
    conservative; the caller must still read each community's own rules.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .agent import Agent
from .messages import Result, Task
from .store import now_iso

# --- intent model ----------------------------------------------------

# (phrase, weight, intent bucket). Matched case-insensitively as a
# substring of the normalised title+text. Order does not matter.
_PHRASES: tuple[tuple[str, int, str], ...] = (
    ("first paying customer", 3, "high"),
    ("first paying customers", 3, "high"),
    ("get my first customer", 3, "high"),
    ("find my first customer", 3, "high"),
    ("cant get my first customer", 3, "high"),
    ("can't get my first customer", 3, "high"),
    ("0 paying customers", 3, "high"),
    ("zero paying customers", 3, "high"),
    ("no paying customers", 3, "high"),
    ("no customers after launch", 3, "high"),
    ("nobody is buying", 3, "high"),
    ("no one is buying", 3, "high"),
    ("nobody buying my product", 3, "high"),
    ("first 10 customers", 2, "high"),
    ("struggling to get customers", 2, "medium"),
    ("how to find first clients", 2, "medium"),
    ("how to find my first client", 2, "medium"),
    ("first client", 2, "medium"),
    ("first clients", 2, "medium"),
    ("my first users", 2, "medium"),
    ("first users", 1, "low"),
    ("customers for my saas", 2, "medium"),
    ("get customers for my saas", 2, "medium"),
    ("how do i get customers", 1, "medium"),
    ("how to get customers", 1, "medium"),
    ("how do i get users", 1, "low"),
    ("launched but no", 2, "medium"),
    ("no traction", 1, "low"),
)

# Default search strings for `discover-opportunities` (user-facing intent).
SEARCH_QUERIES: tuple[str, ...] = (
    "how do I get my first paying customer",
    "how to find my first customers",
    "can't get my first customer",
    "how do I get customers for my SaaS",
    "0 paying customers",
    "no customers after launch",
    "how to find first clients",
    "nobody is buying my product",
    "how do I get my first users",
)

_BUCKET_RANK = {"low": 1, "medium": 2, "high": 3}
_MAX_SUMMARY = 400

# Advisory promotion policy by platform family. Conservative by design.
PROMO_POLICY: dict[str, tuple[str, str]] = {
    "hacker news": (
        "caution",
        "HN tolerates a genuinely helpful reply in a relevant thread but bans "
        "cold pitching. Only reply if you add real value first."),
    "reddit": (
        "caution",
        "Reddit self-promotion rules vary by subreddit and are often strict "
        "(e.g. the 9:1 rule, or outright bans). Read that subreddit's rules "
        "before any reply."),
}
_PROMO_DEFAULT = (
    "unknown",
    "Unknown platform - verify its self-promotion rules before replying.")

_FAKE_HOSTS = {
    "example.com", "example.org", "example.net", "localhost", "127.0.0.1",
    "test.com", "test", "invalid",
}
_REAL_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+$")

_REDDIT_PERMALINK = re.compile(r"^(/r/[^/]+/comments/[a-z0-9]+)")


# --- canonical URL (dedup key) --------------------------------------

def canonical_url(url: str) -> str:
    """Normalise a URL for de-duplication: lowercase host, drop `www.`,
    drop the fragment and tracking query params, strip a trailing slash,
    and truncate a Reddit permalink to /r/<sub>/comments/<id>."""
    url = str(url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower().split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if host in ("reddit.com", "old.reddit.com", "np.reddit.com"):
        host = "reddit.com"
        m = _REDDIT_PERMALINK.match(path.lower())
        if m:
            path = m.group(1)
    path = path.rstrip("/") or "/"
    keep = {"id"}  # e.g. news.ycombinator.com/item?id=123
    query = "&".join(sorted(
        f"{k}={v}" for k, v in parse_qsl(parts.query) if k.lower() in keep))
    out = f"{scheme}://{host}{path}"
    return f"{out}?{query}" if query else out


def _host(url: str) -> str:
    return urlsplit(str(url or "")).netloc.lower().split("@")[-1].removeprefix("www.")


# --- deterministic scoring ----------------------------------------

_NORM_RE = re.compile(r"[^a-z0-9'\s]+")


def _normalise(text: str) -> str:
    return _NORM_RE.sub(" ", str(text or "").lower())


def _recency_bonus(posted_at: str) -> int:
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - dt).days
    if age_days <= 90:
        return 12
    if age_days <= 365:
        return 6
    return 0


def score_lead(record) -> dict | None:
    """Deterministically score one raw record. Returns None when no intent
    phrase matches (i.e. it is not an acquisition opportunity)."""
    title = getattr(record, "title", "") or (record.get("title", "")
                                             if isinstance(record, dict) else "")
    text = getattr(record, "text", "") or (record.get("text", "")
                                           if isinstance(record, dict) else "")
    posted_at = getattr(record, "posted_at", "") or (
        record.get("posted_at", "") if isinstance(record, dict) else "")

    title_n = _normalise(title)
    body_n = _normalise(text)
    hay = f"{title_n} {body_n}"

    matched: list[tuple[str, int, str, bool]] = []
    for phrase, weight, bucket in _PHRASES:
        p = _normalise(phrase).strip()
        if p and p in hay:
            matched.append((phrase, weight, bucket, p in title_n))
    if not matched:
        return None

    raw = sum(w * (2 if in_title else 1) for _, w, _, in_title in matched)
    base = min(70, raw * 10)
    title_bonus = 10 if any(in_title for *_, in_title in matched) else 0
    fit_score = max(0, min(100, base + title_bonus + _recency_bonus(posted_at)))

    intent = max((b for _, _, b, _ in matched), key=lambda b: _BUCKET_RANK[b])
    # a weak overall score should not read as high intent
    if fit_score < 30:
        intent = "low"
    elif fit_score < 50 and intent == "high":
        intent = "medium"

    parts = []
    for phrase, _, _, in_title in matched:
        parts.append(f"'{phrase}'" + (" (in title)" if in_title else ""))
    match_reason = "matched " + ", ".join(parts)

    return {
        "fit_score": int(fit_score),
        "buying_intent": intent,
        "matched_phrases": tuple(m[0] for m in matched),
        "match_reason": match_reason,
    }


def _promo_for(platform: str) -> tuple[str, str]:
    p = str(platform or "").lower()
    if p.startswith("r/") or "reddit" in p:
        return PROMO_POLICY["reddit"]
    if "hacker news" in p or p in ("hn", "ycombinator"):
        return PROMO_POLICY["hacker news"]
    return _PROMO_DEFAULT


# --- the lead ------------------------------------------------------

@dataclass(frozen=True)
class AcquisitionLead:
    canonical_url: str
    url: str
    source: str
    platform: str
    title: str
    fit_score: int
    buying_intent: str
    match_reason: str
    promo_allowed: str
    promo_note: str
    problem_summary: str = ""
    author: str = ""
    posted_at: str = ""
    matched_phrases: tuple = ()
    query: str = ""
    discovered_at: str = ""

    def to_dict(self) -> dict:
        d = {
            "canonical_url": self.canonical_url,
            "url": self.url,
            "source": self.source,
            "platform": self.platform,
            "title": self.title,
            "author": self.author,
            "posted_at": self.posted_at,
            "problem_summary": self.problem_summary,
            "match_reason": self.match_reason,
            "matched_phrases": list(self.matched_phrases),
            "fit_score": self.fit_score,
            "buying_intent": self.buying_intent,
            "promo_allowed": self.promo_allowed,
            "promo_note": self.promo_note,
            "query": self.query,
            "discovered_at": self.discovered_at,
        }
        return d


def build_lead(record, score: dict) -> AcquisitionLead:
    """Assemble a lead from a raw record + its score. Only copies fields
    the record actually carries; nothing is synthesised."""
    def g(name):
        if isinstance(record, dict):
            return str(record.get(name, "") or "").strip()
        return str(getattr(record, name, "") or "").strip()

    url = g("url")
    platform = g("platform")
    promo_allowed, promo_note = _promo_for(platform)
    return AcquisitionLead(
        canonical_url=canonical_url(url),
        url=url,
        source=g("source"),
        platform=platform,
        title=g("title"),
        author=g("author"),
        posted_at=g("posted_at"),
        problem_summary=(g("text") or g("title"))[:_MAX_SUMMARY],
        match_reason=score["match_reason"],
        matched_phrases=tuple(score["matched_phrases"]),
        fit_score=int(score["fit_score"]),
        buying_intent=score["buying_intent"],
        promo_allowed=promo_allowed,
        promo_note=promo_note,
        query=g("query"),
        discovered_at=now_iso(),
    )


def qc_lead(lead: AcquisitionLead) -> list[str]:
    """Deterministic quality control. Returns a list of problems; an empty
    list means the lead passes."""
    problems: list[str] = []
    cu = lead.canonical_url
    if not cu:
        problems.append("no url")
    else:
        parts = urlsplit(cu)
        host = _host(cu)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            problems.append("url is not http(s)")
        elif host in _FAKE_HOSTS:
            problems.append(f"placeholder host {host!r}")
        elif not _REAL_HOST_RE.match(host):
            problems.append(f"not a real host {host!r}")
    if not lead.title.strip():
        problems.append("no title")
    if not lead.matched_phrases:
        problems.append("no intent phrase matched")
    if not 0 <= lead.fit_score <= 100:
        problems.append("fit_score out of range")
    if lead.buying_intent not in _BUCKET_RANK:
        problems.append("invalid buying_intent")
    return problems


# --- the agent ----------------------------------------------------

class AcquisitionAgent(Agent):
    """Turns raw records (task.payload['records']) into scored, quality-
    checked AcquisitionLeads. Deterministic; contacts no one."""

    role = "acquisition_scout"
    objective = "Find public posts asking how to get their first paying customers."
    capabilities = ("discover_acquisition",)

    def run(self, task: Task) -> Result:
        records = task.payload.get("records")
        if not isinstance(records, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload needs a list of records")
        min_score = int(task.payload.get("min_score", 0))

        by_url: dict[str, AcquisitionLead] = {}
        dropped: list[dict] = []
        considered = 0
        no_match = 0
        collapsed = 0
        for rec in records:
            considered += 1
            score = score_lead(rec)
            if score is None:
                no_match += 1
                continue
            lead = build_lead(rec, score)
            problems = qc_lead(lead)
            if problems:
                dropped.append({"title": lead.title[:80],
                                "url": lead.url, "reasons": problems})
                continue
            if lead.fit_score < min_score:
                continue
            keep = by_url.get(lead.canonical_url)
            if keep is not None:
                collapsed += 1
                if lead.fit_score <= keep.fit_score:
                    continue
            by_url[lead.canonical_url] = lead

        leads = sorted(by_url.values(), key=lambda x: x.fit_score, reverse=True)
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={
                "leads": [l.to_dict() for l in leads],
                "dropped": dropped,
                "considered": considered,
                "no_match": no_match,
                "collapsed": collapsed,
            },
        )


# --- the store ----------------------------------------------------

class AcquisitionStore:
    """One JSON list, atomically written, keyed by canonical_url."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_url: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "AcquisitionStore":
        store = cls(path)
        if not store.path.exists():
            return store
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt acquisition store {store.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"acquisition store {store.path} must be a JSON list")
        for entry in raw:
            store._by_url[str(entry["canonical_url"])] = dict(entry)
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.ranked(), indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, canonical_url: str) -> dict | None:
        return self._by_url.get(str(canonical_url))

    def all(self) -> list[dict]:
        return list(self._by_url.values())

    def ranked(self) -> list[dict]:
        return sorted(self._by_url.values(),
                      key=lambda d: d.get("fit_score", 0), reverse=True)

    def add(self, lead: dict) -> bool:
        """Insert a lead. Returns False (and keeps the existing one) if a
        lead with the same canonical_url is already stored."""
        key = str(lead.get("canonical_url", "")).strip()
        if not key:
            raise ValueError("lead has no canonical_url")
        if key in self._by_url:
            return False
        self._by_url[key] = dict(lead)
        return True
