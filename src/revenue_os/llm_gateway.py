"""The single gateway every LLM interaction routes through.

It does NOT replace the money firewall. `guard_no_money_in_autonomy`
still hard-blocks PayPal / payments / e-mail / deploy inside the
autonomous loop. This layer adds *controlled* LLM usage on top:

  * per-call / task / hourly / daily / global spend limits
  * a calls-per-minute rate limit
  * an emergency stop (kill switch)
  * a full call-by-call audit log (data/llm_audit.json)
  * configurable provider + model (anthropic | mock | none)
  * a scoped, policy-gated permission for the AUTONOMOUS loop
  * graceful fallback: when the LLM is unavailable it raises
    `LlmUnavailable`, which callers catch and use their deterministic path

With Anthropic disabled/unavailable and the default policy, behaviour is
IDENTICAL to before: `build_client` still refuses inside the autonomous
context, and every LLM agent runs its deterministic mode.

State: data/llm_policy.json   data/llm_audit.json
Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .action_class import ActionBlocked
from .store import now_iso

_AUDIT_CAP = 500
_UNLIMITED = 1e9        # returned by preflight when the LLM tier is off
PROVIDERS = ("none", "mock", "anthropic")


class LlmUnavailable(ActionBlocked):
    """The LLM cannot be used right now - callers fall back to deterministic.
    NOT a spend/limit error; a 'not turned on / not permitted / stopped' state.

    Subclasses ActionBlocked so the existing money-firewall guarantee holds:
    inside the autonomous context, with the default policy, constructing an
    LLM client still raises ActionBlocked exactly as before."""


class LlmBudgetExceeded(RuntimeError):
    """A spend, rate, or per-call limit would be breached by this call."""


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

@dataclass
class LlmPolicy:
    # master switches - all default OFF / safe
    enabled: bool = False              # LLM tier turned on at all (needs credits + opt-in)
    autonomous_enabled: bool = False   # may the autonomous loop use the LLM?
    emergency_stop: bool = False       # kill switch - blocks ALL llm use everywhere
    provider: str = "none"             # none | mock | anthropic
    model: str = "claude-sonnet-5"
    fallback_model: str = "claude-haiku-4-5-20251001"
    # spend limits (USD)
    per_call_usd: float = 0.05
    hourly_usd: float = 0.50
    daily_usd: float = 2.00
    global_usd: float = 5.00
    task_default_usd: float = 0.20
    task_limits: dict = field(default_factory=dict)   # {task: usd}
    # rate limit
    max_calls_per_min: int = 12
    updated_at: str = ""
    updated_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def limit_for(self, task: str) -> float:
        return float(self.task_limits.get(task, self.task_default_usd))


def _policy_path(data_dir) -> Path:
    return Path(data_dir) / "llm_policy.json"


def load_policy(data_dir) -> LlmPolicy:
    p = _policy_path(data_dir)
    if not p.exists():
        return LlmPolicy()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return LlmPolicy()
    if not isinstance(raw, dict):
        return LlmPolicy()
    fields = set(LlmPolicy().__dict__)
    return LlmPolicy(**{k: v for k, v in raw.items() if k in fields})


def save_policy(data_dir, policy: LlmPolicy, *, by: str = "human") -> None:
    policy.updated_at = now_iso()
    policy.updated_by = by
    p = _policy_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(policy.to_dict(), indent=2, sort_keys=True))
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------

def _audit_path(data_dir) -> Path:
    return Path(data_dir) / "llm_audit.json"


def load_audit(data_dir) -> list[dict]:
    p = _audit_path(data_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _append_audit(data_dir, entry: dict) -> None:
    entries = load_audit(data_dir)
    entries.append(entry)
    entries = entries[-_AUDIT_CAP:]
    p = _audit_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(entries, indent=2))
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# gateway
# ---------------------------------------------------------------------------

def _anthropic_importable() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


class LlmGateway:
    def __init__(self, data_dir) -> None:
        self.data_dir = Path(data_dir)
        self.policy = load_policy(data_dir)

    # --- availability -------------------------------------------------
    def available(self) -> tuple[bool, str]:
        p = self.policy
        if p.emergency_stop:
            return False, "emergency stop is engaged"
        if not p.enabled:
            return False, "LLM tier is disabled (no credits / not opted in)"
        if p.provider not in PROVIDERS or p.provider == "none":
            return False, f"LLM provider is {p.provider!r} - no real client"
        if p.provider == "anthropic" and not _anthropic_importable():
            return False, "the 'anthropic' package is not installed"
        return True, ""

    # --- spend windows (from the audit log = call-by-call actuals) ---
    def _spent_since(self, seconds: float) -> float:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        total = 0.0
        for e in load_audit(self.data_dir):
            t = _parse(e.get("ts", ""))
            if t is not None and t >= cutoff:
                total += float(e.get("actual_usd", e.get("est_usd", 0.0)) or 0.0)
        return round(total, 4)

    def _spent_today(self) -> float:
        today = datetime.now(timezone.utc).date()
        total = 0.0
        for e in load_audit(self.data_dir):
            t = _parse(e.get("ts", ""))
            if t is not None and t.date() == today:
                total += float(e.get("actual_usd", e.get("est_usd", 0.0)) or 0.0)
        return round(total, 4)

    def _spent_total(self) -> float:
        try:
            from .llm_spend import LlmSpendLog
            ledger = LlmSpendLog.load(self.data_dir / "llm_spend.json"
                                      ).summary()["total_cost_usd"]
        except Exception:
            ledger = 0.0
        audit = sum(float(e.get("actual_usd", e.get("est_usd", 0.0)) or 0.0)
                    for e in load_audit(self.data_dir))
        return round(max(ledger, audit), 4)

    def _calls_last_minute(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        return sum(1 for e in load_audit(self.data_dir)
                   if (_parse(e.get("ts", "")) or datetime.min.replace(
                       tzinfo=timezone.utc)) >= cutoff)

    def balances(self) -> dict:
        p = self.policy
        return {
            "spent_this_hour": self._spent_since(3600),
            "spent_today": self._spent_today(),
            "spent_total": self._spent_total(),
            "remaining_hour": round(max(0.0, p.hourly_usd - self._spent_since(3600)), 4),
            "remaining_today": round(max(0.0, p.daily_usd - self._spent_today()), 4),
            "remaining_global": round(max(0.0, p.global_usd - self._spent_total()), 4),
            "calls_last_minute": self._calls_last_minute(),
        }

    # --- shared gate: is ANY llm call allowed right now? --------------
    def _gate(self, *, autonomous: bool | None) -> None:
        """Raise LlmUnavailable when no LLM call may happen at all.
        This is the real hard block: emergency stop, and the autonomous
        loop's scoped permission. Default policy => refuses inside the
        autonomous context, exactly like the old guard."""
        from .action_class import in_autonomous_context

        p = self.policy
        if p.emergency_stop:
            raise LlmUnavailable("emergency stop is engaged - no LLM calls")
        auton = in_autonomous_context() if autonomous is None else autonomous
        if auton:
            if not p.autonomous_enabled:
                raise LlmUnavailable(
                    "the autonomous loop is not authorised to use the LLM. "
                    "Run `llm-policy --enable-autonomous` outside autonomous "
                    "mode first.")
            ok, why = self.available()
            if not ok:
                raise LlmUnavailable(why)

    # --- the light check for build_client -----------------------------
    def assert_client_allowed(self, *, autonomous: bool | None = None) -> None:
        """Called by llm_normalize.build_client. Raises LlmUnavailable when
        the client must not be constructed. Does NOT check spend.

        Strict / fail-closed: the emergency stop and the autonomous-loop
        permission (via `_gate`), AND `available()` - so a disabled tier,
        `provider="none"`, an unknown provider, or a missing `anthropic`
        package all block client construction, in EVERY mode (not just the
        autonomous loop)."""
        self._gate(autonomous=autonomous)
        ok, why = self.available()
        if not ok:
            raise LlmUnavailable(why)

    def resolve_provider(self, *, autonomous: bool | None = None) -> str:
        """The one place provider selection happens. Returns exactly one of
        "mock" / "anthropic" after all gates pass, or raises LlmUnavailable.
        Never returns "none" and never falls through to a paid provider for
        an unknown/disabled state."""
        self.assert_client_allowed(autonomous=autonomous)
        prov = self.policy.provider
        if prov == "mock":
            return "mock"
        if prov == "anthropic":
            return "anthropic"
        raise LlmUnavailable(f"LLM provider is {prov!r} - no real client")

    # --- the full pre-flight for a specific call ---------------------
    def preflight(self, estimate_usd: float, *, task: str = "generic",
                  autonomous: bool | None = None) -> float:
        """Enforce the LLM limits for one upcoming call.

        Raises `LlmUnavailable` (=> caller falls back to deterministic) for
        emergency stop / autonomous-not-authorised, and `LlmBudgetExceeded`
        for the calls-per-minute rate limit. The per-call / task / hourly /
        daily / global spend limits are returned as a single effective
        ceiling (clamped, never negative); the caller mins it into its own
        ceiling and `LlmNormalizer` stops the moment it is reached. The
        cumulative `llm_budget` cap and the pre-sale cap are enforced
        separately by `llm_workers.budget_gate`."""
        est = max(0.0, float(estimate_usd))
        self._gate(autonomous=autonomous)

        p = self.policy
        # The spend + rate limits guard *real* money. While the LLM tier is
        # off (provider "none" / not enabled) no money can move, so they are
        # dormant - tracked and logged, but not blocking. They engage the
        # moment a human runs `llm-policy --enable`. The autonomous gate and
        # the emergency stop in `_gate()` above are ALWAYS enforced.
        if not p.enabled:
            # tier off: no real money can move (build_client refuses to make a
            # client), so the spend limits are dormant - tracked, not blocking.
            return _UNLIMITED

        # enabled: an invalid provider combo (e.g. provider="none", or
        # "anthropic" with no package) fails closed rather than handing back
        # a spendable ceiling.
        ok, why = self.available()
        if not ok:
            raise LlmUnavailable(why)

        if self._calls_last_minute() >= p.max_calls_per_min:
            raise LlmBudgetExceeded(
                f"rate limit: {p.max_calls_per_min} LLM calls/min reached")

        b = self.balances()
        ceiling = min(p.per_call_usd, p.limit_for(task), b["remaining_hour"],
                      b["remaining_today"], b["remaining_global"])
        ceiling = round(max(0.0, ceiling), 4)
        if est > ceiling + 1e-9:
            raise LlmBudgetExceeded(
                f"estimate ${est:.4f} exceeds the effective LLM ceiling "
                f"${ceiling:.4f} (per-call ${p.per_call_usd}, task '{task}' "
                f"${p.limit_for(task)}, hour ${b['remaining_hour']}, "
                f"day ${b['remaining_today']}, global ${b['remaining_global']})")
        return ceiling

    # --- record what happened --------------------------------------
    def record(self, *, task: str, model: str, est_usd: float,
               actual_usd: float, in_tokens: int = 0, out_tokens: int = 0,
               cache_hit: bool = False, autonomous: bool = False,
               outcome: str = "ok") -> None:
        _append_audit(self.data_dir, {
            "ts": now_iso(), "task": task, "provider": self.policy.provider,
            "model": model, "est_usd": round(float(est_usd), 5),
            "actual_usd": round(float(actual_usd), 5),
            "in_tokens": int(in_tokens), "out_tokens": int(out_tokens),
            "cache_hit": bool(cache_hit), "autonomous": bool(autonomous),
            "outcome": outcome,
        })


def gateway(data_dir) -> LlmGateway:
    return LlmGateway(data_dir)


def assert_client_allowed(data_dir=None, *, autonomous: bool | None = None) -> None:
    """Module-level helper for build_client. Without a data_dir it can only
    fall back to the autonomous-context hard block (the safe default)."""
    if data_dir is None:
        from .action_class import in_autonomous_context
        auton = in_autonomous_context() if autonomous is None else autonomous
        if auton:
            raise LlmUnavailable(
                "autonomous LLM use is not authorised (no policy in scope)")
        return
    gateway(data_dir).assert_client_allowed(autonomous=autonomous)


def status(data_dir) -> dict:
    g = gateway(data_dir)
    ok, why = g.available()
    return {"policy": g.policy.to_dict(), "available": ok, "reason": why,
            "balances": g.balances(),
            "recent_calls": load_audit(data_dir)[-8:][::-1]}
