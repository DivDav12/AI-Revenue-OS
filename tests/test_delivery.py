import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from revenue_os.delivery import (
    DeliveryError,
    DeliveryStore,
    EmailConfig,
    delivery_status,
    send_delivery,
    stage_delivery,
)
from revenue_os.intake import IntakeStore
from revenue_os.revenue import RevenueLedger
from revenue_os.store import Candidate, CandidateStore

_PLAN = {
    "status": "approved",
    "basis": "web search, 3 sources",
    "model": "claude-sonnet-5",
    "drafted_at": "2026-08-30T00:00:00+00:00",
    "business_analysis": {"what_sold": "widgets", "problem_solved": "slow",
                          "value_proposition": "fast widgets"},
    "ideal_customer": {"profile": "SMB ops", "characteristics": "busy",
                       "where_to_reach": "LinkedIn"},
    "acquisition_opportunities": [
        {"name": "Reddit", "channel": "r/x", "why_relevant": "buyers there",
         "first_step": "post"},
    ],
    "prioritized_strategy": {"ranking": ["Reddit"], "start_with": "Reddit",
                             "reasoning": "cheap"},
    "action_plan_14_day": [{"day": d, "focus": f"day {d}", "actions": "do things"}
                           for d in range(1, 15)],
    "outreach_templates": [{"name": "cold dm", "context": "", "body": "hi {name}"}],
    "next_steps": ["publish", "measure", "iterate"],
    "sources": [{"title": "S", "url": "https://s.example/x"}],
    "qc": {"passed": True, "checks": ["14-day plan complete"]},
}

CFG = EmailConfig(host="smtp.example", user="bot@shop.example",
                  password="app-pw", sender="bot@shop.example")


def _seed(d: Path, *, status="reviewed", plan=_PLAN, booked=True):
    cs = CandidateStore(d / "candidates.json")
    cs.put(Candidate(name="cand", description="d", status="earning",
                     offer={"price": 29.9, "currency": "EUR"}))
    cs.save()
    led = RevenueLedger(d / "revenue.json")
    if booked:
        led.add({"candidate_name": "cand", "amount": 29.9, "currency": "EUR",
                 "received_at": "2026-08-29T00:00:00+00:00", "actor": "paypal",
                 "ref": "paypal:CAP1"})
    led.save()
    it = IntakeStore(d / "intake.json")
    entry = it.add("ORD1", "cand",
                   {"name": "Jane Doe", "email": "jane@buyer.example",
                    "sells": "widgets"},
                   capture_id="CAP1")
    entry["status"] = status
    if plan is not None:
        entry["plan"] = dict(plan)
    it.save()


class StageGateTests(unittest.TestCase):
    def setUp(self):
        self._t = TemporaryDirectory()
        self.d = Path(self._t.name)
        self.addCleanup(self._t.cleanup)

    def test_requires_reviewed_intake(self):
        _seed(self.d, status="new")
        with self.assertRaisesRegex(DeliveryError, "intake-review"):
            stage_delivery(self.d, "ORD1")

    def test_requires_approved_plan(self):
        _seed(self.d, plan={**_PLAN, "status": "draft"})
        with self.assertRaisesRegex(DeliveryError, "plan-approve"):
            stage_delivery(self.d, "ORD1")

    def test_requires_booked_payment(self):
        _seed(self.d, booked=False)
        with self.assertRaisesRegex(DeliveryError, "not a booked payment"):
            stage_delivery(self.d, "ORD1")

    def test_unknown_order(self):
        _seed(self.d)
        with self.assertRaises(DeliveryError):
            stage_delivery(self.d, "NOPE")


class StageSendTests(unittest.TestCase):
    def setUp(self):
        self._t = TemporaryDirectory()
        self.d = Path(self._t.name)
        self.addCleanup(self._t.cleanup)
        _seed(self.d)
        self.sent = []
        self.mailer = lambda cfg, msg: (self.sent.append(msg) or "<mid-123@shop>")

    def test_stage_writes_a_real_pdf_and_records_it(self):
        r = stage_delivery(self.d, "ORD1")
        self.assertEqual(r["status"], "staged")
        pdf = Path(r["pdf_path"])
        self.assertTrue(pdf.is_file())
        self.assertEqual(pdf.name, "plan-ORD1.pdf")
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-1.4"))
        self.assertEqual(len(r["pdf_sha256"]), 64)
        rec = DeliveryStore.load(self.d / "deliveries.json").get("ORD1")
        self.assertEqual(rec["to_email"], "jane@buyer.example")

    def test_send_emails_the_pdf_and_marks_sent(self):
        stage_delivery(self.d, "ORD1")
        r = send_delivery(self.d, "ORD1", mailer=self.mailer, config=CFG)
        self.assertEqual(r["status"], "sent")
        self.assertEqual(r["message_id"], "<mid-123@shop>")
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["To"], "jane@buyer.example")
        self.assertIn("Customer Launch Plan", msg["Subject"])
        atts = list(msg.iter_attachments())
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0].get_content_type(), "application/pdf")
        self.assertTrue(atts[0].get_payload(decode=True).startswith(b"%PDF"))

    def test_send_auto_stages_when_pdf_missing(self):
        r = send_delivery(self.d, "ORD1", mailer=self.mailer, config=CFG)
        self.assertEqual(r["status"], "sent")
        self.assertEqual(len(self.sent), 1)

    def test_second_send_is_refused_without_force(self):
        send_delivery(self.d, "ORD1", mailer=self.mailer, config=CFG)
        with self.assertRaisesRegex(DeliveryError, "already delivered"):
            send_delivery(self.d, "ORD1", mailer=self.mailer, config=CFG)
        self.assertEqual(len(self.sent), 1)
        r = send_delivery(self.d, "ORD1", mailer=self.mailer, config=CFG, force=True)
        self.assertEqual(r["status"], "sent")
        self.assertEqual(len(self.sent), 2)

    def test_restage_keeps_sent_status(self):
        send_delivery(self.d, "ORD1", mailer=self.mailer, config=CFG)
        r = stage_delivery(self.d, "ORD1")
        self.assertEqual(r["status"], "sent")

    def test_status_summary(self):
        stage_delivery(self.d, "ORD1")
        s = delivery_status(self.d)
        self.assertEqual(s["staged"], 1)
        self.assertEqual(s["sent"], 0)


class EmailConfigTests(unittest.TestCase):
    def test_missing_vars_raise_and_name_them(self):
        with self.assertRaisesRegex(DeliveryError, "SMTP_HOST"):
            EmailConfig.from_env({"SMTP_USER": "u", "SMTP_PASSWORD": "p"})

    def test_defaults_and_from_fallback_to_business_email(self):
        c = EmailConfig.from_env({
            "SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
            "BUSINESS_EMAIL": "biz@x.example"})
        self.assertEqual(c.port, 587)
        self.assertTrue(c.starttls)
        self.assertEqual(c.sender, "biz@x.example")


class CliTests(unittest.TestCase):
    def setUp(self):
        self._t = TemporaryDirectory()
        self.d = Path(self._t.name)
        self.addCleanup(self._t.cleanup)
        _seed(self.d)

    def test_plan_deliver_stages_via_cli(self):
        from revenue_os.cli import main
        rc = main(["plan-deliver", "ORD1", "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.d / "deliverables" / "cand" / "plan-ORD1.pdf").is_file())

    def test_plan_deliver_status_via_cli(self):
        from revenue_os.cli import main
        stage_delivery(self.d, "ORD1")
        rc = main(["plan-deliver", "ORD1", "--status", "--data-dir", str(self.d)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
