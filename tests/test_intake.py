import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.cli import main
from revenue_os.intake import IntakeStore, import_submissions
from revenue_os.revenue import RevenueLedger, record_payment
from revenue_os.store import Candidate, CandidateStore

_CAND = "ask-hn-how-do-you-find-your-first-paying-customers"
_FIELDS = {
    "name": "Dana", "email": "dana@example.com",
    "business": "example.com", "sells": "a scheduling app",
    "current_price": "9 EUR/mo", "target_audience": "clinics",
    "customer_situation": "3 beta users", "previous_attempts": "cold email, twitter",
    "biggest_problem": "no repeatable channel",
}


def _row(capture="CAP1", order="ORD1", candidate=_CAND, **over):
    return {"capture_id": capture, "order_id": order, "candidate": candidate,
            **_FIELDS, **over}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.p = Path(self._dir.name) / "intake.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_round_trip_and_required_fields(self):
        s = IntakeStore.load(self.p)
        s.add("ORD1", _CAND, _FIELDS, capture_id="CAP1")
        s.save()
        got = IntakeStore.load(self.p).get("ORD1")
        self.assertEqual(got["candidate"], _CAND)
        self.assertEqual(got["fields"]["email"], "dana@example.com")
        self.assertEqual(got["status"], "new")
        with self.assertRaisesRegex(ValueError, "missing required"):
            s.add("ORD2", _CAND, {"name": "x"})

    def test_mark_reviewed(self):
        s = IntakeStore.load(self.p)
        s.add("ORD1", _CAND, _FIELDS, capture_id="CAP1")
        s.mark_reviewed("ORD1", actor="human-owner")
        self.assertEqual(s.get("ORD1")["status"], "reviewed")
        with self.assertRaises(ValueError):
            s.mark_reviewed("nope", actor="x")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.store = CandidateStore(self.d / "candidates.json")
        self.store.put(Candidate(name=_CAND, status="launched"))
        self.store.save()
        self.ledger = RevenueLedger(self.d / "revenue.json")
        record_payment(self.store, self.ledger, _CAND, 29.9, actor="paypal",
                       currency="EUR", ref="paypal:CAP1")
        self.intake = IntakeStore(self.d / "intake.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_stores_row_matching_a_booked_payment(self):
        r = import_submissions(self.intake, self.ledger, [_row()])
        self.assertEqual(len(r["stored"]), 1)
        self.assertEqual(r["skipped"], [])
        e = IntakeStore.load(self.d / "intake.json").get("ORD1")
        self.assertEqual(e["capture_id"], "CAP1")
        self.assertEqual(e["candidate"], _CAND)

    def test_skips_unpaid_capture(self):
        r = import_submissions(self.intake, self.ledger, [_row(capture="GHOST")])
        self.assertEqual(r["stored"], [])
        self.assertIn("not a booked payment", r["skipped"][0]["reason"])
        self.assertFalse((self.d / "intake.json").exists())

    def test_skips_candidate_mismatch(self):
        self.store.put(Candidate(name="other", status="launched"))
        self.store.save()
        r = import_submissions(self.intake, self.ledger, [_row(candidate="other")])
        self.assertIn("belongs to", r["skipped"][0]["reason"])

    def test_nested_fields_and_missing_required(self):
        good = {"capture_id": "CAP1", "order_id": "ORD9", "fields": _FIELDS}
        bad = {"capture_id": "CAP1", "order_id": "ORD8",
               "fields": {"name": "x", "email": "", "sells": ""}}
        r = import_submissions(self.intake, self.ledger, [good, bad])
        self.assertEqual(len(r["stored"]), 1)
        self.assertIn("missing required", r["skipped"][0]["reason"])

    def test_reimport_is_idempotent_by_order_id(self):
        import_submissions(self.intake, self.ledger, [_row()])
        again = IntakeStore.load(self.d / "intake.json")
        import_submissions(again, self.ledger, [_row(name="Dana 2")])
        self.assertEqual(len(again.all()), 1)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        store = CandidateStore(self.d / "candidates.json")
        store.put(Candidate(name=_CAND, status="launched"))
        store.save()
        ledger = RevenueLedger(self.d / "revenue.json")
        record_payment(store, ledger, _CAND, 29.9, actor="paypal",
                       currency="EUR", ref="paypal:CAP1")

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, *args):
        return main([*args, "--data-dir", str(self.d)])

    def test_import_list_show_review(self):
        exp = self.d / "export.json"
        exp.write_text(json.dumps([_row()]), encoding="utf-8")
        self.assertEqual(self._run("intake-import", str(exp)), 0)
        self.assertEqual(self._run("intake-list"), 0)
        self.assertEqual(self._run("intake-show", "ORD1"), 0)
        self.assertEqual(self._run("intake-review", "ORD1"), 0)
        e = IntakeStore.load(self.d / "intake.json").get("ORD1")
        self.assertEqual(e["status"], "reviewed")

    def test_import_rejects_unpaid(self):
        exp = self.d / "e2.json"
        exp.write_text(json.dumps([_row(capture="NOPE")]), encoding="utf-8")
        self.assertEqual(self._run("intake-import", str(exp)), 0)   # runs, stores 0
        self.assertFalse((self.d / "intake.json").exists())


if __name__ == "__main__":
    unittest.main()
