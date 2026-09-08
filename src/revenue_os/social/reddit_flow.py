"""Reddit distribution flow - one bounded, safe pass.

  real threads (official API)               reddit_api.RedditClient.search
    -> buying-intent gate                   social.intent.assess_draft
    -> bind to a deployed guide + offer     (match by topic overlap)
    -> real affiliate link                  social.links.resolve_link
    -> helpful comment draft + disclosure   social.content.reddit_reply
    -> per-target dedup + per-run cap        social.store
    -> compliance gate                      social.compliance.check
    -> HUMAN_REQUIRED + finished draft       (Reddit never auto-posts today)

Nothing is posted unless `compliance.auto_post_allowed` is True (needs an
allow-listed subreddit AND operator attestation) AND a write token exists
- in practice: draft only. One failing query never stops the pass.
"""

from __future__ import annotations

import re

from ..ecosystem import model
from ..ecosystem.affiliate_model import AffiliateAssetStore, AffiliateOfferStore
from ..ecosystem.demand_sources import acq_record_to_draft
from ..store import now_iso as _now_iso
from . import compliance, content, intent, links
from .reddit_api import RedditClient, RedditUnavailable
from .store import (
    STATE_HUMAN_REQUIRED,
    STATE_PUBLISHED,
    SocialAction,
    SocialActionStore,
    new_action_id,
)

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = frozenset({"the", "and", "for", "with", "you", "your", "does", "how",
                   "what", "this", "that", "any", "are", "was", "would"})


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOP}


def default_queries(offer) -> list[str]:
    """A few honest, buyer-intent search phrases derived from the offer's
    own category + keywords. Never fabricates demand - these only retrieve
    candidate threads; the intent gate still judges each on its own text."""
    cat = (offer.category or "").replace("-", " ").replace("_", " ").strip()
    kws = [k for k in (offer.keywords or []) if k][:3]
    base = []
    if cat:
        base += [f"{cat} recommendation", f"best budget {cat}", f"which {cat} should i buy"]
    for k in kws:
        base.append(f"{k} recommendation")
    # de-dup, keep order
    seen, out = set(), []
    for q in base:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out or ["product recommendation"]


def _pick_asset(assets, record_tokens: set[str]):
    """Choose the deployed guide whose topic best overlaps this thread.

    Requires a real topic overlap; only falls back to the sole candidate
    when there is exactly one (unambiguous). Returns None otherwise, so a
    thread never gets linked to an unrelated guide."""
    live = [a for a in assets if a.live_url]
    if not live:
        return None
    best, best_score = None, 0
    for a in live:
        at = _tokens(f"{a.title} {a.slug} {a.guide_title}")
        score = len(at & record_tokens)
        if score > best_score:
            best, best_score = a, score
    if best is not None:
        return best
    return live[0] if len(live) == 1 else None


def scan_reddit(data_dir, *, offer_id: str, subreddits: list[str],
                queries: list[str] | None = None, opportunity_id: str = "",
                link_mode: str = links.LINK_MODE_GUIDE, limit_per_query: int = 8,
                max_new_actions: int = 5, time_filter: str = "month",
                tracking_base_url: str = "", client: RedditClient | None = None,
                environ=None, now_iso: str = "") -> dict:
    now_iso = now_iso or _now_iso()
    offers = AffiliateOfferStore.load(data_dir)
    offer = offers.get(offer_id)
    if offer is None:
        return {"status": "ERROR", "reason": f"offer {offer_id!r} not found"}
    if not offer.usable:
        return {"status": "ERROR",
                "reason": f"offer {offer_id!r} not usable (status={offer.status})"}

    all_assets = [a for a in AffiliateAssetStore.load(data_dir).by_opportunity(opportunity_id)] \
        if opportunity_id else \
        [a for a in AffiliateAssetStore.load(data_dir).all() if a.offer_id == offer_id]
    guide_assets = [a for a in all_assets if a.offer_id == offer_id]
    deployed = [a for a in guide_assets if a.live_url]
    if link_mode == links.LINK_MODE_GUIDE and not deployed:
        return {"status": "HUMAN_REQUIRED", "blocker": "no_deployed_guide",
                "reason": "link_mode=guide needs at least one deployed guide "
                          f"page for offer {offer_id!r}",
                "setup": ["deploy an affiliate guide page for this offer, or "
                          "run with --link-mode direct"]}

    cl = client if client is not None else RedditClient(environ=environ)
    if not cl.available:
        return {"status": "HUMAN_REQUIRED", "blocker": "reddit_not_configured",
                "reason": "Reddit official API is not configured",
                "setup": [
                    "Register an app at https://www.reddit.com/prefs/apps "
                    "(type: 'script' if you also want the human-review post "
                    "path, else 'web app').",
                    "Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and "
                    "REDDIT_USER_AGENT (e.g. 'ai-revenue-os/0.1 by u/<name>') "
                    "in .env.",
                    "Optional (only for the human-approved post path): "
                    "REDDIT_USERNAME + REDDIT_PASSWORD on a 'script' app.",
                ]}

    queries = queries or default_queries(offer)
    subreddits = [s.strip().lstrip("r/").strip() for s in subreddits if s.strip()]
    if not subreddits:
        return {"status": "ERROR", "reason": "no subreddits given (--subreddit)"}

    store = SocialActionStore.load(data_dir)
    errors: list[str] = []
    seen_threads: set[str] = set()
    assessed = relevant = 0
    rejected_no_intent = duplicate = no_link = 0
    new_actions: list[dict] = []

    for sub in subreddits:
        for q in queries:
            if len(new_actions) >= max_new_actions:
                break
            try:
                records = cl.search(q, subreddit=sub, limit=limit_per_query,
                                    time_filter=time_filter)
            except RedditUnavailable as exc:
                errors.append(f"search r/{sub} {q!r}: {exc}")
                continue
            for rec in records:
                if len(new_actions) >= max_new_actions:
                    break
                if rec.url in seen_threads:
                    continue
                seen_threads.add(rec.url)
                if rec.meta.get("over_18"):
                    continue
                assessed += 1
                draft = acq_record_to_draft(rec, now_iso=now_iso)
                a = intent.assess_draft(draft)
                if not a.relevant:
                    rejected_no_intent += 1
                    continue
                relevant += 1
                if store.already_handled("reddit", rec.url, offer_id):
                    duplicate += 1
                    continue

                rtok = _tokens(f"{rec.title} {rec.text}")
                asset = _pick_asset(deployed or guide_assets, rtok)
                if asset is None:
                    no_link += 1
                    continue
                opp_for_link = asset.opportunity_id

                rl = links.resolve_link(
                    data_dir, draft=draft, offer_id=offer_id,
                    opportunity_id=opp_for_link, platform="reddit",
                    community=sub, mode=link_mode,
                    tracking_base_url=tracking_base_url, now_iso=now_iso)
                if not rl.ok:
                    no_link += 1
                    errors.append(f"link {rec.url}: {rl.reason}")
                    continue

                reply = content.reddit_reply(
                    offer=offer, product_intent={"category_phrase": a.category_phrase},
                    matched_terms=sorted(rtok & _tokens(" ".join(offer.keywords))),
                    assessment_reason=a.reason, link_url=rl.published_target,
                    link_mode=link_mode, community=sub)

                verdict = compliance.check("reddit", sub,
                                           has_affiliate_link=(link_mode == links.LINK_MODE_DIRECT),
                                           environ=environ)

                action = SocialAction(
                    action_id=new_action_id(), platform="reddit", community=sub,
                    target_ref=rec.url, opportunity_id=opp_for_link,
                    offer_id=offer_id, asset_id=rl.asset_id, link_id=rl.link_id,
                    intent_decision=a.decision, compliance=verdict.to_dict(),
                    draft={**reply.to_dict(),
                           "thread_title": rec.title,
                           "thread_fullname": rec.meta.get("fullname", ""),
                           "assessment": a.to_dict(),
                           "resolved_link": rl.to_dict()},
                    created_at=now_iso, updated_at=now_iso)

                posted = False
                if verdict.auto_post_allowed and cl.can_write:
                    try:
                        res = cl.submit_comment(
                            thing_fullname=rec.meta.get("fullname", ""),
                            text=reply.body, i_have_read_the_rules=True)
                        action.state = STATE_PUBLISHED
                        action.published_url = res.get("url", "")
                        action.published_at = now_iso
                        action.published_by = "auto"
                        posted = True
                    except RedditUnavailable as exc:
                        errors.append(f"auto-post {rec.url}: {exc}")

                if not posted:
                    action.state = STATE_HUMAN_REQUIRED
                    action.human_action_needed = (
                        f"Open {rec.url} , read r/{sub}'s rules on affiliate / "
                        "self-promotion links, and - only if they allow it and "
                        "the reply genuinely helps - post the draft (edit into "
                        "your own voice) and paste the resulting comment URL "
                        f"back with: revenue_os social-mark-published {action.action_id} "
                        "--url <comment_url>")

                store.upsert(action)
                new_actions.append({
                    "action_id": action.action_id, "state": action.state,
                    "subreddit": sub, "thread": rec.url,
                    "thread_title": rec.title,
                    "link_id": rl.link_id, "link_mode": link_mode,
                    "auto_post_allowed": verdict.auto_post_allowed,
                    "published_url": action.published_url,
                })

    store.save()
    return {
        "status": "OK",
        "offer_id": offer_id, "subreddits": subreddits, "queries": queries,
        "link_mode": link_mode,
        "counts": {
            "threads_assessed": assessed, "relevant": relevant,
            "rejected_no_buying_intent": rejected_no_intent,
            "already_handled": duplicate, "no_usable_link": no_link,
            "new_actions": len(new_actions),
        },
        "new_actions": new_actions,
        "errors": errors,
        "note": ("Reddit affiliate posting is HUMAN_REQUIRED unless a subreddit "
                 "is on the reviewed allow-list AND REDDIT_AUTOPOST_CONFIRMED=1. "
                 "Drafts are ready under `revenue_os social-status`."),
        "ran_at": now_iso,
    }
