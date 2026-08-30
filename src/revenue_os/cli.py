"""Command-line interface.

Read commands:
  run              discovery cycle against a source, then print the report
                   (--evaluator llm opt-in; keyword heuristic by default)
  report           print the report only (no discovery)
  discover-opportunities   find CURRENT public posts from founders struggling
                           to get first customers (free sources; --source web optional)
  discover-free            same, but ONLY free keyless sources ($0, no Anthropic)
  top-opportunities        the human-review shortlist, ranked by final_score
  outreach-brief ID [--draft llm]   human-review outreach draft for one lead;
                           --draft llm adds a metered, tailored reply draft (never posts)
  autopilot start|status|pause|resume|stop   one orchestrator, EUR 3 pre-sale cap
  revenue-step / revenue-loop / revenue-status   supervisor: one safe non-human
                   action per step (pipeline -> deploy, payment sync, PDF staging),
                   stops at every human gate with a concrete action queue
  revenue-loop --watch   run the supervisor continuously (bounded, resumable,
                   Ctrl-C-safe; deterministic; no spend / API / PayPal)
  experiments / experiment-close ID no_sale|skipped   revenue-experiment ledger
  outreach-feedback   settled outreach outcomes by source/quality/type (advisory)
  review-opportunity ID --approve|--reject   record a human verdict (no contact)
  acquisition-rescore   re-score the whole lead store with the current model ($0)
  acquisition-queue     high/medium prospects still waiting on a human (de-duped)
  outreach-status ID posted|skipped   record what YOU did with a drafted brief
  digest [-q]      one-line summary of what needs the human
  agent-run        operator agent: one loop to a fixed point (also the cron primitive)
  agent-loop       operator agent: tick / sleep / repeat, bounded and resumable
  agent-step / agent-log / agent-goal
  llm-costs        print recorded AI operating spend
  outcomes         retrospective on validated vs rejected candidates
  blockers         list / add / resolve the blockers shown on the dashboard
  dashboard        write a static HTML pipeline snapshot (no discovery)
  dashboard-serve  serve an interactive dashboard on localhost (human gates only)
  candidate NAME   print one candidate's full state
  demo             full end-to-end walkthrough in a throwaway directory

Human decision commands (operate on the persistent --data-dir store):
  approve NAME / reject NAME
  investigate       (--planner llm opt-in; template by default)
  outcome NAME {validated|rejected} --metric TEXT
  prepare-launch    (--proposer llm opt-in; template by default)
  launch NAME
  build-checkout NAME --price N [--currency EUR]   write a real PayPal checkout page
  deploy-checkout NAME    publish checkout.html/intake.html to GitHub Pages (needs
                          GITHUB_TOKEN + GITHUB_PAGES_REPO in .env); stores public_url
  deploy-status NAME      is the checkout page built / deployed?
  plan-deliver ORDER_ID [--send]   render the approved plan to PDF; --send emails it
  intake-import FILE      store buyer intake rows (JSON or CSV) that match a booked payment
  intake-list / intake-show ORDER_ID / intake-review ORDER_ID
  draft-launch-plan ORDER_ID   draft the Customer Launch Plan (web-grounded LLM)
  plan-approve ORDER_ID / plan-render ORDER_ID   human gate, then Markdown
  payment NAME AMOUNT
  paypal-verify NAME ORDER_ID   book one verified PayPal order (read-only API)
  paypal-sync [--days N] [--dry-run]   book recent PayPal payments by custom_id

Cost-control commands (authorize/record only; never move money):
  budget NAME AMOUNT
  authorize-spend NAME AMOUNT --purpose TEXT [--ceiling N]
  deny-spend NAME AMOUNT --purpose TEXT --reason TEXT
  record-spend NAME AMOUNT
  llm-budget [AMOUNT]   show or raise the cumulative AI spend cap
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from .approval import record_decision
from .revenuedashboard import render_html
from .discovery_log import DiscoveryLog
from .llm_spend import LlmSpendLog
from .llm_workers import (
    budget_gate as _llm_budget_gate,
    build_evaluator,
    build_planner,
    build_proposer,
    llm_budget as _llm_budget,
    llm_spend_log as _llm_spend_log,
    record_llm_spend as _record_llm_spend,
)
from .filtering import is_relevant
from .report import digest_line, pipeline_report, render_candidate, render_text
from .revenue import RevenueLedger, mark_launched, record_payment
from .sources import FilteredSource, build_source
from .spend import (
    DEFAULT_CEILING,
    SpendLedger,
    SpendRequest,
    authorize_spend,
    deny_spend,
    record_spend,
    set_budget,
)
from .store import CandidateStore, now_iso
from .validation import record_validation_outcome
from .workflow import investigate_approved, prepare_launch, run_discovery_cycle

_DEFAULT_DATA_DIR = "data"


def _data_dir(args) -> Path:
    explicit = getattr(args, "data_dir", None)
    return Path(explicit or os.environ.get("REVENUE_OS_DATA_DIR", _DEFAULT_DATA_DIR))


def _load(data_dir: Path):
    return (
        CandidateStore.load(data_dir / "candidates.json"),
        RevenueLedger.load(data_dir / "revenue.json"),
        SpendLedger.load(data_dir / "spend.json"),
    )


def _require(store: CandidateStore, name: str):
    candidate = store.get(name)
    if candidate is None:
        raise ValueError(f"unknown candidate: {name!r}")
    return candidate


# --- read commands ---------------------------------------------------------


def _discovery_log(data_dir: Path) -> DiscoveryLog:
    return DiscoveryLog.load(data_dir / "discovery_runs.json")


def _cmd_run(args) -> int:
    data_dir = _data_dir(args)
    store, revenue_ledger, spend_ledger = _load(data_dir)
    discovery_log = _discovery_log(data_dir)
    source = build_source(args.source, args.source_path)
    if args.filter:
        source = FilteredSource(source, is_relevant)
    normalizer, evaluator, est_cost, cache = build_evaluator(
        mode=args.evaluator, source=source, limit=args.limit, model=args.model,
        max_cost_usd=args.max_eval_cost, refresh=args.refresh_eval, data_dir=data_dir,
    )
    run_discovery_cycle(
        source,
        store,
        limit=args.limit,
        shortlist_n=args.shortlist,
        min_score=args.min_score,
        log=discovery_log,
        normalizer=normalizer,
        evaluator=evaluator,
        est_cost_usd=est_cost,
        calibrated=args.calibrated,
    )
    if cache is not None:
        cache.save()
    if evaluator == "llm":
        _record_llm_spend(data_dir, "evaluate", normalizer)
        meter = getattr(normalizer, "meter", None)
        actual = meter.cost_usd if meter is not None else 0.0
        note = " (cost ceiling hit)" if getattr(normalizer, "ceiling_hit", False) else ""
        print(
            f"llm evaluator: est ${est_cost}, actual ${actual}; "
            f"cache {normalizer.cache_hits} hit / "
            f"{normalizer.cache_misses} miss{note}"
        )
    print(render_text(pipeline_report(
        store, revenue_ledger, spend_ledger, discovery_log,
        _llm_spend_log(data_dir), _llm_budget(data_dir),
    )))
    return 0


def _acq_llm_scorer(data_dir: Path, *, model: str, max_cost: float,
                    refresh: bool, est: float):
    from .acquisition_llm import AcquisitionLlmScorer
    from .llm_cache import LlmCache
    from .llm_normalize import build_client

    if est > max_cost:
        raise ValueError(
            f"estimated acquisition-llm cost ${est} exceeds the ${max_cost} "
            "ceiling; nothing was scored")
    ceiling = _llm_budget_gate(data_dir, est, max_cost)
    cache = LlmCache.load(data_dir / "llm_acquisition_cache.json")
    scorer = AcquisitionLlmScorer(
        client=build_client(), model=model, max_cost_usd=ceiling,
        cache=cache, refresh=refresh)
    return scorer, cache


def _acq_web_source(data_dir: Path, *, model: str, max_cost: float,
                    refresh: bool, n_queries: int):
    """Build the opt-in web-search source, budget-gated like --score llm."""
    from .acquisition_web import WebSearchSource
    from .llm_cache import LlmCache
    from .llm_normalize import build_client

    est = round(0.05 * n_queries, 4)   # ~1 grounded call + up to 3 searches / query
    if est > max_cost:
        raise ValueError(
            f"estimated web-search cost ${est} exceeds the ${max_cost} ceiling; "
            "run without --source web or raise --max-cost")
    ceiling = _llm_budget_gate(data_dir, est, max_cost)
    cache = LlmCache.load(data_dir / "llm_acquisition_web_cache.json")
    src = WebSearchSource(client=build_client(), model=model,
                          max_cost_usd=ceiling, cache=cache, refresh=refresh)
    return src, cache


def _outreach_drafter(data_dir: Path, *, model: str, max_cost: float,
                      refresh: bool, lead: dict, checkout_url: str):
    """Build the opt-in tailored-reply drafter, budget-gated like --score llm."""
    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .outreach_llm import OutreachDrafter, estimate_draft_cost_usd

    cache = LlmCache.load(data_dir / "llm_outreach_cache.json")
    est = estimate_draft_cost_usd(
        [lead], model, cache=None if refresh else cache, checkout_url=checkout_url)
    if est > max_cost:
        raise ValueError(
            f"estimated outreach-draft cost ${est} exceeds the ${max_cost} "
            "ceiling; run without --draft llm or raise --max-cost")
    ceiling = _llm_budget_gate(data_dir, est, max_cost)
    drafter = OutreachDrafter(
        client=build_client(), model=model, max_cost_usd=ceiling,
        checkout_url=checkout_url, cache=cache, refresh=refresh)
    return drafter, cache


def _print_lead_row(d: dict) -> None:
    age = ("age unknown" if d.get("age_days") is None
           else f"{d['age_days']}d ({d.get('age_bucket', '?')})")
    print(f"  [{d.get('final_score', d.get('fit_score', 0)):3}] "
          f"q:{d.get('prospect_quality', '?'):6} {d.get('prospect_type', '?'):15} "
          f"{age:20} {d.get('source', ''):12} {d.get('title', '')[:58]}")
    print(f"        {d.get('url', '')}")
    for w in (d.get("why") or [])[:6]:
        print(f"        - {w}")
    if d.get("llm_reason"):
        print(f"        - (llm) {d['llm_reason'][:150]}")


def _source_status_report(names: list[str], statuses: dict) -> None:
    from .acquisition_sources import SOURCE_REGISTRY
    by_tier = {"free": [], "paid": [], "authenticated": [], "other": []}
    for n in names:
        reg = SOURCE_REGISTRY.get(n, {})
        tier = reg.get("tier", "other")
        st = statuses.get(n, "not run")
        by_tier.setdefault(tier, []).append(
            (n, st, reg.get("note", ""), reg.get("auth", False)))
    labels = [("free", "FREE SOURCES"), ("authenticated", "UNAVAILABLE (auth)"),
              ("paid", "PAID"), ("other", "OTHER")]
    for key, label in labels:
        rows = by_tier.get(key) or []
        if not rows:
            continue
        print(f"  {label}")
        for n, st, note, _auth in rows:
            mark = "[ok]" if st == "ok" else "[! ]"
            print(f"    {mark} {n:14} {st}" + (f"  ({note})" if st != "ok" else ""))


def _run_discovery(args, *, names, allow_web, allow_llm):
    from .acquisition import SEARCH_QUERIES, AcquisitionStore
    from .acquisition_sources import build_acquisition_source
    from .workflow import discover_acquisition_opportunities

    data_dir = _data_dir(args)
    queries = tuple(args.query) if args.query else SEARCH_QUERIES

    wants_web = any(n in ("web", "all") for n in names)
    if wants_web and not allow_web:
        raise ValueError("the 'web' source is paid (Anthropic) and is not "
                         "available in this command; use `discover-opportunities`")
    web_source = web_cache = None
    if wants_web:
        web_source, web_cache = _acq_web_source(
            data_dir, model=args.model, max_cost=args.max_cost,
            refresh=args.refresh, n_queries=len(queries))
    source = build_acquisition_source(names, args.source_path, web_source=web_source)

    scorer = cache = None
    if allow_llm and getattr(args, "score", "deterministic") == "llm":
        est = round(0.003 * args.limit * len(queries), 4)
        scorer, cache = _acq_llm_scorer(
            data_dir, model=args.model, max_cost=args.max_cost,
            refresh=args.refresh, est=est)

    store = AcquisitionStore.load(data_dir / "acquisition.json")
    if args.dry_run:
        store.save = lambda: None

    r = discover_acquisition_opportunities(
        store, source, queries=queries, limit=args.limit,
        min_score=args.min_score, max_age_days=args.max_age_days,
        llm_scorer=scorer, politeness_delay=args.delay,
    )
    if not args.dry_run:
        for c in (cache, web_cache):
            if c is not None:
                c.save()
    for w in (scorer, web_source):
        if w is not None:
            _record_llm_spend(data_dir, "acquisition", w)

    _emit_discovery(args, r, names, queries, web_source, scorer)
    return 0


def _emit_discovery(args, r, names, queries, web_source, scorer) -> None:
    leads = r["leads"]
    if args.json:
        print(json.dumps(leads, indent=2))
    else:
        for d in leads:
            _print_lead_row(d)
    for d in r["dropped"]:
        print(f"  dropped: {d['title']} ({', '.join(d['reasons'])})")
    for e in r["query_errors"]:
        print(f"  query error {e['query']!r}: {e['error']}")
    _source_status_report(names, r["sources_status"])
    web_cost = round(web_source.meter.cost_usd, 4) if web_source else 0.0
    llm_cost = round(scorer.meter.cost_usd, 4) if scorer else 0.0
    if web_source is not None:
        print(f"  web search: ${web_cost} ({web_source.searches} searches, "
              f"{web_source.cache_hits} hit / {web_source.cache_misses} miss)")
    if scorer is not None:
        print(f"  llm score: ${llm_cost} ({scorer.cache_hits} hit / "
              f"{scorer.cache_misses} miss)")
    print(f"  external spend this run: ${round(web_cost + llm_cost, 4)}")
    tag = "(dry-run, nothing persisted) " if args.dry_run else ""
    print(f"{tag}{len(queries)} queries - considered {r['considered']} - "
          f"no positive signal {r['no_match']} - collapsed {r['collapsed']} - "
          f"older than {args.max_age_days}d {r['too_old']} - dropped "
          f"{len(r['dropped'])} - this run: {len(leads)} scored, new {r['new']}, "
          f"updated {r['updated']} - {len(r['store_leads'])} total in store")
    print("Human review required. This tool never posts, messages, or "
          "contacts anyone.")


def _cmd_discover_opportunities(args) -> int:
    from .acquisition_sources import FREE_SOURCES
    names = list(args.source) if args.source else list(FREE_SOURCES)
    return _run_discovery(args, names=names, allow_web=True, allow_llm=True)


def _cmd_discover_free(args) -> int:
    from .acquisition_sources import FREE_SOURCES
    names = list(args.source) if args.source else list(FREE_SOURCES)
    bad = [n for n in names if n in ("web", "all")]
    if bad:
        raise ValueError("discover-free uses only free sources; drop "
                         f"{bad} or use `discover-opportunities`")
    args.score = "deterministic"        # never touch Anthropic
    return _run_discovery(args, names=names, allow_web=False, allow_llm=False)



def _cmd_top_opportunities(args) -> int:
    from .acquisition import AcquisitionStore

    store = AcquisitionStore.load(_data_dir(args) / "acquisition.json")
    good_q = {"high", "medium"} if not args.all else {"high", "medium", "low", "none"}
    rows = []
    for d in store.ranked():
        if d.get("human_review_status") == "rejected":
            continue
        # current-model leads only, and only ones actually worth a look
        if not args.all and d.get("prospect_quality", "none") not in good_q:
            continue
        if d.get("final_score", 0) < args.min_score:
            continue
        age = d.get("age_days")
        if args.max_age_days is not None and age is not None and age > args.max_age_days:
            continue
        rows.append(d)
    rows = rows[: args.limit]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print("TOP FREE ACQUISITION OPPORTUNITIES\n")
    if not rows:
        print("  (none match the filters - see the discovery report for why)")
        return 0
    for i, d in enumerate(rows, 1):
        age = ("age unknown" if d.get("age_days") is None
               else f"{d['age_days']}d ({d.get('age_bucket', '?')})")
        print(f"{i}. Score: {d.get('final_score', 0)}  (relevance {d.get('relevance_score', 0)})")
        print(f"   Quality: {d.get('prospect_quality', '?')}   "
              f"Type: {d.get('prospect_type', '?')}   "
              f"Intent: {d.get('buying_intent', '?')}   Age: {age}   "
              f"Source: {d.get('source', '?')}")
        excerpt = (d.get("problem_summary") or d.get("title") or "").strip()[:200]
        print(f'   "{excerpt}"')
        print("   Why:")
        for w in (d.get("why") or [d.get("match_reason", "")]):
            print(f"     - {w}")
        if d.get("llm_reason"):
            print(f"     - (llm) {d['llm_reason']}")
        if d.get("matched_queries"):
            print(f"   Matched queries: {', '.join(d['matched_queries'][:5])}")
        print(f"   URL: {d.get('url', '')}")
        print(f"   Promo policy: {d.get('promo_allowed', 'unknown')} - "
              f"{d.get('promo_note', '')}")
        print(f"   review: {d.get('human_review_status', 'new')}   "
              f"id: {d.get('lead_id', '')}")
        print()
    print("Human review required. The system never posts, messages, or "
          "contacts anyone.")
    return 0


def _cmd_review_opportunity(args) -> int:
    from .acquisition import AcquisitionStore

    if args.approve == args.reject:
        raise ValueError("pass exactly one of --approve / --reject")
    store = AcquisitionStore.load(_data_dir(args) / "acquisition.json")
    status = "reviewed" if args.approve else "rejected"
    entry = store.set_review(args.lead_id, status, actor=args.actor)
    store.save()
    print(f"{entry['lead_id']}: human_review_status -> {status}")
    print(f"   \"{entry.get('title', '')}\"")
    if args.approve:
        print("   Confirmed as a relevant opportunity. This does NOT post, "
              "contact, or message anyone.")
    return 0


def _cmd_acquisition_rescore(args) -> int:
    from .acquisition import AcquisitionStore

    data_dir = _data_dir(args)
    path = data_dir / "acquisition.json"
    if not path.exists():
        print("no acquisition store yet - run `discover-free` first")
        return 1
    store = AcquisitionStore.load(path)
    before = len(store.all())
    r = store.rescore(max_age_days=args.max_age_days)
    if not args.dry_run:
        store.save()

    if args.json:
        print(json.dumps(r, indent=2))
        return 0

    after = store.all()
    quality: dict[str, int] = {}
    for d in after:
        quality[d.get("prospect_quality", "none")] = (
            quality.get(d.get("prospect_quality", "none"), 0) + 1)
    tag = "(dry-run, nothing persisted) " if args.dry_run else ""
    print(f"{tag}ACQUISITION RESCORE  {before} lead(s) -> {r['rescored']} re-derived, "
          f"{r['unchanged']} unchanged, {r['dropped']} dropped ({r['total']} remain)")
    for d in r["dropped_rows"]:
        print(f"  dropped: {d['title']} ({d['reason']})")
    print("  quality now: " + ", ".join(
        f"{k}={quality[k]}" for k in ("high", "medium", "low", "none")
        if quality.get(k)))
    print("Deterministic re-scoring only - no network, no spend, contacts no one.")
    return 0


def _cmd_outreach_brief(args) -> int:
    from .acquisition import AcquisitionStore
    from .outreach import OutreachStore, outreach_brief, resolve_checkout_url

    data_dir = _data_dir(args)
    store = AcquisitionStore.load(data_dir / "acquisition.json")
    lead = store.by_id(args.lead_id)
    if lead is None:
        raise ValueError(f"no single lead matches id {args.lead_id!r}")
    cand_store, _, _ = _load(data_dir)
    checkout_url = args.checkout_url or resolve_checkout_url(cand_store)

    drafter = cache = None
    if getattr(args, "draft", "template") == "llm":
        drafter, cache = _outreach_drafter(
            data_dir, model=args.model, max_cost=args.max_cost,
            refresh=args.refresh, lead=lead, checkout_url=checkout_url)

    brief = outreach_brief(lead, checkout_url=checkout_url, drafter=drafter)
    briefs = OutreachStore.load(data_dir / "outreach.json")
    briefs.put(brief)
    briefs.save()
    if drafter is not None:
        cache.save()
        _record_llm_spend(data_dir, "acquisition", drafter)

    if args.json:
        print(json.dumps(brief, indent=2))
        return 0
    b = brief
    print(f"OUTREACH BRIEF  (lead {b['lead_id']}, quality {b['prospect_quality']}, "
          f"{b['age_bucket']})")
    print(f"  URL:      {b['url']}")
    print(f"  Platform: {b['platform']}   Promo policy: {b['promo_allowed']}")
    print(f"\n  THEIR WORDS:\n    {b['their_words']}")
    print("\n  WHY RELEVANT:")
    for w in b["why_relevant"]:
        print(f"    - {w}")
    print(f"\n  ANSWER ANGLE:\n    {b['answer_angle']}")
    print("\n  TALKING POINTS (generic - adapt to their post):")
    for p in b["talking_points"]:
        print(f"    - {p}")
    print(f"\n  {b['help_first']}")
    print(f"\n  OPTIONAL CTA (last line, after you've actually helped):\n    {b['optional_cta']}")
    print(f"\n  COMMUNITY RULES: {b['promo_note']}")
    dr = b.get("draft_reply")
    if isinstance(dr, dict) and dr.get("error"):
        print(f"\n  TAILORED DRAFT: (llm draft failed: {dr['error']})")
    elif isinstance(dr, dict):
        print("\n  TAILORED DRAFT (llm - edit into your own voice before posting):")
        for line in dr.get("reply_draft", "").splitlines() or [""]:
            print(f"    {line}")
        if dr.get("help_summary"):
            print(f"\n    core advice: {dr['help_summary']}")
        print(f"    soft CTA included: {dr.get('cta_included')}")
        if dr.get("promise_language_flagged"):
            print(f"    !! CHECK - possible promise language: "
                  f"{dr['promise_language_flagged']}")
        for c in dr.get("caveats_for_the_human", []):
            print(f"    - {c}")
    if drafter is not None:
        print(f"\n  llm draft cost: ${round(drafter.meter.cost_usd, 4)} "
              f"({drafter.cache_hits} hit / {drafter.cache_misses} miss)")
    print(f"\n  {b['human_approval']}")
    print(f"  {b['no_fabrication_note']}")
    return 0


def _cmd_acquisition_queue(args) -> int:
    from . import autopilot as ap

    q = ap.acquisition_queue(_data_dir(args))
    if args.json:
        print(json.dumps(q, indent=2))
        return 0
    if not q:
        print("ACQUISITION QUEUE: empty - no high/medium prospect is waiting "
              "on you. Run `discover-free` to find more.")
        return 0
    print(f"ACQUISITION QUEUE - {len(q)} prospect(s) need a human\n")
    for i, row in enumerate(q, 1):
        age = ("age unknown" if row.get("age_days") is None
               else f"{row['age_days']}d ({row.get('age_bucket', '?')})")
        print(f"{i}. [{row.get('prospect_quality', '?')}]  score "
              f"{row.get('final_score', 0)}  {age}  {row.get('platform', '')}")
        print(f'   "{row.get("their_words", "")}"')
        print(f"   stage: {row.get('stage', '?')}   brief: {row.get('brief_status', '?')}"
              f"   review: {row.get('human_review_status', '?')}")
        print(f"   promo: {row.get('promo_allowed', '?')} - {row.get('promo_note', '')}")
        print(f"   next:  {row.get('next_action', '')}")
        print(f"   URL:   {row.get('url', '')}")
        print(f"   id:    {row.get('lead_id', '')}"
              + (f"   ->  revenue_os outreach-brief {row.get('lead_id', '')}"
                 if row.get("stage") == "prepared" else ""))
        print()
    print("The system drafted these. You check each community's self-promotion "
          "rules and post every reply yourself - it never posts, DMs, or emails.")
    return 0


def _cmd_outreach_status(args) -> int:
    from .outreach import OutreachStore

    data_dir = _data_dir(args)
    briefs = OutreachStore.load(data_dir / "outreach.json")
    matches = [b for b in briefs.all()
               if str(b.get("lead_id", "")).startswith(args.lead_id)]
    if len(matches) != 1:
        print(f"error: {'no' if not matches else 'multiple'} brief(s) match "
              f"id {args.lead_id!r}", file=sys.stderr)
        return 1
    lid = matches[0]["lead_id"]
    reason = (getattr(args, "reason", "") or "").strip()
    entry = briefs.set_status(lid, args.status, reason=reason)
    briefs.save()
    print(f"{lid}: outreach status -> {entry['status']}"
          + (f"  ({reason})" if reason else ""))

    # keep the experiment ledger in step with the human's action
    if args.status in ("posted", "skipped"):
        from . import experiments
        experiments.open_from_briefs(data_dir)
        try:
            experiments.advance(data_dir, lid, args.status,
                                note="via outreach-status", reason=reason)
            print(f"  experiment {lid}: -> {args.status}")
        except ValueError as exc:
            print(f"  (experiment not advanced: {exc})")
        print("  Recorded by YOU - this prospect drops out of the acquisition "
              "queue. The system posted nothing.")
    return 0


def _cmd_experiments(args) -> int:
    from . import experiments as ex

    r = ex.rollup(_data_dir(args))
    if args.json:
        print(json.dumps(r, indent=2))
        return 0
    o = r["overall"]
    print(f"EXPERIMENTS  {r['total']} total  ({r['open']} open, {r['closed']} closed)")
    print("  overall: " + ", ".join(f"{k}={o[k]}" for k in ex.STATUSES if o.get(k)))
    if r["by_source"]:
        print("  by source:")
        for src, c in sorted(r["by_source"].items()):
            print(f"    {src:16} " + " ".join(
                f"{k}={c[k]}" for k in ex.STATUSES if c.get(k)))
    if r["rows"]:
        print("  rows:")
        for row in r["rows"]:
            age = "age ?" if row["age_days"] is None else f"{row['age_days']}d"
            print(f"    {row['status']:8} {str(row['offer_price']):>6} "
                  f"{row['currency']:3} {row['source']:14} {age:6}"
                  + (f"  {row['revenue_ref']}" if row["revenue_ref"] else ""))
    print("Deterministic ledger - no network, no PayPal, no LLM. Tracking only.")
    return 0


def _cmd_outreach_feedback(args) -> int:
    from . import experiments as ex

    fb = ex.feedback(_data_dir(args))
    if args.json:
        print(json.dumps(fb, indent=2))
        return 0
    print(f"OUTREACH FEEDBACK  {fb['settled']}/{fb['needed']} settled "
          f"({fb['sale']} sale, {fb['no_sale']} no_sale)  ready={fb['ready']}")
    print(f"  {fb['note']}")
    for dim in ("by_source", "by_quality", "by_type"):
        d = fb.get(dim) or {}
        if not d:
            continue
        print(f"  {dim.replace('by_', 'by ')}:")
        for k, b in d.items():
            print(f"    {k:16} settled={b['settled']} sale={b['sale']} "
                  f"no_sale={b['no_sale']} rate={b['sale_rate']}")
    print("Deterministic, read-only. No weighting / query / source change is "
          "applied automatically (>=8-settled rule).")
    return 0


def _cmd_experiment_close(args) -> int:
    from . import experiments as ex

    reason = (getattr(args, "reason", "") or "").strip()
    try:
        entry = ex.advance(_data_dir(args), args.lead_id, args.status,
                           note="manual close via CLI", reason=reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"experiment {entry['lead_id']}: -> {entry['status']}"
          + (f"  ({reason})" if reason else ""))
    return 0


def _cmd_pipeline(args) -> int:
    from .pipeline import pipeline_status, run_pipeline

    data_dir = _data_dir(args)
    if args.action == "status":
        rep = pipeline_status(data_dir, args.name)
    else:
        if not args.name:
            print("pipeline run: a candidate name is required")
            return 2
        rep = run_pipeline(data_dir, args.name, restart=args.restart)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(f"PIPELINE  candidate={rep.get('candidate')}  status={rep.get('status')}")
    for s in rep.get("steps", []):
        line = f"  {s['step']:<20} {s['status']}"
        if s.get("reason"):
            line += f"  - {s['reason']}"
        elif s.get("summary"):
            line += f"  {s['summary']}"
        print(line)
    hg = rep.get("human_gate")
    if hg:
        print(f"\n  HUMAN GATE: {hg.get('reason', '')}")
        if hg.get("public_url"):
            print(f"    LIVE checkout: {hg['public_url']}")
        if hg.get("payment_ready"):
            print("    payment-ready: a real buyer can now pay on that page")
        for x in (hg.get("blocking_issues") or hg.get("human_gated_next") or []):
            print(f"    - {x}")
    if rep.get("error"):
        print(f"\n  ERROR: {rep['error']}")
    print("\n  The pipeline publishes nothing, sends nothing, spends nothing.")
    return 0


def _cmd_autopilot(args) -> int:
    from . import autopilot as ap

    data_dir = _data_dir(args)
    action = args.action
    if action == "pause":
        ap.pause(data_dir, args.reason or "manual pause")
        print("AUTOPILOT PAUSED")
        return 0
    if action == "resume":
        ap.resume(data_dir)
        print("AUTOPILOT RESUMED")
        return 0
    if action == "stop":
        ap.stop(data_dir)
        print("AUTOPILOT STOPPED (state preserved - `autopilot start` resumes)")
        return 0
    if action == "status":
        print(json.dumps(ap.status(data_dir), indent=2))
        return 0

    # start / cycle
    r = ap.run_cycle(data_dir, allow_web=args.allow_web,
                     max_age_days=args.max_age_days, limit=args.limit,
                     politeness_delay=args.delay, checkout_url=args.checkout_url)
    if "skipped" in r:
        print(f"AUTOPILOT: {r['skipped']}")
        return 0
    if args.json:
        print(json.dumps(r, indent=2))
        return 0
    cap = r["spend"]
    print("AUTOPILOT CYCLE COMPLETE")
    print(f"  Pre-sale: {'ACTIVE' if cap['presale_active'] else 'OFF (a real sale exists)'}"
          f"   spent ${cap['external_spent_usd']:.2f} / ${cap['presale_cap_usd']:.2f}"
          f"   (EUR {cap['presale_cap_eur']:.2f} cap)")
    d = r.get("discovery", {})
    if "scored" in d:
        print(f"  Discovery: {d['scored']} scored, {d['new']} new, "
              f"web cost ${d.get('web_cost_usd', 0)}")
        for n, s in sorted(d.get("sources_status", {}).items()):
            print(f"    source {n}: {s}")
    o = r.get("outreach", {})
    print(f"  Outreach: {o.get('prepared', 0)} briefs prepared, "
          f"{o.get('awaiting_post', 0)} awaiting a human post")
    aq = r.get("acquisition_queue", [])
    if aq:
        print(f"  Acquisition queue: {len(aq)} prospect(s) need you "
              "(`revenue_os acquisition-queue`)")
        for row in aq[:5]:
            age = ("age unknown" if row.get("age_days") is None
                   else f"{row['age_days']}d/{row.get('age_bucket', '?')}")
            print(f"    - [{row.get('prospect_quality', '?')}] "
                  f"{row.get('stage', '?'):9} promo:{row.get('promo_allowed', '?'):8} "
                  f"{age:16} {row.get('lead_id', '')}  {row.get('url', '')}")
        if len(aq) > 5:
            print(f"    ... and {len(aq) - 5} more")
    p = r.get("payment", {})
    print(f"  Payment: {'ok' if p.get('ok') else p.get('note', 'skipped')}"
          + (f" - {len(p.get('booked', []))} new" if p.get("ok") else ""))
    if r.get("sale"):
        print("  *** NEW SALE BOOKED - pre-sale mode is now OFF ***")
    for note in r.get("notes", []):
        print(f"  NOTE: {note}")
    print("\n  HUMAN ACTION QUEUE:")
    if not r["actions"]:
        print(f"    (none) {r.get('idle', '')}")
    for a in r["actions"]:
        print(f"    - {a}")
    print("\n  The autopilot never posts, messages, emails, or spends past the "
          "EUR 3.00 pre-sale cap.")
    return 0


def _print_loop_step(s: dict) -> None:
    print(f"  {s['action']}: {s['reason']}")
    r = s.get("result") or {}
    if r and not r.get("noop"):
        compact = {k: v for k, v in r.items() if k != "observed"}
        print(f"    -> {compact}")


def _cmd_revenue_step(args) -> int:
    from . import revenue_loop as rl

    out = rl.step(_data_dir(args), allow_discovery=not args.no_discovery,
                  discovery_cooldown_hours=getattr(
                      args, "discovery_cooldown_hours", 6.0))
    _print_loop_step(out)
    if out["action"] == "stop":
        print("\n  HUMAN ACTION QUEUE:")
        for a in out["human_queue"] or ["    (nothing - waiting on discovery)"]:
            print(f"    - {a}")
    return 0


def _cmd_revenue_loop(args) -> int:
    from . import revenue_loop as rl

    data_dir = _data_dir(args)
    if getattr(args, "watch", False):
        def on_tick(steps, feedback):
            if args.dashboard:
                _write_dashboard(data_dir)
            acted = [s["action"] for s in steps if s["action"] != "stop"]
            fb = {k: v for k, v in (feedback or {}).items() if v}
            print(f"  tick: {acted or 'no non-human action'}"
                  + (f"  feedback={fb}" if fb else ""))
        sess = rl.watch(
            data_dir, interval=args.interval, max_ticks=args.max_ticks,
            max_runtime_s=args.max_runtime, max_spend_usd=args.max_spend,
            fresh=args.fresh, allow_discovery=not args.no_discovery,
            max_steps=args.max_steps,
            discovery_cooldown_hours=args.discovery_cooldown_hours,
            followup_days=args.followup_days, on_tick=on_tick)
        if args.dashboard:
            _write_dashboard(data_dir)
        print(f"stopped: {sess['end_reason']} ({sess['ticks']} tick(s))")
        print("  No money spent, no message sent, no API/PayPal call. Every "
              "human gate is intact.")
        return 0

    steps = rl.run(data_dir, max_steps=args.max_steps,
                   allow_discovery=not args.no_discovery,
                   discovery_cooldown_hours=args.discovery_cooldown_hours)
    print(f"REVENUE LOOP: {len(steps)} step(s)")
    for s in steps:
        _print_loop_step(s)
    last = steps[-1]
    if last["action"] == "stop":
        print("\n  HUMAN ACTION QUEUE (loop stopped - only human steps remain):")
        for a in last["human_queue"] or ["    (nothing - waiting on discovery)"]:
            print(f"    - {a}")
    print("\n  The loop published only the operator's own checkout page, sent no "
          "messages, and spent no money.")
    return 0


def _cmd_revenue_status(args) -> int:
    from . import revenue_loop as rl

    data_dir = _data_dir(args)
    state = rl.load_state(data_dir)
    state["human_queue"] = rl._human_queue(rl.observe(data_dir))
    print(json.dumps(state, indent=2))
    return 0


def _cmd_report(args) -> int:
    data_dir = _data_dir(args)
    store, revenue_ledger, spend_ledger = _load(data_dir)
    print(render_text(pipeline_report(
        store, revenue_ledger, spend_ledger,
        _discovery_log(data_dir), _llm_spend_log(data_dir), _llm_budget(data_dir),
    )))
    return 0


def _cmd_llm_budget(args) -> int:
    data_dir = _data_dir(args)
    budget = _llm_budget(data_dir)
    if args.amount is None:
        spent = _llm_spend_log(data_dir).summary()["total_cost_usd"]
        print(
            f"cap ${budget.cap}  spent ${spent}  "
            f"remaining ${round(budget.cap - spent, 4)}"
        )
        return 0
    new_cap = budget.set_cap(args.amount, actor=args.actor)
    print(f"llm budget cap -> ${new_cap}")
    return 0


def _cmd_digest(args) -> int:
    data_dir = _data_dir(args)
    store, revenue_ledger, spend_ledger = _load(data_dir)
    queue = pipeline_report(store, revenue_ledger, spend_ledger)["action_queue"]
    if args.quiet:
        return 1 if queue else 0
    print(digest_line(queue))
    return 0


def _cmd_agent_goal(args) -> int:
    from dataclasses import replace

    from .operator import load_goal, save_goal

    data_dir = _data_dir(args)
    goal = load_goal(data_dir)
    updates: dict = {}
    if args.sources is not None:
        updates["sources"] = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    if args.filter is not None:
        updates["filter"] = args.filter
    if args.calibrated is not None:
        updates["calibrated"] = args.calibrated
    if args.min_score is not None:
        updates["min_score"] = args.min_score
    if args.shortlist is not None:
        updates["shortlist_n"] = args.shortlist
    if args.limit is not None:
        updates["limit"] = args.limit
    if args.target_validated is not None:
        updates["target_validated"] = (
            None if args.target_validated < 0 else args.target_validated
        )
    if args.evaluator is not None:
        updates["evaluator"] = args.evaluator
    if args.planner is not None:
        updates["planner"] = args.planner
    if args.proposer is not None:
        updates["proposer"] = args.proposer
    if args.research is not None:
        updates["research"] = args.research
    if args.competition is not None:
        updates["competition"] = args.competition
    if args.copywriter is not None:
        updates["copywriter"] = args.copywriter
    if args.trend_hunter is not None:
        updates["trend_hunter"] = args.trend_hunter
    if args.revenue_analyst is not None:
        updates["revenue_analyst"] = args.revenue_analyst
    if args.content_creator is not None:
        updates["content_creator"] = args.content_creator
    if args.decision_policy is not None:
        updates["decision_policy"] = args.decision_policy
    if args.model is not None:
        updates["model"] = args.model
    if args.max_llm_cost_per_action is not None:
        updates["max_llm_cost_per_action"] = args.max_llm_cost_per_action
    if args.max_decision_cost is not None:
        updates["max_decision_cost"] = args.max_decision_cost
    if updates:
        goal = replace(goal, **updates)
        save_goal(data_dir, goal)
    print(json.dumps(goal.to_dict(), indent=2))
    return 0


def _cmd_agent_step(args) -> int:
    from .operator import OperatorAgent, load_goal

    data_dir = _data_dir(args)
    step = OperatorAgent(data_dir, load_goal(data_dir)).step()
    print(f"{step.decision.action}: {step.decision.reason}")
    if step.result:
        print(f"  -> {step.result}")
    print(step.digest)
    return 0


def _print_steps(steps) -> None:
    for step in steps:
        line = f"  {step.decision.action}: {step.decision.reason}"
        if step.result:
            line += f"  {step.result}"
        print(line)


def _cmd_agent_run(args) -> int:
    from .operator import OperatorAgent, load_goal

    data_dir = _data_dir(args)
    steps = OperatorAgent(data_dir, load_goal(data_dir)).run(max_cycles=args.max_cycles)
    _print_steps(steps)
    print(steps[-1].digest if steps else "nothing to do")
    return 0


def _cmd_agent_loop(args) -> int:
    from .operator import OperatorAgent, load_goal

    data_dir = _data_dir(args)
    agent = OperatorAgent(data_dir, load_goal(data_dir))

    def on_tick(steps):
        if args.dashboard:
            _write_dashboard(data_dir)
        if args.quiet and len(steps) == 1 and steps[0].decision.action == "stop":
            return
        _print_steps(steps)
        print(f"  digest: {steps[-1].digest}")

    session = agent.run_continuous(
        args.interval,
        max_ticks=args.max_ticks,
        max_runtime_s=args.max_runtime,
        max_total_cycles=args.max_total_cycles,
        max_spend_usd=args.max_spend,
        fresh=args.fresh,
        on_tick=on_tick,
    )
    if args.dashboard:
        _write_dashboard(data_dir)
    print(
        f"stopped: {session.end_reason} "
        f"({session.ticks} tick(s), {session.cycles} cycle(s))"
    )
    return 0


def _cmd_agent_log(args) -> int:
    from .agent_log import AgentLog

    data_dir = _data_dir(args)
    entries = AgentLog.load(data_dir / "agent_log.json").entries()
    if not entries:
        print("(no agent decisions recorded)")
        return 0
    for e in entries[-args.limit:]:
        print(f"{e['ts']}  cycle {e['cycle']}  {e['action']}: {e['reason']}")
    return 0


def _cmd_outcomes(args) -> int:
    from .calibration import calibration_weights
    from .opportunity import CRITERIA
    from .retro import outcome_retro

    store, _, _ = _load(_data_dir(args))
    retro = outcome_retro(store)
    c = retro["counts"]
    have = c["validated"] + c["rejected"]
    if not retro["ready"]:
        print(f"(need more recorded outcomes; have {have})")
        return 0
    tot = retro["total"]
    print(
        f"validated {c['validated']} / rejected {c['rejected']}  "
        f"avg score {tot['validated_avg']} vs {tot['rejected_avg']}"
    )
    weights = calibration_weights(store)
    print(f"  {'criterion':<24} {'validated':>10} {'rejected':>10} {'gap':>8} {'weight':>8}")
    for name in CRITERIA:
        row = retro["by_criterion"][name]
        w = "-" if weights is None else weights[name]
        print(
            f"  {name:<24} {row['validated_avg']:>10} "
            f"{row['rejected_avg']:>10} {row['gap']:>+8} {w:>8}"
        )
    if weights is None:
        print("  weights: equal (need >= 8 outcomes with both classes)")
    print("recorded outcomes:")
    for o in retro["outcomes"]:
        print(f"  {o['name']} [{o['outcome']}] score={o['score']} -> {o['metric_value']}")
    return 0


def _cmd_llm_costs(args) -> int:
    data_dir = _data_dir(args)
    entries = _llm_spend_log(data_dir).entries()
    if not entries:
        print("(no LLM runs recorded)")
        return 0
    for e in entries:
        print(
            f"{e['ts']}  {e['activity']:<8} {e.get('model', '')}  "
            f"calls={e.get('api_calls', 0)} "
            f"tokens={e.get('input_tokens', 0)}+{e.get('output_tokens', 0)} "
            f"cost=${e.get('cost_usd', 0)} "
            f"cache={e.get('cache_hits', 0)}h/{e.get('cache_misses', 0)}m"
        )
    s = LlmSpendLog.load(data_dir / "llm_spend.json").summary()
    by = s["by_activity"]
    activities = " ".join(f"{a} ${by[a]}" for a in by)
    print(
        f"total ${s['total_cost_usd']} over {s['runs']} run(s), "
        f"{s['total_api_calls']} api call(s) ({activities})"
    )
    return 0


def build_dashboard_html(
    data_dir: Path, *, interactive: bool = False,
    flash: str | None = None, csrf: str | None = None,
) -> str:
    """Load all persisted state and render the dashboard HTML. Shared by
    the static `dashboard` writer and the `dashboard-serve` server so both
    build the report identically."""
    import json

    from .agent_log import AgentLog
    from .operator import load_goal, session_dict
    from .task_log import load_task_log

    store, revenue_ledger, spend_ledger = _load(data_dir)
    report = pipeline_report(
        store, revenue_ledger, spend_ledger,
        _discovery_log(data_dir), _llm_spend_log(data_dir), _llm_budget(data_dir),
    )

    def _load_json(name):
        p = data_dir / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    return render_html(
        report, generated_at=now_iso(),
        agent_log=AgentLog.load(data_dir / "agent_log.json").entries(),
        session=session_dict(data_dir),
        spend_entries=_llm_spend_log(data_dir).entries(),
        goal=load_goal(data_dir).to_dict(),
        task_log=load_task_log(data_dir).entries(),
        trend=_load_json("trend_report.json"),
        revenue_analysis=_load_json("revenue_analysis.json"),
        agent_outputs=_load_json("agent_outputs.json"),
        pipeline=_load_json("pipeline.json"),
        acquisition={
            "leads": _load_json("acquisition.json") or [],
            "briefs": _load_json("outreach.json") or [],
            "queue": _acquisition_queue_safe(data_dir),
            "readiness": _first_sale_readiness(data_dir, store, report),
            "loop": _load_json("revenue_loop.json") or {},
            "experiments": _experiments_snapshot_safe(data_dir),
        },
        blockers=_blockers_safe(data_dir),
        interactive=interactive, flash=flash, csrf=csrf,
    )


def _blockers_safe(data_dir: Path) -> list:
    """The human-maintained blocker register; never fail a dashboard build."""
    try:
        from .blockers import load_blockers
        return load_blockers(data_dir).all()
    except Exception:
        return []


def _acquisition_queue_safe(data_dir: Path) -> list:
    """autopilot.acquisition_queue, but never let a dashboard build fail."""
    try:
        from . import autopilot as _ap
        return _ap.acquisition_queue(data_dir)
    except Exception:
        return []


def _experiments_snapshot_safe(data_dir: Path) -> dict:
    """Deterministic, read-only experiment rollup; never fail the build."""
    try:
        from . import experiments as _ex
        return {"rollup": _ex.rollup(data_dir), "feedback": _ex.feedback(data_dir)}
    except Exception:
        return {}


def _first_sale_readiness(data_dir: Path, store, report: dict) -> dict | None:
    """Disk-only facts the 'first sale readiness' panel needs. No API calls."""
    launched = next(
        (c for c in store.all()
         if c.status in ("launched", "earning") and c.offer), None)
    if launched is None:
        return None
    from .outreach import resolve_checkout_url

    spend = _llm_spend_log(data_dir)
    r = {
        "candidate": launched.name,
        "candidate_status": launched.status,
        "offer_price": launched.offer.get("price"),
        "offer_currency": launched.offer.get("currency", "EUR"),
        "candidate_public_url": launched.public_url or "",
        # what an outreach brief actually resolves to right now
        "outreach_default_url": resolve_checkout_url(store),
        "revenue_eur": report["totals"]["grand_revenue"],
        "llm_api_calls": sum(int(e.get("api_calls", 0)) for e in spend.entries()),
        "llm_cost_usd": spend.summary()["total_cost_usd"],
        "checkout_built": False,
        "checkout_deployed": False,
    }
    try:
        from .deploy import deploy_status
        ds = deploy_status(data_dir, launched.name)
        r["checkout_built"] = bool(ds.get("checkout_built"))
        r["checkout_deployed"] = bool(ds.get("deployed"))
    except Exception:
        pass
    return r


def _write_dashboard(data_dir: Path, out: Path | None = None) -> Path:
    html = build_dashboard_html(data_dir)
    out = out or data_dir / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def _cmd_dashboard(args) -> int:
    data_dir = _data_dir(args)
    out = _write_dashboard(data_dir, Path(args.out) if args.out else None)
    print(f"dashboard written: {out}")
    return 0


def _cmd_blockers(args) -> int:
    """The blocker register is human-maintained: nothing here detects a
    blocker, and the dashboard shows exactly what this command records."""
    from .blockers import load_blockers

    data_dir = _data_dir(args)
    store = load_blockers(data_dir)
    action = getattr(args, "action", "list")

    if action == "add":
        if not args.id or not args.title:
            print("usage: blockers add ID --title TEXT [--detail TEXT] "
                  "[--area TEXT] [--severity critical|warning|info]")
            return 2
        entry = store.add(args.id, args.title, area=args.area, detail=args.detail,
                          severity=args.severity)
        store.save()
        print(f"blocker recorded: {entry['id']} [{entry['severity']}] {entry['title']}")
        return 0

    if action == "resolve":
        if not args.id:
            print("usage: blockers resolve ID")
            return 2
        try:
            entry = store.resolve(args.id)
        except ValueError as exc:
            print(str(exc))
            return 1
        store.save()
        print(f"blocker resolved: {entry['id']}")
        return 0

    rows = store.all() if args.all else store.open()
    if not rows:
        print("no blockers recorded"
              if args.all else "no open blockers (nothing recorded, or all resolved)")
        return 0
    for entry in rows:
        state = entry.get("status", "open")
        area = f" ({entry['area']})" if entry.get("area") else ""
        print(f"{entry.get('id')}  [{entry.get('severity')}] {state}{area}  "
              f"{entry.get('title')}")
        if entry.get("detail"):
            print(f"    {entry['detail']}")
    return 0


def _cmd_dashboard_serve(args) -> int:
    from .dashboard_server import serve

    serve(_data_dir(args), host=args.host, port=args.port, actor=args.actor)
    return 0


def _cmd_candidate(args) -> int:
    store, _, _ = _load(_data_dir(args))
    print(render_candidate(_require(store, args.name)))
    return 0


def _cmd_demo(args) -> int:
    from .runner import demo

    demo()
    return 0


# --- human decision commands ---------------------------------------------


def _cmd_approve(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = record_decision(store, args.name, "approve", approver=args.actor, note=args.note)
    print(f"approved: {out.name} -> {out.status}")
    return 0


def _cmd_reject(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = record_decision(store, args.name, "reject", approver=args.actor, note=args.note)
    print(f"rejected: {out.name} -> {out.status}")
    return 0


def _cmd_investigate(args) -> int:
    data_dir = _data_dir(args)
    store, _, _ = _load(data_dir)
    planner, cache = build_planner(
        mode=args.planner, store=store, model=args.model,
        max_cost_usd=args.max_plan_cost, refresh=args.refresh_plan, data_dir=data_dir,
    )
    investigating = investigate_approved(store, planner=planner)
    if cache is not None:
        cache.save()
    if args.planner == "llm":
        _record_llm_spend(data_dir, "plan", planner)
        meter = getattr(planner, "meter", None)
        actual = meter.cost_usd if meter is not None else 0.0
        note = " (cost ceiling hit)" if getattr(planner, "ceiling_hit", False) else ""
        print(
            f"llm planner: actual ${actual}; cache {planner.cache_hits} hit / "
            f"{planner.cache_misses} miss{note}"
        )
    print(f"investigating: {len(investigating)} candidate(s)")
    return 0


def _cmd_outcome(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = record_validation_outcome(
        store, args.name, args.result, metric_value=args.metric, actor=args.actor,
        note=args.note,
    )
    print(f"outcome: {out.name} -> {out.status}")
    return 0


def _cmd_prepare_launch(args) -> int:
    data_dir = _data_dir(args)
    store, _, _ = _load(data_dir)
    proposer, cache = build_proposer(
        mode=args.proposer, store=store, model=args.model,
        max_cost_usd=args.max_offer_cost, refresh=args.refresh_offer, data_dir=data_dir,
    )
    validated = prepare_launch(store, proposer=proposer)
    if cache is not None:
        cache.save()
    if args.proposer == "llm":
        _record_llm_spend(data_dir, "offer", proposer)
        meter = getattr(proposer, "meter", None)
        actual = meter.cost_usd if meter is not None else 0.0
        note = " (cost ceiling hit)" if getattr(proposer, "ceiling_hit", False) else ""
        print(
            f"llm proposer: actual ${actual}; cache {proposer.cache_hits} hit / "
            f"{proposer.cache_misses} miss{note}"
        )
    print(f"prepared: offer attached to {len(validated)} validated candidate(s)")
    return 0


def _cmd_launch(args) -> int:
    store, _, _ = _load(_data_dir(args))
    out = mark_launched(store, args.name, actor=args.actor, note=args.note)
    print(f"launched: {out.name} -> {out.status}")
    return 0


def _cmd_payment(args) -> int:
    store, revenue_ledger, _ = _load(_data_dir(args))
    out = record_payment(
        store, revenue_ledger, args.name, args.amount, actor=args.actor, note=args.note
    )
    print(f"payment: {out.name} {args.amount} -> {out.status}")
    return 0


def _cmd_paypal_verify(args) -> int:
    from .paypal import verify_and_book_order

    store, revenue_ledger, _ = _load(_data_dir(args))
    r = verify_and_book_order(
        store, revenue_ledger, candidate=args.name, order_id=args.order_id,
        actor=args.actor, force=args.force,
    )
    print(f"{r['outcome']}: {r['candidate']} +{r['amount']} {r['currency']} "
          f"(paypal:{r['capture_id']})")
    return 0


def _cmd_build_checkout(args) -> int:
    from .deliverable import render_checkout_html, render_intake_html
    from .offer import paid_offer

    data_dir = _data_dir(args)
    store, _, _ = _load(data_dir)
    cand = _require(store, args.name)
    if cand.status not in ("launched", "earning"):
        raise ValueError(
            f"candidate {args.name!r} is {cand.status!r}; must be launched or "
            "earning before taking a real payment (run `revenue_os launch`)"
        )

    client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    env = os.environ.get("PAYPAL_ENV", "sandbox").strip().lower()
    if not client_id:
        raise ValueError("set PAYPAL_CLIENT_ID in the environment")
    if env != "live":
        raise ValueError(f"PAYPAL_ENV is {env!r}; set PAYPAL_ENV=live for a real checkout")

    offer = dict(cand.offer)
    if args.price is not None:
        offer = paid_offer(
            cand, price=args.price, currency=args.currency,
            what_is_sold=args.what or "", delivery=args.delivery,
            call_to_action=args.cta or "", positioning=args.promise or "",
            includes=tuple(args.include or ()),
            delivery_note=args.delivery_note or "",
            disclaimer=args.disclaimer or "",
        ).to_dict()
        store.put(replace(cand, offer=offer))
        store.save()
    if not offer.get("price"):
        raise ValueError("no offer on this candidate; pass --price to create one")

    form_action = (args.form_action or "").strip()
    business_email = (args.business_email
                      or os.environ.get("BUSINESS_EMAIL", "")).strip()
    html = render_checkout_html(
        {"name": cand.name, "description": cand.description}, offer,
        client_id=client_id, currency=offer.get("currency") or args.currency,
        form_action=form_action, business_email=business_email,
    )
    out = (Path(args.out) if args.out
           else data_dir / "deliverables" / cand.name / "checkout.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    intake_path = out.parent / "intake.html"
    intake_path.write_text(
        render_intake_html(cand.name, form_action=form_action,
                           product=offer.get("what_is_sold") or cand.name,
                           business_email=business_email),
        encoding="utf-8",
    )
    print(f"wrote {out}")
    print(f"wrote {intake_path}")
    print(f"custom_id = {cand.name}")
    print(f"price = {offer['price']} {offer.get('currency', 'EUR')}")
    if not form_action:
        print("NOTE: form endpoint not set - pass --form-action <url> so the "
              "intake form submits somewhere (both pages show a placeholder).")
    if not business_email:
        print("NOTE: BUSINESS_EMAIL not set - pages show the generic "
              "\"the address that sold you this plan\" wording.")
    print(
        f"after a real payment: `revenue_os paypal-sync`, then "
        f"`revenue_os intake-import <export.json>`"
    )
    return 0


def _cmd_deploy_checkout(args) -> int:
    from .deploy import DeployError, deploy_checkout

    try:
        r = deploy_checkout(_data_dir(args), args.name)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"deployed: {args.name} -> {r['repo']}@{r['branch']}")
    if r["deployed"]:
        print(f"  updated: {', '.join(r['deployed'])}")
    if r["unchanged"]:
        print(f"  unchanged: {', '.join(r['unchanged'])}")
    print(f"  public checkout URL: {r['public_url']}")
    if r.get("intake_url"):
        print(f"  public intake URL:   {r['intake_url']}")
    print("  GitHub Pages may take ~1 min to serve the first change.")
    return 0


def _cmd_deploy_status(args) -> int:
    from .deploy import DeployError, deploy_status

    try:
        r = deploy_status(_data_dir(args), args.name)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2))
    return 0


def _cmd_paypal_sync(args) -> int:
    from .paypal import sync_transactions

    store, revenue_ledger, _ = _load(_data_dir(args))
    r = sync_transactions(
        store, revenue_ledger, days=args.days, actor=args.actor, dry_run=args.dry_run,
    )
    tag = "would book" if args.dry_run else "booked"
    for b in r["booked"]:
        print(f"  {tag}: {b['candidate']} +{b['amount']} {b['currency']} "
              f"(paypal:{b['capture_id']})")
    for s in r["skipped"]:
        print(f"  skipped {s['amount']} {s['currency']} (paypal:{s['capture_id']}): "
              f"{s['reason']}")
    print(f"{tag} {len(r['booked'])} payment(s), {r['total_booked']} total; "
          f"{len(r['skipped'])} skipped (last {r['range_days']} days)")
    return 0


def _intake_store(data_dir: Path):
    from .intake import IntakeStore

    return IntakeStore.load(data_dir / "intake.json")


def _cmd_intake_import(args) -> int:
    from .intake import import_submissions, read_submissions

    data_dir = _data_dir(args)
    _, revenue_ledger, _ = _load(data_dir)
    rows = read_submissions(Path(args.file))

    intake = _intake_store(data_dir)
    r = import_submissions(intake, revenue_ledger, rows, candidate=args.candidate)
    for e in r["stored"]:
        print(f"  stored: {e['order_id']} -> {e['candidate']} ({e['fields']['email']})")
    for s in r["skipped"]:
        print(f"  skipped row {s['row']}"
              + (f" ({s['order_id']})" if s.get("order_id") else "")
              + f": {s['reason']}")
    print(f"imported {len(r['stored'])} submission(s); {len(r['skipped'])} skipped")
    return 0


def _cmd_intake_list(args) -> int:
    entries = _intake_store(_data_dir(args)).all()
    if not entries:
        print("no intake submissions")
        return 0
    for e in sorted(entries, key=lambda x: x.get("submitted_at", "")):
        f = e["fields"]
        print(f"  {e['status']:9} {e['order_id']:20} {e['candidate']}  "
              f"{f['name']} <{f['email']}>")
    return 0


def _cmd_intake_show(args) -> int:
    from .intake import INTAKE_FIELDS

    e = _intake_store(_data_dir(args)).get(args.order_id)
    if e is None:
        raise ValueError(f"no intake for order {args.order_id!r}")
    print(f"order_id   {e['order_id']}")
    print(f"capture_id {e.get('capture_id', '')}")
    print(f"candidate  {e['candidate']}")
    print(f"status     {e['status']}")
    print(f"submitted  {e.get('submitted_at', '')}")
    for key, label in INTAKE_FIELDS:
        print(f"\n{label}:\n  {e['fields'].get(key, '')}")
    plan = e.get("plan")
    if isinstance(plan, dict):
        qc = plan.get("qc") or {}
        print(f"\nplan       {plan.get('status')} ({plan.get('basis')})")
        for chk in qc.get("checks", []):
            print(f"  qc: {chk}")
        for s in plan.get("sources", []):
            print(f"  source: {s.get('title')} - {s.get('url')}")
    return 0


def _cmd_intake_review(args) -> int:
    data_dir = _data_dir(args)
    intake = _intake_store(data_dir)
    e = intake.mark_reviewed(args.order_id, actor=args.actor)
    intake.save()
    print(f"reviewed: {e['order_id']} ({e['candidate']}) -> {e['status']}")
    return 0


def _cmd_draft_launch_plan(args) -> int:
    from .launch_plan import LaunchPlanWorker, estimate_launch_plan_cost_usd
    from .llm_cache import LlmCache
    from .llm_normalize import build_client
    from .workflow import draft_launch_plan

    data_dir = _data_dir(args)
    _, revenue_ledger, _ = _load(data_dir)
    intake = _intake_store(data_dir)
    entry = intake.get(args.order_id)
    if entry is None:
        raise ValueError(f"no intake for order {args.order_id!r}")
    if entry.get("plan"):
        raise ValueError(f"order {args.order_id!r} already has a plan")
    if entry.get("status") != "reviewed":
        raise ValueError(
            f"order {args.order_id!r} is {entry.get('status')!r}; run "
            "`intake-review` first (human gate)")

    mode = args.mode
    cache = LlmCache.load(data_dir / "llm_launch_plan_cache.json")
    est = estimate_launch_plan_cost_usd(entry["fields"], args.model, mode=mode)
    if est > args.max_cost:
        raise ValueError(
            f"estimated launch-plan cost ${est} exceeds the ${args.max_cost} "
            "ceiling; nothing was drafted")
    ceiling = _llm_budget_gate(data_dir, est, args.max_cost)

    worker = LaunchPlanWorker(
        client=build_client(), model=args.model, max_cost_usd=ceiling,
        cache=cache, refresh=args.refresh, mode=mode,
    )
    updated = draft_launch_plan(intake, revenue_ledger, worker, args.order_id)
    cache.save()
    _record_llm_spend(data_dir, "launch_plan", worker)

    plan = updated["plan"]
    print(f"drafted: {args.order_id} ({updated['candidate']}) -> plan status "
          f"{plan['status']}")
    print(f"  basis: {plan['basis']}; cost ${round(worker.meter.cost_usd, 4)} "
          f"({worker.cache_hits} cache hit / {worker.cache_misses} miss)")
    for chk in plan.get("qc", {}).get("checks", []):
        print(f"  qc: {chk}")
    print("next: review the draft, then `revenue_os plan-approve "
          f"{args.order_id}`")
    return 0


def _cmd_plan_approve(args) -> int:
    data_dir = _data_dir(args)
    intake = _intake_store(data_dir)
    e = intake.approve_plan(args.order_id, actor=args.actor)
    intake.save()
    print(f"approved: {args.order_id} ({e['candidate']}) -> plan "
          f"{e['plan']['status']}")
    return 0


def _cmd_plan_deliver(args) -> int:
    from .delivery import DeliveryError, delivery_status, send_delivery, stage_delivery

    data_dir = _data_dir(args)
    if args.status:
        print(json.dumps(delivery_status(data_dir, args.order_id), indent=2))
        return 0
    try:
        if args.send:
            r = send_delivery(data_dir, args.order_id, force=args.force)
            print(f"SENT: order {r['order_id']} -> {r['to_email']} "
                  f"(message {r['message_id']})")
            print(f"  pdf: {r['pdf_path']} ({r['pdf_bytes']} bytes)")
            return 0
        r = stage_delivery(data_dir, args.order_id)
        print(f"staged: order {r['order_id']} ({r['candidate']}) -> {r['status']}")
        print(f"  pdf written: {r['pdf_path']} ({r['pdf_bytes']} bytes, "
              f"sha256 {r['pdf_sha256'][:12]}...)")
        print(f"  buyer: {r['to_name']} <{r['to_email']}>")
        print("  review the PDF, then send it:")
        print(f"    revenue_os plan-deliver {r['order_id']} --send")
        return 0
    except DeliveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_plan_render(args) -> int:
    from .deliverable import render_launch_plan_md

    data_dir = _data_dir(args)
    e = _intake_store(data_dir).get(args.order_id)
    if e is None:
        raise ValueError(f"no intake for order {args.order_id!r}")
    plan = e.get("plan") or {}
    if plan.get("status") != "approved":
        raise ValueError(
            f"plan for {args.order_id!r} is {plan.get('status')!r}; run "
            "`plan-approve` first (human gate before delivery)")
    md = render_launch_plan_md(e)
    out = (Path(args.out) if args.out
           else data_dir / "deliverables" / e["candidate"]
           / f"plan-{args.order_id}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"wrote {out}")
    print("convert to PDF yourself (pandoc / print-to-PDF); nothing is sent "
          "to the customer automatically")
    return 0


# --- cost-control commands (authorize/record only; never move money) ----


def _cmd_budget(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    cap = set_budget(spend_ledger, args.name, args.amount, approver=args.actor)
    print(f"budget: {args.name} -> {cap}")
    return 0


def _cmd_authorize_spend(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    request = SpendRequest(
        candidate_name=args.name,
        purpose=args.purpose,
        amount=args.amount,
        requested_by=args.actor,
    )
    authorize_spend(spend_ledger, request, approver=args.actor, ceiling=args.ceiling)
    print(f"authorized: {args.name} {args.amount} (purpose: {args.purpose})")
    return 0


def _cmd_deny_spend(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    request = SpendRequest(
        candidate_name=args.name,
        purpose=args.purpose,
        amount=args.amount,
        requested_by=args.actor,
    )
    deny_spend(spend_ledger, request, approver=args.actor, reason=args.reason)
    print(f"denied: {args.name} {args.amount} (reason: {args.reason})")
    return 0


def _cmd_record_spend(args) -> int:
    store, _, spend_ledger = _load(_data_dir(args))
    _require(store, args.name)
    record_spend(spend_ledger, args.name, args.amount, actor=args.actor, note=args.note)
    print(f"spent: {args.name} {args.amount}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir", default=argparse.SUPPRESS, help="state directory (default: ./data)"
    )

    actor_only = argparse.ArgumentParser(add_help=False)
    actor_only.add_argument("--actor", default="human-owner", help="who is acting")

    actor = argparse.ArgumentParser(add_help=False, parents=[actor_only])
    actor.add_argument("--note", default="", help="note recorded in history")

    parser = argparse.ArgumentParser(prog="revenue_os", parents=[common])
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="discovery cycle then report")
    run.add_argument("--source", choices=("static", "hn", "file"), default="static")
    run.add_argument(
        "--source-path", default=None, help="signal JSON file (required for --source file)"
    )
    run.add_argument("--limit", type=int, default=10)
    run.add_argument("--shortlist", type=int, default=3)
    run.add_argument(
        "--filter", action="store_true", help="keep only commercially relevant signals"
    )
    run.add_argument(
        "--min-score", type=float, default=0.0, help="drop candidates below this score"
    )
    run.add_argument(
        "--evaluator", choices=("keyword", "llm"), default="keyword",
        help="how to score signals (default: deterministic keyword heuristic)",
    )
    run.add_argument(
        "--model", default="claude-sonnet-5", help="model for --evaluator llm"
    )
    run.add_argument(
        "--max-eval-cost", type=float, default=1.0,
        help="USD ceiling for one --evaluator llm run (default 1.00)",
    )
    run.add_argument(
        "--refresh-eval", action="store_true",
        help="ignore cached llm scores and re-call the API",
    )
    run.add_argument(
        "--calibrated", action="store_true",
        help="reweight the criterion mean from recorded validation outcomes",
    )
    run.set_defaults(func=_cmd_run)

    def _add_discovery_args(p, *, with_web):
        srcs = ("hn-algolia", "stackexchange", "lobsters", "lemmy", "bluesky",
                "reddit", "file", "static", "free") + (
                    ("web", "all") if with_web else ())
        p.add_argument(
            "--source", action="append", default=None, choices=srcs,
            help="repeatable. Default: hn-algolia + stackexchange + lobsters + "
                 "lemmy (all keyless, $0). "
                 + ("'web' = Anthropic web search (paid, budget-gated). "
                    if with_web else "")
                 + "'free' = the keyless set.")
        p.add_argument("--source-path", default=None,
                       help="JSON record file for --source file")
        p.add_argument("--query", action="append", default=None, metavar="TEXT",
                       help="search query; repeat for several (default: built-in)")
        p.add_argument("--limit", type=int, default=15, help="hits per query (max 50)")
        p.add_argument("--min-score", type=int, default=0,
                       help="drop leads whose final_score is below this")
        p.add_argument("--max-age-days", type=int, default=30,
                       help="posts older than N days score much lower (default 30)")
        p.add_argument("--delay", type=float, default=1.0,
                       help="seconds between queries (politeness)")
        p.add_argument("--dry-run", action="store_true",
                       help="fetch, score and print, but persist nothing")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if with_web:
            p.add_argument("--score", choices=("deterministic", "llm"),
                           default="deterministic",
                           help="'llm' adds a metered relevance pass (paid)")
            p.add_argument("--model", default="claude-sonnet-5")
            p.add_argument("--max-cost", type=float, default=1.0,
                           help="USD ceiling for --score llm and/or --source web")
            p.add_argument("--refresh", action="store_true",
                           help="ignore cached llm results and re-call the API")

    disco = sub.add_parser(
        "discover-opportunities", parents=[common],
        help="find CURRENT public posts from founders struggling to get customers",
    )
    _add_discovery_args(disco, with_web=True)
    disco.set_defaults(func=_cmd_discover_opportunities)

    dfree = sub.add_parser(
        "discover-free", parents=[common],
        help="discovery using ONLY free keyless sources - $0, never calls Anthropic",
    )
    _add_discovery_args(dfree, with_web=False)
    dfree.set_defaults(func=_cmd_discover_free, score="deterministic",
                       model="claude-sonnet-5", max_cost=0.0, refresh=False)

    topo = sub.add_parser(
        "top-opportunities", parents=[common],
        help="show the human-review shortlist of real, current opportunities",
    )
    topo.add_argument("--limit", type=int, default=10)
    topo.add_argument("--min-score", type=int, default=60,
                      help="minimum final_score (default 60)")
    topo.add_argument("--max-age-days", type=int, default=None,
                      help="hide opportunities older than N days")
    topo.add_argument("--all", action="store_true",
                      help="include success-story / educational / irrelevant rows")
    topo.add_argument("--json", action="store_true")
    topo.set_defaults(func=_cmd_top_opportunities)

    resc = sub.add_parser(
        "acquisition-rescore", parents=[common],
        help="re-derive every stored lead's score with the current model "
             "($0, no network, contacts no one)",
    )
    resc.add_argument("--max-age-days", type=int, default=30,
                      help="recency cliff for the re-derived final_score (default 30)")
    resc.add_argument("--dry-run", action="store_true",
                      help="score and print, but persist nothing")
    resc.add_argument("--json", action="store_true")
    resc.set_defaults(func=_cmd_acquisition_rescore)

    revo = sub.add_parser(
        "review-opportunity", parents=[common, actor_only],
        help="mark one opportunity reviewed (approve) or rejected - never contacts anyone",
    )
    revo.add_argument("lead_id", help="lead id (or a unique prefix)")
    revo.add_argument("--approve", action="store_true",
                      help="human confirms this is a relevant opportunity")
    revo.add_argument("--reject", action="store_true",
                      help="human marks this as not relevant")
    revo.set_defaults(func=_cmd_review_opportunity)

    obrief = sub.add_parser(
        "outreach-brief", parents=[common],
        help="prepare a human-review outreach draft for one lead (never posts)",
    )
    obrief.add_argument("lead_id", help="lead id (or a unique prefix)")
    obrief.add_argument("--checkout-url", default=None,
                        help="checkout URL for the tracked link (default: the live one)")
    obrief.add_argument("--draft", choices=("template", "llm"), default="template",
                        help="'llm' adds one metered Claude call that drafts a "
                             "tailored reply (paid, budget-gated; still a draft)")
    obrief.add_argument("--model", default="claude-sonnet-5")
    obrief.add_argument("--max-cost", type=float, default=0.10,
                        help="USD ceiling for --draft llm (default 0.10)")
    obrief.add_argument("--refresh", action="store_true",
                        help="ignore the cached draft and re-call the API")
    obrief.add_argument("--json", action="store_true")
    obrief.set_defaults(func=_cmd_outreach_brief)

    aq = sub.add_parser(
        "acquisition-queue", parents=[common],
        help="list every high/medium prospect still waiting on a human "
             "(de-duped; the system never posts)",
    )
    aq.add_argument("--json", action="store_true")
    aq.set_defaults(func=_cmd_acquisition_queue)

    exls = sub.add_parser(
        "experiments", parents=[common],
        help="revenue-experiment ledger: offer / price / source / outcome "
             "(deterministic, read-only, no PayPal/LLM/network)",
    )
    exls.add_argument("--json", action="store_true")
    exls.set_defaults(func=_cmd_experiments)

    ofb = sub.add_parser(
        "outreach-feedback", parents=[common],
        help="settled outreach experiments by source / quality / type "
             "(deterministic, read-only, advisory only - no auto weighting)",
    )
    ofb.add_argument("--json", action="store_true")
    ofb.set_defaults(func=_cmd_outreach_feedback)

    exclose = sub.add_parser(
        "experiment-close", parents=[common],
        help="human closes one experiment (no_sale | skipped)",
    )
    exclose.add_argument("lead_id", help="the experiment's lead id")
    exclose.add_argument("status", choices=("no_sale", "skipped"))
    exclose.add_argument("--reason", default="",
                         help="optional human note stored on the experiment")
    exclose.set_defaults(func=_cmd_experiment_close)

    ostatus = sub.add_parser(
        "outreach-status", parents=[common],
        help="record what YOU did with a drafted brief "
             "(posted / skipped removes it from the acquisition queue)",
    )
    ostatus.add_argument("lead_id", help="lead id (or a unique prefix)")
    ostatus.add_argument("status", choices=("draft", "approved", "posted", "skipped"))
    ostatus.add_argument("--reason", default="",
                         help="optional human note (why you posted / skipped)")
    ostatus.set_defaults(func=_cmd_outreach_status)

    apilot = sub.add_parser(
        "autopilot", parents=[common],
        help="one orchestrator: discover -> brief -> payment -> plan -> delivery, "
             "stopping at every human gate; enforces the EUR 3.00 pre-sale cap",
    )
    apilot.add_argument("action",
                        choices=("start", "cycle", "status", "pause", "resume", "stop"))
    apilot.add_argument("--allow-web", action="store_true",
                        help="allow the paid Anthropic web-search source (budget-gated)")
    apilot.add_argument("--max-age-days", type=int, default=14)
    apilot.add_argument("--limit", type=int, default=15)
    apilot.add_argument("--delay", type=float, default=1.0)
    apilot.add_argument("--checkout-url", default=None,
                        help="set/override the checkout URL (persisted in autopilot state)")
    apilot.add_argument("--reason", default=None, help="pause reason")
    apilot.add_argument("--json", action="store_true")
    apilot.set_defaults(func=_cmd_autopilot)

    pipe = sub.add_parser(
        "pipeline", parents=[common],
        help="run/inspect the one-cycle agent pipeline for a qualified "
             "candidate (Opportunity Finder -> ... -> Quality Control -> human gate); "
             "publishes nothing, spends nothing",
    )
    pipe.add_argument("action", choices=("run", "status"))
    pipe.add_argument("name", nargs="?", help="candidate name")
    pipe.add_argument("--restart", action="store_true",
                      help="discard prior run state and start from step 1")
    pipe.add_argument("--json", action="store_true")
    pipe.set_defaults(func=_cmd_pipeline)

    sub.add_parser("report", parents=[common], help="print the report only").set_defaults(
        func=_cmd_report
    )

    rstep = sub.add_parser(
        "revenue-step", parents=[common],
        help="supervisor: observe state, run ONE safe non-human action, persist",
    )
    rstep.add_argument("--no-discovery", action="store_true",
                       help="do not fall back to a free discovery cycle")
    rstep.add_argument("--discovery-cooldown-hours", type=float, default=6.0,
                       help="min hours between real discovery runs (0 = none)")
    rstep.set_defaults(func=_cmd_revenue_step)

    rloop = sub.add_parser(
        "revenue-loop", parents=[common],
        help="supervisor: step until only human-gated actions remain, or "
             "--watch to run continuously (no messages, no spend, no API)",
    )
    rloop.add_argument("--max-steps", type=int, default=25)
    rloop.add_argument("--no-discovery", action="store_true")
    rloop.add_argument("--discovery-cooldown-hours", type=float, default=6.0,
                       help="min hours between real discovery runs (0 = none)")
    rloop.add_argument("--followup-days", type=float, default=14.0,
                       help="posted -> no_sale after this many days (0 = never)")
    rloop.add_argument("--watch", action="store_true",
                       help="run continuously: tick, sleep, repeat (bounded, resumable)")
    rloop.add_argument("--interval", type=float, default=900.0,
                       help="seconds between --watch ticks (default 900)")
    rloop.add_argument("--max-ticks", type=int, default=None)
    rloop.add_argument("--max-runtime", type=float, default=None, help="seconds")
    rloop.add_argument("--max-spend", type=float, default=None,
                       help="USD LLM spend since session start (safety bound)")
    rloop.add_argument("--fresh", action="store_true",
                       help="ignore an unfinished --watch session")
    rloop.add_argument("--dashboard", action="store_true",
                       help="regenerate <data-dir>/dashboard.html after each tick")
    rloop.set_defaults(func=_cmd_revenue_loop)

    sub.add_parser(
        "revenue-status", parents=[common],
        help="supervisor: last state + the current human action queue",
    ).set_defaults(func=_cmd_revenue_status)

    sub.add_parser(
        "llm-costs", parents=[common], help="print recorded AI operating spend"
    ).set_defaults(func=_cmd_llm_costs)

    sub.add_parser(
        "outcomes", parents=[common],
        help="retrospective: how validated vs rejected candidates scored",
    ).set_defaults(func=_cmd_outcomes)

    digest = sub.add_parser(
        "digest", parents=[common],
        help="one-line summary of what needs the human",
    )
    digest.add_argument(
        "-q", "--quiet", action="store_true",
        help="no output; exit 1 if anything awaits a human",
    )
    digest.set_defaults(func=_cmd_digest)

    ag_run = sub.add_parser(
        "agent-run", parents=[common],
        help="operator agent: loop to a fixed point once (also the cron primitive)",
    )
    ag_run.add_argument("--max-cycles", type=int, default=20)
    ag_run.set_defaults(func=_cmd_agent_run)

    ag_loop = sub.add_parser(
        "agent-loop", parents=[common],
        help="operator agent: tick / sleep / repeat, bounded and resumable",
    )
    ag_loop.add_argument("--interval", type=float, required=True, help="seconds between ticks")
    ag_loop.add_argument("--max-ticks", type=int, default=None)
    ag_loop.add_argument("--max-runtime", type=float, default=None, help="seconds")
    ag_loop.add_argument("--max-total-cycles", type=int, default=None)
    ag_loop.add_argument("--max-spend", type=float, default=None, help="USD since session start")
    ag_loop.add_argument("--fresh", action="store_true", help="ignore an unfinished session")
    ag_loop.add_argument(
        "--dashboard", action="store_true",
        help="regenerate <data-dir>/dashboard.html after each tick",
    )
    ag_loop.add_argument("-q", "--quiet", action="store_true", help="print only ticks that did something")
    ag_loop.set_defaults(func=_cmd_agent_loop)

    sub.add_parser(
        "agent-step", parents=[common], help="operator agent: one decide/act step"
    ).set_defaults(func=_cmd_agent_step)

    ag_log = sub.add_parser(
        "agent-log", parents=[common], help="operator agent: recent decisions"
    )
    ag_log.add_argument("--limit", type=int, default=20)
    ag_log.set_defaults(func=_cmd_agent_log)

    ag_goal = sub.add_parser(
        "agent-goal", parents=[common], help="operator agent: show or set the goal"
    )
    ag_goal.add_argument("--sources", default=None, help="comma list, e.g. static,hn,file:leads.json")
    ag_goal.add_argument("--filter", action=argparse.BooleanOptionalAction, default=None)
    ag_goal.add_argument("--calibrated", action=argparse.BooleanOptionalAction, default=None)
    ag_goal.add_argument("--min-score", type=float, default=None)
    ag_goal.add_argument("--shortlist", type=int, default=None)
    ag_goal.add_argument("--limit", type=int, default=None)
    ag_goal.add_argument(
        "--target-validated", type=int, default=None,
        help="stop once this many are validated (negative clears)",
    )
    ag_goal.add_argument("--evaluator", choices=("keyword", "llm"), default=None)
    ag_goal.add_argument("--planner", choices=("template", "llm"), default=None)
    ag_goal.add_argument("--proposer", choices=("template", "llm"), default=None)
    ag_goal.add_argument("--research", choices=("off", "llm", "web"), default=None)
    ag_goal.add_argument("--competition", choices=("off", "llm", "web"), default=None)
    ag_goal.add_argument("--copywriter", choices=("off", "llm"), default=None)
    ag_goal.add_argument(
        "--trend-hunter", action=argparse.BooleanOptionalAction, default=None,
        help="deterministic trend analysis over the candidate corpus",
    )
    ag_goal.add_argument(
        "--revenue-analyst", action=argparse.BooleanOptionalAction, default=None,
        help="deterministic portfolio ROI analysis over the ledgers",
    )
    ag_goal.add_argument(
        "--content-creator", action=argparse.BooleanOptionalAction, default=None,
        help="deterministic landing-page packager for validated offers",
    )
    ag_goal.add_argument("--decision-policy", choices=("rules", "llm"), default=None)
    ag_goal.add_argument("--model", default=None, help="model for the llm workers")
    ag_goal.add_argument("--max-llm-cost-per-action", type=float, default=None)
    ag_goal.add_argument("--max-decision-cost", type=float, default=None)
    ag_goal.set_defaults(func=_cmd_agent_goal)

    llm_budget = sub.add_parser(
        "llm-budget", parents=[common, actor_only],
        help="show or raise the cumulative AI spend cap",
    )
    llm_budget.add_argument(
        "amount", type=float, nargs="?", default=None,
        help="new cap in USD (omit to show cap/spent/remaining)",
    )
    llm_budget.set_defaults(func=_cmd_llm_budget)

    dash = sub.add_parser(
        "dashboard", parents=[common], help="write a static HTML pipeline snapshot"
    )
    dash.add_argument(
        "--out", default=None, help="output path (default: <data-dir>/dashboard.html)"
    )
    dash.set_defaults(func=_cmd_dashboard)

    blk = sub.add_parser(
        "blockers", parents=[common],
        help="list / record / resolve operational blockers shown on the dashboard",
    )
    blk.add_argument("action", nargs="?", default="list",
                     choices=("list", "add", "resolve"))
    blk.add_argument("id", nargs="?", default=None, help="blocker id (add / resolve)")
    blk.add_argument("--title", default="", help="short title (add)")
    blk.add_argument("--detail", default="", help="one-line explanation (add)")
    blk.add_argument("--area", default="", help="what it blocks, e.g. payment (add)")
    blk.add_argument("--severity", default="warning",
                     choices=("critical", "warning", "info"))
    blk.add_argument("--all", action="store_true", help="include resolved blockers")
    blk.set_defaults(func=_cmd_blockers)

    dserve = sub.add_parser(
        "dashboard-serve", parents=[common],
        help="serve an interactive dashboard on localhost (human gates only)",
    )
    dserve.add_argument("--port", type=int, default=8787)
    dserve.add_argument("--host", default="127.0.0.1",
                        help="loopback only; a non-loopback host is refused")
    dserve.add_argument("--actor", default="dashboard",
                        help="recorded as the actor for gate actions")
    dserve.set_defaults(func=_cmd_dashboard_serve)

    cand = sub.add_parser("candidate", parents=[common], help="show one candidate")
    cand.add_argument("name")
    cand.set_defaults(func=_cmd_candidate)

    sub.add_parser("demo", parents=[common], help="end-to-end walkthrough").set_defaults(
        func=_cmd_demo
    )

    approve = sub.add_parser("approve", parents=[common, actor], help="approve a candidate")
    approve.add_argument("name")
    approve.set_defaults(func=_cmd_approve)

    reject = sub.add_parser("reject", parents=[common, actor], help="reject a candidate")
    reject.add_argument("name")
    reject.set_defaults(func=_cmd_reject)

    investigate = sub.add_parser(
        "investigate", parents=[common], help="plan + advance all approved candidates"
    )
    investigate.add_argument(
        "--planner", choices=("template", "llm"), default="template",
        help="how to design the validation test (default: deterministic template)",
    )
    investigate.add_argument(
        "--model", default="claude-sonnet-5", help="model for --planner llm"
    )
    investigate.add_argument(
        "--max-plan-cost", type=float, default=0.5,
        help="USD ceiling for one --planner llm run (default 0.50)",
    )
    investigate.add_argument(
        "--refresh-plan", action="store_true",
        help="ignore cached llm plans and re-call the API",
    )
    investigate.set_defaults(func=_cmd_investigate)

    outcome = sub.add_parser(
        "outcome", parents=[common, actor], help="record a validation outcome"
    )
    outcome.add_argument("name")
    outcome.add_argument("result", choices=("validated", "rejected"))
    outcome.add_argument("--metric", required=True, help="observed metric value")
    outcome.set_defaults(func=_cmd_outcome)

    prep = sub.add_parser(
        "prepare-launch", parents=[common], help="attach offers to validated candidates"
    )
    prep.add_argument(
        "--proposer", choices=("template", "llm"), default="template",
        help="how to draft the first offer (default: deterministic template)",
    )
    prep.add_argument("--model", default="claude-sonnet-5", help="model for --proposer llm")
    prep.add_argument(
        "--max-offer-cost", type=float, default=0.5,
        help="USD ceiling for one --proposer llm run (default 0.50)",
    )
    prep.add_argument(
        "--refresh-offer", action="store_true",
        help="ignore cached llm offers and re-call the API",
    )
    prep.set_defaults(func=_cmd_prepare_launch)

    launch = sub.add_parser("launch", parents=[common, actor], help="mark an offer live")
    launch.add_argument("name")
    launch.set_defaults(func=_cmd_launch)

    payment = sub.add_parser(
        "payment", parents=[common, actor], help="record a received payment"
    )
    payment.add_argument("name")
    payment.add_argument("amount", type=float)
    payment.set_defaults(func=_cmd_payment)

    pv = sub.add_parser(
        "paypal-verify", parents=[common, actor_only],
        help="verify one PayPal order and book it into the revenue ledger",
    )
    pv.add_argument("name", help="candidate the payment belongs to")
    pv.add_argument("order_id", help="the PayPal order ID")
    pv.add_argument("--force", action="store_true",
                    help="book even if the order's custom_id names a different candidate")
    pv.set_defaults(func=_cmd_paypal_verify, actor="paypal")

    ps = sub.add_parser(
        "paypal-sync", parents=[common, actor_only],
        help="book recent PayPal payments (matched to candidates by custom_id)",
    )
    ps.add_argument("--days", type=int, default=31, help="lookback window (max 31)")
    ps.add_argument("--dry-run", action="store_true", help="report only, book nothing")
    ps.set_defaults(func=_cmd_paypal_sync, actor="paypal")

    bc = sub.add_parser(
        "build-checkout", parents=[common],
        help="write a real PayPal checkout page for a launched candidate's offer",
    )
    bc.add_argument("name")
    bc.add_argument(
        "--price", type=float, default=None,
        help="offer price; creates/updates the offer (omit to reuse the stored one)",
    )
    bc.add_argument("--currency", default="EUR")
    bc.add_argument("--what", default=None,
                    help="product name / what is sold (default: candidate description)")
    bc.add_argument("--delivery", choices=("digital", "manual", "subscription"),
                    default="manual")
    bc.add_argument("--cta", default=None, help="call-to-action line")
    bc.add_argument("--promise", default=None, help="one-line core promise (subheadline)")
    bc.add_argument("--include", action="append", default=None, metavar="LINE",
                    help='one "what you get" bullet; repeat for each')
    bc.add_argument("--delivery-note", default=None,
                    help='e.g. "Delivered as a personalized PDF within 3 business days."')
    bc.add_argument("--disclaimer", default=None,
                    help="what is NOT promised (shown prominently on the page)")
    bc.add_argument("--form-action", default=None, metavar="URL",
                    help="endpoint the post-payment intake form POSTs to "
                         "(a form provider; a placeholder is shown if omitted)")
    bc.add_argument("--business-email", default=None, metavar="ADDR",
                    help="contact address shown on the pages "
                         "(default: $BUSINESS_EMAIL; generic wording if unset)")
    bc.add_argument("--out", default=None,
                    help="output path (default: <data-dir>/deliverables/<name>/checkout.html)")
    bc.set_defaults(func=_cmd_build_checkout)

    dc = sub.add_parser(
        "deploy-checkout", parents=[common],
        help="publish deliverables/<name>/{checkout,intake}.html to GitHub Pages "
             "and store the live URL on the candidate",
    )
    dc.add_argument("name")
    dc.set_defaults(func=_cmd_deploy_checkout)

    ds = sub.add_parser(
        "deploy-status", parents=[common],
        help="show whether a candidate's checkout page is built and deployed",
    )
    ds.add_argument("name")
    ds.set_defaults(func=_cmd_deploy_status)

    ii = sub.add_parser(
        "intake-import", parents=[common],
        help="store buyer intake submissions that match a booked PayPal payment",
    )
    ii.add_argument("file", help="JSON or CSV export from the form provider")
    ii.add_argument("--candidate", default=None,
                    help="candidate name if a row does not carry one")
    ii.set_defaults(func=_cmd_intake_import)

    il = sub.add_parser("intake-list", parents=[common],
                        help="list buyer intake submissions")
    il.set_defaults(func=_cmd_intake_list)

    ish = sub.add_parser("intake-show", parents=[common],
                         help="print one buyer intake submission")
    ish.add_argument("order_id")
    ish.set_defaults(func=_cmd_intake_show)

    ir = sub.add_parser("intake-review", parents=[common, actor_only],
                        help="mark a buyer intake submission as reviewed")
    ir.add_argument("order_id")
    ir.set_defaults(func=_cmd_intake_review)

    dlp = sub.add_parser(
        "draft-launch-plan", parents=[common],
        help="draft the Customer Launch Plan for one paid, reviewed intake",
    )
    dlp.add_argument("order_id")
    dlp.add_argument("--mode", choices=("web", "llm"), default="web",
                     help="web = grounded in real web search with sources (default)")
    dlp.add_argument("--model", default="claude-sonnet-5")
    dlp.add_argument("--max-cost", type=float, default=1.5,
                     help="USD ceiling for this draft (default 1.50)")
    dlp.add_argument("--refresh", action="store_true",
                     help="ignore a cached plan and re-call the API")
    dlp.set_defaults(func=_cmd_draft_launch_plan)

    pa = sub.add_parser("plan-approve", parents=[common, actor_only],
                        help="approve a drafted Customer Launch Plan for delivery")
    pa.add_argument("order_id")
    pa.set_defaults(func=_cmd_plan_approve)

    pd = sub.add_parser(
        "plan-deliver", parents=[common],
        help="render the approved plan to a real PDF (default), then "
             "--send it to the buyer by email (human gate)",
    )
    pd.add_argument("order_id")
    pd.add_argument("--send", action="store_true",
                    help="actually email the staged PDF to the buyer")
    pd.add_argument("--force", action="store_true",
                    help="resend even if this order was already delivered")
    pd.add_argument("--status", action="store_true",
                    help="show the delivery record for this order and exit")
    pd.set_defaults(func=_cmd_plan_deliver)

    pr = sub.add_parser("plan-render", parents=[common],
                        help="write an approved Customer Launch Plan to Markdown")
    pr.add_argument("order_id")
    pr.add_argument("--out", default=None,
                    help="output path (default: "
                         "<data-dir>/deliverables/<candidate>/plan-<order_id>.md)")
    pr.set_defaults(func=_cmd_plan_render)

    budget = sub.add_parser(
        "budget", parents=[common, actor_only], help="set/raise a candidate's spend cap"
    )
    budget.add_argument("name")
    budget.add_argument("amount", type=float)
    budget.set_defaults(func=_cmd_budget)

    auth = sub.add_parser(
        "authorize-spend", parents=[common, actor_only], help="authorize a spend request"
    )
    auth.add_argument("name")
    auth.add_argument("amount", type=float)
    auth.add_argument("--purpose", required=True, help="what the spend is for")
    auth.add_argument(
        "--ceiling", type=float, default=DEFAULT_CEILING,
        help="max authorizable amount (default 0.0)",
    )
    auth.set_defaults(func=_cmd_authorize_spend)

    deny = sub.add_parser(
        "deny-spend", parents=[common, actor_only], help="deny a spend request"
    )
    deny.add_argument("name")
    deny.add_argument("amount", type=float)
    deny.add_argument("--purpose", required=True, help="what the spend was for")
    deny.add_argument("--reason", required=True, help="why it was denied")
    deny.set_defaults(func=_cmd_deny_spend)

    rec = sub.add_parser(
        "record-spend", parents=[common, actor], help="log money already spent"
    )
    rec.add_argument("name")
    rec.add_argument("amount", type=float)
    rec.set_defaults(func=_cmd_record_spend)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Auto-load .env only for a real CLI invocation (argv is None). Tests
    # always pass an explicit argv list and manage their own environment,
    # so this keeps the real credentials out of the test process.
    if argv is None:
        from .envfile import load_env

        loaded = load_env()
        if loaded:
            logging.getLogger("revenue_os").info(
                "loaded %d key(s) from .env: %s", len(loaded), ", ".join(sorted(loaded))
            )
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        args.command = "report"
        func = _cmd_report
    try:
        return func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
