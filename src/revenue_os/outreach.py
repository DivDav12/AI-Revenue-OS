"""Outreach preparation - the bridge between a qualified lead and the
checkout.

Given a lead the acquisition agent already found and scored, this builds
a BRIEF for a human: what the person's problem is (their own words), a
genuinely-helpful answer angle, generic talking points, the community's
promo rules, and a tracked checkout link.

Hard rules:
  - It NEVER posts, comments, DMs, or emails. Output is a draft for a
    human to edit and post themselves.
  - It never invents the lead's business, never promises results, never
    claims the person will buy. Every lead-specific line comes from the
    stored `problem_summary` / `why` (the lead's own text + observed
    signals). The talking points are generic first-customer advice, not
    claims about this lead.
  - "Help first" - the CTA is optional and secondary.

Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .store import now_iso

# Last-resort fallback only. The real checkout URL is a launched candidate's
# deployed `public_url` (set by `deploy-checkout`); callers with a
# CandidateStore should resolve it with `resolve_checkout_url()`. This
# constant matches what `deploy-checkout` publishes for the default
# GITHUB_PAGES_REPO so an un-resolved brief still points somewhere real.
DEFAULT_CHECKOUT_URL = "https://DivDav12.github.io/AI-Revenue-OS/checkout.html"


def resolve_checkout_url(store, *, fallback: str = DEFAULT_CHECKOUT_URL) -> str:
    """The checkout URL an outreach reply should link to: the deployed
    `public_url` of a launched / earning candidate that has one.

    Falls back to `fallback` only when nothing is deployed yet. `store` is
    a CandidateStore (or anything whose `.all()` yields objects with
    `.status` and `.public_url`). Never hits the network.
    """
    try:
        candidates = list(store.all())
    except AttributeError:
        return fallback
    for cand in candidates:
        if (getattr(cand, "status", "") in ("launched", "earning")
                and getattr(cand, "public_url", "")):
            return cand.public_url
    return fallback

_GENERIC_POINTS = [
    "Ask what they have actually tried so far - most first-time founders "
    "have not done any manual, 1:1 outreach yet.",
    "Suggest picking ONE channel where their exact user already spends "
    "time, and going deep there instead of spreading across five.",
    "Recommend 10-20 personal conversations (DMs / calls, not broadcasts) "
    "with people in the target group before spending on 'marketing'.",
    "Warn against paid ads until they have closed ~5 customers by hand - "
    "ads amplify a funnel that converts, they do not create one.",
    "If you know the specific subreddit / forum / Slack / newsletter for "
    "their niche, name it - that is the single most useful thing you can give.",
]

_MARKETING_POINTS = [
    "Marketing with no audience = distribution problem, not a copy problem. "
    "Point them at where their users already gather.",
    "One concrete channel + one weekly cadence beats a scattered plan.",
    "Content only compounds after ~10-20 posts; set that expectation.",
    "A short 'build in public' log in the right community often out-performs "
    "a landing page in the first month.",
]

_FREELANCE_POINTS = [
    "First clients almost always come from the existing network, not cold "
    "channels - suggest a direct message to 10 past colleagues / contacts.",
    "A single well-scoped, visible sample project beats a broad portfolio.",
    "Recommend one niche + one platform (a specific community, not 'Upwork "
    "and LinkedIn and Twitter').",
    "Warn against lowering rates to win the first client - it sets a bad anchor.",
]


def _angle_for(lead: dict) -> tuple[str, list[str]]:
    phr = " ".join(str(p) for p in lead.get("matched_phrases", [])).lower()
    txt = (lead.get("problem_summary", "") + " " + lead.get("title", "")).lower()
    hay = phr + " " + txt
    if "client" in hay or "freelanc" in hay:
        return ("They are trying to land their first freelance client. The useful "
                "answer is about tapping the existing network and picking one "
                "niche, not about job boards.", list(_FREELANCE_POINTS))
    if "market" in hay or "promote" in hay:
        return ("They are asking how to market / promote a product that has "
                "already launched. Frame it as a distribution problem: where do "
                "their users already are?", list(_MARKETING_POINTS))
    return ("They have built and launched something but have no customers - the "
            "classic 'build it and they will not come'. The useful answer is "
            "about ONE distribution channel plus real 1:1 conversations, not "
            "more building.", list(_GENERIC_POINTS))


def tracked_checkout_link(checkout_url: str, lead_id: str) -> str:
    lid = str(lead_id or "").strip()
    if not lid:
        return checkout_url
    sep = "&" if "?" in checkout_url else "?"
    return f"{checkout_url}{sep}lead={lid}"


def outreach_brief(lead: dict, *, checkout_url: str = DEFAULT_CHECKOUT_URL,
                   drafter=None) -> dict:
    """A human-review draft. Contains no invented facts about the lead.

    `drafter` (optional): a callable (lead dict) -> tailored reply-draft
    dict. When given, its result is attached as `draft_reply`; the
    deterministic angle/points stay as the fallback. A drafter failure is
    recorded, never raised - the brief is still useful without it. The
    drafter must never post; it only writes a draft for a person.
    """
    lid = str(lead.get("lead_id") or "")
    angle, points = _angle_for(lead)
    link = tracked_checkout_link(checkout_url, lid)
    brief = {
        "lead_id": lid,
        "url": lead.get("url", ""),
        "platform": lead.get("platform", ""),
        "source": lead.get("source", ""),
        "posted_at": lead.get("posted_at", ""),
        "age_days": lead.get("age_days"),
        "age_bucket": lead.get("age_bucket", "unknown"),
        "prospect_type": lead.get("prospect_type", "unknown"),
        "prospect_quality": lead.get("prospect_quality", "none"),
        "relevance_score": lead.get("relevance_score"),
        "their_words": str(lead.get("problem_summary") or lead.get("title") or "")[:600],
        "why_relevant": list(lead.get("why", [])),
        "answer_angle": angle,
        "talking_points": points,
        "help_first": "Post a genuinely useful answer to their actual question "
                      "FIRST. The line below is optional and comes last.",
        "optional_cta": (
            "If a structured plan would help, I put together a 14-day "
            "first-customers plan you can follow - EUR 29.90: " + link
            + " (totally fine to ignore)."),
        "checkout_link": link,
        "promo_allowed": lead.get("promo_allowed", "unknown"),
        "promo_note": lead.get("promo_note", ""),
        "human_approval": "DRAFT ONLY. A human must rewrite this in their own "
                          "voice, check this community's self-promotion rules, "
                          "and post it themselves. The system never posts.",
        "no_fabrication_note": "This brief makes no claim about the lead's "
                               "business beyond their own words above.",
        "generated_at": now_iso(),
    }
    if drafter is not None:
        try:
            brief["draft_reply"] = drafter(lead)
        except Exception as exc:  # a drafter failure never breaks the brief
            brief["draft_reply"] = {"error": f"llm draft failed: {exc}"}
    return brief


class OutreachStore:
    """One JSON list, atomically written, keyed by lead_id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_id: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "OutreachStore":
        s = cls(path)
        if not s.path.exists():
            return s
        try:
            raw = json.loads(s.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt outreach store {s.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"outreach store {s.path} must be a JSON list")
        for e in raw:
            s._by_id[str(e["lead_id"])] = dict(e)
        return s

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(list(self._by_id.values()), indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, lead_id: str) -> dict | None:
        return self._by_id.get(str(lead_id))

    def all(self) -> list[dict]:
        return list(self._by_id.values())

    def has(self, lead_id: str) -> bool:
        return str(lead_id) in self._by_id

    def put(self, brief: dict, *, status: str = "draft") -> dict:
        lid = str(brief.get("lead_id") or "").strip()
        if not lid:
            raise ValueError("brief has no lead_id")
        old = self._by_id.get(lid) or {}
        entry = {
            "lead_id": lid,
            "status": old.get("status", status),   # keep a human verdict
            "brief": dict(brief),
            "first_prepared_at": old.get("first_prepared_at") or now_iso(),
            "last_prepared_at": now_iso(),
        }
        self._by_id[lid] = entry
        return entry

    def set_status(self, lead_id: str, status: str, *, reason: str = "") -> dict:
        if status not in ("draft", "approved", "posted", "skipped"):
            raise ValueError("status must be draft|approved|posted|skipped")
        e = self._by_id.get(str(lead_id))
        if e is None:
            raise ValueError(f"no brief for lead {lead_id!r}")
        e["status"] = status
        e["status_changed_at"] = now_iso()
        if reason:
            e["status_reason"] = str(reason)
        return e
