import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.spend import SpendLedger
from revenue_os.store import CandidateStore


def _run(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliSpendTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name
        _run(["run", "--source", "static", "--data-dir", self.data])
        self.name = CandidateStore.load(
            Path(self.data) / "candidates.json"
        ).all()[0].name

    def tearDown(self):
        self._dir.cleanup()

    def _cli(self, *args) -> tuple[int, str, str]:
        return _run([*args, "--data-dir", self.data])

    def _ledger(self) -> SpendLedger:
        return SpendLedger.load(Path(self.data) / "spend.json")

    def _assert_clean_error(self, code, out, err):
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_budget_sets_cap_and_rejects_negative(self):
        code, out, _ = self._cli("budget", self.name, "20")
        self.assertEqual(code, 0)
        self.assertIn("-> 20.0", out)
        self.assertEqual(self._ledger().budget_for(self.name), 20.0)

        code, out, err = self._cli("budget", self.name, "-5")
        self._assert_clean_error(code, out, err)

    def test_authorize_spend_ceiling_and_budget_rules(self):
        self._cli("budget", self.name, "20")

        # default ceiling (0.0) blocks any positive amount
        code, out, err = self._cli(
            "authorize-spend", self.name, "10", "--purpose", "domain"
        )
        self._assert_clean_error(code, out, err)

        # explicit ceiling + within budget succeeds
        code, out, _ = self._cli(
            "authorize-spend", self.name, "15", "--purpose", "domain", "--ceiling", "20"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._ledger().authorized_for(self.name), 15.0)

        # second authorization exceeds remaining budget
        code, out, err = self._cli(
            "authorize-spend", self.name, "10", "--purpose", "hosting", "--ceiling", "20"
        )
        self._assert_clean_error(code, out, err)

    def test_deny_spend_records_and_grants_nothing(self):
        self._cli("budget", self.name, "50")
        code, out, _ = self._cli(
            "deny-spend", self.name, "20", "--purpose", "ads", "--reason", "too early"
        )
        self.assertEqual(code, 0)
        self.assertIn("denied", out)
        self.assertEqual(self._ledger().authorized_for(self.name), 0.0)
        self.assertTrue(
            any(e["type"] == "denied" for e in self._ledger().entries())
        )

    def test_record_spend_within_and_over_authorized(self):
        self._cli("budget", self.name, "50")
        self._cli(
            "authorize-spend", self.name, "30", "--purpose", "domain", "--ceiling", "50"
        )
        code, out, _ = self._cli("record-spend", self.name, "12")
        self.assertEqual(code, 0)
        self.assertIn("spent", out)
        self.assertEqual(self._ledger().spent_for(self.name), 12.0)

        code, out, err = self._cli("record-spend", self.name, "25")
        self._assert_clean_error(code, out, err)

    def test_unknown_candidate_on_every_spend_command(self):
        for args in (
            ("budget", "nope", "10"),
            ("authorize-spend", "nope", "10", "--purpose", "p", "--ceiling", "20"),
            ("deny-spend", "nope", "10", "--purpose", "p", "--reason", "r"),
            ("record-spend", "nope", "10"),
        ):
            code, out, err = self._cli(*args)
            self._assert_clean_error(code, out, err)
            self.assertIn("unknown candidate", err)


class CliFullSpendFlowTests(unittest.TestCase):
    def test_full_revenue_and_cost_loop_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            def run(*args):
                return _run([*args, "--data-dir", d])

            run("run", "--source", "static")
            name = CandidateStore.load(Path(d) / "candidates.json").all()[0].name
            run("approve", name)
            run("investigate")
            run("outcome", name, "validated", "--metric", "27 signups")
            run("prepare-launch")
            run("launch", name)
            run("payment", name, "29")

            self.assertEqual(run("budget", name, "15")[0], 0)
            self.assertEqual(
                run(
                    "authorize-spend", name, "12",
                    "--purpose", "domain", "--ceiling", "20",
                )[0],
                0,
            )
            self.assertEqual(run("record-spend", name, "12")[0], 0)

            code, out, _ = run("report")
            self.assertEqual(code, 0)
            self.assertIn("spent         12.0", out)
            self.assertIn("net           17.0", out)


if __name__ == "__main__":
    unittest.main()
