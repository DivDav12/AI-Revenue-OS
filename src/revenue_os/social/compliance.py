"""Platform + community compliance gate for social affiliate distribution.

Data, not code - the same shape and the same fail-closed default as
`ecosystem/human_fed.py`'s `PLATFORM_POLICY`: a platform/community that is
not explicitly listed as allowing automated affiliate content gets
`human_required=True`, never an auto-post.

`check()` answers four questions for one intended publication:
  * may an affiliate link appear here at all?
  * is an affiliate disclosure required?
  * may the fleet publish this WITHOUT a human (auto-post)?
  * ... or must a human review + post it (HUMAN_REQUIRED)?

Auto-post is deliberately hard to reach:
  1. the platform/community must be listed here as `auto_post: True`, AND
  2. the operator must have attested it by setting the platform's
     `*_AUTOPOST_CONFIRMED=1` environment variable (they have read the
     current rules and configured a compliant, authorised account).
Either one missing -> HUMAN_REQUIRED. This keeps the code honest even if
a platform quietly changes its rules between releases.

Citations are paraphrased-with-source and carry `verify_before_autopost:
True`; a human must confirm the exact current wording before flipping the
env attestation. Nothing here fetches a policy page.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

PLATFORM_REDDIT = "reddit"
PLATFORM_PINTEREST = "pinterest"

# --- attestation env vars (operator confirms they read the rules) ----------
_AUTOPOST_ENV = {
    PLATFORM_REDDIT: "REDDIT_AUTOPOST_CONFIRMED",
    PLATFORM_PINTEREST: "PINTEREST_AUTOPOST_CONFIRMED",
}


@dataclass(frozen=True)
class PlatformRule:
    affiliate_links_allowed: bool
    disclosure_required: bool
    #: the platform's rules, on their face, permit automated posting of
    #: disclosed affiliate content by an authorised account. Still gated by
    #: the operator env attestation before any auto-post happens.
    auto_post_allowed: bool
    citation: str
    source_url: str
    notes: str = ""
    verify_before_autopost: bool = True


# ---------------------------------------------------------------------------
# platform-level defaults
# ---------------------------------------------------------------------------

_PLATFORM_RULES: dict[str, PlatformRule] = {
    PLATFORM_REDDIT: PlatformRule(
        affiliate_links_allowed=False,     # platform default: assume NOT, per-subreddit decides
        disclosure_required=True,
        auto_post_allowed=False,
        citation=(
            "Reddit self-promotion guidance (reddit.com/wiki/selfpromotion): "
            "self-promotion is acceptable only as a minority of a user's "
            "overall activity ('a general rule of thumb ... is the 9:1 "
            "ratio'); accounts that exist mainly to promote are treated as "
            "spam. Reddit Content Policy Rule 2 prohibits spam and "
            "manipulation. The Reddit Data API Terms require an approved "
            "app and forbid using the API to break subreddit rules. Most "
            "subreddits additionally ban affiliate/referral links in their "
            "own rules - so affiliate posting on Reddit is HUMAN_REQUIRED "
            "unless a specific subreddit's posted rules explicitly allow it."
        ),
        source_url="https://www.reddit.com/wiki/selfpromotion",
        notes=("Per-subreddit rules override this. Add an allowing subreddit "
               "to _COMMUNITY_RULES with a verbatim rule quote."),
    ),
    PLATFORM_PINTEREST: PlatformRule(
        affiliate_links_allowed=True,
        disclosure_required=True,
        auto_post_allowed=True,
        citation=(
            "Pinterest Community Guidelines - Spam ('Affiliate spam'): "
            "affiliate links are permitted, but Pinterest prohibits "
            "'adding affiliate disclosure information inconsistently', "
            "cloaking/redirecting links, and Pins whose destination does "
            "not match the Pin. Pinterest Paid Partnership / advertising "
            "guidance requires disclosure of a commercial relationship. "
            "Pinterest API v5 content publishing requires a business "
            "account and an approved app (trial or standard access)."
        ),
        source_url="https://policy.pinterest.com/en/community-guidelines",
        notes=("Auto-post is allowed by the rules for a disclosed, "
               "non-cloaked affiliate Pin from an authorised business "
               "account - still gated on PINTEREST_AUTOPOST_CONFIRMED."),
    ),
}


# ---------------------------------------------------------------------------
# per-community overrides (subreddits, boards). Empty by default: the
# operator adds a community here ONLY with a verbatim quote of that
# community's own rule that permits disclosed affiliate content.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommunityRule:
    platform: str
    community: str                     # subreddit name w/o "r/", or board id
    affiliate_links_allowed: bool
    self_promo_allowed: bool
    disclosure_required: bool
    auto_post_allowed: bool
    citation: str                      # verbatim quote of the community rule
    added_by: str = ""


_COMMUNITY_RULES: dict[tuple[str, str], CommunityRule] = {}


def register_community_rule(rule: CommunityRule) -> None:
    """Register a per-community rule at runtime (e.g. from a loaded config).

    Intended for a community whose OWN published rules explicitly permit
    disclosed affiliate content. `citation` must be the verbatim rule
    text. This never loosens a platform default silently - it is an
    explicit, attributable operator decision."""
    key = (rule.platform.strip().lower(), rule.community.strip().lower().lstrip("r/"))
    _COMMUNITY_RULES[key] = rule


def _community_rule(platform: str, community: str) -> CommunityRule | None:
    if not community:
        return None
    key = (platform.strip().lower(), community.strip().lower().lstrip("r/"))
    return _COMMUNITY_RULES.get(key)


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ComplianceVerdict:
    platform: str
    community: str
    affiliate_link_allowed: bool
    disclosure_required: bool
    #: True only when: platform+community rules allow it AND the operator
    #: env attestation is present. This is the ONLY flag a caller may use
    #: to skip the human gate.
    auto_post_allowed: bool
    human_required: bool
    reasons: list = field(default_factory=list)
    citation: str = ""
    source_url: str = ""

    def to_dict(self) -> dict:
        return {
            "platform": self.platform, "community": self.community,
            "affiliate_link_allowed": self.affiliate_link_allowed,
            "disclosure_required": self.disclosure_required,
            "auto_post_allowed": self.auto_post_allowed,
            "human_required": self.human_required,
            "reasons": list(self.reasons),
            "citation": self.citation, "source_url": self.source_url,
        }


def _attested(platform: str, environ=None) -> bool:
    env = environ if environ is not None else os.environ
    var = _AUTOPOST_ENV.get(platform.strip().lower(), "")
    return bool(var) and str(env.get(var, "")).strip().lower() in ("1", "true", "yes")


def check(platform: str, community: str = "", *, has_affiliate_link: bool,
          environ=None) -> ComplianceVerdict:
    """Compliance gate for one intended publication.

    `has_affiliate_link` = does the content we want to publish contain an
    affiliate link (vs. a link to our own guide page / plain helpful text)?
    """
    p = (platform or "").strip().lower()
    reasons: list[str] = []

    prule = _PLATFORM_RULES.get(p)
    if prule is None:
        return ComplianceVerdict(
            platform=p, community=community, affiliate_link_allowed=False,
            disclosure_required=True, auto_post_allowed=False, human_required=True,
            reasons=[f"unknown platform {p!r} - failing closed to HUMAN_REQUIRED"],
            citation="no policy on file", source_url="")

    crule = _community_rule(p, community)

    # affiliate link permission
    if crule is not None:
        link_ok = crule.affiliate_links_allowed
        citation = crule.citation
        source_url = prule.source_url
    else:
        link_ok = prule.affiliate_links_allowed
        citation = prule.citation
        source_url = prule.source_url
        if p == PLATFORM_REDDIT:
            reasons.append(
                f"subreddit r/{community or '?'} is not on the reviewed "
                "allow-list - Reddit affiliate posting defaults to HUMAN_REQUIRED")

    disclosure_required = (
        crule.disclosure_required if crule is not None else prule.disclosure_required)

    # auto-post: needs BOTH the rule AND the operator attestation
    rule_allows_autopost = (
        (crule.auto_post_allowed if crule is not None else prule.auto_post_allowed)
        and (link_ok or not has_affiliate_link))
    attested = _attested(p, environ)
    auto_post_allowed = bool(rule_allows_autopost and attested)

    if has_affiliate_link and not link_ok:
        reasons.append("affiliate links are not permitted here - link to our "
                       "own guide page instead, or route to a human")
    if rule_allows_autopost and not attested:
        reasons.append(
            f"platform rules permit auto-post but operator attestation "
            f"{_AUTOPOST_ENV[p]}=1 is not set - staying HUMAN_REQUIRED")
    if not rule_allows_autopost:
        reasons.append("platform/community rules do not clearly permit "
                       "automated affiliate posting - HUMAN_REQUIRED")

    human_required = not auto_post_allowed

    return ComplianceVerdict(
        platform=p, community=community,
        affiliate_link_allowed=bool(link_ok),
        disclosure_required=bool(disclosure_required),
        auto_post_allowed=auto_post_allowed,
        human_required=human_required,
        reasons=reasons, citation=citation, source_url=source_url)
