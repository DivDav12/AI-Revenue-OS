"""Phase 6 - the mandatory task classifier for the execution layer.

Every `ExecutionTask` the Worker is about to run is first sent through
`classify_task()`. This is NOT a second security policy: it reuses the
existing `action_class` rule table (the same firewall the autonomous loop
uses), mapping each `task_type` to the stable action *kind* that
`action_class.classify()` already knows, then expressing the result in the
task layer's vocabulary:

  SAFE_AUTONOMOUS      the Worker may run it unattended
  EXTERNAL_AUTHORIZED  acts on an OWNED external resource; the Worker may
                       run it only when that specific channel/adapter is
                       explicitly authorized, or a human released the task
  MONEY / IDENTITY / LEGAL
                       needs a human approval first - the Worker blocks the
                       task (BLOCKED_APPROVAL) and never executes it
  TOS_BLOCKED          forbidden by a platform's rules - permanently failed
  SAFETY_BLOCKED       fail-closed: unknown task type / unknown action

The Worker (`worker.Worker._authorize` / `_gate`) is the single choke
point; there is no execution path that skips it.

Pure logic, deterministic, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import action_class as _ac
from .execution import TASK_TYPES

SAFE_AUTONOMOUS = "SAFE_AUTONOMOUS"
EXTERNAL_AUTHORIZED = "EXTERNAL_AUTHORIZED"
MONEY = "MONEY"
IDENTITY = "IDENTITY"
LEGAL = "LEGAL"
TOS_BLOCKED = "TOS_BLOCKED"
SAFETY_BLOCKED = "SAFETY_BLOCKED"

TASK_CLASSES: tuple[str, ...] = (
    SAFE_AUTONOMOUS, EXTERNAL_AUTHORIZED, MONEY, IDENTITY, LEGAL,
    TOS_BLOCKED, SAFETY_BLOCKED,
)

#: the human-approval firewall bucket for each blocking class
_APPROVAL_TYPE = {MONEY: "money", IDENTITY: "identity", LEGAL: "legal"}

#: task_type -> the action_class *kind* it is an instance of. DEPLOY /
#: DISTRIBUTE / DELIVER have dedicated rules in classify_task().
_TASK_KIND: dict[str, str] = {
    "RESEARCH":         "research",
    "SCORE":            "score_opportunity",
    "PLAN":             "agent_spec_draft",
    "BUILD_PRODUCT":    "create_digital_product",
    "BUILD_PAGE":       "build_landing_page",
    "CREATE_CONTENT":   "write_copy",
    "VALIDATE_PRODUCT": "run_deterministic_agent",
    "VALIDATE_PAGE":    "run_deterministic_agent",
    "ANALYZE":          "analytics",
    "CHECK_TRAFFIC":    "analytics",
    "CHECK_LEADS":      "analytics",
    "CHECK_REVENUE":    "monitor",          # read-only incoming-payment poll
    "OPTIMIZE":         "optimize_nonfinancial",
    "SPAWN_VARIANT":    "experiment_no_spend",
    "SCALE":            "optimize_nonfinancial",
    # ecosystem - all read-only projection, never spend / post / contact
    "DISCOVER":         "discover_opportunity",
    "VERIFY":           "verify_opportunity",
    "EVALUATE":         "evaluate_profitability",
    "SELECT_STRATEGY":  "select_strategy",
    # ecosystem TASK-strategy execution - all three reuse an EXISTING SAFE
    # kind (no new action_class rule): PLAN_TASK is a spec draft, EXECUTE_TASK
    # is exactly the already-declared "prepare_task_solution" kind, and
    # VERIFY_RESULT is the same deterministic self-check kind VALIDATE_*
    # already uses. None of them spend money, post anywhere, or touch an
    # identity - the external submission step stays a human action (see
    # ecosystem/pipeline.py + acceptance.pending_actions SUBMIT_TASK).
    "PLAN_TASK":        "agent_spec_draft",
    "EXECUTE_TASK":     "prepare_task_solution",
    "VERIFY_RESULT":    "run_deterministic_agent",
}

#: DISTRIBUTE sub-channels that are pure drafting (no external action)
_DRAFT_CHANNELS = frozenset({"community_draft", "social_draft"})
#: DISTRIBUTE sub-channels that publish to a channel the owner controls
_OWNED_CHANNELS = frozenset({"owned_web", "owned_content"})


@dataclass(frozen=True)
class TaskVerdict:
    task_type: str
    task_class: str
    reason: str
    approval_type: str = ""      # "money" / "identity" / "legal" when it blocks
    #: an EXTERNAL_AUTHORIZED task that, when the channel is NOT authorized,
    #: performs no outward action at all (a documented safe no-op). The
    #: Worker runs it rather than blocking a dependent chain.
    safe_when_unauthorized: bool = False

    @property
    def autonomous(self) -> bool:
        return self.task_class == SAFE_AUTONOMOUS

    @property
    def needs_authorization(self) -> bool:
        return self.task_class == EXTERNAL_AUTHORIZED

    @property
    def needs_approval(self) -> bool:
        return self.task_class in (MONEY, IDENTITY, LEGAL)

    @property
    def blocked_forever(self) -> bool:
        return self.task_class in (TOS_BLOCKED, SAFETY_BLOCKED)


def _from_action_class(kind: str, context: dict) -> tuple[str, str]:
    """Translate an `action_class` verdict into a task class + reason."""
    v = _ac.classify(kind, context)
    ac = v.action_class
    if ac is _ac.ActionClass.SAFE_AUTONOMOUS:
        return SAFE_AUTONOMOUS, v.reason
    if ac is _ac.ActionClass.MONEY_APPROVAL_REQUIRED:
        return MONEY, v.reason
    if ac is _ac.ActionClass.IDENTITY_APPROVAL_REQUIRED:
        return IDENTITY, v.reason
    if ac is _ac.ActionClass.LEGAL_APPROVAL_REQUIRED:
        return LEGAL, v.reason
    # action_class.SAFETY_BLOCKED -> fail closed
    return SAFETY_BLOCKED, v.reason


def classify_task(task_type: str, context: dict | None = None) -> TaskVerdict:
    """The single classification entry point for the execution layer."""
    tt = (task_type or "").strip()
    ctx = dict(context or {})

    if tt not in TASK_TYPES:
        return TaskVerdict(tt or "<empty>", SAFETY_BLOCKED,
                           f"unknown task_type {tt!r} - failing closed (safety)")

    # --- DEPLOY: publish the offer page on the owner's own hosting ------
    if tt == "DEPLOY":
        if ctx.get("has_checkout") or ctx.get("collects_payment"):
            return TaskVerdict(
                tt, MONEY,
                "the page collects payment - activating a paid checkout needs "
                "a human money approval", "money")
        return TaskVerdict(
            tt, EXTERNAL_AUTHORIZED,
            "publishes the landing page on the owner's own hosting - the "
            "Worker runs it only against an explicitly authorized deploy "
            "channel, or after a human release", "money")

    # --- DISTRIBUTE: one channel at a time -----------------------------
    if tt == "DISTRIBUTE":
        channel = str(ctx.get("channel") or "owned_web")
        if channel in _DRAFT_CHANNELS:
            return TaskVerdict(
                tt, SAFE_AUTONOMOUS,
                f"{channel}: prepares a review-only draft - nothing is posted")
        if channel in _OWNED_CHANNELS:
            return TaskVerdict(
                tt, EXTERNAL_AUTHORIZED,
                f"{channel}: publishes to a channel the owner controls - runs "
                "only against an authorized channel (else a safe no-op)",
                safe_when_unauthorized=True)
        return TaskVerdict(
            tt, TOS_BLOCKED,
            f"automated posting to {channel!r} is not permitted by its rules - "
            "the fleet drafts, a human posts")

    # --- DELIVER: fulfilment of a completed sale -----------------------
    if tt == "DELIVER":
        # Sending the purchased digital product to the buyer who already
        # paid fulfils an obligation the sale created - it is not a new
        # outward decision. The delivery-adapter layer is Null-by-default
        # (fail closed) and the real SMTP path additionally refuses inside
        # autonomous_context(), so a real send still cannot happen
        # unattended without configuration.
        return TaskVerdict(
            tt, SAFE_AUTONOMOUS,
            "delivers an already-purchased digital product to the paying "
            "buyer - the delivery-adapter layer is fail-closed")

    # --- everything else: the shared action_class rule table -----------
    kind = _TASK_KIND.get(tt)
    if kind is None:
        return TaskVerdict(tt, SAFETY_BLOCKED,
                           f"no classification rule for {tt!r} - failing closed")
    cls, reason = _from_action_class(kind, ctx)
    return TaskVerdict(tt, cls, reason, _APPROVAL_TYPE.get(cls, ""))
