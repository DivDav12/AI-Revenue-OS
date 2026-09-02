"""Distribution adapters + DISTRIBUTE task (Phase 9)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os.distribution_adapters import (
    DistributionRequest,
    FakeDistributionAdapter,
    GitHubPagesDistributionAdapter,
    NullDistributionAdapter,
)
from revenue_os.events import load_events
from revenue_os.execution import load_tasks
from revenue_os.opportunity_store import Opportunity, OpportunityStore, load_opportunities
from revenue_os.task_adapters import DistributeTaskAdapter, default_registry
from revenue_os.worker import Worker

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _req(channel="owned_web", oid="opp_x"):
    return DistributionRequest(opportunity_id=oid, channel=channel,
                               content={"html": "<h1>hi</h1>"},
                               live_url="https://x.pages.test/o/index.html")


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------

class AdapterTests(unittest.TestCase):
    def test_null_is_blocked(self):
        r = NullDistributionAdapter().distribute(_req())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)
        self.assertEqual(r.published_url, "")

    def test_fake_success(self):
        r = FakeDistributionAdapter().distribute(_req())
        self.assertTrue(r.success)
        self.assertTrue(r.published_url.startswith("https://"))
        self.assertTrue(r.distribution_id)
        self.assertEqual(r.channel, "owned_web")

    def test_fake_fail_and_blocked(self):
        self.assertFalse(FakeDistributionAdapter(fail=True).distribute(_req()).success)
        self.assertTrue(FakeDistributionAdapter(blocked=True).distribute(_req()).blocked)

    def test_fake_deterministic_and_idempotent(self):
        a = FakeDistributionAdapter()
        r1 = a.distribute(_req())
        r2 = a.distribute(_req())
        self.assertEqual(r1.distribution_id, r2.distribution_id)
        self.assertEqual(r1.published_url, r2.published_url)
        self.assertTrue(r2.details.get("duplicate_suppressed"))
        b = FakeDistributionAdapter()
        self.assertEqual(b.distribute(_req()).distribution_id, r1.distribution_id)

    def test_fake_success_without_url_is_not_usable(self):
        from revenue_os.distribution_adapters import valid_url
        r = FakeDistributionAdapter(drop_url=True).distribute(_req())
        self.assertFalse(valid_url(r.published_url))

    def test_github_pages_adapter_missing_creds_fail_closed(self):
        r = GitHubPagesDistributionAdapter(environ={}).distribute(_req())
        self.assertFalse(r.success)
        self.assertTrue(r.blocked)
        self.assertIn("GITHUB_TOKEN", r.error)
        self.assertEqual(r.published_url, "")

    def test_github_pages_adapter_with_injected_client(self):
        from revenue_os.deploy import GitHubPagesConfig

        class _FakeGH:
            def __init__(self):
                self.puts = []

            def get_file(self, repo_path):
                return None

            def put_file(self, repo_path, content, *, message, sha=None):
                self.puts.append(repo_path)
                return {"commit": {"sha": "d15"}}

        cfg = GitHubPagesConfig(token="t", owner="me", repo="site", branch="main")
        gh = _FakeGH()
        r = GitHubPagesDistributionAdapter(config=cfg, client=gh).distribute(_req())
        self.assertTrue(r.success)
        self.assertEqual(r.published_url,
                         "https://me.github.io/site/opp-x/announce.html")
        self.assertIn("opp-x/announce.html", gh.puts)

    def test_no_dangerous_functions_exist(self):
        import revenue_os.distribution_adapters as mod
        import revenue_os.task_adapters as tmod
        for name in ("auto_post_reddit", "mass_post", "bypass_rate_limit",
                     "create_account", "send_mass_dm", "auto_post"):
            self.assertFalse(hasattr(mod, name))
            self.assertFalse(hasattr(tmod, name))


# ---------------------------------------------------------------------------
# DISTRIBUTE task through the real worker
# ---------------------------------------------------------------------------

class DistributeTaskTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        s = OpportunityStore(self.d / "opportunities.json")
        self.oid = s.upsert(Opportunity(title="Cold-email pack",
                                        category="saas"))["id"]
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING", "LIVE"):
            s.transition(self.oid, st, reason="setup", source="test")
        s.record_deployment(self.oid, {"live_url": "https://x.pages.test/o/index.html",
                                       "provider": "fake"})
        s.save()

    def tearDown(self):
        self._d.cleanup()

    def _reg(self, dist):
        reg = default_registry()
        reg.register(DistributeTaskAdapter(dist))
        return reg

    def _seed(self, channel="owned_web"):
        q = load_tasks(self.d)
        q.create(self.oid, "DISTRIBUTE", priority=6,
                 idempotency_key=f"accept:{self.oid}:DISTRIBUTE",
                 input={"channel": channel})
        q.resolve_dependencies()
        q.save()

    def _state(self):
        return load_opportunities(self.d).get(self.oid)["state"]

    def _dists(self, channel=None):
        d = load_opportunities(self.d).get(self.oid)["execution"].get(
            "distributions", [])
        return [x for x in d if channel is None or x["channel"] == channel]

    def test_successful_owned_distribution_moves_to_acquiring_traffic(self):
        dist = FakeDistributionAdapter()
        self._seed()
        Worker(self.d, registry=self._reg(dist), name="w").run(max_ticks=30)

        self.assertEqual(self._state(), "ACQUIRING_TRAFFIC")
        ow = self._dists("owned_web")
        self.assertEqual(len(ow), 1)
        self.assertTrue(ow[0]["resulting_url"].startswith("https://"))
        self.assertEqual(ow[0]["status"], "success")

        # exactly ONE ACQUIRING_TRAFFIC transition, from LIVE (only the
        # primary owned_web publish drives it - not owned_content, not drafts)
        trans = [e for e in load_events(self.d).all()
                 if e["type"] == "OPPORTUNITY_TRANSITIONED"
                 and e["data"].get("to") == "ACQUIRING_TRAFFIC"]
        self.assertEqual(len(trans), 1)
        self.assertEqual(trans[0]["data"]["from"], "LIVE")
        self.assertEqual(trans[0]["task_id"],
                         next(t.task_id for t in load_tasks(self.d).all()
                              if t.task_type == "DISTRIBUTE"
                              and t.input.get("channel") == "owned_web"))

    def test_failed_distribution_does_not_move_state(self):
        self._seed()
        Worker(self.d, registry=self._reg(FakeDistributionAdapter(fail=True)),
               name="w").run(max_ticks=10)
        self.assertEqual(self._state(), "LIVE")
        self.assertEqual(self._dists(), [])
        self.assertIn(
            load_tasks(self.d).by_opportunity(self.oid)[0].status,
            ("FAILED_RETRYABLE", "FAILED_FINAL"))

    def test_blocked_distribution_is_a_noop_not_an_error(self):
        # no owned channel configured -> DISTRIBUTE completes as a no-op so
        # the dependent CHECK_* tasks still run; nothing is published, no
        # state change.
        self._seed()
        Worker(self.d, registry=self._reg(NullDistributionAdapter()),
               name="w").run(max_ticks=10)
        self.assertEqual(self._state(), "LIVE")
        self.assertEqual(self._dists(), [])
        dt = next(t for t in load_tasks(self.d).all() if t.task_type == "DISTRIBUTE")
        self.assertEqual(dt.status, "SUCCEEDED")
        self.assertFalse(dt.output["success"])
        self.assertTrue(dt.output["blocked"])
        self.assertFalse(dt.output.get("distributed", True))
        self.assertNotIn("DISTRIBUTION_COMPLETED",
                         [e["type"] for e in load_events(self.d).all()])

    def test_draft_channel_publishes_nothing_and_does_not_move_state(self):
        self._seed(channel="community_draft")
        Worker(self.d, registry=self._reg(FakeDistributionAdapter()),
               name="w").run(max_ticks=10)
        self.assertEqual(self._state(), "LIVE")           # draft != distribution
        d = self._dists()
        self.assertEqual(len(d), 1)
        self.assertTrue(d[0]["draft_only"])
        self.assertEqual(d[0]["resulting_url"], "")
        self.assertFalse(d[0]["draft"].get("auto_post"))
        self.assertEqual(d[0]["draft"]["platform"], "reddit")

    def test_success_without_url_is_not_acquiring_traffic(self):
        self._seed()
        Worker(self.d, registry=self._reg(FakeDistributionAdapter(drop_url=True)),
               name="w").run(max_ticks=10)
        self.assertEqual(self._state(), "LIVE")
        dt = next(t for t in load_tasks(self.d).all()
                  if t.task_type == "DISTRIBUTE"
                  and t.input.get("channel") == "owned_web")
        self.assertEqual(dt.status, "FAILED_FINAL")
        self.assertIn("no", dt.error.lower())
        self.assertEqual(self._dists("owned_web"), [])

    def test_fan_out_creates_bounded_channel_tasks(self):
        dist = FakeDistributionAdapter()
        self._seed()
        Worker(self.d, registry=self._reg(dist), name="w").run(max_ticks=30)
        chans = sorted(t.input.get("channel") for t in load_tasks(self.d).all()
                       if t.task_type == "DISTRIBUTE")
        self.assertEqual(chans, ["community_draft", "owned_content",
                                 "owned_web", "social_draft"])
        # run again many times: no new DISTRIBUTE tasks
        for _ in range(5):
            Worker(self.d, registry=self._reg(dist), name="w").run(max_ticks=30)
        self.assertEqual(
            len([t for t in load_tasks(self.d).all() if t.task_type == "DISTRIBUTE"]),
            4)

    def test_idempotent_duplicate_task(self):
        dist = FakeDistributionAdapter()
        self._seed()
        Worker(self.d, registry=self._reg(dist), name="w").run(max_ticks=30)
        self.assertEqual(len(self._dists("owned_web")), 1)
        ow_calls = dist.calls.count((self.oid, "owned_web"))

        # a second DISTRIBUTE task for the owned_web channel + same content
        q = load_tasks(self.d)
        q.create(self.oid, "DISTRIBUTE", priority=6, input={"channel": "owned_web"})
        q.resolve_dependencies()
        q.save()
        Worker(self.d, registry=self._reg(dist), name="w").run(max_ticks=30)
        self.assertEqual(len(self._dists("owned_web")), 1)          # not 2
        self.assertEqual(dist.calls.count((self.oid, "owned_web")), ow_calls)  # no re-publish
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DISTRIBUTION_COMPLETED"
                 and e["data"].get("channel") == "owned_web"]), 1)

    def test_retry_then_success_publishes_once(self):
        class _Flaky(FakeDistributionAdapter):
            def __init__(self):
                super().__init__()
                self.n = 0

            def distribute(self, req):
                if req.channel == "owned_web":
                    self.n += 1
                    if self.n == 1:
                        from revenue_os.distribution_adapters import DistributionResult
                        return DistributionResult(success=False,
                                                  channel=req.channel,
                                                  error="transient")
                return super().distribute(req)

        flaky = _Flaky()
        self._seed()
        base = BASE
        Worker(self.d, registry=self._reg(flaky), name="w").run(
            now=_iso(base), max_ticks=10)
        dt = next(t for t in load_tasks(self.d).by_opportunity(self.oid)
                  if t.task_type == "DISTRIBUTE" and t.input.get("channel") == "owned_web")
        self.assertEqual(dt.status, "FAILED_RETRYABLE")
        self.assertEqual(self._dists("owned_web"), [])
        Worker(self.d, registry=self._reg(flaky), name="w").run(
            now=_iso(base + timedelta(hours=2)), max_ticks=30)
        self.assertEqual(load_tasks(self.d).get(dt.task_id).status, "SUCCEEDED")
        self.assertEqual(len(self._dists("owned_web")), 1)
        self.assertEqual(self._state(), "ACQUIRING_TRAFFIC")
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DISTRIBUTION_COMPLETED"
                 and e["data"].get("channel") == "owned_web"]), 1)

    def test_restart_does_not_double_publish(self):
        self._seed()
        Worker(self.d, registry=self._reg(FakeDistributionAdapter()),
               name="w").run(max_ticks=30)
        self.assertEqual(len(self._dists("owned_web")), 1)
        # a fresh worker replays a DISTRIBUTE for the same owned_web channel
        q = load_tasks(self.d)
        q.create(self.oid, "DISTRIBUTE", priority=6, input={"channel": "owned_web"})
        q.resolve_dependencies()
        q.save()
        Worker(self.d, registry=self._reg(FakeDistributionAdapter()),
               name="w2").run(max_ticks=30)
        self.assertEqual(len(self._dists("owned_web")), 1)          # not 2
        self.assertEqual(
            len([e for e in load_events(self.d).all()
                 if e["type"] == "DISTRIBUTION_COMPLETED"
                 and e["data"].get("channel") == "owned_web"]), 1)

    def test_no_regression_from_an_advanced_state(self):
        s = load_opportunities(self.d)
        for st in ("ACQUIRING_TRAFFIC", "MEASURING", "FIRST_VISITOR"):
            s.transition(self.oid, st, reason="setup", source="test")
        s.save()
        self._seed()
        Worker(self.d, registry=self._reg(FakeDistributionAdapter()),
               name="w").run(max_ticks=10)
        self.assertEqual(self._state(), "FIRST_VISITOR")   # not pulled back

    def test_no_money_spend_smtp_or_autopost(self):
        self._seed()
        Worker(self.d, registry=self._reg(FakeDistributionAdapter()),
               name="w").run(max_ticks=30)
        for artefact in ("revenue.json", "spend.json", "llm_spend.json",
                         "deliveries.json", "messages.json"):
            self.assertFalse((self.d / artefact).exists(), artefact)
        for t in load_tasks(self.d).all():
            if t.task_type in ("SPAWN_VARIANT", "SCALE", "OPTIMIZE"):
                self.assertNotEqual(t.status, "SUCCEEDED")

    def test_event_sequence_stays_monotonic(self):
        self._seed()
        Worker(self.d, registry=self._reg(FakeDistributionAdapter()),
               name="w").run(max_ticks=30)
        seqs = [e["seq"] for e in load_events(self.d).all()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
