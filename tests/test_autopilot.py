import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import autopilot as ap
from revenue_os.acquisition import AcquisitionStore
from revenue_os.acquisition_sources import AcqRecord
from revenue_os.cli import main
from revenue_os.outreach import OutreachStore
from revenue_os.revenue import RevenueLedger


def _good_lead_record():
    return AcqRecord(
        title="Ask HN: how do I get my first customers for my SaaS?",
        url="https://news.ycombinator.com/item?id=1",
        text="I launched my SaaS two weeks ago and have 0 paying customers.",
        author="founder", posted_at="2026-08-27T00:00:00+00:00",
        platform="Hacker News", source="hn-algolia", query="q")


class _FakeSource:
    name = "hn-algolia"

    def __init__(self, records):
        self._records = records

    def search(self, query, limit, *, since_ts=None):
        return list(self._records)


class _OfflineSource(unittest.TestCase):
    """Base: patch discovery to an offline fake so no test hits the network."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        import revenue_os.acquisition_sources as S
        self._orig = S.build_acquisition_source
        S.build_acquisition_source = (
            lambda names, path=None, web_source=None:
            _FakeSource([_good_lead_record()]))

    def tearDown(self):
        import revenue_os.acquisition_sources as S
        S.build_acquisition_source = self._orig
        self._dir.cleanup()


class LifecycleTests(_OfflineSource):
    def _run(self, *args):
        return main(["autopilot", *args, "--data-dir", str(self.d)])

    def test_start_status_pause_resume_stop_restart(self):
        self.assertEqual(self._run("start", "--delay", "0"), 0)
        self.assertTrue((self.d / "autopilot.json").exists())
        self.assertEqual(self._run("status"), 0)
        self.assertEqual(self._run("pause", "--reason", "check"), 0)
        st = json.loads((self.d / "autopilot.json").read_text())
        self.assertEqual(st["status"], "paused")
        self.assertEqual(st["pause_reason"], "check")
        # a paused autopilot does not run a cycle
        cycles = st["cycles"]
        self._run("start", "--delay", "0")
        st2 = json.loads((self.d / "autopilot.json").read_text())
        self.assertEqual(st2["cycles"], cycles)
        self.assertEqual(self._run("resume"), 0)
        self.assertEqual(self._run("stop"), 0)
        # stop preserves data; start resumes
        self.assertEqual(self._run("start", "--delay", "0"), 0)

    def test_corrupted_state_starts_fresh(self):
        (self.d / "autopilot.json").write_text("{not json", encoding="utf-8")
        st = ap.AutopilotState.load(self.d / "autopilot.json")
        self.assertEqual(st.data["status"], "stopped")

    def test_empty_state_status(self):
        s = ap.status(self.d)
        self.assertEqual(s["autopilot"], "stopped")
        self.assertEqual(s["revenue_eur"], 0.0)
        self.assertEqual(s["capital"]["presale_cap_eur"], 3.0)


class CycleTests(_OfflineSource):
    def _cycle(self, **kw):
        return ap.run_cycle(self.d, max_age_days=30, politeness_delay=0, **kw)

    def test_cycle_discovers_and_prepares_a_brief_then_stops_for_human(self):
        r = self._cycle()
        self.assertEqual(r["spend"]["external_spent_usd"], 0.0)
        self.assertGreaterEqual(r["discovery"]["scored"], 1)
        self.assertEqual(r["outreach"]["prepared"], 1)
        # a human action was queued (post the reply)
        self.assertTrue(any("HUMAN" in a for a in r["actions"]))
        # brief persisted
        b = OutreachStore.load(self.d / "outreach.json").all()
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["status"], "draft")

    def test_cycle_is_idempotent_no_duplicate_briefs(self):
        self._cycle()
        self._cycle()
        self.assertEqual(len(OutreachStore.load(self.d / "outreach.json").all()), 1)
        st = json.loads((self.d / "autopilot.json").read_text())
        self.assertEqual(st["cycles"], 2)

    def test_paypal_missing_credentials_is_a_note_not_a_crash(self):
        r = self._cycle()
        self.assertFalse(r["payment"]["ok"])
        self.assertIn("PAYPAL", r["payment"]["note"])
        self.assertTrue(any("PAYPAL" in n for n in r["notes"]))

    def test_web_source_blocked_by_presale_cap(self):
        # pre-seed near the cap so --allow-web is refused before any client
        from revenue_os.llm_spend import LlmSpendLog
        log = LlmSpendLog(self.d / "llm_spend.json")
        log.add({"activity": "acquisition", "cost_usd": 3.2, "api_calls": 1})
        log.save()
        r = self._cycle(allow_web=True)
        self.assertEqual(r["discovery"].get("skipped"), "budget")
        self.assertTrue(any("BLOCK" in n for n in r["notes"]))
        self.assertEqual(r["spend"]["external_spent_usd"], 3.2)

    def test_paypal_check_books_a_live_capture_when_credentials_present(self):
        import os

        from revenue_os import paypal as pp
        from revenue_os.store import Candidate, CandidateStore

        store = CandidateStore(self.d / "candidates.json")
        store.put(Candidate(name="cand", description="d", status="launched",
                            offer={"price": 29.9, "currency": "EUR"}))
        store.save()

        def fake_sync(store, ledger, **kw):
            from revenue_os.revenue import record_payment
            record_payment(store, ledger, "cand", 29.9, actor="paypal",
                           currency="EUR", ref="paypal:LIVECAP1", note="test")
            return {"booked": [{"candidate": "cand", "amount": 29.9,
                                "capture_id": "LIVECAP1"}],
                    "skipped": [], "total_booked": 29.9}

        env = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": "sec",
               "PAYPAL_ENV": "live"}
        old_env = {k: os.environ.get(k) for k in env}
        old_sync = pp.sync_transactions
        old_cfg = pp.PayPalConfig.from_env
        os.environ.update(env)
        pp.sync_transactions = fake_sync
        pp.PayPalConfig.from_env = staticmethod(lambda environ=None: None)
        try:
            r = self._cycle()
        finally:
            pp.sync_transactions = old_sync
            pp.PayPalConfig.from_env = old_cfg
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.assertTrue(r["payment"]["ok"])
        self.assertEqual(len(r["payment"]["booked"]), 1)
        self.assertTrue(r["sale"])
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 29.9)

    def test_sale_flips_presale_off(self):
        RevenueLedger(self.d / "revenue.json")   # ensure dir
        led = RevenueLedger.load(self.d / "revenue.json")
        led.add({"candidate_name": "x", "amount": 29.9, "currency": "EUR",
                 "received_at": "2026-01-01T00:00:00+00:00", "actor": "t",
                 "ref": "paypal:seed"})
        led.save()
        r = self._cycle()
        self.assertFalse(r["spend"]["presale_active"])
        self.assertEqual(r["status"]["capital"]["growth_capital_available_eur"], 17.0)


if __name__ == "__main__":
    unittest.main()
