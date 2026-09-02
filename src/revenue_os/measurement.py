"""Measurement adapters + traction evaluation (Phase 10).

  adapter.measure(kind, opportunity_id, live_url) -> MeasurementSnapshot

`kind` is "traffic" or "leads". Revenue measurement is CHECK_REVENUE's
existing Phase-11 payment path (payments.py) - this module does not touch
money.

Recurrence: the worker re-enqueues a CHECK_* task after each terminal run,
scheduled `MEASUREMENT_INTERVAL_SECONDS` ahead via ExecutionTask.not_before,
with an incrementing `cycle` in the task input and an idempotency key of
`measure:<opp>:<task_type>:<cycle>`. Exactly one live occurrence per type
per opportunity - no task explosion.

NO_TRACTION is deliberately NOT decided on a few watchdog ticks. It needs a
believable basis: enough measurement rounds AND (enough real elapsed time
OR enough traffic seen) AND zero conversions AND the opportunity still in
an early measurement state.

Nothing here spends money, posts, buys, or creates an account. Fail-closed
when no analytics provider is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

MEASUREMENT_TASK_TYPES: tuple[str, ...] = ("CHECK_TRAFFIC", "CHECK_LEADS",
                                           "CHECK_REVENUE")
MEASUREMENT_INTERVAL_SECONDS: int = 6 * 3600
MAX_MEASUREMENT_CYCLES: int = 1000

#: while the opportunity is in one of these, the measurement loop keeps going
KEEP_MEASURING_STATES: frozenset[str] = frozenset({
    "LIVE", "ACQUIRING_TRAFFIC", "MEASURING", "FIRST_VISITOR", "FIRST_LEAD",
    "FIRST_SALE", "DELIVERING", "ACTIVE", "OPTIMIZING",
})

#: LIVE -> MEASURING may fire only from these (never a regression from a
#: milestone already reached)
MEASURING_FROM: tuple[str, ...] = ("LIVE", "ACQUIRING_TRAFFIC")
FIRST_VISITOR_FROM: tuple[str, ...] = ("MEASURING",)
FIRST_LEAD_FROM: tuple[str, ...] = ("MEASURING", "FIRST_VISITOR")
NO_TRACTION_FROM: tuple[str, ...] = ("MEASURING", "FIRST_VISITOR")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------

@dataclass
class MeasurementSnapshot:
    ok: bool
    provider: str
    kind: str = ""
    metrics: dict = field(default_factory=dict)
    blocked: bool = False
    error: str = ""


class MeasurementAdapter:
    provider = "base"

    def measure(self, *, kind: str, opportunity_id: str,
                live_url: str = "") -> MeasurementSnapshot:  # pragma: no cover
        raise NotImplementedError


class NullMeasurementAdapter(MeasurementAdapter):
    provider = "none"

    def measure(self, *, kind, opportunity_id, live_url="") -> MeasurementSnapshot:
        return MeasurementSnapshot(
            ok=False, provider=self.provider, kind=kind, blocked=True,
            error="no analytics provider is configured - the measurement path "
                  "is ready, a real provider adapter must be wired")


class FakeMeasurementAdapter(MeasurementAdapter):
    """Deterministic, offline. `traffic` / `leads` may be a fixed dict, or a
    list of dicts consumed one per call (the last entry repeats)."""

    provider = "fake"

    def __init__(self, *, traffic=None, leads=None, fail: bool = False,
                 blocked: bool = False, error: str = "") -> None:
        self._spec = {"traffic": traffic, "leads": leads}
        self.fail = fail
        self.blocked = blocked
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self._n = {"traffic": 0, "leads": 0}

    def measure(self, *, kind, opportunity_id, live_url="") -> MeasurementSnapshot:
        self.calls.append((kind, opportunity_id))
        if self.blocked:
            return MeasurementSnapshot(ok=False, provider=self.provider, kind=kind,
                                       blocked=True,
                                       error=self.error or "fake: no credentials")
        if self.fail:
            return MeasurementSnapshot(ok=False, provider=self.provider, kind=kind,
                                       error=self.error or "fake: provider error")
        spec = self._spec.get(kind)
        if isinstance(spec, list):
            if not spec:
                m: dict = {}
            else:
                m = dict(spec[min(self._n.get(kind, 0), len(spec) - 1)])
            self._n[kind] = self._n.get(kind, 0) + 1
        elif isinstance(spec, dict):
            m = dict(spec)
        else:
            m = {}
        return MeasurementSnapshot(ok=True, provider=self.provider, kind=kind,
                                   metrics=m)


def default_measurement_adapter() -> MeasurementAdapter:
    return NullMeasurementAdapter()


# ---------------------------------------------------------------------------
# traction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TractionPolicy:
    min_cycles: int = 8              # never before this many measurement rounds
    min_wall_seconds: int = 2 * 86400   # OR this much real elapsed measurement time
    min_visitors: int = 40          # OR at least this much traffic actually seen


DEFAULT_TRACTION_POLICY = TractionPolicy()


@dataclass
class TractionVerdict:
    no_traction: bool = False
    reason: str = ""


def _cumulative(series: list, kind: str, key: str) -> float:
    return sum(_num((s.get("metrics") or {}).get(key))
               for s in series if s.get("kind") == kind)


def evaluate_traction(opp: dict, *, now: str | None = None,
                      policy: TractionPolicy = DEFAULT_TRACTION_POLICY) -> TractionVerdict:
    ex = opp.get("execution") or {}
    series = ex.get("measurement_series") or []
    rounds = len(series)
    if rounds < policy.min_cycles:
        return TractionVerdict()
    if opp.get("state") not in NO_TRACTION_FROM:
        return TractionVerdict()

    visitors = _cumulative(series, "traffic", "visitors")
    leads = _cumulative(series, "leads", "leads")
    revenue = _num((ex.get("metrics") or {}).get("revenue", {}).get("revenue_eur"))
    if leads > 0 or revenue > 0:
        return TractionVerdict()

    tss = [_parse(s.get("ts")) for s in series if s.get("ts")]
    tss = [t for t in tss if t is not None]
    wall = (tss[-1] - tss[0]).total_seconds() if len(tss) >= 2 else 0.0

    if wall < policy.min_wall_seconds and visitors < policy.min_visitors:
        return TractionVerdict()          # not a believable basis yet

    return TractionVerdict(
        True,
        f"{rounds} measurement rounds over {wall / 86400:.1f}d, {int(visitors)} "
        f"visitors, 0 leads, EUR 0 revenue - no conversion on a real basis")
