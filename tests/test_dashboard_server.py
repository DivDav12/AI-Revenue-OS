import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from revenue_os.dashboard_server import (
    _LOOPBACK,
    _make_handler,
    apply_action,
    serve,
)
from revenue_os.revenue import RevenueLedger
from revenue_os.store import Candidate, CandidateStore


def _f(**kw):
    return {k: [v] for k, v in kw.items()}


class ApplyActionTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.store = CandidateStore(self.d / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def _reload(self, name):
        return CandidateStore.load(self.d / "candidates.json").get(name)

    def test_approve_and_reject_go_through_record_decision(self):
        self.store.put(Candidate(name="a", status="shortlisted"))
        self.store.put(Candidate(name="b", status="shortlisted"))
        self.store.save()
        self.assertEqual(apply_action(self.d, "t", _f(action="approve", name="a")),
                         "ok: a -> approved")
        self.assertEqual(self._reload("a").status, "approved")
        apply_action(self.d, "t", _f(action="reject", name="b"))
        self.assertEqual(self._reload("b").status, "rejected")

    def test_outcome_requires_a_valid_result(self):
        self.store.put(Candidate(name="c", status="investigating"))
        self.store.save()
        self.assertIn("error", apply_action(
            self.d, "t", _f(action="outcome", name="c", result="maybe")))
        self.assertEqual(self._reload("c").status, "investigating")
        apply_action(self.d, "t",
                     _f(action="outcome", name="c", result="validated", metric="ok"))
        got = self._reload("c")
        self.assertEqual(got.status, "validated")
        self.assertEqual(got.outcome["metric_value"], "ok")

    def test_launch_then_payment_advances_and_records(self):
        self.store.put(Candidate(name="d", status="validated", offer={"price": 9.0}))
        self.store.save()
        apply_action(self.d, "t", _f(action="launch", name="d"))
        self.assertEqual(self._reload("d").status, "launched")
        apply_action(self.d, "t", _f(action="payment", name="d", amount="150"))
        self.assertEqual(self._reload("d").status, "earning")
        self.assertEqual(
            RevenueLedger.load(self.d / "revenue.json").total_for("d"), 150.0)

    def test_payment_rejects_non_positive_and_non_numeric(self):
        self.store.put(Candidate(name="e", status="launched"))
        self.store.save()
        self.assertIn("error", apply_action(
            self.d, "t", _f(action="payment", name="e", amount="0")))
        self.assertIn("error", apply_action(
            self.d, "t", _f(action="payment", name="e", amount="abc")))
        self.assertEqual(RevenueLedger.load(self.d / "revenue.json").total(), 0.0)

    def test_illegal_transition_is_a_flash_error_not_a_mutation(self):
        self.store.put(Candidate(name="f", status="discovered"))
        self.store.save()
        msg = apply_action(self.d, "t", _f(action="launch", name="f"))
        self.assertTrue(msg.startswith("error"))
        self.assertEqual(self._reload("f").status, "discovered")

    def test_unknown_action_and_missing_name(self):
        self.assertIn("unknown action",
                      apply_action(self.d, "t", _f(action="delete", name="x")))
        self.assertIn("missing candidate",
                      apply_action(self.d, "t", _f(action="approve")))

    def test_unknown_candidate(self):
        self.assertIn("error",
                      apply_action(self.d, "t", _f(action="approve", name="nope")))


class ServeGuardTests(unittest.TestCase):
    def test_non_loopback_host_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                serve(d, host="0.0.0.0")
        self.assertEqual(set(_LOOPBACK), {"127.0.0.1", "::1", "localhost"})


class HttpTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        store = CandidateStore(self.d / "candidates.json")
        store.put(Candidate(name="alpha", status="shortlisted", total=3.0))
        store.save()
        self.csrf = "TESTTOKEN"
        from revenue_os.cli import build_dashboard_html
        handler = _make_handler(
            self.d, "tester", self.csrf,
            lambda flash, csrf: build_dashboard_html(
                self.d, interactive=True, flash=flash, csrf=csrf),
            {f"127.0.0.1:0"},
        )
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv._flash = None
        self.port = self.srv.server_address[1]
        self._t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._dir.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port)

    def _post(self, body):
        c = self._conn()
        c.request("POST", "/action", body,
                  {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        return r

    def _reload(self, name):
        return CandidateStore.load(self.d / "candidates.json").get(name)

    def test_get_renders_interactive_forms(self):
        c = self._conn()
        c.request("GET", "/")
        r = c.getresponse()
        body = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn("action='/action'", body)
        self.assertIn(self.csrf, body)
        self.assertIn(">Approve<", body)
        self.assertIn(">Reject<", body)

    def test_unknown_path_is_404(self):
        c = self._conn()
        c.request("GET", "/secrets")
        self.assertEqual(c.getresponse().status, 404)

    def test_post_without_csrf_is_403_and_no_mutation(self):
        r = self._post("action=approve&name=alpha")
        self.assertEqual(r.status, 403)
        self.assertEqual(self._reload("alpha").status, "shortlisted")

    def test_post_with_bad_csrf_is_403(self):
        r = self._post("csrf=WRONG&action=approve&name=alpha")
        self.assertEqual(r.status, 403)
        self.assertEqual(self._reload("alpha").status, "shortlisted")

    def test_post_good_csrf_runs_the_gate_and_redirects(self):
        r = self._post(f"csrf={self.csrf}&action=approve&name=alpha")
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/")
        self.assertEqual(self._reload("alpha").status, "approved")
        # the flash shows on the next GET
        c = self._conn()
        c.request("GET", "/")
        self.assertIn("ok: alpha -&gt; approved", c.getresponse().read().decode())

    def test_non_allowlisted_action_redirects_with_error_and_no_mutation(self):
        r = self._post(f"csrf={self.csrf}&action=delete&name=alpha")
        self.assertEqual(r.status, 303)
        self.assertEqual(self._reload("alpha").status, "shortlisted")
        c = self._conn()
        c.request("GET", "/")
        self.assertIn("unknown action", c.getresponse().read().decode())

    def test_cross_origin_post_is_blocked(self):
        c = self._conn()
        c.request("POST", "/action", f"csrf={self.csrf}&action=approve&name=alpha",
                  {"Content-Type": "application/x-www-form-urlencoded",
                   "Origin": "http://evil.example"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 403)
        self.assertEqual(self._reload("alpha").status, "shortlisted")


if __name__ == "__main__":
    unittest.main()
