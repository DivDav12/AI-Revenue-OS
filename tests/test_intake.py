import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.cli import main
from revenue_os.intake import IntakeStore, import_submissions, read_submissions
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


# Formspree-style CSV: a provider id column + our fields + the hidden fields.
_CSV_COLUMNS = ["_id", "date", "candidate", "order_id", "capture_id", *_FIELDS]


def _csv_text(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in _CSV_COLUMNS})
    return buf.getvalue()


def _csv_row(capture="CAP1", order="ORD1", candidate=_CAND, **over):
    return {"_id": "fs_abc123", "date": "2026-08-29T12:00:00Z",
            "candidate": candidate, "order_id": order, "capture_id": capture,
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


class ReadSubmissionsTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_csv_parses_to_field_dicts_and_drops_blank_rows(self):
        p = self.d / "x.csv"
        p.write_text(_csv_text([_csv_row(), _csv_row(order="ORD2", capture="CAP2")])
                     + "\n,,,,,,,,,,,,,\n", encoding="utf-8")
        rows = read_submissions(p)
        self.assertEqual(len(rows), 2)                       # blank line dropped
        self.assertEqual(rows[0]["capture_id"], "CAP1")
        self.assertEqual(rows[0]["email"], _FIELDS["email"])

    def test_csv_keeps_multiline_quoted_values(self):
        # a textarea answer with a newline must stay one row, one value
        p = self.d / "m.csv"
        p.write_bytes(
            _csv_text([_csv_row(previous_attempts="tried A\nthen B"),
                       _csv_row(order="ORD2", capture="CAP2")]).encode("utf-8"))
        rows = read_submissions(p)
        self.assertEqual(len(rows), 2)
        self.assertIn("tried A", rows[0]["previous_attempts"])
        self.assertIn("then B", rows[0]["previous_attempts"])
        self.assertEqual(rows[1]["order_id"], "ORD2")

    def test_csv_tolerates_utf8_bom(self):
        p = self.d / "b.csv"
        p.write_bytes(b"\xef\xbb\xbf" + _csv_text([_csv_row()]).encode("utf-8"))
        self.assertEqual(read_submissions(p)[0]["capture_id"], "CAP1")

    def test_json_paths_still_work(self):
        p = self.d / "a.json"
        p.write_text(json.dumps({"submissions": [_row()]}), encoding="utf-8")
        self.assertEqual(read_submissions(p)[0]["order_id"], "ORD1")


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.store = CandidateStore(self.d / "candidates.json")
        self.store.put(Candidate(name=_CAND, status="launched"))
        self.store.save()
        self.ledger = RevenueLedger(self.d / "revenue.json")
        record_payment(self.store, self.ledger, _CAND, 29.9, actor="paypal",
                       currency="EUR", ref="paypal:CAP1")

    def tearDown(self):
        self._dir.cleanup()

    def _import(self, rows, name="e.csv"):
        p = self.d / name
        p.write_text(_csv_text(rows), encoding="utf-8")
        rc = main(["intake-import", str(p), "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)
        return IntakeStore.load(self.d / "intake.json")

    def test_valid_booked_payment_is_stored(self):
        s = self._import([_csv_row()])
        e = s.get("ORD1")
        self.assertIsNotNone(e)
        self.assertEqual(e["capture_id"], "CAP1")
        self.assertEqual(e["candidate"], _CAND)
        self.assertEqual(e["status"], "new")
        self.assertEqual(e["fields"]["biggest_problem"], _FIELDS["biggest_problem"])
        self.assertNotIn("_id", e["fields"])            # provider column ignored

    def test_unpaid_capture_is_rejected(self):
        self._import([_csv_row(capture="GHOST")])
        self.assertFalse((self.d / "intake.json").exists())

    def test_unknown_capture_does_not_bypass_the_gate(self):
        self._import([_csv_row(capture="", order="")])   # no capture id at all
        self.assertFalse((self.d / "intake.json").exists())

    def test_candidate_mismatch_is_rejected(self):
        self.store.put(Candidate(name="other", status="launched"))
        self.store.save()
        s = self._import([_csv_row(candidate="other")])
        self.assertEqual(s.all(), [])

    def test_missing_required_field_is_rejected(self):
        s = self._import([_csv_row(email="")])
        self.assertEqual(s.all(), [])

    def test_duplicate_submissions_collapse_to_one(self):
        # same order_id twice in one file, then re-import the file again
        self._import([_csv_row(name="First"), _csv_row(name="Second")])
        s = self._import([_csv_row(name="Third")], name="e2.csv")
        self.assertEqual(len(s.all()), 1)
        self.assertEqual(s.get("ORD1")["fields"]["name"], "Third")

    def test_mixed_batch_stores_only_the_paid_row(self):
        record_payment(self.store, self.ledger, _CAND, 29.9, actor="paypal",
                       currency="EUR", ref="paypal:CAP2")
        s = self._import([
            _csv_row(order="ORD1", capture="CAP1"),
            _csv_row(order="ORD9", capture="NOPE"),
            _csv_row(order="ORD2", capture="CAP2"),
        ])
        self.assertEqual(sorted(e["order_id"] for e in s.all()), ["ORD1", "ORD2"])


if __name__ == "__main__":
    unittest.main()
