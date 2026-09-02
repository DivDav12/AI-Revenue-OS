"""End-to-end FAKE autonomous cycle.

Fake opportunities (engine), fake build (deterministic agents), fake
publication (local staging), fake revenue (RevenueLedger written directly,
outside autonomous context), fake LLM (never called), EUR 0, no messages,
no payments. Proves the loop: discovers -> builds -> stages -> measures ->
learns -> promotes/abandons -> asks for money only when scaling -> keeps
going.
"""

import json
import tempfile
import unittest
from pathlib import Path

from revenue_os import autonomy
from revenue_os.jarvis_server import apply_control, jarvis_snapshot
from revenue_os.opportunity_store import load_opportunities
from revenue_os.revenue import RevenueLedger


def _form(**kw):
    return {k: [str(v)] for k, v in kw.items()}


class AutonomousE2ETests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        (self.d / "candidates.json").write_text(json.dumps([
            {"name": "sig1", "description": "founders first paying customers "
             "onboarding SaaS changelog release notes API docs cold email research"}]))

    def tearDown(self):
        self._d.cleanup()

    def test_full_fake_autonomous_run(self):
        # 1. enable autonomous mode via JARVIS
        msg = apply_control(self.d, "owner", _form(action="set-mode", mode="autonomous"))
        self.assertIn("AUTONOMOUS", msg)

        # 2. first cycle - discovers, builds, stages product pages
        rep = autonomy.run_cycle(self.d)
        self.assertIsNone(rep.get("stopped"))
        s = load_opportunities(self.d)
        board = s.board()
        self.assertGreater(len(s), 5)
        self.assertTrue(board["testing"])
        self.assertTrue(list((self.d / "published").iterdir()))   # real staged pages

        # a couple more cycles - the loop keeps working, abandons nothing yet
        for _ in range(2):
            rep = autonomy.run_cycle(self.d)
            self.assertIsNone(rep.get("stopped"))

        # 3. a fake sale lands on one experiment (booked OUTSIDE autonomy)
        s = load_opportunities(self.d)
        board = s.board()
        target = (board["testing"] or board["building"])[0]
        led = RevenueLedger.load(self.d / "revenue.json")
        led.add({"candidate_name": target["id"], "amount": 29.90,
                 "currency": "EUR", "actor": "fake-paypal",
                 "note": "fake e2e sale", "ref": "paypal:FAKE1"})
        led.save()

        # 4. next cycle picks it up, promotes it, spawns adjacent ideas,
        #    files a scaling money request
        rep = autonomy.run_cycle(self.d)
        s = load_opportunities(self.d)
        self.assertEqual(s.get(target["id"])["status"], "successful")
        self.assertGreater(s.counts()["discovered"], 0)          # adjacent
        snap = jarvis_snapshot(self.d)
        money = snap["autonomy"]["pending"]["money"]
        self.assertTrue(any("caling" in r["what"] for r in money))

        # 5. owner approves the scaling money request from JARVIS
        scaling = next(r for r in money if "caling" in r["what"])
        msg = apply_control(self.d, "owner",
                            _form(action="approve-request", id=scaling["id"]))
        self.assertIn("approved", msg)

        # 6. loop keeps running; nothing external was touched
        rep = autonomy.run_cycle(self.d)
        self.assertIsNone(rep.get("stopped"))
        self.assertFalse((self.d / "deliveries.json").exists())
        self.assertFalse((self.d / "llm_spend.json").exists())
        # the JARVIS view is real, not a label
        snap = jarvis_snapshot(self.d)
        self.assertEqual(snap["mode"], "autonomous")
        self.assertGreaterEqual(snap["autonomy"]["state"]["cycles"], 4)
        self.assertTrue(snap["autonomy"]["state"]["reasoning"])

    def test_denied_money_request_does_not_block_the_loop(self):
        for _ in range(2):
            autonomy.run_cycle(self.d)
        snap = jarvis_snapshot(self.d)
        req = snap["autonomy"]["pending"]["money"][0]
        apply_control(self.d, "owner", _form(action="deny-request", id=req["id"]))
        rep = autonomy.run_cycle(self.d)   # keeps working on everything else
        self.assertIsNone(rep.get("stopped"))
        self.assertGreater(rep["phases"][0]["new_opportunities"], 0)


if __name__ == "__main__":
    unittest.main()
