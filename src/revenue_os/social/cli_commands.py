"""CLI wiring for social affiliate distribution.

Kept out of the monolithic `cli.py` on purpose: `register()` is the single
hook `cli.py` calls, so this feature adds one import + one call there and
nothing else. Every handler mirrors the `cli.py` convention: parse args,
call into `social/`, print JSON, return an exit code.
"""

from __future__ import annotations

import json
import os


def _data_dir(args):
    # mirror cli._data_dir without importing the big module at import time
    from ..cli import _data_dir as _dd
    return _dd(args)


def _tracking_base() -> str:
    return os.environ.get("AFFILIATE_TRACKING_BASE_URL", "").rstrip("/")


def _cmd_reddit_scan(args) -> int:
    from .reddit_flow import scan_reddit

    out = scan_reddit(
        _data_dir(args), offer_id=args.offer_id,
        subreddits=list(args.subreddit or []),
        queries=list(args.query) if args.query else None,
        opportunity_id=args.opportunity or "", link_mode=args.link_mode,
        limit_per_query=max(1, int(args.limit_per_query)),
        max_new_actions=max(1, int(args.max_new_actions)),
        time_filter=args.time_filter, tracking_base_url=_tracking_base())
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_pinterest_plan(args) -> int:
    from .pinterest_flow import plan_pinterest

    out = plan_pinterest(
        _data_dir(args), offer_id=args.offer_id,
        opportunity_id=args.opportunity or "", link_mode=args.link_mode,
        board=args.board or "", image_url=args.image_url or "",
        keyword=args.keyword or "", tracking_base_url=_tracking_base())
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_pinterest_connect(args) -> int:
    from . import oauth_store

    data_dir = _data_dir(args)
    if args.refresh_token:
        oauth_store.put(data_dir, "pinterest",
                        refresh_token=args.refresh_token.strip())
        print(json.dumps({"stored": True, "platform": "pinterest",
                          "note": "refresh token saved to data/oauth_tokens.json "
                                  "(not printed)."}, indent=2))
        return 0
    print(json.dumps({
        "stored": False,
        "how_to_get_a_refresh_token": [
            "Create an app at https://developers.pinterest.com/apps/ on a "
            "Pinterest BUSINESS account; add a redirect URI you control.",
            "Set PINTEREST_CLIENT_ID + PINTEREST_CLIENT_SECRET in .env.",
            "Open the authorize URL with scopes "
            "'boards:read,pins:read,pins:write,user_accounts:read' and log in.",
            "Exchange the returned ?code= for tokens (POST /v5/oauth/token, "
            "grant_type=authorization_code).",
            "Re-run: revenue_os social-pinterest-connect --refresh-token <token>",
        ]}, indent=2))
    return 0


def _cmd_status(args) -> int:
    from .report import status

    out = status(_data_dir(args), show_drafts=bool(args.show_drafts),
                 platform=args.platform or "")
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_mark_published(args) -> int:
    from ..store import now_iso
    from .store import STATE_PUBLISHED, SocialActionStore

    store = SocialActionStore.load(_data_dir(args))
    act = store.get(args.action_id)
    if act is None:
        print(json.dumps({"error": f"unknown action {args.action_id!r}"}))
        return 1
    act.state = STATE_PUBLISHED
    act.published_url = args.url
    act.published_at = now_iso()
    act.published_by = args.actor
    act.updated_at = now_iso()
    store.upsert(act)
    store.save()
    print(json.dumps({"action_id": act.action_id, "state": act.state,
                      "published_url": act.published_url,
                      "published_by": act.published_by}, indent=2))
    return 0


def _cmd_measure(args) -> int:
    from .measure import measure

    out = measure(_data_dir(args), days=max(1, int(args.days)))
    print(json.dumps(out, indent=2, default=str))
    return 0


def register(sub, common, actor_only) -> None:
    """Add the social-* subcommands. Called once from cli.build_parser()."""
    srs = sub.add_parser(
        "social-reddit-scan", parents=[common],
        help="Social distribution: search real Reddit threads (official API) "
             "for genuine buying intent that fits an offer, draft a helpful "
             "reply with our real affiliate link + disclosure, and record it "
             "HUMAN_REQUIRED (Reddit is never auto-posted unless a subreddit "
             "is allow-listed AND REDDIT_AUTOPOST_CONFIRMED=1)")
    srs.add_argument("offer_id", metavar="OFFER_ID")
    srs.add_argument("--subreddit", action="append", metavar="NAME", required=True,
                     help="subreddit to search (repeatable)")
    srs.add_argument("--query", action="append", metavar="TEXT",
                     help="search phrase (repeatable); default: derived from the offer")
    srs.add_argument("--opportunity", default="", help="bind to one opportunity id")
    srs.add_argument("--link-mode", choices=("guide", "direct"), default="guide",
                     help="guide: link our deployed buying guide (default); "
                          "direct: link the Amazon product page")
    srs.add_argument("--limit-per-query", type=int, default=8)
    srs.add_argument("--max-new-actions", type=int, default=5)
    srs.add_argument("--time-filter", default="month",
                     choices=("hour", "day", "week", "month", "year", "all"))
    srs.set_defaults(func=_cmd_reddit_scan)

    spp = sub.add_parser(
        "social-pinterest-plan", parents=[common],
        help="Social distribution: build a compliant Pin for an offer + one "
             "deployed guide; publish via the official API when connected + "
             "attested + a creative is supplied, else record it HUMAN_REQUIRED")
    spp.add_argument("offer_id", metavar="OFFER_ID")
    spp.add_argument("--opportunity", default="", help="bind to one opportunity id")
    spp.add_argument("--board", default="", help="board name or id on the connected account")
    spp.add_argument("--link-mode", choices=("guide", "direct"), default="guide")
    spp.add_argument("--keyword", default="",
                     help="override the dominant Pinterest search keyword")
    spp.add_argument("--image-url", default="",
                     help="hosted creative image URL (required for auto-publish)")
    spp.set_defaults(func=_cmd_pinterest_plan)

    spc = sub.add_parser(
        "social-pinterest-connect", parents=[common],
        help="Store the Pinterest OAuth refresh token from the one-time human "
             "grant (written to data/oauth_tokens.json, never printed)")
    spc.add_argument("--refresh-token", default="",
                     help="the refresh token; omit to print how to obtain one")
    spc.set_defaults(func=_cmd_pinterest_connect)

    sst = sub.add_parser(
        "social-status", parents=[common],
        help="Social distribution: read-only view of drafted / HUMAN_REQUIRED "
             "/ published actions (data/social_actions.json)")
    sst.add_argument("--platform", default="", choices=("", "reddit", "pinterest"))
    sst.add_argument("--show-drafts", action="store_true")
    sst.set_defaults(func=_cmd_status)

    smp = sub.add_parser(
        "social-mark-published", parents=[common, actor_only],
        help="Social distribution: a human confirms they posted a "
             "HUMAN_REQUIRED draft and supplies the resulting public URL")
    smp.add_argument("action_id", metavar="ACTION_ID")
    smp.add_argument("--url", required=True, help="the public comment / pin URL")
    smp.set_defaults(func=_cmd_mark_published)

    sme = sub.add_parser(
        "social-measure", parents=[common],
        help="Social distribution: pull real metrics (affiliate redirect "
             "clicks, Pinterest pin analytics) into each action - never "
             "fabricated; revenue stays PartnerNet-only")
    sme.add_argument("--days", type=int, default=30)
    sme.set_defaults(func=_cmd_measure)
