"""The Opportunity lifecycle state machine + its store integration."""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import opportunity_state as ostate
from revenue_os.opportunity_state import (
    IllegalTransition,
    STATES,
    can_transition,
    check_transition,
    next_states,
)
from revenue_os.opportunity_store import (
    Opportunity,
    OpportunityStore,
    load_opportunities,
)


class StateMachineTests(unittest.TestCase):
    def test_all_spec_states_present(self):
        for name in ("DISCOVERED", "RESEARCHING", "SCORED", "SELECTED",
                     "PLANNING", "BUILDING", "VALIDATING", "READY_TO_DEPLOY",
                     "DEPLOYING", "LIVE", "ACQUIRING_TRAFFIC", "MEASURING",
                     "FIRST_VISITOR", "FIRST_LEAD", "FIRST_SALE", "DELIVERING",
                     "ACTIVE", "OPTIMIZING", "PROFITABLE", "SCALING",
                     "NO_TRACTION", "ABANDONED", "BLOCKED", "FAILED"):
            self.assertIn(name, STATES)

    def test_happy_path_is_legal(self):
        path = ["DISCOVERED", "SCORED", "SELECTED", "PLANNING", "BUILDING",
                "VALIDATING", "READY_TO_DEPLOY", "DEPLOYING", "LIVE",
                "MEASURING", "FIRST_SALE", "DELIVERING", "ACTIVE",
                "PROFITABLE", "SCALING"]
        for a, b in zip(path, path[1:]):
            self.assertTrue(can_transition(a, b), f"{a} -> {b} should be legal")

    def test_illegal_skips_are_rejected(self):
        self.assertFalse(can_transition("READY_TO_DEPLOY", "LIVE"))
        self.assertFalse(can_transition("SCORED", "DEPLOYING"))
        self.assertFalse(can_transition("DISCOVERED", "ACTIVE"))
        with self.assertRaises(IllegalTransition):
            check_transition("READY_TO_DEPLOY", "LIVE")

    def test_abandon_and_block_from_any_non_terminal_state(self):
        for s in STATES:
            if s == "ABANDONED":            # terminal - no moves out
                continue
            self.assertTrue(can_transition(s, "ABANDONED"))
            if s != "BLOCKED":
                self.assertTrue(can_transition(s, "BLOCKED"))

    def test_terminal_and_noop(self):
        self.assertFalse(can_transition("ABANDONED", "DISCOVERED"))
        self.assertFalse(can_transition("LIVE", "LIVE"))

    def test_recoverable_states_rejoin_the_path(self):
        self.assertTrue(can_transition("BLOCKED", "DEPLOYING"))
        self.assertTrue(can_transition("FAILED", "BUILDING"))
        self.assertTrue(can_transition("BLOCKED", "ABANDONED"))
        self.assertIn("LIVE", next_states("BLOCKED"))


class StoreTransitionTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        self.s = OpportunityStore(self.d / "opportunities.json")
        self.oid = self.s.upsert(Opportunity(title="X", category="saas"))["id"]

    def tearDown(self):
        self._d.cleanup()

    def test_new_record_has_bootstrap_transition(self):
        r = self.s.get(self.oid)
        self.assertEqual(r["state"], "DISCOVERED")
        self.assertEqual(len(r["transitions"]), 1)
        t = r["transitions"][0]
        self.assertEqual(t["previous_state"], "")
        self.assertEqual(t["next_state"], "DISCOVERED")
        self.assertIn("source", t)
        self.assertIn("actor", t)
        self.assertIn("ts", t)

    def test_transition_records_every_required_field(self):
        self.s.transition(self.oid, "SCORED", reason="engine scored it",
                          source="task", actor="strategist", task_id="t-1")
        t = self.s.get(self.oid)["transitions"][-1]
        for key in ("ts", "previous_state", "next_state", "reason", "source",
                    "actor"):
            self.assertIn(key, t)
        self.assertEqual(t["previous_state"], "DISCOVERED")
        self.assertEqual(t["next_state"], "SCORED")
        self.assertEqual(t["task_id"], "t-1")
        self.assertNotIn("forced", t)
        self.assertEqual(self.s.get(self.oid)["state"], "SCORED")

    def test_illegal_transition_raises_unless_forced(self):
        with self.assertRaises(IllegalTransition):
            self.s.transition(self.oid, "LIVE", reason="skip", source="task")
        # still DISCOVERED - nothing moved
        self.assertEqual(self.s.get(self.oid)["state"], "DISCOVERED")
        rec = self.s.transition(self.oid, "LIVE", reason="human confirmed live",
                                source="human", actor="owner", force=True)
        self.assertTrue(rec["forced"])
        self.assertEqual(self.s.get(self.oid)["state"], "LIVE")

    def test_deploying_to_live_needs_a_real_move(self):
        for st in ("SCORED", "SELECTED", "PLANNING", "BUILDING", "VALIDATING",
                   "READY_TO_DEPLOY", "DEPLOYING"):
            self.s.transition(self.oid, st, reason="step", source="task")
        rec = self.s.transition(self.oid, "LIVE",
                                reason="deploy adapter returned https://x.example",
                                source="task", actor="system")
        self.assertFalse(rec.get("forced", False))
        self.assertEqual(self.s.get(self.oid)["state"], "LIVE")

    def test_legacy_set_status_mirrors_into_the_machine(self):
        self.s.set_status(self.oid, "building", note="picked", actor="strategist")
        r = self.s.get(self.oid)
        self.assertEqual(r["status"], "building")
        self.assertEqual(r["state"], "BUILDING")
        last = r["transitions"][-1]
        self.assertEqual(last["source"], "legacy_status_sync")
        self.assertEqual(last["actor"], "strategist")
        # experiments still gets exactly one note entry (unchanged behaviour)
        self.assertEqual(len(r["experiments"]), 1)

    def test_legacy_testing_is_ready_to_deploy_not_live(self):
        self.s.set_status(self.oid, "testing")
        self.assertEqual(self.s.get(self.oid)["state"], "READY_TO_DEPLOY")

    def test_by_state_and_counts(self):
        self.s.transition(self.oid, "SCORED", reason="r", source="task")
        b = self.s.upsert(Opportunity(title="Y", category="saas"))["id"]
        self.assertEqual({r["id"] for r in self.s.by_state("SCORED")}, {self.oid})
        self.assertEqual({r["id"] for r in self.s.by_state("DISCOVERED")}, {b})
        self.assertEqual(self.s.state_counts(),
                         {"SCORED": 1, "DISCOVERED": 1})

    def test_migration_backfills_state_for_old_records(self):
        raw = [{"id": "opp_old", "title": "legacy", "category": "saas",
                "status": "testing", "score": 1.0}]
        (self.d / "opportunities.json").write_text(json.dumps(raw))
        s = load_opportunities(self.d)
        r = s.get("opp_old")
        self.assertEqual(r["state"], "READY_TO_DEPLOY")
        self.assertEqual(len(r["transitions"]), 1)
        self.assertEqual(r["transitions"][0]["source"], "migration")

    def test_upsert_preserves_state_and_history(self):
        self.s.transition(self.oid, "SCORED", reason="r", source="task")
        self.s.upsert(Opportunity(title="X", category="saas",
                                  est_revenue_eur=999))
        r = self.s.get(self.oid)
        self.assertEqual(r["state"], "SCORED")
        self.assertEqual(len(r["transitions"]), 2)


if __name__ == "__main__":
    unittest.main()
