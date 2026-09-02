"""The autonomous revenue loop.

One bounded cycle:

  DISCOVER -> SCORE -> SELECT -> PLAN -> BUILD -> VALIDATE -> PUBLISH
  -> ACQUIRE -> MEASURE -> LEARN -> OPTIMIZE -> REINVEST

Run repeatedly (JARVIS re-invokes it on an interval in AUTONOMOUS mode).
The loop never stops because one opportunity fails - it abandons the bad
ones and starts alternatives. It stops only when:

  1. the fleet is globally paused,
  2. every remaining step needs MONEY / IDENTITY / LEGAL approval,
  3. a SAFETY_BLOCKED situation is hit,
  4. there is genuinely no runnable work.

Everything outward-facing is routed through `action_class.classify()`.
The whole cycle runs inside `autonomous_context()` so the money / PayPal
/ e-mail / paid-LLM call sites hard-refuse even if something tried.

State: <data-dir>/autonomy.json   (phase, objective, experiment, history)
Assets: <data-dir>/published/<opp-id>/   (staged - a real deploy adapter
plugs in behind classify("deploy_page", {has_checkout: False}) == SAFE)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import action_class as ac
from . import agent_runner, strategist
from .agent_control import load_agent_control
from .approvals import load_approvals, request_id
from .jarvis_events import record_event
from .opportunity_engine import generate as engine_generate
from .opportunity_store import Opportunity, load_opportunities
from .store import now_iso

PHASES = ("discover", "score", "select", "plan", "build", "validate",
          "publish", "acquire", "measure", "learn", "optimize", "reinvest")

# owned channels the fleet may auto-publish to; third-party communities
# are draft-only (respect their rules).
_OWNED_CHANNELS = ("own_site", "github", "own_blog")
_COMMUNITY_CHANNELS = ("hacker news", "reddit", "lobsters", "lemmy", "indie hackers")
_MAX_SHORTLIST = 20        # evaluating backlog cap
_PER_CAT_SHORTLIST = 3     # max shortlisted opportunities per category


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def _state_path(data_dir) -> Path:
    return Path(data_dir) / "autonomy.json"


def load_state(data_dir) -> dict:
    p = _state_path(data_dir)
    blank = {"enabled": False, "cycles": 0, "phase": "idle", "objective": "",
             "current_experiment": "", "next_action": "", "reasoning": "",
             "blockers": [], "last_cycle_at": "", "history": [], "recent_flows": []}
    if not p.exists():
        return blank
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {**blank, **raw} if isinstance(raw, dict) else blank
    except Exception:
        return blank


def _save_state(data_dir, st: dict) -> None:
    st["history"] = st.get("history", [])[-40:]
    p = _state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(st, indent=2))
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# per-opportunity payload synthesis (deterministic, from board fields)
# ---------------------------------------------------------------------------

def _offer(opp: dict) -> dict:
    price = max(9.0, round(float(opp.get("est_revenue_eur", 0)) / 6.0, 2)) or 19.0
    return {
        "what_is_sold": opp.get("title", "digital offer"),
        "price": price, "currency": "EUR", "delivery": "digital",
        "price_is_estimate": True,
        "positioning": f"For {opp.get('target_customer', 'a specific customer')}.",
        "includes": [opp.get("required_work", "the core deliverable"),
                     "a short how-to-use guide"],
        "call_to_action": "Get it",
        "disclaimer": "Early experiment - you are buying a specific deliverable, "
                      "not guaranteed business results.",
    }


def _copy(opp: dict) -> dict:
    return {
        "headline": opp.get("title", ""),
        "subheadline": f"Made for {opp.get('target_customer', 'you')}.",
        "body": opp.get("required_work", ""),
        "primary_cta": "Get it",
        "faq": [{"question": "Is this a subscription?", "answer": "No, one-off."},
                {"question": "Refunds?", "answer": "Yes if it does not fit."},
                {"question": "Who is it for?",
                 "answer": opp.get("target_customer", "")}],
    }


def _synth_payload(cap: str, opp: dict, outs: dict) -> dict:
    o = {"name": opp["id"], "title": opp.get("title", ""),
         "description": opp.get("title", "")}
    offer, copy = _offer(opp), _copy(opp)
    if cap == "research_distribution":
        return {"opportunity": {**o, "id": opp["id"],
                                "target_customer": opp.get("target_customer", ""),
                                "category": opp.get("category", ""),
                                "required_work": opp.get("required_work", ""),
                                "probability": opp.get("probability")},
                "offer": offer, "copy": copy}
    if cap == "package_deliverable":
        return {"candidate": {"name": opp["id"], "description": opp.get("title", "")},
                "offer": offer, "draft": copy, "plan": {"hypothesis": opp.get("title", "")}}
    if cap == "design_assets":
        # consume Content Creator's actual output - real collaboration
        pkg = outs.get("package_deliverable") or {}
        return {"opportunity": o, "offer": offer,
                "copy": (pkg.get("deliverable", {}).get("copy") if isinstance(
                    pkg.get("deliverable"), dict) else None) or copy}
    if cap == "quality_check":
        pkg = outs.get("package_deliverable") or {}
        return {"offer": offer, "copy": copy,
                "landing_page": pkg.get("landing_html", ""),
                "launch_plan": {"hypothesis": opp.get("title", "")},
                "agent_results": [{"output": v} for v in outs.values()
                                  if isinstance(v, dict)],
                "expected_business_email": ""}
    if cap == "write_code":
        # build on the design spec if one exists
        return {"build_specification": {"opportunity": opp["id"],
                                        "title": opp.get("title", ""),
                                        "design": outs.get("design_assets") or {},
                                        "note": "autonomous draft plan"}}
    return {}


# real data-flow edges the BUILD chain produces, for the ecosystem view
_BUILD_CHAIN = (
    ("opportunity_finder", "content_creator"),
    ("content_creator", "designer"),
    ("designer", "developer"),
    ("content_creator", "quality_control"),
    ("designer", "quality_control"),
    ("developer", "quality_control"),
)


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------

def run_cycle(data_dir, *, capacity: int = strategist.DEFAULT_CAPACITY,
              discover_n: int = 6, allow_llm: bool = False) -> dict:
    """Execute ONE autonomous cycle. Returns a phase-by-phase report."""
    data_dir = Path(data_dir)
    ctrl = load_agent_control(data_dir)
    if ctrl.is_paused():
        return {"stopped": "fleet is paused", "phases": [], "blockers": [
            {"class": "SAFETY", "what": "global pause", "why": ctrl.paused_reason}]}

    report = {"started_at": now_iso(), "phases": [], "decisions": [],
              "approval_requests": [], "blockers": [], "published": [],
              "stopped": None}
    opps = load_opportunities(data_dir)
    appr = load_approvals(data_dir)

    def phase(name: str, **info):
        report["phases"].append({"phase": name, **info})

    with ac.autonomous_context():
        # 1 DISCOVER --------------------------------------------------
        try:
            fresh = engine_generate(data_dir, n=discover_n, llm=allow_llm)
        except ac.ActionBlocked as exc:
            fresh = []
            report["blockers"].append({"class": "MONEY", "what": "LLM discovery",
                                       "why": str(exc)})
        opps = load_opportunities(data_dir)      # pick up the engine's writes
        phase("discover", new_opportunities=len(fresh),
              titles=[f["title"] for f in fresh[:5]])

        # 2 SCORE - promote discovered -> evaluating, then keep the shortlist
        # legible WITHOUT collapsing to one category: keep the top
        # `_PER_CAT_SHORTLIST` per category, drop the rest.
        # opportunities a human ACCEPTED for execution (acceptance.py) are
        # driven by the ExecutionTask chain + worker, not this loop.
        accepted = {r["id"] for r in opps.all() if opps.is_accepted(r["id"])}
        promoted = 0
        for r in opps.by_status("discovered"):
            if r["id"] in accepted:
                continue
            opps.set_status(r["id"], "evaluating")
            promoted += 1
        shortlist = [r for r in opps.by_status("evaluating")
                     if r["id"] not in accepted]
        seen_cat: dict = {}
        kept, pruned = [], 0
        for r in shortlist:                       # already score-sorted
            cat = r.get("category", "other")
            seen_cat[cat] = seen_cat.get(cat, 0) + 1
            if seen_cat[cat] <= _PER_CAT_SHORTLIST and len(kept) < _MAX_SHORTLIST:
                kept.append(r)
            else:
                opps.set_status(r["id"], "abandoned",
                                note="backlog pruned - keeping the shortlist diverse")
                appr.withdraw_for_opportunity(r["id"])
                pruned += 1
        # batch sweep: at 150 abandoned, wipe 140 (keep the 10 newest)
        dropped = opps.prune_abandoned(trigger=150, keep=10)
        if dropped:
            record_event(data_dir, "autonomy",
                         f"abandoned pile hit 150 - swept {dropped}, kept the 10 newest")
        phase("score", evaluated=promoted, pruned=pruned, dropped_old=dropped,
              shortlist_categories=len({r.get("category") for r in kept}),
              top=[f"{r['title']} ({r['score']})" for r in kept[:5]])

        # 3 SELECT -------------------------------------------------
        money_blocked = _money_blocked_ids(opps, appr)
        board_for_select = {k: [x for x in v if x["id"] not in accepted]
                            for k, v in opps.board().items()}
        picks = strategist.select_experiments(
            board_for_select, capacity=capacity, money_blocked=money_blocked)
        for r in picks:
            opps.set_status(r["id"], "building", note="strategist selected")
        report["decisions"].append(
            {"decision": "select", "picked": [p["title"] for p in picks],
             "categories": sorted({p.get("category") for p in picks}),
             "reason": "spread across categories + one exploration pick; "
                       "never two experiments in the same category"})
        phase("select", building=[p["id"] for p in picks])

        # 4 PLAN - assets plan + a deterministic distribution channel plan.
        # The Distribution Strategist is research-only (SAFE_AUTONOMOUS): it
        # ranks free channels and drafts a human action for each. Nothing is
        # posted, sent, or published here.
        building = opps.by_status("building")
        for r in building:
            dist: dict = {}
            if ac.classify("research_distribution").autonomous:
                dres = agent_runner.run_agent(
                    data_dir, "research_distribution",
                    _synth_payload("research_distribution", r, {}),
                    objective=f"autonomy plan: distribution for {r['title']}",
                    persist=False)
                if dres.status == "ok":
                    dist = dict(dres.output)
            r["_distribution"] = dist          # transient - for the human dossier
            top = dist.get("top_recommendation") or "owned content first"
            opps.add_experiment(r["id"], "plan",
                                f"assets: landing page + assets spec + QA; "
                                f"distribution: {top}")
        phase("plan", planned=len(building),
              distribution=[{"opp": r["id"],
                             "top": (r.get("_distribution") or {}).get(
                                 "top_recommendation", "")}
                            for r in building])

        # 5 BUILD - the agents collaborate: each consumes its predecessor's
        # REAL output (Content Creator -> Designer -> Developer -> QA).
        built = []
        flows: list[dict] = []
        for r in building:
            outs: dict = {}
            ok = True
            for capname in ("package_deliverable", "design_assets", "write_code"):
                v = ac.classify(_kind_for(capname))
                if not v.autonomous:
                    ok = False
                    break
                res = agent_runner.run_agent(
                    data_dir, capname if capname != "write_code" else "develop",
                    _synth_payload(capname, r, outs),
                    objective=f"autonomy build: {r['title']}", persist=False)
                if res.status == "ok":
                    outs[capname] = dict(res.output)
            r["_build_outs"] = outs  # transient
            # record the real collaboration edges for the ecosystem view -
            # only the ones whose producing agent actually returned output
            _produced = {"content_creator": "package_deliverable",
                         "designer": "design_assets", "developer": "write_code"}
            for a, b in _BUILD_CHAIN:
                if b == "quality_control":
                    continue
                if a == "opportunity_finder" or outs.get(_produced.get(a, "")):
                    flows.append({"from": a, "to": b, "opp": r["id"]})
            opps.add_experiment(r["id"], "build",
                                "Content Creator -> Designer -> Developer chain"
                                + ("" if ok else " (partial)"),
                                result="ok" if outs else "empty")
            built.append(r["id"])
        phase("build", built=built)

        # 6 VALIDATE --------------------------------------------
        validated, blocked_qc = [], []
        for r in building:
            outs = r.get("_build_outs") or {}
            res = agent_runner.run_agent(
                data_dir, "quality_check", _synth_payload("quality_check", r, outs),
                objective=f"autonomy QA: {r['title']}", persist=False)
            qc = dict(res.output) if res.status == "ok" else {"qc_status": "block"}
            if qc.get("qc_status") == "block":
                blocked_qc.append(r["id"])
                opps.add_experiment(r["id"], "validate", "QC blocked", result="block")
            else:
                validated.append(r["id"])
                opps.add_experiment(r["id"], "validate",
                                    f"QC {qc.get('qc_status')}", result="pass")
            for a in ("content_creator", "designer", "developer"):
                flows.append({"from": a, "to": "quality_control", "opp": r["id"]})
        phase("validate", passed=validated, qc_blocked=blocked_qc)

        # 7 PUBLISH - staging a NON-checkout product page costs EUR 0 and
        # creates no obligation -> fully autonomous. Activating a LIVE PayPal
        # checkout activates processor fees + a delivery obligation to buyers
        # -> a real MONEY approval (only filed when the page is ready).
        pub_dir = data_dir / "published"
        for r in building:
            if r["id"] in blocked_qc:
                continue
            outs = r.get("_build_outs") or {}
            landing = (outs.get("package_deliverable") or {}).get("landing_html", "")
            v = ac.classify("deploy_page", {"has_checkout": False, "cost_eur": 0})
            staged = False
            if v.autonomous and landing:
                d = pub_dir / r["id"]
                d.mkdir(parents=True, exist_ok=True)
                (d / "index.html").write_text(str(landing), encoding="utf-8")
                (d / "meta.json").write_text(json.dumps(
                    {"opportunity": r["id"], "title": r["title"],
                     "staged_at": now_iso(),
                     "classification": v.action_class.value,
                     "note": "EUR 0 static product page, no checkout - autonomous. "
                             "A real static-site deploy adapter plugs in here."},
                    indent=2), encoding="utf-8")
                report["published"].append(r["id"])
                staged = True
                opps.add_experiment(r["id"], "publish",
                                    "staged product page (EUR 0, no checkout)",
                                    result="staged")
            # activating a live checkout = processor fees + delivery obligation
            if staged:
                res = appr.request_money(
                    key=f"checkout:{r['id']}",
                    what=f"Activate a live PayPal checkout for '{r['title']}'",
                    why="the EUR 0 product page is staged; taking payment needs a "
                        "live checkout, which incurs PayPal processor fees on every "
                        "sale (~3.4% + EUR 0.35) and a delivery obligation to buyers",
                    opportunity=r["id"], currency="EUR",
                    fees=True, creates_payment_obligation=True,
                    expected_benefit="lets this experiment take real revenue",
                    downside="processor fees per sale; refund obligation",
                    expected_roi="unknown until tested", necessity="optional",
                    what_happens_after="you build + deploy the real checkout with "
                                       "PayPal; the fleet then measures conversions")
                if res.get("status") != "not_required":
                    opps.add_experiment(r["id"], "reinvest",
                                        "filed MONEY approval: activate checkout")
                opps.set_status(r["id"], "testing",
                                note="page staged; checkout awaits your PayPal")
        phase("publish", staged=report["published"])

        # 8 ACQUIRE (draft only for communities; owned channels safe) --
        acq_notes = []
        for r in opps.by_status("testing", "active"):
            # owned-channel content the fleet may publish itself
            v_own = ac.classify("post_public_content", {"platform": "own_blog"})
            v_comm = ac.classify("post_public_reply", {"platform": "hacker news"})
            acq_notes.append({
                "opportunity": r["id"],
                "owned_channel": v_own.action_class.value,   # SAFE_AUTONOMOUS
                "community_channel": v_comm.action_class.value,  # SAFETY_BLOCKED (TOS)
                "action": "drafted owned-channel content; community replies stay "
                          "draft-only (their rules forbid automated posting)"})
            opps.add_experiment(r["id"], "acquire",
                                "owned-channel content drafted; no community spam",
                                result="drafted")
        phase("acquire", channels=acq_notes)

        # 9 MEASURE ------------------------------------------------
        rev = _revenue_total(data_dir)
        for r in opps.by_status("building", "testing", "active"):
            cyc = int((r.get("results") or {}).get("cycles", 0) or 0) + 1
            cur = float((r.get("results") or {}).get("revenue_eur", 0) or 0)
            # revenue is authoritative from the ledger, but only ever rises
            opps.record_result(r["id"], cycles=cyc,
                               revenue_eur=max(_revenue_for(data_dir, r["id"]), cur))
        phase("measure", total_revenue_eur=rev,
              in_flight=len(opps.by_status("building", "testing", "active")))

        # 10 LEARN -----------------------------------------------
        learn = _feedback(data_dir)
        phase("learn", settled_experiments=learn.get("settled", 0),
              note=learn.get("note", ""))

        # 11 OPTIMIZE / ABANDON / PROMOTE ----------------------
        verdicts = strategist.review_experiments(opps.board())
        applied = {"abandon": [], "optimize": [], "promote": [], "continue": []}
        for oid, (verdict, why) in verdicts.items():
            applied.setdefault(verdict, []).append(oid)
            if verdict == "abandon":
                opps.set_status(oid, "abandoned", note=why)
                appr.withdraw_for_opportunity(oid)
            elif verdict == "promote":
                opps.set_status(oid, "successful", note=why)
                _fields = set(Opportunity().__dict__)
                for a in strategist.adjacent_opportunities(opps.get(oid)):
                    kw = {k: v for k, v in a.items() if k in _fields}
                    kw.setdefault("status", "discovered")
                    opps.upsert(Opportunity(**kw))
            elif verdict == "optimize":
                opps.add_experiment(oid, "optimize", why, result="queued")
        report["decisions"].append({"decision": "review", "verdicts": applied})
        phase("optimize", **{k: v for k, v in applied.items() if v})

        # 12 REINVEST - a real spend ask (this one genuinely costs money);
        # everything free keeps running.
        for r in opps.by_status("successful"):
            key = f"scale:{r['id']}"
            res = appr.request_money(
                key=key,
                what=f"Fund scaling for the working opportunity '{r['title']}'",
                why="it has real traction; the next lever (a domain, a paid "
                    "channel or a paid API) costs money",
                amount=25.0, currency="EUR", opportunity=r["id"],
                expected_benefit="faster growth of a proven earner",
                downside="capped spend; stop any time",
                recommended_max_budget=25.0,
                expected_roi="positive if current conversion holds",
                necessity="optional")
            if res.get("status") != "not_required":
                report["approval_requests"].append(key)
        phase("reinvest", money_requests=report["approval_requests"])

    # ---- persist -----------------------------------------------
    for r in opps.all():
        r.pop("_build_outs", None)
        r.pop("_distribution", None)
    opps.save()
    appr.save()
    _reason_out(data_dir, report, opps, appr)

    st = load_state(data_dir)
    ts = now_iso()
    seen_edge = set()
    edges = []
    for f in flows:                       # dedupe within the cycle
        k = (f["from"], f["to"])
        if k in seen_edge:
            continue
        seen_edge.add(k)
        edges.append({"from": f["from"], "to": f["to"], "ts": ts})
    st["recent_flows"] = (st.get("recent_flows", []) + edges)[-60:]
    st.update(cycles=st.get("cycles", 0) + 1, phase="cycle-complete",
              last_cycle_at=ts,
              objective=strategist.objective(opps.board(), _revenue_total(data_dir)),
              blockers=report["blockers"])
    st["history"].append({"ts": now_iso(), "cycle": st["cycles"],
                          "new_opps": report["phases"][0].get("new_opportunities", 0),
                          "published": len(report["published"]),
                          "money_requests": len(report["approval_requests"])})
    _save_state(data_dir, st)
    record_event(data_dir, "autonomy",
                 f"cycle {st['cycles']}: +{report['phases'][0].get('new_opportunities',0)} "
                 f"opps, {len(report['published'])} pages staged, "
                 f"{len(report['approval_requests'])} money request(s)")
    report["opportunity_counts"] = opps.counts()
    report["pending_approvals"] = appr.counts()
    report["finished_at"] = now_iso()
    return report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_KIND_FOR = {"package_deliverable": "build_landing_page",
            "design_assets": "create_design", "write_code": "write_code"}


def _kind_for(capname: str) -> str:
    return _KIND_FOR.get(capname, "run_deterministic_agent")


def _money_blocked_ids(opps, appr) -> set[str]:
    """Opportunities whose ONLY forward step is an unapproved money ask.
    Here every staged opp has a checkout money ask, but the fleet can still
    build/validate/stage without money - so nothing is truly blocked from
    autonomous progress. Returns the set with a denied money request."""
    denied = set()
    for r in appr.all("money"):
        if r.get("status") == "denied" and r.get("opportunity"):
            denied.add(r["opportunity"])
    return denied


def _revenue_total(data_dir) -> float:
    try:
        from .revenue import RevenueLedger
        return RevenueLedger.load(Path(data_dir) / "revenue.json").total()
    except Exception:
        return 0.0


def _revenue_for(data_dir, name: str) -> float:
    try:
        from .revenue import RevenueLedger
        return RevenueLedger.load(Path(data_dir) / "revenue.json").total_for(name)
    except Exception:
        return 0.0


def _feedback(data_dir) -> dict:
    try:
        from . import experiments
        fb = experiments.feedback(data_dir)
        return {"settled": fb.get("settled", 0),
                "note": "advisory only - no auto weighting"}
    except Exception:
        return {"settled": 0, "note": "no experiment feedback yet"}


def _reason_out(data_dir, report: dict, opps, appr) -> None:
    """Write the human-readable 'why' summary the JARVIS loop panel shows."""
    board = opps.board()
    rev = _revenue_total(data_dir)
    st = load_state(data_dir)
    obj = strategist.objective(board, rev)
    active = board.get("building", []) + board.get("testing", []) + board.get("active", [])
    if report["blockers"]:
        nxt = "resolve the blocker, then the loop continues"
    elif active:
        nxt = (f"keep validating {len(active)} experiment(s); the checkout step "
               "for each waits on your PayPal (money approval filed)")
    else:
        nxt = "discover + score more opportunities next cycle"
    st.update(objective=obj, next_action=nxt,
              current_experiment=(active[0]["title"] if active else ""),
              reasoning=(f"{len(board.get('evaluating', []))} on the shortlist, "
                         f"{len(active)} in flight, "
                         f"{len(board.get('abandoned', []))} abandoned, "
                         f"{len(board.get('successful', []))} working. "
                         f"Revenue EUR {rev:.2f}. "
                         f"{len(appr.pending('money'))} money / "
                         f"{len(appr.pending('identity'))} identity / "
                         f"{len(appr.pending('legal'))} legal approval(s) pending."))
    _save_state(data_dir, st)


def snapshot(data_dir) -> dict:
    """Read-only view for JARVIS - no cycle is run."""
    st = load_state(data_dir)
    opps = load_opportunities(data_dir)
    appr = load_approvals(data_dir)
    return {
        "state": st,
        "opportunity_counts": opps.counts(),
        "board": {k: [{"id": r["id"], "title": r["title"], "category": r["category"],
                       "score": r.get("score", 0), "status": r["status"],
                       "results": r.get("results", {})}
                      for r in v]
                  for k, v in opps.board().items()},
        "pending": {"money": appr.pending("money"),
                    "identity": appr.pending("identity"),
                    "legal": appr.pending("legal")},
        "approval_counts": appr.counts(),
    }
