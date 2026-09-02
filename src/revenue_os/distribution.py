"""Distribution Strategist (acquisition cluster) - deterministic channel plan.

Given an opportunity + its offer, this ranks free / very-low-cost
distribution channels by fit, reach, effort and community-rules risk, and
turns each into a concrete recommendation a HUMAN then carries out.

RESEARCH ONLY. It never posts, comments, DMs, emails, creates an account,
buys ads, or spends money: every channel it returns carries
`requires_human_action: true` and the plan carries `human_gate_required:
true`. No network, no LLM, no I/O of its own - the ranking is a pure
function of the caller-supplied opportunity / offer text plus a fixed
channel catalogue.

Channel `type`:
  organic   - publish on a channel the operator owns / controls
  direct    - 1:1 to a named person (warm intro, cold email, DM)
  community - a third-party community, forum, or launch board
  content   - evergreen SEO, directories, listings
  partner   - a mutual arrangement with an adjacent, non-competing party

`fit_score` / `reach_score` / `risk_score` are 0 (low) .. 10 (high).
`effort_score` is 0 (trivial) .. 10 (weeks of sustained work).
`cost` is always 0 here - only free / no-card channels are proposed.
"""

from __future__ import annotations

import re

from .action_class import posting_permitted
from .agent import Agent
from .messages import Result, Task
from .store import now_iso

_TOKEN = re.compile(r"[a-z][a-z0-9+.#-]{2,}")

# Each channel: base reach / effort / risk, the tags that raise its fit
# (multi-word / hyphenated tags are matched as substrings, single words as
# whole tokens), an optional `always_fit` floor, the platform key used to
# ask `action_class.posting_permitted`, and ONE concrete action for a human.
_CHANNELS: tuple[dict, ...] = (
    {
        "channel": "Existing network & warm intros",
        "type": "direct", "reach": 3, "effort": 2, "risk": 1,
        "tags": (), "always_fit": 5, "post_as": "network",
        "action": "List 15 people who have this problem or know someone who "
                  "does; a human sends 5 personal notes asking for a reaction, "
                  "not a sale.",
    },
    {
        "channel": "Hacker News (Ask/Show HN + helpful comments)",
        "type": "community", "reach": 8, "effort": 4, "risk": 7,
        "tags": ("developer", "dev", "engineer", "saas", "api", "cli",
                 "open source", "oss", "technical", "startup", "indie", "b2b",
                 "devtool", "infrastructure", "self-hosted", "programming",
                 "automation", "ai"),
        "post_as": "hacker news",
        "action": "A human writes one substantive answer on a real current "
                  "thread (or a plain Show HN); never top-post a link.",
    },
    {
        "channel": "Indie Hackers (forum + progress posts)",
        "type": "community", "reach": 6, "effort": 3, "risk": 4,
        "tags": ("indie", "bootstrap", "solo", "founder", "saas", "side project",
                 "micro", "newsletter", "no-code", "nocode", "maker",
                 "first customers", "mrr"),
        "post_as": "indie hackers",
        "action": "A human posts a genuine progress update or question and "
                  "replies to comments; links only where the group allows it.",
    },
    {
        "channel": "Reddit (niche subreddits)",
        "type": "community", "reach": 8, "effort": 5, "risk": 8,
        "tags": ("saas", "marketing", "freelance", "no-code", "nocode",
                 "creator", "ecommerce", "side project", "small business",
                 "design", "writing", "productivity", "seo", "startups",
                 "entrepreneur"),
        "post_as": "reddit",
        "action": "Pick the 1-2 subreddits where the buyer already asks for "
                  "help; a human reads each rulebook, then answers questions "
                  "with no link for a while before anything promotional.",
    },
    {
        "channel": "Product Hunt launch",
        "type": "community", "reach": 7, "effort": 5, "risk": 3,
        "tags": ("launch", "tool", "app", "ai", "no-code", "nocode",
                 "productivity", "design", "chrome extension",
                 "browser extension", "saas", "template", "generator",
                 "dashboard"),
        "post_as": "product hunt",
        "action": "A human prepares assets, lines up a few genuine supporters, "
                  "and schedules a single launch day.",
    },
    {
        "channel": "LinkedIn (organic posts + targeted 1:1)",
        "type": "direct", "reach": 7, "effort": 4, "risk": 5,
        "tags": ("b2b", "agency", "consultant", "enterprise", "sales",
                 "marketing", "recruiting", "hr", "professional", "service",
                 "audit", "operations", "smb", "procurement", "manager"),
        "post_as": "linkedin",
        "action": "A human publishes 1-2 useful posts a week and sends a "
                  "handful of personalised connection notes to profiles that "
                  "match the ideal customer.",
    },
    {
        "channel": "X / Twitter (build-in-public)",
        "type": "organic", "reach": 7, "effort": 5, "risk": 3,
        "tags": ("creator", "indie", "ai", "build in public", "founder",
                 "maker", "marketing", "design", "developer", "startup",
                 "newsletter"),
        "post_as": "x",
        "action": "A human posts progress and concrete lessons 3-5x a week and "
                  "replies in relevant conversations; no automation.",
    },
    {
        "channel": "Niche forums / Slack / Discord communities",
        "type": "community", "reach": 5, "effort": 4, "risk": 6,
        "tags": ("community", "forum", "slack", "discord", "niche", "hobby",
                 "professional", "association", "guild", "cohort"),
        "always_fit": 4, "post_as": "discord",
        "action": "Find the 2-3 named communities where the buyer already "
                  "gathers; a human joins, reads the promo rules, and "
                  "contributes real help first.",
    },
    {
        "channel": "SEO / content on an owned channel (blog, newsletter)",
        "type": "content", "reach": 6, "effort": 7, "risk": 1,
        "tags": ("guide", "template", "course", "information", "comparison",
                 "directory", "evergreen", "how-to", "checklist", "report",
                 "benchmark", "tutorial"),
        "always_fit": 4, "post_as": "own_blog",
        "action": "A human publishes one focused, genuinely useful article or "
                  "issue per week targeting the buyer's search terms; it "
                  "compounds over months.",
    },
    {
        "channel": "Directories, marketplaces & 'best X for Y' roundups",
        "type": "content", "reach": 4, "effort": 3, "risk": 2,
        "tags": ("directory", "marketplace", "tool", "listing", "comparison",
                 "review", "template", "plugin", "extension", "integration",
                 "app store"),
        "always_fit": 3, "post_as": "directory",
        "action": "A human submits the offer to 5 relevant directories / "
                  "listing sites this week; small but durable real-intent "
                  "traffic.",
    },
    {
        "channel": "Partnerships with adjacent, non-competing products / creators",
        "type": "partner", "reach": 5, "effort": 5, "risk": 2,
        "tags": ("agency", "creator", "newsletter", "community",
                 "complementary", "integration", "b2b", "platform",
                 "ecosystem", "reseller"),
        "always_fit": 3, "post_as": "partner",
        "action": "A human lists 5 non-competing parties already serving this "
                  "buyer and proposes one specific swap (mutual mention, "
                  "bundle, guest post).",
    },
    {
        "channel": "Direct cold outreach to a named prospect list",
        "type": "direct", "reach": 5, "effort": 5, "risk": 6,
        "tags": ("b2b", "agency", "service", "audit", "consulting",
                 "enterprise", "smb", "done-for-you", "dfy", "lead", "sales",
                 "outbound"),
        "post_as": "cold outreach",
        "action": "A human builds a 30-row list (name, link, fit reason) and "
                  "sends 10 personalised messages a day; the system drafts, a "
                  "human is the one who contacts anyone.",
    },
)


def _haystack(opportunity: dict, offer: dict, copy: dict) -> str:
    parts = [
        opportunity.get("title") or opportunity.get("name") or "",
        opportunity.get("description") or "",
        opportunity.get("target_customer") or "",
        opportunity.get("category") or "",
        opportunity.get("need") or opportunity.get("problem")
        or opportunity.get("required_work") or "",
        offer.get("what_is_sold") or "",
        offer.get("positioning") or "",
        " ".join(str(x) for x in (offer.get("includes") or [])),
        copy.get("headline") or "",
        copy.get("subheadline") or "",
    ]
    return " ".join(str(p) for p in parts).lower()


def _fit(channel: dict, hay: str, hay_tokens: set[str]) -> tuple[int, list[str]]:
    hits: list[str] = []
    for tag in channel["tags"]:
        if (" " in tag or "-" in tag):
            if tag in hay:
                hits.append(tag)
        elif tag in hay_tokens:
            hits.append(tag)
    score = min(10, int(channel.get("always_fit", 2)) + 2 * len(hits))
    return score, hits


def _buying_probability(opportunity: dict, signals: dict) -> float | None:
    for src in (opportunity, signals):
        p = src.get("probability")
        if isinstance(p, (int, float)) and 0.0 <= p <= 1.0:
            return round(float(p), 2)
    breakdown = signals.get("breakdown") or opportunity.get("breakdown") or {}
    demand = breakdown.get("demand", breakdown.get("real_demand"))
    if isinstance(demand, (int, float)) and demand > 0:
        return round(min(1.0, float(demand) / 5.0), 2)
    return None


def _reason(channel: dict, hits: list[str], reach: int, effort: int,
            risk: int, auto_ok: bool) -> str:
    fit_txt = (f"fits on: {', '.join(hits)}. " if hits
               else "general-purpose fit, no specific signal. ")
    rules = ("This is an owned channel - publishing is fine once it is written."
             if auto_ok else
             "Automated posting is NOT permitted here - a human must post, "
             "after reading this community's self-promotion rules.")
    return (f"{channel['type']} channel; {fit_txt}"
            f"reach~{reach}/10, effort~{effort}/10, rules/spam risk~{risk}/10. "
            f"{rules}")


def build_distribution_plan(*, opportunity: dict, offer: dict | None = None,
                            copy: dict | None = None,
                            signals: dict | None = None,
                            now: str | None = None) -> dict:
    """Deterministic channel plan for one opportunity + its offer.

    Pure: no network, no LLM, no I/O, no money. Returns a structured plan
    whose every channel `requires_human_action` and which carries
    `human_gate_required: true`.
    """
    opportunity = opportunity or {}
    offer = offer or {}
    copy = copy or {}
    signals = signals or {}

    oid = str(opportunity.get("id") or opportunity.get("opportunity_id")
              or opportunity.get("name") or "")
    hay = _haystack(opportunity, offer, copy)
    hay_tokens = {t for t in _TOKEN.findall(hay) if len(t) >= 3}
    prob = _buying_probability(opportunity, signals)

    price = offer.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    prob_factor = 0.7 + 0.6 * (prob if prob is not None else 0.5)

    rows: list[dict] = []
    for ch in _CHANNELS:
        fit, hits = _fit(ch, hay, hay_tokens)
        reach, effort, risk = ch["reach"], ch["effort"], ch["risk"]
        # deterministic price-band nudges
        if price is not None and price >= 200 and ch["type"] in ("direct", "partner"):
            fit = min(10, fit + 1)
        if price is not None and price <= 50 and ch["type"] in ("community", "content"):
            fit = min(10, fit + 1)
        if fit <= 3:
            reach = max(1, reach - 3)
        elif fit <= 5:
            reach = max(1, reach - 1)
        auto_ok = posting_permitted(ch["post_as"])
        priority = round((fit * 1.5 + reach - effort * 0.7 - risk * 0.6)
                         * prob_factor, 2)
        rows.append({
            "channel": ch["channel"],
            "type": ch["type"],
            "fit_score": fit,
            "reach_score": reach,
            "effort_score": effort,
            "cost": 0,
            "risk_score": risk,
            "auto_post_allowed": bool(auto_ok),
            "matched_signals": hits,
            "reason": _reason(ch, hits, reach, effort, risk, auto_ok),
            "recommended_action": ch["action"],
            "requires_human_action": True,
            "_priority": priority,
        })

    rows.sort(key=lambda r: (-r["_priority"], r["effort_score"], r["channel"]))
    top = rows[0] if rows else None
    return {
        "opportunity_id": oid,
        "audience": str(opportunity.get("target_customer") or ""),
        "need": str(opportunity.get("need") or opportunity.get("problem")
                    or opportunity.get("required_work") or ""),
        "offer_summary": str(offer.get("what_is_sold")
                             or offer.get("positioning") or ""),
        "buying_probability": prob if prob is not None else "unknown",
        "channels": [{k: v for k, v in r.items() if k != "_priority"}
                     for r in rows],
        "shortlist": [r["channel"] for r in rows[:3]],
        "top_recommendation": (
            f"{top['channel']} - {top['type']} channel, fit {top['fit_score']}/10"
            if top else "none"),
        "human_gate_required": True,
        "no_action_note": "research + prioritisation only - nothing was posted, "
                          "sent, published, purchased, or automated. Every "
                          "channel needs a human to act.",
        "generated_at": now or now_iso(),
    }


class DistributionAgent(Agent):
    role = "distribution_strategist"
    objective = ("Rank free / low-cost distribution channels for an offer and "
                 "turn each into a human-review recommendation; never acts.")
    capabilities = ("research_distribution",)

    def run(self, task: Task) -> Result:
        opp = task.payload.get("opportunity")
        if not isinstance(opp, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['opportunity'] must be a dict")
        plan = build_distribution_plan(
            opportunity=opp,
            offer=task.payload.get("offer")
            if isinstance(task.payload.get("offer"), dict) else None,
            copy=task.payload.get("copy")
            if isinstance(task.payload.get("copy"), dict) else None,
            signals=task.payload.get("signals")
            if isinstance(task.payload.get("signals"), dict) else None,
            now=task.payload.get("now"),
        )
        return Result(task_id=task.id, agent=self.name, status="ok", output=plan)
