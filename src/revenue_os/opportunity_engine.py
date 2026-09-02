"""The Revenue Opportunity Engine.

Continuously proposes NEW money-making opportunities across every
category, deterministically, at EUR 0 and with no network:

  * a broad catalogue of ~30 opportunity ARCHETYPES (one or more per
    category the owner listed),
  * instantiated against REAL current signals - trending keywords in the
    discovery corpus, phrases from acquisition leads, categories the
    fleet has not tried yet - so each run yields a contextual set,
  * plus cross-category recombinations,
  * scored by `opportunity_store.score_opportunity` and de-duplicated
    against everything already on the board.

`generate(..., llm=True)` is a wired hook for richer, open-ended
discovery once the owner enables + funds an LLM. It is refused here
(that is a `real_llm_call` -> MONEY_APPROVAL_REQUIRED).

No hardcoded "the product is X" - the board is the source of truth and it
grows every cycle.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .action_class import ActionBlocked
from .opportunity_store import Opportunity, OpportunityStore, load_opportunities

# ---------------------------------------------------------------------------
# archetypes: (key, category, title_tmpl, target, base estimates)
# est_revenue_eur = realistic monthly in first 90 days for a solo operator
# ---------------------------------------------------------------------------
_A = [
    ("notion_template", "template_pack",
     "{kw} template pack for {aud}", "{aud} who plan in Notion/Sheets",
     dict(est_revenue_eur=120, est_cost_eur=0, effort_points=2, difficulty=2,
          probability=0.22, time_to_first_revenue_days=14, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("prompt_pack", "digital_product",
     "Curated {kw} prompt + workflow pack", "solo operators using AI for {kw}",
     dict(est_revenue_eur=90, est_cost_eur=0, effort_points=2, difficulty=2,
          probability=0.18, time_to_first_revenue_days=10, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("micro_saas_tool", "micro_saas",
     "Single-purpose {kw} web tool (freemium)", "developers/marketers doing {kw}",
     dict(est_revenue_eur=250, est_cost_eur=0, effort_points=5, difficulty=4,
          probability=0.10, time_to_first_revenue_days=45, scalability=5,
          competition="medium", legal_platform_risk="low")),
    ("cli_devtool", "developer_tool",
     "Open-source {kw} CLI with a paid pro tier", "developers who touch {kw}",
     dict(est_revenue_eur=140, est_cost_eur=0, effort_points=4, difficulty=4,
          probability=0.09, time_to_first_revenue_days=40, scalability=4,
          competition="medium", legal_platform_risk="low")),
    ("api_wrapper", "api_product",
     "Simple hosted API that packages {kw}", "indie builders needing {kw}",
     dict(est_revenue_eur=180, est_cost_eur=0, effort_points=4, difficulty=4,
          probability=0.08, time_to_first_revenue_days=45, scalability=5,
          competition="medium", legal_platform_risk="medium")),
    ("automation_service", "automation_service",
     "Done-for-you {kw} automation setup", "small businesses drowning in {kw}",
     dict(est_revenue_eur=400, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.16, time_to_first_revenue_days=21, scalability=2,
          competition="medium", legal_platform_risk="low")),
    ("ai_service", "ai_service",
     "{kw} generation as a fixed-price service", "founders/creators needing {kw}",
     dict(est_revenue_eur=350, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.15, time_to_first_revenue_days=18, scalability=2,
          competition="high", legal_platform_risk="low")),
    ("info_product", "information_product",
     "Practical guide: shipping {kw} without a team", "beginners at {kw}",
     dict(est_revenue_eur=110, est_cost_eur=0, effort_points=3, difficulty=2,
          probability=0.14, time_to_first_revenue_days=21, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("lead_gen_list", "lead_generation",
     "Curated {kw} lead list + weekly refresh", "agencies selling into {kw}",
     dict(est_revenue_eur=300, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.12, time_to_first_revenue_days=25, scalability=3,
          competition="medium", legal_platform_risk="medium")),
    ("directory_site", "website",
     "Niche directory of {kw} tools/providers", "people comparing {kw}",
     dict(est_revenue_eur=130, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.12, time_to_first_revenue_days=35, scalability=4,
          competition="medium", legal_platform_risk="low")),
    ("affiliate_review", "affiliate",
     "In-depth {kw} comparison/review site", "buyers researching {kw}",
     dict(est_revenue_eur=90, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.10, time_to_first_revenue_days=60, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("marketplace_supply", "marketplace",
     "Sell {kw} assets on existing marketplaces", "creators buying {kw} assets",
     dict(est_revenue_eur=110, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.16, time_to_first_revenue_days=20, scalability=4,
          competition="high", legal_platform_risk="medium")),
    ("pod_niche", "print_on_demand",
     "Print-on-demand for the {kw} community", "the {kw} hobby/pro community",
     dict(est_revenue_eur=70, est_cost_eur=0, effort_points=3, difficulty=2,
          probability=0.10, time_to_first_revenue_days=25, scalability=3,
          competition="high", legal_platform_risk="medium")),
    ("data_product", "data_product",
     "{kw} dataset / snapshot report, updated monthly", "analysts tracking {kw}",
     dict(est_revenue_eur=200, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.11, time_to_first_revenue_days=30, scalability=4,
          competition="low", legal_platform_risk="medium")),
    ("content_newsletter", "content_business",
     "Focused {kw} newsletter with sponsor slots", "practitioners in {kw}",
     dict(est_revenue_eur=120, est_cost_eur=0, effort_points=3, difficulty=2,
          probability=0.12, time_to_first_revenue_days=45, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("b2b_audit", "b2b_service",
     "Fixed-scope {kw} audit with a report deliverable", "SMB teams doing {kw}",
     dict(est_revenue_eur=500, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.14, time_to_first_revenue_days=20, scalability=2,
          competition="medium", legal_platform_risk="low")),
    ("freelance_productised", "freelancing",
     "Productised {kw} service (one package, one price)", "clients needing {kw}",
     dict(est_revenue_eur=450, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.17, time_to_first_revenue_days=14, scalability=2,
          competition="high", legal_platform_risk="low")),
    ("ecom_digital", "ecommerce",
     "Small store of {kw} digital goods", "{aud} buying {kw} digital goods",
     dict(est_revenue_eur=140, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.12, time_to_first_revenue_days=30, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("niche_calculator", "niche_service",
     "Free {kw} calculator/estimator with lead capture", "people evaluating {kw}",
     dict(est_revenue_eur=80, est_cost_eur=0, effort_points=2, difficulty=2,
          probability=0.13, time_to_first_revenue_days=30, scalability=4,
          competition="low", legal_platform_risk="low")),
    ("arbitrage_content", "arbitrage",
     "Repackage public {kw} data into a paid summary", "busy people tracking {kw}",
     dict(est_revenue_eur=100, est_cost_eur=0, effort_points=2, difficulty=2,
          probability=0.10, time_to_first_revenue_days=21, scalability=3,
          competition="medium", legal_platform_risk="medium")),
]

# extra archetypes so the deterministic space stays large across many cycles
_A += [
    ("boilerplate_kit", "developer_tool",
     "Production {kw} starter kit / boilerplate", "developers starting a {kw} project",
     dict(est_revenue_eur=160, est_cost_eur=0, effort_points=4, difficulty=3,
          probability=0.12, time_to_first_revenue_days=25, scalability=4,
          competition="medium", legal_platform_risk="low")),
    ("chrome_extension", "micro_saas",
     "Browser extension for {kw}", "people who do {kw} in the browser daily",
     dict(est_revenue_eur=150, est_cost_eur=0, effort_points=4, difficulty=3,
          probability=0.11, time_to_first_revenue_days=30, scalability=5,
          competition="medium", legal_platform_risk="medium")),
    ("swipe_file", "information_product",
     "{kw} swipe file + teardown library", "marketers/founders learning {kw}",
     dict(est_revenue_eur=95, est_cost_eur=0, effort_points=2, difficulty=2,
          probability=0.15, time_to_first_revenue_days=12, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("cohort_workshop", "content_business",
     "Small paid {kw} workshop / cohort", "practitioners upskilling in {kw}",
     dict(est_revenue_eur=300, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.13, time_to_first_revenue_days=30, scalability=2,
          competition="medium", legal_platform_risk="low")),
    ("scorecard_tool", "lead_generation",
     "Free {kw} scorecard/assessment with a paid report", "teams evaluating {kw}",
     dict(est_revenue_eur=220, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.13, time_to_first_revenue_days=28, scalability=4,
          competition="low", legal_platform_risk="low")),
    ("integration_service", "b2b_service",
     "Set up + maintain {kw} integrations", "SMBs stitching {kw} tools together",
     dict(est_revenue_eur=450, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.15, time_to_first_revenue_days=18, scalability=2,
          competition="medium", legal_platform_risk="low")),
    ("micro_course", "digital_product",
     "One-hour {kw} micro-course", "beginners who want {kw} fast",
     dict(est_revenue_eur=120, est_cost_eur=0, effort_points=3, difficulty=2,
          probability=0.14, time_to_first_revenue_days=18, scalability=4,
          competition="high", legal_platform_risk="low")),
    ("bench_report", "data_product",
     "Quarterly {kw} benchmark report", "operators who compare {kw}",
     dict(est_revenue_eur=260, est_cost_eur=0, effort_points=3, difficulty=3,
          probability=0.10, time_to_first_revenue_days=40, scalability=4,
          competition="low", legal_platform_risk="medium")),
]

_STOP = frozenset("""a an the and or of for to in on with your you our we is are be
it this that from at as by using use free open source new get make build app tool
platform simple based via into out up not no yes can will just like more how do i
what why when where who""".split())
_TOKEN = re.compile(r"[a-z][a-z0-9+.#-]{2,}")
_DEFAULT_KW = ("developer onboarding", "SEO reporting", "invoice chasing",
               "changelog writing", "API docs", "cold email research",
               "podcast show notes", "meeting notes", "release notes",
               "customer feedback triage", "PR review", "incident retros",
               "landing page copy", "backlog grooming", "competitor tracking",
               "user research synthesis")

_AUDS = ("indie founders", "small agencies", "solo consultants", "SaaS teams",
         "bootstrapped startups", "freelance developers", "content creators",
         "e-commerce operators", "B2B marketing teams", "dev-tool companies")

# angles applied to spawn genuinely different variants of an idea
_MODIFIERS = ("", " (premium concierge tier)", " for agencies (white-label)",
              " - self-serve, no-touch", " with a weekly-refresh subscription",
              " (free core + paid pro)", " - fixed-scope one-off",
              " for non-technical buyers")


def _gather_signals(data_dir) -> dict:
    data_dir = Path(data_dir)
    import json

    def _j(name):
        p = data_dir / name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    words: Counter = Counter()
    for c in (_j("candidates.json") or []):
        for tok in _TOKEN.findall(f"{c.get('name','')} {c.get('description','')}".lower()):
            if tok not in _STOP and len(tok) > 3:
                words[tok] += 1
    phrases: list[str] = []
    for lead in (_j("acquisition.json") or []):
        t = (lead.get("title") or "")[:80]
        if t:
            phrases.append(t)
        for tok in _TOKEN.findall(t.lower()):
            if tok not in _STOP and len(tok) > 3:
                words[tok] += 1

    common = [w for w, _ in words.most_common(16)]
    kw = common or list(_DEFAULT_KW)
    # topic pairs widen the space a lot (kw1 + kw2)
    pairs = [f"{a} + {b}" for i, a in enumerate(kw[:6]) for b in kw[i + 1:6]]
    topics = list(dict.fromkeys(kw + pairs + list(_DEFAULT_KW)))
    tried = {r.get("category") for r in (load_opportunities(data_dir).all())}
    return {"keywords": kw, "topics": topics, "phrases": phrases[:12],
            "tried_categories": tried}


def _instantiate(arch, kw: str, aud: str, modifier: str = "") -> Opportunity:
    key, cat, title_t, target_t, est = arch
    title = (title_t.format(kw=kw, aud=aud) + modifier).strip()
    e = dict(est)
    if "premium" in modifier or "concierge" in modifier:
        e["est_revenue_eur"] = round(e["est_revenue_eur"] * 1.8, 0)
        e["effort_points"] = min(5, e["effort_points"] + 1)
    if "subscription" in modifier or "weekly-refresh" in modifier:
        e["est_revenue_eur"] = round(e["est_revenue_eur"] * 1.3, 0)
        e["scalability"] = min(5, e["scalability"] + 1)
    if "one-off" in modifier or "fixed-scope" in modifier:
        e["time_to_first_revenue_days"] = max(7, e["time_to_first_revenue_days"] - 7)
    return Opportunity(
        title=title, category=cat,
        target_customer=(target_t.format(kw=kw, aud=aud)),
        required_work=f"build the asset(s) for: {title}",
        required_human_actions=["approve any paid launch (ads / domain / paid API)"],
        source="engine", **e)


def _variations(store) -> list[Opportunity]:
    """Take opportunities abandoned only for 'no traction' and re-try them
    with a genuinely different angle (spec: modify it, test again if
    justified). Fresh id -> not treated as the same experiment."""
    out: list[Opportunity] = []
    stale = [r for r in store.by_status("abandoned")
             if "no traction" in (r.get("experiments") or [{}])[-1].get("note", "")
             and not r.get("_revived")]
    for r in stale[:6]:
        base = r["title"].split(" - ")[0].split(" (")[0]
        for m in (_MODIFIERS[1], _MODIFIERS[4], _MODIFIERS[2]):
            title = base + m
            out.append(Opportunity(
                title=title, category=r["category"],
                target_customer=r.get("target_customer", ""),
                required_work=f"re-try with a new angle: {title}",
                source="revived", parent_id=r["id"],
                est_revenue_eur=float(r.get("est_revenue_eur", 100)),
                probability=max(0.06, float(r.get("probability", 0.12)) * 0.85),
                effort_points=int(r.get("effort_points", 3)),
                difficulty=int(r.get("difficulty", 3)),
                time_to_first_revenue_days=int(r.get("time_to_first_revenue_days", 30)),
                scalability=int(r.get("scalability", 3)),
                competition=r.get("competition", "unknown"),
                legal_platform_risk=r.get("legal_platform_risk", "low")))
        r["_revived"] = True
    return out


def generate(data_dir, *, n: int = 8, llm: bool = False,
             persist: bool = True) -> list[dict]:
    """Propose up to `n` NEW opportunities (not already on the board).

    Draws from ~28 archetypes x ~30 topics x 10 audiences x 8 angle
    modifiers, plus re-tried variations of opportunities that were
    abandoned only for lack of traction. Deterministic: each cycle
    surfaces the next-best combos not already on the board."""
    if llm:
        raise ActionBlocked(
            "LLM-based opportunity discovery is a paid action "
            "(real_llm_call -> MONEY_APPROVAL_REQUIRED). Approve an LLM budget "
            "outside autonomous mode to enable open-ended discovery.")

    signals = _gather_signals(data_dir)
    store = load_opportunities(data_dir)
    existing = set(store._by_id)

    proposed: list[Opportunity] = []
    topics = signals["topics"]
    for i, arch in enumerate(_A):
        for j, topic in enumerate(topics[:10]):
            aud = _AUDS[(i + j) % len(_AUDS)]
            mod = _MODIFIERS[(i + j) % len(_MODIFIERS)]
            proposed.append(_instantiate(arch, topic, aud, mod))
    proposed += _variations(store)
    proposed.sort(key=lambda o: -_prescore(o))

    # spread across categories so a cycle never returns one business model
    fresh: list[dict] = []
    per_cat: Counter = Counter()
    for pass_cap in (2, 3, 99):
        for opp in proposed:
            if len(fresh) >= n:
                break
            if opp.id in existing or per_cat[opp.category] >= pass_cap:
                continue
            existing.add(opp.id)
            per_cat[opp.category] += 1
            fresh.append(store.upsert(opp) if persist else opp.to_dict())
        if len(fresh) >= n:
            break
    if persist:
        store.save()
    return fresh


def _prescore(o: Opportunity) -> float:
    from .opportunity_store import score_opportunity
    return score_opportunity(o)
