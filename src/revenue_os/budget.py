"""Pre-sale hard budget cap.

Business rule: **before the first real sale, at most EUR 3.00 TOTAL may be
spent on external paid APIs** (not per agent / per day / per run - total).
The remaining EUR 17.00 growth capital is locked until a real payment
lands in the RevenueLedger.

This is enforced in ONE place: `guard()` is called from
`llm_workers.budget_gate`, which every paid LLM / web-search path already
goes through. No second billing system.

"First real sale" = RevenueLedger.total() > 0, i.e. a verified LIVE PayPal
capture booked through the existing payment rules. Nothing here books
revenue or moves money.
"""

from __future__ import annotations

from pathlib import Path

# LLM / web-search spend is metered in USD; the cap is a EUR 3.00 rule.
# 3.20 USD ~= 3.00 EUR - deliberately a hair under so rounding never lets
# a EUR-3.01 spend through.
PRESALE_CAP_USD = 3.20
PRESALE_CAP_EUR = 3.00
RESERVED_GROWTH_CAPITAL_EUR = 17.00
INITIAL_CAPITAL_EUR = 20.00


class BudgetBlocked(ValueError):
    """Raised when a paid operation would breach the pre-sale hard cap."""


def _revenue_total(data_dir) -> float:
    from .revenue import RevenueLedger

    return RevenueLedger.load(Path(data_dir) / "revenue.json").total()


def _spent_usd(data_dir) -> float:
    from .llm_spend import LlmSpendLog

    return float(LlmSpendLog.load(
        Path(data_dir) / "llm_spend.json").summary()["total_cost_usd"])


def presale_active(data_dir) -> bool:
    """True until the first real sale is booked."""
    return _revenue_total(data_dir) <= 0.0


def presale_remaining_usd(data_dir) -> float:
    return round(max(0.0, PRESALE_CAP_USD - _spent_usd(data_dir)), 4)


def status(data_dir) -> dict:
    spent = _spent_usd(data_dir)
    rev = _revenue_total(data_dir)
    active = rev <= 0.0
    return {
        "presale_active": active,
        "initial_capital_eur": INITIAL_CAPITAL_EUR,
        "presale_cap_eur": PRESALE_CAP_EUR,
        "presale_cap_usd": PRESALE_CAP_USD,
        "reserved_growth_capital_eur": RESERVED_GROWTH_CAPITAL_EUR,
        "external_spent_usd": round(spent, 4),
        "presale_remaining_usd": round(max(0.0, PRESALE_CAP_USD - spent), 4),
        "growth_capital_available_eur": (0.0 if active
                                         else RESERVED_GROWTH_CAPITAL_EUR),
        "revenue_eur": round(rev, 2),
    }


def guard(data_dir, estimate_usd: float) -> None:
    """Raise BudgetBlocked if this paid op would breach the pre-sale cap.
    A no-op once a real sale exists (growth capital is a human decision,
    never spent automatically by this guard)."""
    if not presale_active(data_dir):
        return
    spent = _spent_usd(data_dir)
    if spent + float(estimate_usd) > PRESALE_CAP_USD + 1e-9:
        raise BudgetBlocked(
            f"BLOCK - pre-sale hard limit. Recorded external spend "
            f"${spent:.2f} + estimated ${float(estimate_usd):.2f} would exceed "
            f"the ${PRESALE_CAP_USD:.2f} cap (EUR {PRESALE_CAP_EUR:.2f}). The "
            f"reserved EUR {RESERVED_GROWTH_CAPITAL_EUR:.0f} growth capital is "
            f"locked until the first real sale. Nothing was spent."
        )
