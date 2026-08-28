import tempfile
import unittest
from pathlib import Path

from revenue_os.analytics import roi_summary
from revenue_os.approval import record_decision
from revenue_os.revenue import RevenueLedger, mark_launched, record_payment
from revenue_os.sources import RawSignal, StaticSource
from revenue_os.spend import (
    SpendLedger,
    SpendRequest,
    authorize_spend,
    deny_spend,
    record_spend,
    set_budget,
)
from revenue_os.store import Candidate, CandidateStore
from revenue_os.validation import record_validation_outcome
from revenue_os.workflow import (
    investigate_approved,
    prepare_launch,
    run_discovery_cycle,
)


def _req(name="c", amount=10.0, purpose="ads") -> SpendRequest:
    return SpendRequest(
        candidate_name=name, purpose=purpose, amount=amount, requested_by="system"
    )


class SpendGateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.ledger = SpendLedger(Path(self._dir.name) / "spend.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_default_budget_zero_blocks_authorization(self):
        self.assertEqual(self.ledger.budget_for("c"), 0.0)
        with self.assertRaises(ValueError):
            authorize_spend(self.ledger, _req(amount=5.0), approver="o", ceiling=100.0)

    def test_set_budget_then_authorize_within_cap(self):
        set_budget(self.ledger, "c", 20.0, approver="o")
        authorize_spend(self.ledger, _req(amount=15.0), approver="o", ceiling=100.0)
        self.assertEqual(self.ledger.authorized_for("c"), 15.0)
        # second authorization that exceeds remaining budget fails
        with self.assertRaises(ValueError):
            authorize_spend(self.ledger, _req(amount=10.0), approver="o", ceiling=100.0)

    def test_ceiling_blocks_even_when_budget_allows(self):
        set_budget(self.ledger, "c", 100.0, approver="o")
        with self.assertRaises(ValueError):
            authorize_spend(self.ledger, _req(amount=5.0), approver="o")  # ceiling 0.0

    def test_negative_budget_and_nonpositive_amount_raise(self):
        with self.assertRaises(ValueError):
            set_budget(self.ledger, "c", -1.0, approver="o")
        set_budget(self.ledger, "c", 10.0, approver="o")
        with self.assertRaises(ValueError):
            authorize_spend(self.ledger, _req(amount=0.0), approver="o", ceiling=10.0)

    def test_deny_spend_records_and_grants_nothing(self):
        set_budget(self.ledger, "c", 50.0, approver="o")
        deny_spend(self.ledger, _req(amount=20.0), approver="o", reason="not now")
        self.assertEqual(self.ledger.authorized_for("c"), 0.0)
        self.assertTrue(any(e["type"] == "denied" for e in self.ledger.entries()))

    def test_record_spend_within_and_over_authorized(self):
        set_budget(self.ledger, "c", 50.0, approver="o")
        authorize_spend(self.ledger, _req(amount=30.0), approver="o", ceiling=100.0)
        record_spend(self.ledger, "c", 10.0, actor="o")
        record_spend(self.ledger, "c", 15.0, actor="o")
        self.assertEqual(self.ledger.spent_for("c"), 25.0)
        with self.assertRaises(ValueError):
            record_spend(self.ledger, "c", 10.0, actor="o")  # would exceed 30 authorized


class SpendLedgerPersistenceTests(unittest.TestCase):
    def test_roundtrip_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "spend.json"
            self.assertEqual(SpendLedger.load(path).entries(), [])

            ledger = SpendLedger(path)
            set_budget(ledger, "c", 20.0, approver="o")
            authorize_spend(ledger, _req(amount=5.0), approver="o", ceiling=10.0)
            record_spend(ledger, "c", 5.0, actor="o")

            reloaded = SpendLedger.load(path)
            self.assertEqual(reloaded.budget_for("c"), 20.0)
            self.assertEqual(reloaded.authorized_for("c"), 5.0)
            self.assertEqual(reloaded.spent_for("c"), 5.0)
            self.assertEqual(reloaded.total_spent(), 5.0)

            path.write_text("{bad", encoding="utf-8")
            with self.assertRaises(ValueError):
                SpendLedger.load(path)


class RoiSummaryTests(unittest.TestCase):
    def test_net_and_ratio(self):
        with tempfile.TemporaryDirectory() as d:
            store = CandidateStore(Path(d) / "c.json")
            store.put(Candidate(name="c", status="launched"))
            rev = RevenueLedger(Path(d) / "revenue.json")
            spend = SpendLedger(Path(d) / "spend.json")

            record_payment(store, rev, "c", 100.0, actor="o")
            set_budget(spend, "c", 50.0, approver="o")
            authorize_spend(spend, _req("c", 40.0), approver="o", ceiling=100.0)
            record_spend(spend, "c", 40.0, actor="o")

            summary = roi_summary(store, rev, spend)
            row = summary["candidates"]["c"]
            self.assertEqual(row["revenue"], 100.0)
            self.assertEqual(row["spent"], 40.0)
            self.assertEqual(row["net"], 60.0)
            self.assertEqual(row["roi_ratio"], 1.5)
            self.assertEqual(summary["grand_net"], 60.0)


class EndToEndTests(unittest.TestCase):
    def test_full_pipeline_with_spend(self):
        with tempfile.TemporaryDirectory() as d:
            store = CandidateStore.load(Path(d) / "candidates.json")
            rev = RevenueLedger.load(Path(d) / "revenue.json")
            spend = SpendLedger.load(Path(d) / "spend.json")
            signals = [
                RawSignal(title="automation automate no-code api saas platform revenue"),
                RawSignal(title="plain note"),
            ]
            run_discovery_cycle(StaticSource(signals), store, shortlist_n=1)
            top = store.all()[0].name
            record_decision(store, top, "approve", approver="o")
            investigate_approved(store)
            record_validation_outcome(
                store, top, "validated", metric_value="26 signups", actor="o"
            )
            prepare_launch(store)
            mark_launched(store, top, actor="o")
            record_payment(store, rev, top, 29.0, actor="o")

            set_budget(spend, top, 15.0, approver="o")
            authorize_spend(
                spend, _req(top, 12.0, "domain"), approver="o", ceiling=20.0
            )
            record_spend(spend, top, 12.0, actor="o")

            summary = roi_summary(store, rev, spend)
            self.assertEqual(summary["grand_revenue"], 29.0)
            self.assertEqual(summary["grand_spent"], 12.0)
            self.assertEqual(summary["grand_net"], 17.0)
            self.assertEqual(summary["candidates"][top]["status"], "earning")


if __name__ == "__main__":
    unittest.main()
