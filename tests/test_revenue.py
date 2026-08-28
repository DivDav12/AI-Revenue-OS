import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from revenue_os.approval import record_decision
from revenue_os.offer import propose_offer
from revenue_os.revenue import (
    RevenueLedger,
    mark_launched,
    record_payment,
    revenue_summary,
)
from revenue_os.sources import RawSignal, StaticSource
from revenue_os.store import Candidate, CandidateStore
from revenue_os.validation import record_validation_outcome
from revenue_os.workflow import (
    investigate_approved,
    prepare_launch,
    run_discovery_cycle,
)


def _validated(name="c", *, description="thing", test="Run 5 problem-interview conversations.") -> Candidate:
    return Candidate(
        name=name,
        description=description,
        status="validated",
        plan={"cheapest_test": test},
    )


class ProposeOfferTests(unittest.TestCase):
    def test_deterministic_and_estimate_flag(self):
        c = _validated()
        o1, o2 = propose_offer(c), propose_offer(c)
        self.assertEqual(o1.to_dict()["price"], o2.to_dict()["price"])
        self.assertTrue(o1.price_is_estimate)

    def test_landing_plan_is_digital(self):
        o = propose_offer(_validated(test="Publish a one-page landing site with a waitlist."))
        self.assertEqual(o.delivery, "digital")
        self.assertEqual(o.price, 29.0)

    def test_other_plan_is_manual(self):
        o = propose_offer(_validated(test="Direct outreach to 10 named prospects."))
        self.assertEqual(o.delivery, "manual")
        self.assertEqual(o.price, 250.0)


class PrepareLaunchTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_attaches_offer_to_validated_only_no_status_change(self):
        self.store.put(_validated("v"))
        self.store.put(Candidate(name="i", status="investigating"))

        out = prepare_launch(self.store)

        self.assertEqual([c.name for c in out], ["v"])
        self.assertTrue(self.store.get("v").offer)
        self.assertEqual(self.store.get("v").status, "validated")
        self.assertFalse(self.store.get("i").offer)

    def test_idempotent_does_not_regenerate(self):
        self.store.put(_validated("v"))
        prepare_launch(self.store)
        first_offer = self.store.get("v").offer
        prepare_launch(self.store)
        self.assertEqual(self.store.get("v").offer, first_offer)


class MarkLaunchedTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "c.json")
        self.store.put(_validated("v"))

    def tearDown(self):
        self._dir.cleanup()

    def test_validated_to_launched(self):
        out = mark_launched(self.store, "v", actor="owner")
        self.assertEqual(out.status, "launched")

    def test_wrong_state_raises(self):
        self.store.put(Candidate(name="i", status="investigating"))
        with self.assertRaises(ValueError):
            mark_launched(self.store, "i", actor="owner")

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            mark_launched(self.store, "missing", actor="owner")


class RevenueLedgerAndPaymentTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cpath = Path(self._dir.name) / "c.json"
        self.lpath = Path(self._dir.name) / "revenue.json"
        self.store = CandidateStore(self.cpath)
        self.store.put(_validated("v"))
        mark_launched(self.store, "v", actor="owner")
        self.ledger = RevenueLedger(self.lpath)

    def tearDown(self):
        self._dir.cleanup()

    def test_first_payment_moves_launched_to_earning(self):
        out = record_payment(self.store, self.ledger, "v", 29.0, actor="owner")
        self.assertEqual(out.status, "earning")
        self.assertEqual(self.ledger.total_for("v"), 29.0)

    def test_further_payments_accumulate_and_stay_earning(self):
        record_payment(self.store, self.ledger, "v", 29.0, actor="owner")
        out = record_payment(self.store, self.ledger, "v", 10.0, actor="owner")
        self.assertEqual(out.status, "earning")
        self.assertEqual(self.ledger.total_for("v"), 39.0)

    def test_ledger_roundtrip_and_totals(self):
        record_payment(self.store, self.ledger, "v", 29.0, actor="owner")
        reloaded = RevenueLedger.load(self.lpath)
        self.assertEqual(reloaded.total(), 29.0)
        self.assertEqual(reloaded.total_for("v"), 29.0)
        self.assertIsNotNone(reloaded.first_payment_at("v"))

    def test_missing_ledger_loads_empty_corrupt_raises(self):
        self.assertEqual(RevenueLedger.load(self.lpath).entries(), [])
        self.lpath.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            RevenueLedger.load(self.lpath)

    def test_payment_rules(self):
        self.store.put(_validated("v2"))  # not launched
        with self.assertRaises(ValueError):
            record_payment(self.store, self.ledger, "v2", 10.0, actor="owner")
        with self.assertRaises(ValueError):
            record_payment(self.store, self.ledger, "missing", 10.0, actor="owner")
        with self.assertRaises(ValueError):
            record_payment(self.store, self.ledger, "v", 0.0, actor="owner")

    def test_revenue_summary(self):
        record_payment(self.store, self.ledger, "v", 29.0, actor="owner")
        summary = revenue_summary(self.store, self.ledger)
        self.assertEqual(summary["grand_total"], 29.0)
        self.assertTrue(summary["candidates"]["v"]["first_revenue"])
        self.assertEqual(summary["candidates"]["v"]["status"], "earning")


class EndToEndTests(unittest.TestCase):
    def test_discover_to_earning(self):
        with tempfile.TemporaryDirectory() as d:
            store = CandidateStore.load(Path(d) / "candidates.json")
            ledger = RevenueLedger.load(Path(d) / "revenue.json")
            signals = [
                RawSignal(title="automation automate no-code api saas platform revenue"),
                RawSignal(title="plain note"),
            ]
            run_discovery_cycle(StaticSource(signals), store, shortlist_n=1)
            top = store.all()[0].name
            record_decision(store, top, "approve", approver="owner")
            investigate_approved(store)
            record_validation_outcome(
                store, top, "validated", metric_value="26 signups", actor="owner"
            )
            prepare_launch(store)
            mark_launched(store, top, actor="owner")
            out = record_payment(store, ledger, top, 29.0, actor="owner")

            self.assertEqual(out.status, "earning")
            reloaded = CandidateStore.load(store.path)
            self.assertTrue(reloaded.get(top).offer)
            self.assertEqual(RevenueLedger.load(ledger.path).total(), 29.0)


class RegressionTests(unittest.TestCase):
    def test_candidate_roundtrips_with_offer_field(self):
        c = replace(_validated("x"), offer={"price": 29.0})
        restored = Candidate.from_dict(json.loads(json.dumps(c.to_dict())))
        self.assertEqual(restored.offer, {"price": 29.0})


if __name__ == "__main__":
    unittest.main()
