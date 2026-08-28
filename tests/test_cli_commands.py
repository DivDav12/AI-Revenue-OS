import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.revenue import RevenueLedger
from revenue_os.store import CandidateStore


def _run(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliCommandTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name
        _run(["run", "--source", "static", "--data-dir", self.data])
        self.name = CandidateStore.load(Path(self.data) / "candidates.json").all()[0].name

    def tearDown(self):
        self._dir.cleanup()

    def _status(self, name=None) -> str:
        store = CandidateStore.load(Path(self.data) / "candidates.json")
        return store.get(name or self.name).status

    def _cli(self, *args) -> tuple[int, str, str]:
        return _run([*args, "--data-dir", self.data])

    def test_approve_and_reject(self):
        code, out, _ = self._cli("approve", self.name, "--note", "looks good")
        self.assertEqual(code, 0)
        self.assertIn("approved", out)
        self.assertEqual(self._status(), "approved")

        other = [
            c.name
            for c in CandidateStore.load(Path(self.data) / "candidates.json").all()
            if c.status == "shortlisted"
        ][0]
        code, _, _ = self._cli("reject", other)
        self.assertEqual(code, 0)
        self.assertEqual(self._status(other), "rejected")

    def test_investigate_advances_approved(self):
        self._cli("approve", self.name)
        code, out, _ = self._cli("investigate")
        self.assertEqual(code, 0)
        self.assertIn("1 candidate", out)
        self.assertEqual(self._status(), "investigating")

    def test_outcome_validated_and_wrong_state(self):
        self._cli("approve", self.name)
        # wrong state: still "approved", not "investigating"
        code, _, err = self._cli(
            "outcome", self.name, "validated", "--metric", "x"
        )
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

        self._cli("investigate")
        code, out, _ = self._cli(
            "outcome", self.name, "validated", "--metric", "27 signups"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._status(), "validated")

    def test_prepare_launch_launch_payment(self):
        self._cli("approve", self.name)
        self._cli("investigate")
        self._cli("outcome", self.name, "validated", "--metric", "27 signups")

        code, out, _ = self._cli("prepare-launch")
        self.assertEqual(code, 0)
        self.assertIn("1 validated candidate", out)

        code, _, _ = self._cli("launch", self.name)
        self.assertEqual(code, 0)
        self.assertEqual(self._status(), "launched")

        # payment on wrong state first (a still-shortlisted one)
        other = [
            c.name
            for c in CandidateStore.load(Path(self.data) / "candidates.json").all()
            if c.status == "shortlisted"
        ][0]
        code, _, err = self._cli("payment", other, "10")
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

        code, out, _ = self._cli("payment", self.name, "29")
        self.assertEqual(code, 0)
        self.assertEqual(self._status(), "earning")
        ledger = RevenueLedger.load(Path(self.data) / "revenue.json")
        self.assertEqual(ledger.total_for(self.name), 29.0)

    def test_candidate_detail_and_unknown(self):
        code, out, _ = self._cli("candidate", self.name)
        self.assertEqual(code, 0)
        self.assertIn(f"CANDIDATE {self.name}", out)
        self.assertIn("status", out)
        self.assertIn("plan", out)

        code, out, err = self._cli("candidate", "no-such-candidate")
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_unknown_candidate_on_decision_command(self):
        code, out, err = self._cli("approve", "no-such-candidate")
        self.assertEqual(code, 1)
        self.assertIn("error: unknown candidate", err)
        self.assertNotIn("Traceback", err)


class CliFullWorkflowTests(unittest.TestCase):
    def test_operate_pipeline_entirely_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            def run(*args):
                return _run([*args, "--data-dir", d])

            self.assertEqual(run("run", "--source", "static")[0], 0)
            name = CandidateStore.load(Path(d) / "candidates.json").all()[0].name

            self.assertEqual(run("approve", name)[0], 0)
            self.assertEqual(run("investigate")[0], 0)
            self.assertEqual(
                run("outcome", name, "validated", "--metric", "27 signups")[0], 0
            )
            self.assertEqual(run("prepare-launch")[0], 0)
            self.assertEqual(run("launch", name)[0], 0)
            self.assertEqual(run("payment", name, "29")[0], 0)

            code, out, _ = run("report")
            self.assertEqual(code, 0)
            self.assertIn("earning", out)
            self.assertIn("revenue       29.0", out)


if __name__ == "__main__":
    unittest.main()
