import ast
import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os import experiments as ex
from revenue_os.experiments import ExperimentStore
from revenue_os.store import Candidate, CandidateStore

_URL = "https://DivDav12.github.io/AI-Revenue-OS/checkout.html"


def _iso(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _Dir(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def _cand(self, status="launched", price=29.9):
        cs = CandidateStore.load(self.d / "candidates.json")
        cs.put(Candidate(name="clp", status=status, offer={"price": price,
                                                           "currency": "EUR"},
                         public_url=_URL))
        cs.save()

    def _brief(self, lead_id, *, source="hn-algolia", platform="Hacker News",
               status="draft", prospect_quality="", prospect_type="",
               age_bucket="", relevance_score=None):
        p = self.d / "outreach.json"
        rows = json.loads(p.read_text()) if p.exists() else []
        rows.append({"lead_id": lead_id, "status": status,
                     "brief": {"lead_id": lead_id, "source": source,
                               "platform": platform,
                               "prospect_quality": prospect_quality,
                               "prospect_type": prospect_type,
                               "age_bucket": age_bucket,
                               "relevance_score": relevance_score,
                               "checkout_link": _URL + f"?lead={lead_id}"}})
        p.write_text(json.dumps(rows), encoding="utf-8")

    def _acq_lead(self, lead_id, *, canonical=None, source="hn-algolia"):
        p = self.d / "acquisition.json"
        rows = json.loads(p.read_text()) if p.exists() else []
        rows.append({"canonical_url": canonical or f"https://ex.test/{lead_id}",
                     "lead_id": lead_id, "source": source,
                     "human_review_status": "new", "final_score": 40,
                     "title": f"t{lead_id}"})
        p.write_text(json.dumps(rows), encoding="utf-8")

    def _intake(self, order_id, lead_id, capture_id):
        p = self.d / "intake.json"
        rows = json.loads(p.read_text()) if p.exists() else []
        rows.append({"order_id": order_id, "lead_id": lead_id,
                     "capture_id": capture_id, "candidate": "clp",
                     "status": "new"})
        p.write_text(json.dumps(rows), encoding="utf-8")

    def _revenue(self, capture_id, amount=29.9):
        p = self.d / "revenue.json"
        rows = json.loads(p.read_text()) if p.exists() else []
        rows.append({"candidate_name": "clp", "amount": amount, "currency": "EUR",
                     "received_at": _iso(), "actor": "paypal",
                     "ref": f"paypal:{capture_id}"})
        p.write_text(json.dumps(rows), encoding="utf-8")


class StoreTests(_Dir):
    def test_open_advance_lifecycle(self):
        s = ExperimentStore.load(self.d / "experiments.json")
        self.assertEqual(s.open("L1", candidate="clp", offer_price=29.9,
                                currency="EUR", source="hn-algolia",
                                platform="HN"), "opened")
        self.assertEqual(s.get("L1")["status"], "drafted")
        s.advance("L1", "posted")
        self.assertEqual(s.get("L1")["status"], "posted")
        self.assertIsNotNone(s.get("L1")["posted_at"])
        s.advance("L1", "intake")
        s.advance("L1", "sale", revenue_ref="paypal:X")
        self.assertEqual(s.get("L1")["status"], "sale")
        self.assertEqual(s.get("L1")["revenue_ref"], "paypal:X")
        self.assertIsNotNone(s.get("L1")["outcome_at"])

    def test_drafted_to_skipped(self):
        s = ExperimentStore.load(self.d / "experiments.json")
        s.open("L1", candidate="c", offer_price=1, currency="EUR",
               source="x", platform="x")
        s.advance("L1", "skipped")
        self.assertEqual(s.get("L1")["status"], "skipped")

    def test_invalid_transition_and_closed_is_terminal(self):
        s = ExperimentStore.load(self.d / "experiments.json")
        s.open("L1", candidate="c", offer_price=1, currency="EUR",
               source="x", platform="x")
        with self.assertRaises(ValueError):
            s.advance("L1", "intake")          # drafted -> intake not allowed
        s.advance("L1", "skipped")
        with self.assertRaises(ValueError):
            s.advance("L1", "posted")          # closed is terminal

    def test_one_experiment_per_lead(self):
        s = ExperimentStore.load(self.d / "experiments.json")
        s.open("L1", candidate="c", offer_price=1, currency="EUR",
               source="x", platform="x")
        s.advance("L1", "skipped")
        self.assertEqual(s.open("L1", candidate="c", offer_price=2, currency="EUR",
                                source="y", platform="y"), "exists")
        self.assertEqual(len(s.all()), 1)
        self.assertEqual(s.get("L1")["source"], "x")     # unchanged

    def test_atomic_restart_safe_persistence(self):
        s = ExperimentStore.load(self.d / "experiments.json")
        s.open("L1", candidate="c", offer_price=1, currency="EUR",
               source="x", platform="x")
        s.save()
        raw = json.loads((self.d / "experiments.json").read_text())
        self.assertEqual(len(raw), 1)
        s2 = ExperimentStore.load(self.d / "experiments.json")
        self.assertEqual(s2.get("L1")["status"], "drafted")
        # a partial write left no .tmp behind
        self.assertEqual(list(self.d.glob("*.tmp")), [])


class ModuleOpsTests(_Dir):
    def test_open_from_briefs(self):
        self._cand()
        self._brief("L1")
        self._brief("L2", source="lemmy", platform="Lemmy")
        r = ex.open_from_briefs(self.d)
        self.assertEqual(r["opened"], 2)
        s = ExperimentStore.load(self.d / "experiments.json")
        self.assertEqual(s.get("L1")["candidate"], "clp")
        self.assertEqual(s.get("L1")["offer_price"], 29.9)
        self.assertEqual(s.get("L2")["source"], "lemmy")
        # idempotent
        self.assertEqual(ex.open_from_briefs(self.d)["opened"], 0)

    def test_correlate_sale_read_only(self):
        self._cand()
        self._brief("L1")
        ex.open_from_briefs(self.d)
        ex.advance(self.d, "L1", "posted")
        self._intake("O1", "L1", "CAP1")
        self._revenue("CAP1")
        r = ex.correlate_sale(self.d)
        self.assertEqual(r["sale"], 1)
        s = ExperimentStore.load(self.d / "experiments.json")
        self.assertEqual(s.get("L1")["status"], "sale")
        self.assertEqual(s.get("L1")["revenue_ref"], "paypal:CAP1")

    def test_correlate_sale_no_booked_ref_marks_intake_only(self):
        self._cand()
        self._brief("L1")
        ex.open_from_briefs(self.d)
        ex.advance(self.d, "L1", "posted")
        self._intake("O1", "L1", "CAP1")   # intake but no revenue entry
        r = ex.correlate_sale(self.d)
        self.assertEqual(r["sale"], 0)
        self.assertEqual(r["intake"], 1)
        self.assertEqual(
            ExperimentStore.load(self.d / "experiments.json").get("L1")["status"],
            "intake")

    def test_sweep_closes_stale_posted_as_no_sale(self):
        self._cand()
        self._brief("L1")
        ex.open_from_briefs(self.d)
        s = ExperimentStore.load(self.d / "experiments.json")
        s.get("L1")["status"] = "posted"
        s.get("L1")["posted_at"] = _iso(days_ago=20)
        s.save()
        r = ex.sweep(self.d, followup_days=14)
        self.assertEqual(r["closed"], 1)
        self.assertEqual(
            ExperimentStore.load(self.d / "experiments.json").get("L1")["status"],
            "no_sale")

    def test_sweep_respects_window_and_linked_intake(self):
        self._cand()
        self._brief("L1")
        self._brief("L2")
        ex.open_from_briefs(self.d)
        s = ExperimentStore.load(self.d / "experiments.json")
        for lid, age in (("L1", 5), ("L2", 30)):
            s.get(lid)["status"] = "posted"
            s.get(lid)["posted_at"] = _iso(days_ago=age)
        s.save()
        self._intake("O2", "L2", "CAP2")          # L2 has an intake -> keep
        r = ex.sweep(self.d, followup_days=14)
        self.assertEqual(r["closed"], 0)          # L1 too fresh, L2 has intake

    def test_sweep_disabled_at_zero(self):
        self._cand()
        self._brief("L1")
        ex.open_from_briefs(self.d)
        s = ExperimentStore.load(self.d / "experiments.json")
        s.get("L1")["status"] = "posted"
        s.get("L1")["posted_at"] = _iso(days_ago=99)
        s.save()
        self.assertEqual(ex.sweep(self.d, followup_days=0)["disabled"], True)
        self.assertEqual(
            ExperimentStore.load(self.d / "experiments.json").get("L1")["status"],
            "posted")

    def test_rollup(self):
        self._cand()
        self._brief("L1")
        self._brief("L2", source="lemmy")
        self._brief("L3", source="lemmy")
        ex.open_from_briefs(self.d)
        ex.advance(self.d, "L2", "posted")
        ex.advance(self.d, "L3", "skipped")
        r = ex.rollup(self.d)
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["overall"]["drafted"], 1)
        self.assertEqual(r["overall"]["posted"], 1)
        self.assertEqual(r["overall"]["skipped"], 1)
        self.assertEqual(r["by_source"]["lemmy"]["posted"], 1)
        self.assertEqual(r["open"], 2)      # drafted + posted
        self.assertEqual(r["closed"], 1)    # skipped


class FeedbackMetadataTests(_Dir):
    def _settled(self, lead_id, outcome, *, source="hn-algolia",
                 quality="high", ptype="active_problem"):
        s = ExperimentStore.load(self.d / "experiments.json")
        s.open(lead_id, candidate="clp", offer_price=29.9, currency="EUR",
               source=source, platform="HN", prospect_quality=quality,
               prospect_type=ptype, age_bucket="fresh", relevance_score=70)
        s.advance(lead_id, "posted")
        if outcome == "sale":
            s.advance(lead_id, "intake")
            s.advance(lead_id, "sale", revenue_ref="paypal:X")
        else:
            s.advance(lead_id, "no_sale")
        s.save()

    def test_open_captures_lead_metadata(self):
        s = ExperimentStore.load(self.d / "experiments.json")
        s.open("L1", candidate="clp", offer_price=29.9, currency="EUR",
               source="lemmy", platform="Lemmy", prospect_quality="high",
               prospect_type="active_problem", age_bucket="fresh",
               relevance_score=72)
        e = s.get("L1")
        self.assertEqual(e["prospect_quality"], "high")
        self.assertEqual(e["prospect_type"], "active_problem")
        self.assertEqual(e["age_bucket"], "fresh")
        self.assertEqual(e["relevance_score"], 72)

    def test_open_from_briefs_captures_brief_metadata(self):
        self._cand()
        self._brief("L1", prospect_quality="medium", prospect_type="seeking_advice",
                    age_bucket="recent", relevance_score=44)
        ex.open_from_briefs(self.d)
        e = ExperimentStore.load(self.d / "experiments.json").get("L1")
        self.assertEqual(e["prospect_quality"], "medium")
        self.assertEqual(e["prospect_type"], "seeking_advice")
        self.assertEqual(e["age_bucket"], "recent")
        self.assertEqual(e["relevance_score"], 44)

    def test_legacy_row_without_metadata_still_loads_and_feeds_back(self):
        # a store written by the pre-2.6 code: no quality/type/age keys
        (self.d / "experiments.json").write_text(json.dumps([
            {"lead_id": "L1", "candidate": "clp", "offer_price": 29.9,
             "currency": "EUR", "source": "hn-algolia", "platform": "HN",
             "status": "no_sale", "created_at": _iso(30),
             "posted_at": _iso(25), "outcome_at": _iso(11),
             "revenue_ref": "", "note": ""}]), encoding="utf-8")
        s = ExperimentStore.load(self.d / "experiments.json")
        self.assertEqual(s.get("L1")["status"], "no_sale")
        fb = ex.feedback(self.d)
        self.assertEqual(fb["settled"], 1)
        self.assertIn("unknown", fb["by_quality"])

    def test_advance_stores_optional_reason(self):
        s = ExperimentStore.load(self.d / "experiments.json")
        s.open("L1", candidate="c", offer_price=1, currency="EUR",
               source="x", platform="x")
        s.advance("L1", "skipped", reason="off-topic subreddit")
        self.assertEqual(s.get("L1")["reason"], "off-topic subreddit")
        # no reason -> key absent, unchanged behaviour
        s.open("L2", candidate="c", offer_price=1, currency="EUR",
               source="x", platform="x")
        s.advance("L2", "skipped")
        self.assertNotIn("reason", s.get("L2"))

    def test_feedback_empty(self):
        fb = ex.feedback(self.d)
        self.assertEqual(fb["settled"], 0)
        self.assertFalse(fb["ready"])
        self.assertEqual(fb["needed"], 8)

    def test_feedback_below_threshold_not_ready(self):
        for i in range(4):
            self._settled(f"S{i}", "sale")
        for i in range(3):
            self._settled(f"N{i}", "no_sale")
        fb = ex.feedback(self.d)
        self.assertEqual(fb["settled"], 7)
        self.assertFalse(fb["ready"])          # 7 < 8

    def test_feedback_ready_needs_both_classes(self):
        for i in range(8):
            self._settled(f"N{i}", "no_sale")
        self.assertFalse(ex.feedback(self.d)["ready"])   # 8 settled, one class
        self._settled("S0", "sale")
        fb = ex.feedback(self.d)
        self.assertTrue(fb["ready"])            # 9 settled, both classes
        self.assertEqual(fb["sale"], 1)
        self.assertEqual(fb["no_sale"], 8)

    def test_feedback_dimensions_and_rates(self):
        self._settled("A", "sale", source="hn-algolia", quality="high")
        self._settled("B", "no_sale", source="hn-algolia", quality="high")
        self._settled("C", "no_sale", source="lemmy", quality="medium")
        fb = ex.feedback(self.d)
        self.assertEqual(fb["by_source"]["hn-algolia"],
                         {"settled": 2, "sale": 1, "no_sale": 1, "sale_rate": 0.5})
        self.assertEqual(fb["by_source"]["lemmy"]["sale_rate"], 0.0)
        self.assertEqual(fb["by_quality"]["high"]["settled"], 2)
        self.assertEqual(fb["by_type"]["active_problem"]["settled"], 3)

    def test_feedback_is_read_only(self):
        self._settled("A", "sale")
        before = (self.d / "experiments.json").read_text()
        acq_before = self.d / "acquisition.json"
        ex.feedback(self.d)
        self.assertEqual((self.d / "experiments.json").read_text(), before)
        self.assertFalse(acq_before.exists())

    def test_sync_lead_backrefs_annotates_closed_lead(self):
        self._acq_lead("l1")
        self._cand()
        self._brief("l1", prospect_quality="high")
        ex.open_from_briefs(self.d)
        ex.advance(self.d, "l1", "skipped", reason="rules")
        from revenue_os.acquisition import AcquisitionStore
        lead = AcquisitionStore.load(self.d / "acquisition.json").by_id("l1")
        self.assertEqual(lead["outreach_outcome"]["status"], "skipped")
        self.assertEqual(lead["outreach_outcome"]["prospect_quality"], "high")
        self.assertEqual(lead["human_review_status"], "new")   # untouched

    def test_sync_lead_backrefs_no_acquisition_store_is_noop(self):
        self._cand()
        self._brief("L1")
        ex.open_from_briefs(self.d)
        # no acquisition.json on disk
        self.assertEqual(ex.advance(self.d, "L1", "skipped")["status"], "skipped")
        self.assertFalse((self.d / "acquisition.json").exists())

    def test_correlate_sale_annotates_lead(self):
        self._acq_lead("l1")
        self._cand()
        self._brief("l1")
        ex.open_from_briefs(self.d)
        ex.advance(self.d, "l1", "posted")
        self._intake("O1", "l1", "CAP1")
        self._revenue("CAP1")
        ex.correlate_sale(self.d)
        from revenue_os.acquisition import AcquisitionStore
        lead = AcquisitionStore.load(self.d / "acquisition.json").by_id("l1")
        self.assertEqual(lead["outreach_outcome"]["status"], "sale")
        self.assertEqual(lead["outreach_outcome"]["revenue_ref"], "paypal:CAP1")


class SafetyTests(_Dir):
    def test_no_network_paypal_or_llm_in_experiments_module(self):
        src = inspect.getsource(ex)
        for banned in ("import requests", "urllib.request", "urlopen",
                       "import anthropic", "build_client(", "PayPalClient",
                       "sync_transactions(", "verify_and_book_order(",
                       "record_payment(", "def post", "def send", "smtplib"):
            self.assertNotIn(banned, src)

    def test_experiments_imports_only_safe_modules(self):
        tree = ast.parse(inspect.getsource(ex))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[-1])
            elif isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
        forbidden = {"anthropic", "requests", "socket", "smtplib", "http",
                     "urllib", "paypal", "llm_normalize", "acquisition_web",
                     "acquisition_sources", "acquisition_llm", "outreach_llm"}
        self.assertEqual(mods & forbidden, set())


if __name__ == "__main__":
    unittest.main()
