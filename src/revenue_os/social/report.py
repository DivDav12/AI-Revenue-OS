"""Read-only status view over `data/social_actions.json`."""

from __future__ import annotations

from .store import (
    STATE_HUMAN_REQUIRED,
    STATE_MEASURED,
    STATE_PUBLISHED,
    SocialActionStore,
)


def status(data_dir, *, show_drafts: bool = False, platform: str = "") -> dict:
    store = SocialActionStore.load(data_dir)
    rows = store.all()
    if platform:
        rows = [a for a in rows if a.platform == platform.strip().lower()]

    def _row(a):
        base = {
            "action_id": a.action_id, "platform": a.platform,
            "community": a.community, "state": a.state,
            "target_ref": a.target_ref, "offer_id": a.offer_id,
            "opportunity_id": a.opportunity_id, "link_id": a.link_id,
            "published_url": a.published_url,
            "human_action_needed": a.human_action_needed,
            "metrics": a.metrics,
        }
        if show_drafts:
            base["draft"] = a.draft
        return base

    human_required = [_row(a) for a in rows if a.state == STATE_HUMAN_REQUIRED]
    published = [_row(a) for a in rows if a.state in (STATE_PUBLISHED, STATE_MEASURED)]

    return {
        "summary": store.summary(),
        "human_required": human_required,
        "published": published,
        "all": [_row(a) for a in rows],
    }
