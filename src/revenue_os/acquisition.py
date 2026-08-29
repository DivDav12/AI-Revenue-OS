"""Acquisition Agent - finds CURRENT, real public posts from founders who
are actively struggling to get their first paying customers, i.e. people
the EUR 29.90 Customer Launch Plan could actually help right now.

Hard rules baked in here:
  - Never invent a source, person, thread, URL, or timestamp. Every field
    is copied verbatim from a real API record (acquisition_sources
    .AcqRecord) or computed deterministically from it. A missing
    posted_at is marked `unknown_age`, never fabricated.
  - Deterministic scoring by default - no LLM, no cost, reproducible.
    An optional LLM relevance pass (acquisition_llm.py) can refine it.
  - The system only FINDS and RANKS opportunities. It never posts,
    messages, emails, DMs, or contacts anyone. `promo_allowed` is an
    advisory hint; a human must still read each community's own rules.

Standard library only.

--------------------------------------------------------------------
SCORING MODEL (deterministic)
--------------------------------------------------------------------
`classify(title, text)` scans four signal groups. A signal in the TITLE
counts far more than the same signal in the body.

  POSITIVE
    ASK           - the poster is asking for help ("how do I get...",
                    "struggling to get customers", "can't get customers")
    FIRST_CUSTOMER- the exact ICP ("first paying customer", "0 customers",
                    "no users", "first sale")
    SELF_SITUATION- describing their own build ("my SaaS", "just launched",
                    "I built")
    QUESTION      - title is a question / an "Ask HN" post

  NEGATIVE
    STORY         - retrospective success / case study ("how I got 1,000
                    customers", "from 0 to 10k", "here's how we ...")
    MISC          - tutorial / guide / announcement / news / "someone
                    should build" / pure idea posts

  relevance_score = clamp(0,100,
        ask + first_customer + self_situation + question_bonus
      - story_penalty - misc_penalty )

  prospect_type (from the same signals):
      active_problem  - ASK in title + (FIRST_CUSTOMER or SELF_SITUATION)
                        + relevance >= 55
      seeking_advice  - ASK present + relevance >= 35
      founder_building- SELF_SITUATION, no ASK, 20 <= relevance <= 50
      success_story   - strong STORY signal
      educational     - strong MISC signal, no ASK
      irrelevant      - relevance < 15
      unknown         - anything left

  buying_intent:
      high   - active_problem and relevance >= 65
      medium - active_problem/seeking_advice and relevance >= 40
      low    - otherwise

`score_lead` returns None only when NO positive signal fired at all.

--------------------------------------------------------------------
RECENCY + FINAL SCORE
--------------------------------------------------------------------
  age_bucket: recent (<=7d) / aging (<=30d) / old (>30d) / unknown

  recency_factor:  3d->1.0  7d->0.9  14d->0.75  30d->0.55  90d->0.30
                   365d->0.12  older->0.04   unknown->0.35
  if age_days is not None and age_days > max_age_days:
      recency_factor = min(recency_factor, 0.10)   # explicit cliff

  problem_factor:  active_problem 1.0 / seeking_advice 0.8 /
                   founder_building 0.55 / else 0.35
  intent_factor:   high 1.0 / medium 0.85 / low 0.60

  final_score = round(relevance_score * recency_factor
                      * problem_factor * intent_factor)   (clamped 0-100)

So a 2-day-old genuine "how do I get my first customer" (relevance ~80,
factors 1.0/1.0/1.0) scores ~80, while a 10-year-old perfectly-worded
case study (relevance ~90, recency 0.04, problem 0.35) scores ~1.
"""

from __future__ import annotations

import hashlib
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

# --- signal tables (phrase, weight) --------------------------------

_ASK = (
    ("how do i get customer", 4), ("how do i get client", 4),
    ("how do i get user", 4), ("how do i get my first", 5),
    ("how do i find customer", 4), ("how do i find client", 4),
    ("how do i find user", 4), ("how to get customer", 3),
    ("how to get client", 3), ("how to get user", 3),
    ("how to find customer", 3), ("how to find client", 3),
    ("how to find first", 4), ("how can i get customer", 4),
    ("how can i get user", 4), ("how can i acquire", 4),
    ("how do i acquire", 4), ("how to acquire", 3),
    ("how do i sell", 4), ("how to sell", 3),
    ("how do i get my first sale", 5), ("how do i get sales", 4),
    ("need customers", 4), ("need users", 3), ("need clients", 4),
    ("need help getting", 5), ("need help finding", 4),
    ("struggling to get", 5), ("struggling to find", 4),
    ("struggling with customer", 5), ("struggling with acquisition", 5),
    ("can't get customer", 5), ("cant get customer", 5),
    ("can't get client", 5), ("cant get client", 5),
    ("can't get user", 4), ("cant get user", 4),
    ("can't find customer", 4), ("cant find customer", 4),
    ("having trouble getting", 5), ("having trouble finding", 4),
    ("trouble getting customer", 5), ("trouble finding customer", 4),
    ("where do i find customer", 4), ("where to find customer", 3),
    ("advice on getting", 4), ("advice on customer acquisition", 5),
    ("help me get", 4), ("help getting customer", 5),
    ("looking for my first", 5), ("looking for first customer", 5),
    ("customer acquisition problem", 5), ("no idea how to get", 5),
    ("stuck on customer", 4), ("stuck getting", 4),
    ("best way to get customer", 4), ("best way to get user", 4),
    ("best way to find customer", 4), ("what's the best way to get", 3),
    ("whats the best way to get", 3), ("any tips for getting", 4),
    ("tips for getting customer", 4), ("tips on getting customer", 4),
    ("any advice on getting", 4), ("advice for getting customer", 4),
    ("how should i get", 4), ("should i promote", 3),
    ("how do i market", 3), ("how to market my", 3),
    ("what am i doing wrong", 4), ("what should i do to get", 4),
    ("how do i reach customer", 4), ("how to reach customer", 3),
)

_FIRST_CUSTOMER = (
    ("first paying customer", 4), ("first paying client", 4),
    ("first customer", 3), ("first client", 3), ("first user", 2),
    ("first sale", 3), ("first 10 customer", 3), ("first 100 customer", 2),
    ("0 customer", 3), ("zero customer", 3), ("no customer", 3),
    ("0 paying customer", 4), ("zero paying customer", 4),
    ("no paying customer", 4), ("0 user", 2), ("no user", 2),
    ("no sale", 3), ("no traction", 2), ("no revenue", 2),
    ("nobody is buying", 4), ("no one is buying", 4),
    ("nobody buying my", 4), ("not getting any customer", 4),
    ("still no customer", 4), ("still 0 customer", 4),
)

_SELF_SITUATION = (
    ("my saas", 3), ("my startup", 3), ("my product", 2), ("my app", 2),
    ("my mvp", 3), ("my side project", 2), ("my tool", 2),
    ("my web app", 2), ("my service", 2), ("my business", 2),
    ("i built", 2), ("i launched", 3), ("i made", 2), ("i shipped", 2),
    ("just launched", 3), ("we launched", 3), ("just shipped", 2),
    ("recently launched", 3), ("launched last", 3), ("launched my", 3),
    ("i'm building", 2), ("im building", 2), ("i've been building", 2),
    ("ive been building", 2), ("bootstrapping", 2), ("solo founder", 2),
    ("indie hacker", 2), ("indiehacker", 2), ("my first product", 3),
)

_STORY = (
    ("how i got", 5), ("how we got", 5), ("how i reached", 5),
    ("how we reached", 5), ("how i acquired", 5), ("how we acquired",  5),
    ("how i found my first", 3), ("here is how", 4), ("here's how", 4),
    ("heres how", 4), ("the story of how", 5), ("story of how", 5),
    ("went from 0 to", 5), ("from 0 to", 4), ("0 to 1000", 5),
    ("0 to 10000", 5), ("0 to 10,000", 5), ("0 to 100k", 5),
    ("0 to 1m", 5), ("to $1m", 4), ("lessons learned", 4),
    ("lessons from", 3), ("case study", 4), ("what i learned", 3),
    ("what we learned", 3), ("how startups get", 4), ("how founders get", 4),
    ("got 1000 customer", 5), ("got 10000 customer", 5),
    ("reached 1000 customer", 5), ("acquired 1000 customer", 5),
    ("my journey to", 4), ("our journey to", 4), ("year in review", 4),
    ("recap", 3), ("retrospective", 3),
)
_STORY_RE = (
    re.compile(r"\bi got \d{2,}"),
    re.compile(r"\bwe got \d{2,}"),
    re.compile(r"\bgot \d{3,}\s+(?:paying\s+)?(?:customer|client|user)"),
    re.compile(r"\breached \d{3,}\s+(?:paying\s+)?(?:customer|client|user)"),
    re.compile(r"\bfrom \d+ to \d{3,}"),
    re.compile(r"\b\d{2,}\s+customers? in \d+\s+(?:day|week|month|year)"),
)

_SOLVED = (
    ("solved it", 5), ("figured it out", 5), ("update: solved", 6),
    ("update solved", 6), ("thanks everyone", 4), ("thank you everyone", 4),
    ("thanks for the help", 4), ("for anyone else struggling", 6),
    ("for anyone else who", 4), ("we finally got", 5), ("i finally got", 5),
    ("finally got my first", 5), ("got it sorted", 4), ("no longer an issue", 4),
    ("this is now resolved", 5), ("problem solved", 5),
)
_SOLVED_TITLE_PREFIX = ("update:", "solved:", "solved -", "[solved]", "resolved:")

_MISC = (
    ("tutorial", 3), ("ultimate guide", 4), ("guide to", 3),
    ("step by step", 3), ("step-by-step", 3), ("cheat sheet", 3),
    ("playbook", 2), ("framework for", 2), ("announcing", 4),
    ("we are announcing", 4), ("introducing", 3), ("[news]", 5),
    ("study finds", 4), ("report:", 4), ("survey:", 3),
    ("someone should build", 5), ("someone should make", 5),
    ("idea:", 4), ("what if we", 3), ("wouldn't it be", 3),
    ("show hn: a tool for", 2), ("i built a tool to help you get", 3),
    ("top 10 ways", 4), ("101", 2), ("best practices", 3),
    ("hiring", 3), ("we're hiring", 4),
)

# Default search strings. Chosen to surface CURRENT asks, not case studies.
SEARCH_QUERIES: tuple[str, ...] = (
    "how get first customers",
    "first paying customer",
    "can't get customers",
    "struggling to get customers",
    "how acquire users",
    "how find customers",
    "need customers SaaS",
    "zero customers SaaS",
    "first users startup",
    "customer acquisition problem",
    "how sell SaaS",
    "no paying customers",
)

_BUCKET_RANK = {"low": 1, "medium": 2, "high": 3}
_PROSPECT_TYPES = (
    "active_problem", "seeking_advice", "founder_building",
    "success_story", "educational", "irrelevant", "unknown",
)
_MAX_SUMMARY = 500
_DEFAULT_MAX_AGE_DAYS = 30
_REVIEW_STATUSES = ("new", "reviewed", "rejected")

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
_SE_QUESTION = re.compile(r"^(/questions/\d+)")
_NORM_RE = re.compile(r"[^a-z0-9'$,\s]+")


# --- canonical URL + id ------------------------------------------

def canonical_url(url: str) -> str:
    """Normalise a URL for de-duplication."""
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
    elif host.endswith(".stackexchange.com") or host in (
            "stackoverflow.com", "stackexchange.com", "superuser.com",
            "serverfault.com", "askubuntu.com"):
        m = _SE_QUESTION.match(path)          # /questions/<id>/<slug> -> /questions/<id>
        if m:
            path = m.group(1)
    path = path.rstrip("/") or "/"
    keep = {"id"}
    query = "&".join(sorted(
        f"{k}={v}" for k, v in parse_qsl(parts.query) if k.lower() in keep))
    out = f"{scheme}://{host}{path}"
    return f"{out}?{query}" if query else out


def lead_id(canonical: str) -> str:
    return hashlib.sha1(str(canonical or "").encode("utf-8")).hexdigest()[:12]


def _host(url: str) -> str:
    return urlsplit(str(url or "")).netloc.lower().split("@")[-1].removeprefix("www.")


def _normalise(text: str) -> str:
    return _NORM_RE.sub(" ", str(text or "").lower())


# --- recency ----------------------------------------------------

def age_info(posted_at: str, *, now: datetime | None = None) -> dict:
    """Deterministic age. A missing/unparseable timestamp is `unknown`,
    never guessed."""
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {"age_days": None, "age_bucket": "unknown"}
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max(0, (now - dt).days)
    if age_days <= 7:
        bucket = "recent"
    elif age_days <= 30:
        bucket = "aging"
    else:
        bucket = "old"
    return {"age_days": age_days, "age_bucket": bucket}


def _recency_factor(age_days: int | None, max_age_days: int) -> float:
    if age_days is None:
        return 0.35
    if age_days <= 3:
        f = 1.0
    elif age_days <= 7:
        f = 0.9
    elif age_days <= 14:
        f = 0.75
    elif age_days <= 30:
        f = 0.55
    elif age_days <= 90:
        f = 0.30
    elif age_days <= 365:
        f = 0.12
    else:
        f = 0.04
    if age_days > max_age_days:
        f = min(f, 0.10)
    return f


# --- deterministic classification ------------------------------

def _hits(table, title_n: str, body_n: str):
    """Return [(phrase, weight, in_title)] for every table phrase present."""
    out = []
    hay = f"{title_n} {body_n}"
    for phrase, weight in table:
        p = _normalise(phrase).strip()
        if p and p in hay:
            out.append((phrase, weight, p in title_n))
    return out


def classify(title: str, text: str) -> dict:
    title_n = _normalise(title)
    body_n = _normalise(text)

    ask = _hits(_ASK, title_n, body_n)
    first = _hits(_FIRST_CUSTOMER, title_n, body_n)
    selfsit = _hits(_SELF_SITUATION, title_n, body_n)
    story = _hits(_STORY, title_n, body_n)
    misc = _hits(_MISC, title_n, body_n)
    solved = _hits(_SOLVED, title_n, body_n)
    for rx in _STORY_RE:
        if rx.search(title_n):
            story.append((rx.pattern, 5, True))
        elif rx.search(body_n):
            story.append((rx.pattern, 4, False))
    if title_n.strip().startswith(_SOLVED_TITLE_PREFIX):
        solved.append(("<solved title prefix>", 6, True))

    def pts(hits, title_w, body_w, cap):
        return min(cap, sum((title_w if t else body_w) * w for _, w, t in hits))

    ask_pts = pts(ask, 12, 4, 45)
    first_pts = pts(first, 8, 3, 28)
    self_pts = pts(selfsit, 5, 2, 15)
    question_bonus = 8 if (title_n.strip().endswith("?")
                           or title_n.strip().startswith(("ask hn", "ask "))) else 0

    story_pts = pts(story, 30, 12, 65)
    misc_pts = pts(misc, 10, 6, 28)
    solved_pts = pts(solved, 25, 12, 60)

    relevance = max(0, min(100, ask_pts + first_pts + self_pts + question_bonus
                           - story_pts - misc_pts - solved_pts))
    text_solved = solved_pts >= 12

    ask_in_title = any(t for *_, t in ask)
    has_ask = bool(ask)
    has_self = bool(selfsit)
    has_first = bool(first)

    if story_pts >= 30:
        ptype = "success_story"
    elif text_solved:
        ptype = "success_story"          # they already fixed it -> not a live lead
    elif misc_pts >= 16 and not has_ask:
        ptype = "educational"
    elif ask_in_title and (has_first or has_self) and relevance >= 55:
        ptype = "active_problem"
    elif has_ask and relevance >= 35:
        ptype = "seeking_advice"
    elif has_self and not has_ask and 20 <= relevance <= 50:
        ptype = "founder_building"
    elif relevance < 15:
        ptype = "irrelevant"
    else:
        ptype = "unknown"

    active_problem = ptype == "active_problem"
    if active_problem and relevance >= 65:
        intent = "high"
    elif ptype in ("active_problem", "seeking_advice") and relevance >= 40:
        intent = "medium"
    else:
        intent = "low"

    pos = []
    for phrase, _, t in ask + first + selfsit:
        pos.append(f"'{phrase}'" + (" (title)" if t else ""))
    if question_bonus:
        pos.append("question/Ask-HN format")
    neg = [f"'{p}'" + (" (title)" if t else "")
           for p, _, t in story + misc + solved]

    reason = ("; ".join(pos[:6]) or "weak keyword only")
    if neg:
        reason += " | negatives: " + ", ".join(neg[:4])

    # a title that is merely a question, with no acquisition signal, is not
    # an opportunity - the question format only *boosts* a real signal.
    any_positive = bool(ask or first or selfsit)

    return {
        "relevance_score": int(relevance),
        "prospect_type": ptype,
        "active_problem": active_problem,
        "buying_intent": intent,
        "solved": text_solved,
        "match_reason": reason,
        "matched_phrases": tuple(p for p, *_ in ask + first + selfsit),
        "negative_signals": tuple(p for p, *_ in story + misc + solved),
        "any_positive": any_positive,
    }


def score_lead(record) -> dict | None:
    """Deterministically classify + age one raw record. Returns None only
    when no positive signal fires (not an opportunity at all)."""
    def g(name):
        if isinstance(record, dict):
            return str(record.get(name, "") or "")
        return str(getattr(record, name, "") or "")

    c = classify(g("title"), g("text"))
    if not c["any_positive"]:
        return None
    age = age_info(g("posted_at"))
    c.pop("any_positive")
    c.update(age)

    # source-specific structured signals (Stack Exchange answer stats)
    meta = getattr(record, "meta", None)
    if isinstance(record, dict):
        meta = record.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    se_solved = bool(meta.get("accepted")) or int(meta.get("answer_count", 0) or 0) >= 2
    if se_solved and not c["solved"]:
        c["solved"] = True
        c["match_reason"] += " | negatives: SE question already answered"
    # a recent, entirely-unanswered SE question = still stuck -> small boost
    unanswered = ("answer_count" in meta and int(meta.get("answer_count", 0) or 0) == 0
                  and not meta.get("answered"))
    if unanswered and (age["age_days"] is None or age["age_days"] <= 45):
        c["relevance_score"] = min(100, int(c["relevance_score"]) + 8)
        c["match_reason"] += "; unanswered SE question"

    c["signals"] = meta
    c["fit_score"] = c["relevance_score"]  # backwards-compatible alias
    return c


def _problem_factor(prospect_type: str) -> float:
    return {
        "active_problem": 1.0,    # strongly preferred
        "seeking_advice": 0.8,
        "founder_building": 0.55,
        "unknown": 0.40,
        "educational": 0.28,      # deprioritized
        "irrelevant": 0.18,
        "success_story": 0.12,    # strongly deprioritized
    }.get(prospect_type, 0.35)


def _intent_factor(intent: str) -> float:
    return {"high": 1.0, "medium": 0.85}.get(intent, 0.60)


def final_score(*, relevance_score: int, age_days, prospect_type: str,
                buying_intent: str, max_age_days: int, solved: bool = False,
                recommended_fit: int | None = None) -> int:
    """final = relevance x recency x problem-type x intent x solved.

    Weights (documented): recency 1.0(<=3d) .. 0.04(>1y), with a hard
    <=0.10 cliff once older than --max-age-days. problem-type strongly
    favours active_problem (1.0) and crushes success_story (0.12).
    solved -> x0.20 (they already fixed it). So a 2-day genuine ask
    (~80 x 1.0 x 1.0 x 1.0) far outranks a 5-year article that merely
    contains "first customers" (~90 x 0.04 x 0.12 -> ~0)."""
    base = relevance_score if recommended_fit is None else round(
        0.5 * relevance_score + 0.5 * recommended_fit)
    fs = (base
          * _recency_factor(age_days, max_age_days)
          * _problem_factor(prospect_type)
          * _intent_factor(buying_intent)
          * (0.20 if solved else 1.0))
    return max(0, min(100, round(fs)))


# --- the lead --------------------------------------------------

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
    # scoring
    relevance_score: int = 0
    prospect_type: str = "unknown"
    active_problem: bool = False
    solved: bool = False
    final_score: int = 0
    scoring_mode: str = "deterministic"
    llm_reason: str = ""
    recommended_fit: int = 0
    negative_signals: tuple = ()
    matched_phrases: tuple = ()
    signals: dict = field(default_factory=dict)
    # recency
    age_days: int | None = None
    age_bucket: str = "unknown"
    # provenance / review
    problem_summary: str = ""
    author: str = ""
    posted_at: str = ""
    query: str = ""
    lead_id: str = ""
    human_review_status: str = "new"
    discovered_at: str = ""
    last_seen_at: str = ""

    def to_dict(self) -> dict:
        return {
            "lead_id": self.lead_id,
            "canonical_url": self.canonical_url,
            "url": self.url,
            "source": self.source,
            "platform": self.platform,
            "title": self.title,
            "author": self.author,
            "posted_at": self.posted_at,
            "age_days": self.age_days,
            "age_bucket": self.age_bucket,
            "problem_summary": self.problem_summary,
            "match_reason": self.match_reason,
            "matched_phrases": list(self.matched_phrases),
            "negative_signals": list(self.negative_signals),
            "relevance_score": self.relevance_score,
            "fit_score": self.fit_score,
            "final_score": self.final_score,
            "buying_intent": self.buying_intent,
            "prospect_type": self.prospect_type,
            "active_problem": self.active_problem,
            "solved": self.solved,
            "signals": dict(self.signals),
            "scoring_mode": self.scoring_mode,
            "llm_reason": self.llm_reason,
            "recommended_fit": self.recommended_fit,
            "promo_allowed": self.promo_allowed,
            "promo_note": self.promo_note,
            "query": self.query,
            "human_review_status": self.human_review_status,
            "discovered_at": self.discovered_at,
            "last_seen_at": self.last_seen_at,
        }


def _promo_for(platform: str) -> tuple[str, str]:
    p = str(platform or "").lower()
    if p.startswith("r/") or "reddit" in p:
        return PROMO_POLICY["reddit"]
    if "hacker news" in p or p in ("hn", "ycombinator"):
        return PROMO_POLICY["hacker news"]
    return _PROMO_DEFAULT


def build_lead(record, score: dict, *, max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
               scoring_mode: str = "deterministic") -> AcquisitionLead:
    """Assemble a lead from a raw record + its score. Only copies fields
    the record actually carries; nothing is synthesised."""
    def g(name):
        if isinstance(record, dict):
            return str(record.get(name, "") or "").strip()
        return str(getattr(record, name, "") or "").strip()

    url = g("url")
    platform = g("platform")
    promo_allowed, promo_note = _promo_for(platform)
    cu = canonical_url(url)
    ts = now_iso()
    fs = final_score(
        relevance_score=int(score["relevance_score"]),
        age_days=score["age_days"], prospect_type=score["prospect_type"],
        buying_intent=score["buying_intent"], max_age_days=max_age_days,
        solved=bool(score.get("solved")),
        recommended_fit=(int(score["recommended_fit"])
                         if scoring_mode == "llm" and score.get("recommended_fit")
                         else None),
    )
    return AcquisitionLead(
        canonical_url=cu,
        url=url,
        source=g("source"),
        platform=platform,
        title=g("title"),
        author=g("author"),
        posted_at=g("posted_at"),
        age_days=score["age_days"],
        age_bucket=score["age_bucket"],
        problem_summary=(g("text") or g("title"))[:_MAX_SUMMARY],
        match_reason=score["match_reason"],
        matched_phrases=tuple(score["matched_phrases"]),
        negative_signals=tuple(score.get("negative_signals", ())),
        relevance_score=int(score["relevance_score"]),
        fit_score=int(score["relevance_score"]),
        final_score=fs,
        buying_intent=score["buying_intent"],
        prospect_type=score["prospect_type"],
        active_problem=bool(score["active_problem"]),
        solved=bool(score.get("solved")),
        signals=dict(score.get("signals") or {}),
        scoring_mode=scoring_mode,
        llm_reason=str(score.get("llm_reason", "")),
        recommended_fit=int(score.get("recommended_fit", 0) or 0),
        promo_allowed=promo_allowed,
        promo_note=promo_note,
        query=g("query"),
        lead_id=lead_id(cu),
        discovered_at=ts,
        last_seen_at=ts,
    )


def qc_lead(lead: AcquisitionLead) -> list[str]:
    """Deterministic quality control. Empty list = passes."""
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
        problems.append("no positive signal")
    for name, val in (("relevance_score", lead.relevance_score),
                      ("final_score", lead.final_score)):
        if not 0 <= val <= 100:
            problems.append(f"{name} out of range")
    if lead.buying_intent not in _BUCKET_RANK:
        problems.append("invalid buying_intent")
    if lead.prospect_type not in _PROSPECT_TYPES:
        problems.append("invalid prospect_type")
    return problems


# --- the agent ------------------------------------------------

class AcquisitionAgent(Agent):
    """Turns raw records into scored, quality-checked AcquisitionLeads.
    Deterministic (unless an llm_scorer is supplied). Contacts no one."""

    role = "acquisition_scout"
    objective = "Find current public posts from founders struggling to get first customers."
    capabilities = ("discover_acquisition",)

    def run(self, task: Task) -> Result:
        records = task.payload.get("records")
        if not isinstance(records, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload needs a list of records")
        min_score = int(task.payload.get("min_score", 0))
        max_age_days = int(task.payload.get("max_age_days", _DEFAULT_MAX_AGE_DAYS))
        llm_scorer = task.payload.get("llm_scorer")
        mode = "llm" if llm_scorer is not None else "deterministic"

        by_url: dict[str, AcquisitionLead] = {}
        dropped: list[dict] = []
        considered = no_match = collapsed = 0
        too_old = 0
        for rec in records:
            considered += 1
            score = score_lead(rec)
            if score is None:
                no_match += 1
                continue
            if llm_scorer is not None:
                try:
                    score = {**score, **llm_scorer(_scoring_view(rec, score))}
                except Exception as exc:  # keep the deterministic score
                    score = {**score, "llm_reason": f"(llm scoring failed: {exc})"}
            lead = build_lead(rec, score, max_age_days=max_age_days,
                              scoring_mode=mode)
            problems = qc_lead(lead)
            if problems:
                dropped.append({"title": lead.title[:80], "url": lead.url,
                                "reasons": problems})
                continue
            if lead.age_days is not None and lead.age_days > max_age_days:
                too_old += 1
            if lead.final_score < min_score:
                continue
            keep = by_url.get(lead.canonical_url)
            if keep is not None:
                collapsed += 1
                if lead.final_score <= keep.final_score:
                    continue
            by_url[lead.canonical_url] = lead

        leads = sorted(by_url.values(),
                       key=lambda x: (x.final_score, x.relevance_score),
                       reverse=True)
        return Result(
            task_id=task.id, agent=self.name, status="ok",
            output={
                "leads": [l.to_dict() for l in leads],
                "dropped": dropped,
                "considered": considered,
                "no_match": no_match,
                "collapsed": collapsed,
                "too_old": too_old,
                "scoring_mode": mode,
            },
        )


def _scoring_view(record, score: dict) -> dict:
    """The minimal, safe view handed to the LLM scorer."""
    def g(name):
        if isinstance(record, dict):
            return str(record.get(name, "") or "")
        return str(getattr(record, name, "") or "")
    meta = getattr(record, "meta", None)
    if isinstance(record, dict):
        meta = record.get("meta")
    return {
        "canonical_url": canonical_url(g("url")),
        "title": g("title"),
        "text": g("text")[:_MAX_SUMMARY],
        "posted_at": g("posted_at"),
        "source_signals": dict(meta) if isinstance(meta, dict) else {},
        "deterministic": {k: score.get(k) for k in
                          ("relevance_score", "prospect_type", "buying_intent",
                           "solved")},
    }


# --- the store ------------------------------------------------

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

    def get(self, key: str) -> dict | None:
        return self._by_url.get(str(key))

    def by_id(self, lead_id_prefix: str) -> dict | None:
        p = str(lead_id_prefix or "").strip().lower()
        if not p:
            return None
        matches = [d for d in self._by_url.values()
                   if str(d.get("lead_id", "")).startswith(p)]
        return matches[0] if len(matches) == 1 else None

    def all(self) -> list[dict]:
        return list(self._by_url.values())

    def ranked(self) -> list[dict]:
        # a lead scored by the current model has a real final_score;
        # legacy records without one sink to the bottom (they must not
        # outrank current opportunities).
        return sorted(
            self._by_url.values(),
            key=lambda d: (d.get("final_score", -1),
                           d.get("relevance_score", d.get("fit_score", 0)),
                           d.get("last_seen_at", "")),
            reverse=True)

    def upsert(self, lead: dict) -> str:
        """Insert or merge a lead. On a re-find: keep the better score,
        refresh volatile fields, but preserve the human review status and
        the original discovered_at. Returns 'added' | 'updated' | 'unchanged'."""
        key = str(lead.get("canonical_url", "")).strip()
        if not key:
            raise ValueError("lead has no canonical_url")
        old = self._by_url.get(key)
        if old is None:
            self._by_url[key] = dict(lead)
            return "added"

        merged = dict(old)
        merged["last_seen_at"] = lead.get("last_seen_at") or now_iso()
        # take the better final_score and carry its scoring fields
        old_fs = old.get("final_score", old.get("fit_score", 0))
        new_fs = lead.get("final_score", lead.get("fit_score", 0))
        changed = merged["last_seen_at"] != old.get("last_seen_at")
        if new_fs > old_fs or (new_fs == old_fs
                               and lead.get("scoring_mode") == "llm"
                               and old.get("scoring_mode") != "llm"):
            for k in ("relevance_score", "fit_score", "final_score",
                      "buying_intent", "prospect_type", "active_problem",
                      "match_reason", "matched_phrases", "negative_signals",
                      "scoring_mode", "llm_reason", "recommended_fit"):
                if k in lead:
                    merged[k] = lead[k]
            changed = True
        # refresh volatile provenance if the new record actually has it
        for k in ("posted_at", "age_days", "age_bucket", "title",
                  "problem_summary", "author", "query"):
            v = lead.get(k)
            if v not in (None, "", ()) and merged.get(k) != v:
                merged[k] = v
                changed = True
        merged.setdefault("human_review_status", "new")
        merged.setdefault("lead_id", lead.get("lead_id", ""))
        merged.setdefault("discovered_at", old.get("discovered_at", ""))
        self._by_url[key] = merged
        return "updated" if changed else "unchanged"

    # kept for backwards compatibility with older callers/tests
    def add(self, lead: dict) -> bool:
        return self.upsert(lead) == "added"

    def set_review(self, lead_id_prefix: str, status: str, *, actor: str = "") -> dict:
        if status not in _REVIEW_STATUSES:
            raise ValueError(f"status must be one of {_REVIEW_STATUSES}")
        entry = self.by_id(lead_id_prefix)
        if entry is None:
            raise ValueError(f"no single lead matches id {lead_id_prefix!r}")
        entry["human_review_status"] = status
        entry["reviewed_at"] = now_iso()
        if actor:
            entry["reviewed_by"] = actor
        return entry
