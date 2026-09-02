import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.agent_control import (
    AgentControl,
    AgentPaused,
    check_runnable,
    gate,
    load_agent_control,
)


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        self.path = self.d / "agent_control.json"

    def tearDown(self):
        self._d.cleanup()

    # --- defaults: fail open --------------------------------------------
    def test_absent_file_is_permissive(self):
        ctrl = AgentControl.load(self.path)
        self.assertFalse(ctrl.is_paused())
        self.assertTrue(ctrl.is_enabled("designer"))
        ok, reason = ctrl.runnable("design_assets")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_unknown_capability_is_refused(self):
        ok, reason = AgentControl.load(self.path).runnable("nope")
        self.assertFalse(ok)
        self.assertIn("no roster agent", reason)

    # --- disable one agent --------------------------------------------
    def test_disable_then_runnable_is_false_with_reason(self):
        ctrl = AgentControl.load(self.path)
        ctrl.set_agent("designer", False, by="tester", note="under review")
        ctrl.save()

        reloaded = load_agent_control(self.d)
        self.assertFalse(reloaded.is_enabled("designer"))
        ok, reason = reloaded.runnable("design_assets")
        self.assertFalse(ok)
        self.assertIn("disabled", reason)
        self.assertIn("under review", reason)
        # a different agent is unaffected
        self.assertTrue(reloaded.runnable("find_suppliers")[0])

    def test_re_enable(self):
        ctrl = AgentControl.load(self.path)
        ctrl.set_agent("designer", False)
        ctrl.set_agent("designer", True, by="tester")
        ctrl.save()
        self.assertTrue(load_agent_control(self.d).runnable("design_assets")[0])

    def test_set_agent_rejects_unknown_id(self):
        with self.assertRaises(ValueError):
            AgentControl.load(self.path).set_agent("not_an_agent", False)

    # --- global pause -------------------------------------------------
    def test_global_pause_blocks_every_agent(self):
        ctrl = AgentControl.load(self.path)
        ctrl.set_paused(True, by="tester", reason="maintenance")
        ctrl.save()

        reloaded = load_agent_control(self.d)
        self.assertTrue(reloaded.is_paused())
        for cap in ("design_assets", "find_suppliers", "quality_check"):
            ok, reason = reloaded.runnable(cap)
            self.assertFalse(ok)
            self.assertIn("paused", reason)
            self.assertIn("maintenance", reason)

    def test_resume_clears_pause(self):
        ctrl = AgentControl.load(self.path)
        ctrl.set_paused(True)
        ctrl.set_paused(False, by="tester")
        ctrl.save()
        self.assertFalse(load_agent_control(self.d).is_paused())
        self.assertTrue(load_agent_control(self.d).runnable("design_assets")[0])

    # --- helpers ----------------------------------------------------
    def test_check_runnable_and_gate_helpers(self):
        ok, _ = check_runnable(self.d, "design_assets")
        self.assertTrue(ok)
        gate(self.d, "design_assets")  # no raise

        ctrl = AgentControl.load(self.path)
        ctrl.set_paused(True, reason="x")
        ctrl.save()
        with self.assertRaises(AgentPaused):
            gate(self.d, "design_assets")

    # --- persistence ----------------------------------------------
    def test_atomic_write_is_valid_json_with_expected_shape(self):
        ctrl = AgentControl.load(self.path)
        ctrl.set_agent("designer", False)
        ctrl.set_paused(True, reason="r")
        ctrl.save()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw),
                         {"paused", "paused_reason", "mode", "agents", "updated_at"})
        self.assertTrue(raw["paused"])
        self.assertIn("designer", raw["agents"])
        self.assertFalse(raw["agents"]["designer"]["enabled"])

    # --- human-gate acknowledgement --------------------------------
    def test_acknowledge_and_reopen_gate(self):
        ctrl = AgentControl.load(self.path)
        self.assertFalse(ctrl.gate_acknowledged("store_builder", "2026-01-01T00:00:00"))
        ctrl.acknowledge_gate("store_builder", "2026-01-01T00:00:00",
                              by="me", note="built it")
        ctrl.save()

        r = load_agent_control(self.d)
        self.assertTrue(r.gate_acknowledged("store_builder", "2026-01-01T00:00:00"))
        # a newer output timestamp re-opens the gate
        self.assertFalse(r.gate_acknowledged("store_builder", "2026-06-01T00:00:00"))
        self.assertEqual(r.gate_ack_info("store_builder")["ack_note"], "built it")

        r.reopen_gate("store_builder")
        r.save()
        self.assertFalse(load_agent_control(self.d)
                         .gate_acknowledged("store_builder", "2026-01-01T00:00:00"))

    def test_acknowledge_gate_rejects_non_gated_agent(self):
        with self.assertRaises(ValueError):
            AgentControl.load(self.path).acknowledge_gate("designer", "ts")

    def test_set_agent_preserves_gate_ack(self):
        ctrl = AgentControl.load(self.path)
        ctrl.acknowledge_gate("developer", "ts1", by="me")
        ctrl.set_agent("developer", False, note="off")   # must not wipe the ack
        ctrl.save()
        r = load_agent_control(self.d)
        self.assertTrue(r.gate_acknowledged("developer", "ts1"))
        self.assertFalse(r.is_enabled("developer"))

    def test_corrupt_file_raises(self):
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            AgentControl.load(self.path)

    def test_not_live_capability_is_refused_even_when_permissive(self):
        # every roster agent is live today; this guards the branch anyway
        from revenue_os import roster
        planned = roster.planned()
        if not planned:
            self.skipTest("all roster agents are live")
        ok, reason = AgentControl.load(self.path).runnable(planned[0].capability)
        self.assertFalse(ok)
        self.assertIn("not live", reason)


if __name__ == "__main__":
    unittest.main()
