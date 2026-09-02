"""The persistent ExecutionTask queue.

One unit of real work per record: research an opportunity, build a page,
validate it, deploy it, check traffic, deliver a sale. The queue is a
single JSON file (`<data-dir>/tasks.json`, atomic write) so a JARVIS
restart never loses in-flight work - a RUNNING task whose worker vanished
is reclaimed after its lease expires, a FAILED_RETRYABLE task comes back
when its backoff elapses.

This module is the QUEUE only - it does not execute anything. The worker
executor (later phase) claims READY tasks, runs them, and reports back
through `mark_succeeded` / `mark_failed`. JARVIS turns task results into
opportunity state transitions and follow-up tasks.

Status machine:

  PENDING           deps not yet satisfied (or just created)
  READY             all deps SUCCEEDED - a worker may claim it
  RUNNING           a worker holds a lease and is executing it
  SUCCEEDED         done; output stored                         (terminal)
  FAILED_RETRYABLE  failed, will retry after next_retry_at (backoff)
  FAILED_FINAL      out of attempts, or a dependency failed     (terminal)
  BLOCKED_APPROVAL  needs a human MONEY / IDENTITY / LEGAL approval first
  CANCELLED         withdrawn by a human / the orchestrator      (terminal)

Standard library only. No network, no money, no LLM.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .store import now_iso

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

TASK_STATUSES: tuple[str, ...] = (
    "PENDING", "READY", "RUNNING", "SUCCEEDED", "FAILED_RETRYABLE",
    "FAILED_FINAL", "BLOCKED_APPROVAL", "CANCELLED",
)

TERMINAL: frozenset[str] = frozenset({"SUCCEEDED", "FAILED_FINAL", "CANCELLED"})

#: a task in one of these is still "live" for idempotency purposes
_LIVE = frozenset({"PENDING", "READY", "RUNNING", "FAILED_RETRYABLE",
                   "BLOCKED_APPROVAL"})

TASK_TYPES: tuple[str, ...] = (
    "RESEARCH", "SCORE", "PLAN",
    "BUILD_PRODUCT", "BUILD_PAGE", "CREATE_CONTENT",
    "VALIDATE_PRODUCT", "VALIDATE_PAGE",
    "DEPLOY", "DISTRIBUTE",
    "CHECK_TRAFFIC", "CHECK_LEADS", "CHECK_REVENUE",
    "DELIVER", "ANALYZE", "OPTIMIZE", "SPAWN_VARIANT", "SCALE",
)

_APPROVAL_TYPES: frozenset[str] = frozenset({"money", "identity", "legal"})

_LEGAL: dict[str, set[str]] = {
    "PENDING":          {"READY", "BLOCKED_APPROVAL", "CANCELLED", "FAILED_FINAL"},
    "READY":            {"RUNNING", "BLOCKED_APPROVAL", "CANCELLED", "PENDING"},
    "RUNNING":          {"SUCCEEDED", "FAILED_RETRYABLE", "FAILED_FINAL",
                         "BLOCKED_APPROVAL", "CANCELLED", "PENDING"},
    "FAILED_RETRYABLE": {"PENDING", "READY", "FAILED_FINAL", "CANCELLED"},
    "BLOCKED_APPROVAL": {"PENDING", "READY", "CANCELLED", "FAILED_FINAL"},
    "SUCCEEDED":        set(),
    "FAILED_FINAL":     set(),
    "CANCELLED":        set(),
}

_RETRY_BASE_SECONDS = 60
_RETRY_CAP_SECONDS = 3600
_DEFAULT_LEASE_SECONDS = 900


class TaskError(ValueError):
    """Illegal task operation (bad transition, unknown id, bad type)."""


def new_task_id() -> str:
    return "task_" + uuid4().hex[:16]


def _parse(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _now_dt(now: str | None) -> datetime:
    return _parse(now) or datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# the task record
# ---------------------------------------------------------------------------

@dataclass
class ExecutionTask:
    opportunity_id: str
    task_type: str
    task_id: str = ""
    status: str = "PENDING"
    priority: int = 0
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    attempt_count: int = 0
    max_attempts: int = 3
    next_retry_at: str = ""
    worker: str = ""
    input: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)
    error: str = ""
    depends_on: list = field(default_factory=list)
    cost_estimate: float = 0.0
    actual_cost: float = 0.0
    requires_approval: bool = False
    approval_type: str = ""
    idempotency_key: str = ""
    lease_until: str = ""

    def __post_init__(self):
        if self.task_type not in TASK_TYPES:
            raise TaskError(f"unknown task_type {self.task_type!r}")
        if self.approval_type and self.approval_type not in _APPROVAL_TYPES:
            raise TaskError(f"unknown approval_type {self.approval_type!r}")
        if not self.task_id:
            self.task_id = new_task_id()
        if not self.created_at:
            self.created_at = now_iso()
        self.depends_on = list(self.depends_on)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionTask":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------

class TaskQueue:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_id: dict[str, ExecutionTask] = {}

    # --- persistence ------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "TaskQueue":
        q = cls(path)
        if not q.path.exists():
            return q
        try:
            raw = json.loads(q.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return q                     # a corrupt queue file is not fatal
        for d in raw if isinstance(raw, list) else []:
            if isinstance(d, dict) and d.get("task_id"):
                try:
                    t = ExecutionTask.from_dict(d)
                except TaskError:
                    continue
                q._by_id[t.task_id] = t
        return q

    def save(self) -> None:
        payload = json.dumps([t.to_dict() for t in self._by_id.values()],
                             indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- adding ---------------------------------------------------
    def add(self, task: ExecutionTask) -> ExecutionTask:
        """Enqueue a task. If an idempotency_key is set and a non-terminal
        or already-SUCCEEDED task with that key exists, that task is
        returned unchanged and nothing new is queued."""
        if task.idempotency_key:
            dup = self._find_by_key(task.idempotency_key)
            if dup is not None:
                return dup
        self._by_id[task.task_id] = task
        return task

    def create(self, opportunity_id: str, task_type: str, **kw) -> ExecutionTask:
        return self.add(ExecutionTask(opportunity_id=opportunity_id,
                                      task_type=task_type, **kw))

    def _find_by_key(self, key: str) -> ExecutionTask | None:
        for t in self._by_id.values():
            if t.idempotency_key == key and (
                    t.status in _LIVE or t.status == "SUCCEEDED"):
                return t
        return None

    # --- reads ---------------------------------------------------
    def get(self, task_id: str) -> ExecutionTask | None:
        return self._by_id.get(task_id)

    def all(self) -> list[ExecutionTask]:
        return list(self._by_id.values())

    def by_status(self, *statuses: str) -> list[ExecutionTask]:
        return [t for t in self._by_id.values() if t.status in statuses]

    def by_opportunity(self, opportunity_id: str) -> list[ExecutionTask]:
        return [t for t in self._by_id.values()
                if t.opportunity_id == opportunity_id]

    def counts(self) -> dict:
        c = {s: 0 for s in TASK_STATUSES}
        for t in self._by_id.values():
            c[t.status] = c.get(t.status, 0) + 1
        return c

    def ready(self) -> list[ExecutionTask]:
        """READY tasks, most urgent first (priority desc, then FIFO).

        Call `resolve_dependencies()` first to promote PENDING -> READY."""
        return sorted(self.by_status("READY"),
                      key=lambda t: (-int(t.priority), t.created_at))

    # --- dependency resolution ----------------------------------
    def resolve_dependencies(self) -> dict:
        """Promote PENDING tasks whose deps all SUCCEEDED to READY; fail
        PENDING tasks whose deps ended in FAILED_FINAL / CANCELLED.
        Idempotent - safe to call every tick."""
        promoted, failed, blocked = [], [], []
        for t in self._by_id.values():
            if t.status != "PENDING":
                continue
            if t.requires_approval:
                self._set(t, "BLOCKED_APPROVAL")
                blocked.append(t.task_id)
                continue
            dep_states = [self._by_id[d].status for d in t.depends_on
                          if d in self._by_id]
            missing = [d for d in t.depends_on if d not in self._by_id]
            if any(s in ("FAILED_FINAL", "CANCELLED") for s in dep_states):
                t.error = "a dependency did not succeed"
                self._set(t, "FAILED_FINAL")
                failed.append(t.task_id)
            elif missing:
                t.error = f"unknown dependency: {missing}"
                self._set(t, "FAILED_FINAL")
                failed.append(t.task_id)
            elif all(s == "SUCCEEDED" for s in dep_states):
                self._set(t, "READY")
                promoted.append(t.task_id)
        return {"promoted": promoted, "failed": failed, "blocked": blocked}

    def requeue_due(self, *, now: str | None = None) -> list[str]:
        """FAILED_RETRYABLE tasks whose backoff has elapsed go back to
        PENDING for another attempt."""
        cutoff = _now_dt(now)
        out = []
        for t in self.by_status("FAILED_RETRYABLE"):
            due = _parse(t.next_retry_at)
            if due is None or due <= cutoff:
                self._set(t, "PENDING")
                t.next_retry_at = ""
                out.append(t.task_id)
        return out

    def reclaim_stale(self, *, now: str | None = None) -> list[str]:
        """RUNNING tasks whose worker lease expired are returned to PENDING
        (restart / crash recovery)."""
        cutoff = _now_dt(now)
        out = []
        for t in self.by_status("RUNNING"):
            lease = _parse(t.lease_until)
            if lease is not None and lease <= cutoff:
                t.worker = ""
                t.lease_until = ""
                t.error = "worker lease expired - task reclaimed"
                self._set(t, "PENDING")
                out.append(t.task_id)
        return out

    # --- worker-facing transitions ----------------------------
    def claim(self, task_id: str, worker: str, *,
              lease_seconds: int = _DEFAULT_LEASE_SECONDS,
              now: str | None = None) -> ExecutionTask:
        t = self._require(task_id)
        if t.status != "READY":
            raise TaskError(f"{task_id} is {t.status}, not READY - cannot claim")
        start = _now_dt(now)
        t.worker = worker
        t.started_at = t.started_at or start.isoformat()
        t.lease_until = (start + timedelta(seconds=lease_seconds)).isoformat()
        t.attempt_count += 1
        self._set(t, "RUNNING")
        return t

    def heartbeat(self, task_id: str, *, lease_seconds: int = _DEFAULT_LEASE_SECONDS,
                  now: str | None = None) -> None:
        t = self._require(task_id)
        if t.status == "RUNNING":
            t.lease_until = (_now_dt(now) + timedelta(seconds=lease_seconds)).isoformat()

    def mark_succeeded(self, task_id: str, output: dict | None = None, *,
                       actual_cost: float = 0.0, now: str | None = None) -> ExecutionTask:
        t = self._require(task_id)
        t.output = dict(output or {})
        t.actual_cost = round(float(actual_cost), 4)
        t.error = ""
        t.finished_at = _now_dt(now).isoformat()
        t.lease_until = ""
        self._set(t, "SUCCEEDED")
        return t

    def mark_failed(self, task_id: str, error: str, *, retryable: bool = True,
                    now: str | None = None) -> ExecutionTask:
        """Record a failure. Retryable + attempts left -> FAILED_RETRYABLE
        with an exponential backoff; otherwise FAILED_FINAL."""
        t = self._require(task_id)
        t.error = str(error)
        t.lease_until = ""
        t.worker = ""
        if retryable and t.attempt_count < t.max_attempts:
            delay = min(_RETRY_CAP_SECONDS,
                        _RETRY_BASE_SECONDS * (2 ** max(0, t.attempt_count - 1)))
            t.next_retry_at = (_now_dt(now) + timedelta(seconds=delay)).isoformat()
            self._set(t, "FAILED_RETRYABLE")
        else:
            t.finished_at = _now_dt(now).isoformat()
            self._set(t, "FAILED_FINAL")
        return t

    def block_for_approval(self, task_id: str, approval_type: str) -> ExecutionTask:
        if approval_type not in _APPROVAL_TYPES:
            raise TaskError(f"unknown approval_type {approval_type!r}")
        t = self._require(task_id)
        t.requires_approval = True
        t.approval_type = approval_type
        self._set(t, "BLOCKED_APPROVAL")
        return t

    def unblock(self, task_id: str) -> ExecutionTask:
        """Approval granted - send the task back to dependency resolution."""
        t = self._require(task_id)
        if t.status != "BLOCKED_APPROVAL":
            raise TaskError(f"{task_id} is {t.status}, not BLOCKED_APPROVAL")
        t.requires_approval = False
        self._set(t, "PENDING")
        return t

    def cancel(self, task_id: str, *, reason: str = "") -> ExecutionTask:
        t = self._require(task_id)
        if t.is_terminal:
            raise TaskError(f"{task_id} is already {t.status}")
        if reason:
            t.error = reason
        self._set(t, "CANCELLED")
        return t

    # --- internals ----------------------------------------------
    def _require(self, task_id: str) -> ExecutionTask:
        t = self._by_id.get(task_id)
        if t is None:
            raise TaskError(f"unknown task {task_id!r}")
        return t

    def _set(self, t: ExecutionTask, to: str) -> None:
        if to == t.status:
            return
        if to not in _LEGAL.get(t.status, set()):
            raise TaskError(f"illegal task transition {t.status} -> {to} "
                            f"({t.task_id})")
        t.status = to

    def __len__(self) -> int:
        return len(self._by_id)


def load_tasks(data_dir: str | Path) -> TaskQueue:
    return TaskQueue.load(Path(data_dir) / "tasks.json")
