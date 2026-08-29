import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from revenue_os.store import Candidate, CandidateStore


def _cand(name: str, total: float = 1.0) -> Candidate:
    return Candidate(name=name, description="d", source="s", raw_ref="r", total=total)


class CandidateStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "candidates.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_upsert_new_then_roundtrip(self):
        store = CandidateStore(self.path)
        store.upsert(_cand("alpha"))
        store.save()

        reloaded = CandidateStore.load(self.path)
        got = reloaded.get("alpha")
        self.assertIsNotNone(got)
        self.assertEqual(got.status, "discovered")
        self.assertTrue(got.first_seen)
        self.assertEqual(got.first_seen, got.last_scored)

    def test_upsert_existing_refreshes_score_keeps_status_and_first_seen(self):
        store = CandidateStore(self.path)
        first = store.upsert(_cand("beta", total=1.0))
        # simulate a human-set status
        store.put(replace(first, status="approved"))

        merged = store.upsert(_cand("beta", total=4.2))
        self.assertEqual(merged.total, 4.2)
        self.assertEqual(merged.status, "approved")
        self.assertEqual(merged.first_seen, first.first_seen)
        self.assertTrue(merged.last_scored)
        self.assertGreaterEqual(merged.last_scored, merged.first_seen)

    def test_research_note_round_trips_and_survives_upsert(self):
        store = CandidateStore(self.path)
        c = store.upsert(_cand("gamma"))
        store.put(replace(c, status="shortlisted", research={"verdict": "caution"}))
        store.save()

        reloaded = CandidateStore.load(self.path)
        self.assertEqual(reloaded.get("gamma").research, {"verdict": "caution"})
        # a re-score keeps the note
        merged = reloaded.upsert(_cand("gamma", total=3.3))
        self.assertEqual(merged.research, {"verdict": "caution"})

    def test_competition_note_round_trips_and_survives_upsert(self):
        store = CandidateStore(self.path)
        c = store.upsert(_cand("delta"))
        store.put(replace(c, status="shortlisted",
                          competition={"verdict": "crowded"}))
        store.save()

        reloaded = CandidateStore.load(self.path)
        self.assertEqual(reloaded.get("delta").competition, {"verdict": "crowded"})
        merged = reloaded.upsert(_cand("delta", total=4.1))
        self.assertEqual(merged.competition, {"verdict": "crowded"})

    def test_launch_draft_round_trips_and_survives_upsert(self):
        store = CandidateStore(self.path)
        c = store.upsert(_cand("eps"))
        store.put(replace(c, status="validated",
                          launch_draft={"headline": "Buy this"}))
        store.save()

        reloaded = CandidateStore.load(self.path)
        self.assertEqual(reloaded.get("eps").launch_draft, {"headline": "Buy this"})
        merged = reloaded.upsert(_cand("eps", total=5.0))
        self.assertEqual(merged.launch_draft, {"headline": "Buy this"})

    def test_deliverable_round_trips_and_survives_upsert(self):
        store = CandidateStore(self.path)
        c = store.upsert(_cand("zeta"))
        store.put(replace(c, status="validated",
                          deliverable={"dir": "deliverables/zeta"}))
        store.save()

        reloaded = CandidateStore.load(self.path)
        self.assertEqual(reloaded.get("zeta").deliverable, {"dir": "deliverables/zeta"})
        merged = reloaded.upsert(_cand("zeta", total=6.0))
        self.assertEqual(merged.deliverable, {"dir": "deliverables/zeta"})

    def test_all_is_ranked_by_total_desc(self):
        store = CandidateStore(self.path)
        store.upsert(_cand("low", total=1.0))
        store.upsert(_cand("high", total=5.0))
        self.assertEqual([c.name for c in store.all()], ["high", "low"])

    def test_missing_file_loads_empty(self):
        store = CandidateStore.load(self.path)
        self.assertEqual(store.all(), [])

    def test_corrupt_file_raises_value_error(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            CandidateStore.load(self.path)

    def test_repeated_saves_leave_valid_json_list(self):
        store = CandidateStore(self.path)
        for i in range(3):
            store.upsert(_cand(f"c{i}", total=float(i)))
            store.save()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 3)


if __name__ == "__main__":
    unittest.main()
