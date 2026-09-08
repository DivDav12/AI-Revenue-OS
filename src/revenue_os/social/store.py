"""Persisted record of every social distribution action - `data/social_actions.json`.

One row per (platform, thread-or-pin, offer) unit of work. This is the
measurement backbone: what was drafted, whether a human still needs to
act, what actually went live, and - later - the clicks/revenue joined
back through the affiliate link id.

Never stores anything derived from a visitor. Never fabricates a metric.
Same atomic-write JSON-list pattern as `ecosystem/affiliate_model._JsonListStore`
and `learning.OutcomeStore`.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_FILENAME = "social_actions.json"

# lifecycle
STATE_DRAFTED = "DRAFTED"               # content produced, nothing published
STATE_HUMAN_REQUIRED = "HUMAN_REQUIRED"  # ready for a human to review + post
STATE_REJECTED = "REJECTED"             # intent gate or compliance said no
STATE_PUBLISHED = "PUBLISHED"           # went live (auto or human-confirmed)
STATE_MEASURED = "MEASURED"             # at least one real metric recorded
STATES = (STATE_DRAFTED, STATE_HUMAN_REQUIRED, STATE_REJECTED,
          STATE_PUBLISHED, STATE_MEASURED)

_OPEN_STATES = frozenset({STATE_DRAFTED, STATE_HUMAN_REQUIRED})
_DECIDED_STATES = frozenset({STATE_REJECTED, STATE_PUBLISHED, STATE_MEASURED})


def new_action_id() -> str:
    return f"soc-{uuid.uuid4().hex[:12]}"


@dataclass
class SocialAction:
    action_id: str
    platform: str                      # "reddit" | "pinterest"
    community: str = ""                # subreddit / board
    target_ref: str = ""              # thread URL / pin idea key
    opportunity_id: str = ""
    offer_id: str = ""
    asset_id: str = ""
    link_id: str = ""                 # AffiliateLink.link_id (attribution join)
    state: str = STATE_DRAFTED
    intent_decision: str = ""
    compliance: dict = field(default_factory=dict)
    draft: dict = field(default_factory=dict)   # the ready-to-review content
    human_action_needed: str = ""     # one concrete instruction, "" if none
    published_url: str = ""
    published_at: str = ""
    published_by: str = ""            # "auto" | operator name
    metrics: dict = field(default_factory=dict)   # real, source-attributed only
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id, "platform": self.platform,
            "community": self.community, "target_ref": self.target_ref,
            "opportunity_id": self.opportunity_id, "offer_id": self.offer_id,
            "asset_id": self.asset_id, "link_id": self.link_id,
            "state": self.state, "intent_decision": self.intent_decision,
            "compliance": dict(self.compliance), "draft": dict(self.draft),
            "human_action_needed": self.human_action_needed,
            "published_url": self.published_url, "published_at": self.published_at,
            "published_by": self.published_by, "metrics": dict(self.metrics),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SocialAction":
        d = dict(d or {})
        for k in ("compliance", "draft", "metrics"):
            d[k] = dict(d.get(k) or {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class SocialActionStore:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []

    @classmethod
    def load(cls, data_dir) -> "SocialActionStore":
        s = cls(Path(data_dir) / _FILENAME)
        if s.path.exists():
            try:
                raw = json.loads(s.path.read_text(encoding="utf-8"))
                s._rows = [dict(r) for r in raw] if isinstance(raw, list) else []
            except json.JSONDecodeError:
                s._rows = []
        return s

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._rows, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def all(self) -> list[SocialAction]:
        return [SocialAction.from_dict(r) for r in self._rows]

    def get(self, action_id: str) -> SocialAction | None:
        for r in self._rows:
            if r.get("action_id") == action_id:
                return SocialAction.from_dict(r)
        return None

    def find_by_target(self, platform: str, target_ref: str,
                       offer_id: str) -> SocialAction | None:
        for r in self._rows:
            if (r.get("platform") == platform and r.get("target_ref") == target_ref
                    and r.get("offer_id") == offer_id):
                return SocialAction.from_dict(r)
        return None

    def already_handled(self, platform: str, target_ref: str, offer_id: str) -> bool:
        """True when this exact target already has an action that is open or
        decided - so a re-scan never produces a duplicate draft or a second
        post for the same thread/pin."""
        a = self.find_by_target(platform, target_ref, offer_id)
        return a is not None and a.state in (_OPEN_STATES | _DECIDED_STATES)

    def upsert(self, action: SocialAction) -> None:
        for i, r in enumerate(self._rows):
            if r.get("action_id") == action.action_id:
                self._rows[i] = action.to_dict()
                return
        self._rows.append(action.to_dict())

    # --- read-model helpers ------------------------------------------------
    def open_actions(self) -> list[SocialAction]:
        return [a for a in self.all() if a.state in _OPEN_STATES]

    def summary(self) -> dict:
        rows = self.all()
        by_state: dict[str, int] = {}
        for a in rows:
            by_state[a.state] = by_state.get(a.state, 0) + 1
        return {
            "total": len(rows),
            "by_state": by_state,
            "open": len([a for a in rows if a.state in _OPEN_STATES]),
            "published": len([a for a in rows if a.state in
                              (STATE_PUBLISHED, STATE_MEASURED)]),
        }
