"""The persistent ExecutionTask queue."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.execution import (
    ExecutionTask,
    TASK_TYPES,
    TaskError,
    TaskQueue,
    load_tasks,
)


def _iso(dt):
    return dt.isoformat()


class TaskRecordTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        t = ExecutionTask(opportunity_id="opp_1", task_type="PLAN")
        self.assertTrue(t.task_id.startswith("task_"))
        self.assertEqual(t.status, "PENDING")
        self.assertTrue(t.created_at)
        with self.assertRaises(TaskError):
            ExecutionTask(opportunity_id="o", task_type="NOPE")
        with self.assertRaises(TaskError):
            ExecutionTask(opportunity_id="o", task_type="PLAN", approval_type="x")

    def test_round_trip(self):
        t = ExecutionTask(opportunity_id="o", task_type="BUILD_PAGE",
                          input={"a": 1}, depends_on=["task_x"])
        t2 = ExecutionTask.from_dict(t.to_dict())
        self.assertEqual(t2.to_dict(), t.to_dict())

    def test_all_spec_task_types_present(self):
        for name in ("RESEARCH", "SCORE", "PLAN", "BUILD_PRODUCT", "BUILD_PAGE",
                     "VALIDATE_PRODUCT", "VALIDATE_PAGE", "DEPLOY",
                     "CREATE_CONTENT", "DISTRIBUTE", "CHECK_TRAFFIC",
                     "CHECK_LEADS", "CHECK_REVENUE", "DELIVER", "ANALYZE",
                     "OPTIMIZE", "SPAWN_VARIANT", "SCALE"):
            self.assertIn(name, TASK_TYPES)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        self.q = TaskQueue(self.d / "tasks.json")

    def tearDown(self):
        self._d.cleanup()

    def test_persistence_round_trip(self):
        a = self.q.create("opp", "PLAN", priority=5)
        self.q.save()
        q2 = load_tasks(self.d)
        self.assertEqual(len(q2), 1)
        self.assertEqual(q2.get(a.task_id).priority, 5)

    def test_idempotency_key_dedupes(self):
        a = self.q.create("opp", "DEPLOY", idempotency_key="deploy:opp:v1")
        b = self.q.create("opp", "DEPLOY", idempotency_key="deploy:opp:v1")
        self.assertIs(a, b)
        self.assertEqual(len(self.q), 1)

    def test_dependency_gating(self):
        a = self.q.create("opp", "BUILD_PAGE")
        b = self.q.create("opp", "VALIDATE_PAGE", depends_on=[a.task_id])
        c = self.q.create("opp", "DEPLOY", depends_on=[b.task_id])

        self.q.resolve_dependencies()
        self.assertEqual(self.q.get(a.task_id).status, "READY")
        self.assertEqual(self.q.get(b.task_id).status, "PENDING")  # waits on a
        self.assertEqual([t.task_id for t in self.q.ready()], [a.task_id])

        self.q.claim(a.task_id, "w1")
        self.q.mark_succeeded(a.task_id, {"html": "<x>"})
        self.q.resolve_dependencies()
        self.assertEqual(self.q.get(b.task_id).status, "READY")
        self.assertEqual(self.q.get(c.task_id).status, "PENDING")  # deploy still waits

    def test_dependency_failure_fails_dependents(self):
        a = self.q.create("opp", "BUILD_PAGE")
        b = self.q.create("opp", "DEPLOY", depends_on=[a.task_id])
        self.q.resolve_dependencies()
        self.q.claim(a.task_id, "w1")
        self.q.mark_failed(a.task_id, "build blew up", retryable=False)
        self.q.resolve_dependencies()
        self.assertEqual(self.q.get(b.task_id).status, "FAILED_FINAL")

    def test_priority_order(self):
        lo = self.q.create("opp", "ANALYZE", priority=1)
        hi = self.q.create("opp", "DEPLOY", priority=9)
        self.q.resolve_dependencies()
        self.assertEqual([t.task_id for t in self.q.ready()],
                         [hi.task_id, lo.task_id])

    def test_retry_with_backoff_then_final(self):
        a = self.q.create("opp", "DEPLOY", max_attempts=2)
        self.q.resolve_dependencies()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        self.q.claim(a.task_id, "w1", now=_iso(base))
        self.q.mark_failed(a.task_id, "transient", now=_iso(base))
        t = self.q.get(a.task_id)
        self.assertEqual(t.status, "FAILED_RETRYABLE")
        self.assertTrue(t.next_retry_at)

        # too early - not requeued
        self.assertEqual(self.q.requeue_due(now=_iso(base + timedelta(seconds=5))), [])
        # backoff elapsed - back to PENDING then READY
        self.assertEqual(self.q.requeue_due(now=_iso(base + timedelta(hours=1))),
                         [a.task_id])
        self.q.resolve_dependencies()

        self.q.claim(a.task_id, "w1", now=_iso(base + timedelta(hours=1)))
        self.q.mark_failed(a.task_id, "again", now=_iso(base + timedelta(hours=1)))
        # attempt_count now == max_attempts -> final
        self.assertEqual(self.q.get(a.task_id).status, "FAILED_FINAL")

    def test_lease_reclaim_on_crash(self):
        a = self.q.create("opp", "BUILD_PAGE")
        self.q.resolve_dependencies()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.q.claim(a.task_id, "w1", lease_seconds=300, now=_iso(base))
        self.assertEqual(self.q.reclaim_stale(now=_iso(base + timedelta(seconds=60))),
                         [])
        self.assertEqual(self.q.reclaim_stale(now=_iso(base + timedelta(seconds=600))),
                         [a.task_id])
        self.assertEqual(self.q.get(a.task_id).status, "PENDING")

    def test_approval_block_and_unblock(self):
        a = self.q.create("opp", "DEPLOY", requires_approval=True,
                          approval_type="money")
        self.q.resolve_dependencies()
        self.assertEqual(self.q.get(a.task_id).status, "BLOCKED_APPROVAL")
        self.q.unblock(a.task_id)
        self.q.resolve_dependencies()
        self.assertEqual(self.q.get(a.task_id).status, "READY")

    def test_illegal_transition_rejected(self):
        a = self.q.create("opp", "PLAN")
        with self.assertRaises(TaskError):
            self.q.mark_succeeded(a.task_id)          # PENDING -> SUCCEEDED illegal
        with self.assertRaises(TaskError):
            self.q.claim(a.task_id, "w1")             # not READY

    def test_cancel(self):
        a = self.q.create("opp", "PLAN")
        self.q.cancel(a.task_id, reason="opportunity abandoned")
        self.assertEqual(self.q.get(a.task_id).status, "CANCELLED")
        with self.assertRaises(TaskError):
            self.q.cancel(a.task_id)

    def test_restart_keeps_running_task(self):
        a = self.q.create("opp", "BUILD_PAGE")
        self.q.resolve_dependencies()
        self.q.claim(a.task_id, "w1")
        self.q.save()
        q2 = load_tasks(self.d)
        t = q2.get(a.task_id)
        self.assertEqual(t.status, "RUNNING")
        self.assertEqual(t.worker, "w1")


if __name__ == "__main__":
    unittest.main()
