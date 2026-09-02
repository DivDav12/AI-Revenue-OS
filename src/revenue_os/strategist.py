"""The Revenue Strategist - deterministic coordination decisions.

Pure functions over the opportunity board + results history. No I/O, no
LLM. The autonomy loop calls these to decide what to build, what to keep,
what to kill, and what to try next - so the fleet is never locked into
one business model.
"""

from __future__ import annotations

# how many opportunities the fleet builds/tests in parallel
DEFAULT_CAPACITY = 3
# an opportunity in flight for this many cycles with no traction is abandoned
_STALE_CYCLES = 4


def _result_signal(rec: dict) -> float:
    r = rec.get("results") or {}
    return (float(r.get("revenue_eur", 0) or 0) * 100
            + float(r.get("signups", 0) or 0) * 10
            + float(r.get("leads", 0) or 0) * 2
            + float(r.get("visits", 0) or 0) * 0.1)


def _cycles_in_flight(rec: dict) -> int:
    return int((rec.get("results") or {}).get("cycles", 0) or 0)


def select_experiments(board: dict, *, capacity: int = DEFAULT_CAPACITY,
                       money_blocked: set[str] | None = None) -> list[dict]:
    """Pick opportunities to move into `building`, filling free capacity.

    NOT a plain top-by-score pick: the fleet must never collapse onto one
    business model. So:
      * at most ONE experiment per category is running at a time,
      * picks are spread across DIFFERENT categories, and
      * one slot is always reserved for an *exploration* pick - the best
        opportunity from a category the fleet has NOT tried recently,
        even if its score is lower.
    Skips anything whose next step needs unapproved money."""
    money_blocked = money_blocked or set()
    active = (board.get("building", []) + board.get("testing", [])
              + board.get("active", []))
    free = max(0, capacity - len(active))
    if free == 0:
        return []

    running_cats = {r.get("category") for r in active}
    tested_cats = running_cats | {
        r.get("category") for r in board.get("abandoned", [])
        if _cycles_in_flight(r) > 0}          # categories that got a real test

    pool = [r for r in board.get("evaluating", []) + board.get("discovered", [])
            if r["id"] not in money_blocked]
    pool.sort(key=lambda r: -float(r.get("score", 0)))

    picks: list[dict] = []
    used_cats = set(running_cats)

    # 1) exploration slot: best opp from a never-tested category
    explore = next((r for r in pool
                    if r.get("category") not in tested_cats
                    and r.get("category") not in used_cats), None)
    if explore is not None and free > 0:
        picks.append(explore)
        used_cats.add(explore.get("category"))

    # 2) fill the rest, one per category, highest score first
    for rec in pool:
        if len(picks) >= free:
            break
        cat = rec.get("category")
        if cat in used_cats or rec in picks:
            continue
        picks.append(rec)
        used_cats.add(cat)

    # 3) if still short (few categories left), allow the next best regardless
    for rec in pool:
        if len(picks) >= free:
            break
        if rec not in picks:
            picks.append(rec)
    return picks[:free]


def review_experiments(board: dict) -> dict:
    """For each in-flight opportunity return one of:
       'continue' | 'optimize' | 'abandon' | 'promote'
    plus a reason. Deterministic from results + attempts."""
    verdicts: dict[str, tuple[str, str]] = {}
    for rec in board.get("testing", []) + board.get("active", []) + board.get("building", []):
        oid = rec["id"]
        sig = _result_signal(rec)
        cyc = _cycles_in_flight(rec)
        rev = float((rec.get("results") or {}).get("revenue_eur", 0) or 0)
        if rev > 0:
            verdicts[oid] = ("promote", f"has real revenue (EUR {rev:.2f}) - scale it")
        elif sig >= 20:
            verdicts[oid] = ("optimize", f"early traction (signal {sig:.0f}) - iterate")
        elif cyc >= _STALE_CYCLES and sig < 1:
            verdicts[oid] = ("abandon",
                             f"{cyc} cycles, no traction - drop and try alternatives")
        elif cyc >= 2 and sig < 5:
            verdicts[oid] = ("optimize", "weak after 2 cycles - change the angle")
        else:
            verdicts[oid] = ("continue", f"cycle {cyc + 1} - give it time")
    return verdicts


def adjacent_opportunities(rec: dict) -> list[dict]:
    """When something works, propose 1-2 adjacent ideas (same customer,
    neighbouring category). Returns partial dicts the engine/store fills."""
    cat = rec.get("category", "other")
    tgt = rec.get("target_customer", "the same customers")
    nxt = {
        "template_pack": "micro_saas", "digital_product": "content_business",
        "micro_saas": "api_product", "api_product": "developer_tool",
        "automation_service": "b2b_service", "ai_service": "information_product",
        "freelancing": "productised retainer", "affiliate": "information_product",
        "marketplace": "data_product", "content_business": "template_pack",
    }.get(cat, "niche_service")
    return [
        {"title": f"{rec.get('title','')} - upsell / recurring version",
         "category": cat, "target_customer": tgt, "source": "adjacent",
         "parent_id": rec["id"]},
        {"title": f"{nxt.replace('_',' ')} for {tgt}", "category":
         nxt if "_" in nxt else "niche_service", "target_customer": tgt,
         "source": "adjacent", "parent_id": rec["id"]},
    ]


def objective(board: dict, revenue_eur: float) -> str:
    """One-line current objective, from real board state."""
    if revenue_eur > 0:
        return "scale the earning opportunity and open adjacent revenue streams"
    active = len(board.get("building", []) + board.get("testing", []) + board.get("active", []))
    if active:
        return f"validate {active} opportunity experiment(s) and find first paying customers"
    if board.get("evaluating") or board.get("discovered"):
        return "evaluate the opportunity board and start the top experiments"
    return "discover new revenue opportunities across all categories"
