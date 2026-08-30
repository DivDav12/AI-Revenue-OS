import tempfile
import unittest
from pathlib import Path

from revenue_os.agent_outputs import AgentOutputStore
from revenue_os.agent_runner import last_output, run_agent

_OPP = {"name": "doc-templates"}
_OFFER = {"what_is_sold": "Doc pack", "price": 19.0, "currency": "EUR",
          "includes": ["a", "b"]}


class RunAgentTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_dispatch_and_persist(self):
        r = run_agent(self.d, "find_suppliers",
                      {"opportunity": _OPP, "known_suppliers": []})
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.agent, "supplier_finder")
        # persisted and reloadable across a fresh store load (restart-safe)
        out = last_output(self.d, "find_suppliers")
        self.assertIsNotNone(out)
        self.assertIn("supplier_candidates", out)
        self.assertTrue((self.d / "agent_outputs.json").exists())

    def test_persistence_round_trip_via_store(self):
        run_agent(self.d, "design_assets", {"opportunity": _OPP, "offer": _OFFER})
        reloaded = AgentOutputStore.load(self.d / "agent_outputs.json")
        entry = reloaded.get("design_assets")
        self.assertIn("output", entry)
        self.assertIn("ts", entry)
        self.assertEqual(entry["output"]["assets_exist"], False)

    def test_human_gated_capability_is_tagged_not_executed(self):
        r = run_agent(self.d, "build_store", {"opportunity": _OPP, "offer": _OFFER})
        self.assertEqual(r.status, "ok")
        out = last_output(self.d, "build_store")
        self.assertTrue(out["human_gate_required"])
        self.assertEqual(out["_gate"], "human")
        self.assertEqual(out["build_artifacts"], [])   # nothing was built/published

    def test_unknown_capability_raises(self):
        with self.assertRaises(ValueError):
            run_agent(self.d, "no_such_capability", {})

    def test_not_yet_live_capability_raises(self):
        from revenue_os import roster
        still_planned = roster.planned()
        if not still_planned:
            self.skipTest("all roster agents are live")
        with self.assertRaises(ValueError):
            run_agent(self.d, still_planned[0].capability, {})

    def test_deterministic_output_ignoring_timestamp(self):
        a = run_agent(self.d, "design_assets", {"opportunity": _OPP, "offer": _OFFER})
        b = run_agent(self.d, "design_assets", {"opportunity": _OPP, "offer": _OFFER})
        self.assertEqual(a.output, b.output)

    def test_error_result_is_not_persisted(self):
        r = run_agent(self.d, "develop", {})          # missing build_specification
        self.assertEqual(r.status, "error")
        self.assertIsNone(last_output(self.d, "develop"))


if __name__ == "__main__":
    unittest.main()
