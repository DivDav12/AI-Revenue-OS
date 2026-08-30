import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revenue_os import cli
from revenue_os.blockers import BlockerStore, load_blockers


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class BlockerStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = Path(self._dir.name)
        self.path = self.d / "blockers.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_file_loads_empty(self):
        store = BlockerStore.load(self.path)
        self.assertEqual(store.all(), [])
        self.assertEqual(store.open(), [])
        self.assertEqual(len(store), 0)

    def test_add_save_and_reload_roundtrip(self):
        store = BlockerStore.load(self.path)
        store.add("paypal", "PayPal blocked", area="payment",
                  detail="PAYEE_ACCOUNT_RESTRICTED", severity="critical")
        store.save()
        again = load_blockers(self.d)
        self.assertEqual(len(again), 1)
        entry = again.get("paypal")
        self.assertEqual(entry["title"], "PayPal blocked")
        self.assertEqual(entry["severity"], "critical")
        self.assertEqual(entry["status"], "open")
        self.assertEqual(entry["owner"], "human")
        self.assertIsNone(entry["resolved_at"])

    def test_resolve_marks_resolved_and_drops_out_of_open(self):
        store = BlockerStore.load(self.path)
        store.add("x", "t")
        store.resolve("x")
        store.save()
        again = load_blockers(self.d)
        self.assertEqual(again.open(), [])
        self.assertEqual(len(again.all()), 1)
        self.assertEqual(again.get("x")["status"], "resolved")
        self.assertIsNotNone(again.get("x")["resolved_at"])

    def test_resolve_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            BlockerStore.load(self.path).resolve("nope")

    def test_readding_updates_in_place_and_keeps_opened_at(self):
        store = BlockerStore.load(self.path)
        first = store.add("x", "old title")
        store.resolve("x")
        again = store.add("x", "new title", severity="info")
        self.assertEqual(len(store), 1)
        self.assertEqual(again["title"], "new title")
        self.assertEqual(again["status"], "open")          # re-adding reopens
        self.assertEqual(again["opened_at"], first["opened_at"])

    def test_open_is_ordered_by_severity(self):
        store = BlockerStore.load(self.path)
        store.add("c", "c", severity="info")
        store.add("a", "a", severity="critical")
        store.add("b", "b", severity="warning")
        self.assertEqual([e["id"] for e in store.open()], ["a", "b", "c"])

    def test_bad_severity_is_refused(self):
        with self.assertRaises(ValueError):
            BlockerStore.load(self.path).add("x", "t", severity="apocalyptic")

    def test_corrupt_file_raises_a_clear_error(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            BlockerStore.load(self.path)

    def test_non_list_file_is_refused(self):
        self.path.write_text(json.dumps({"id": "x"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            BlockerStore.load(self.path)


class BlockerCliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_list_empty_is_honest(self):
        code, out = _run(["blockers", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("no open blockers", out)

    def test_add_list_resolve_cycle(self):
        code, _ = _run(["blockers", "add", "paypal", "--title",
                        "PayPal checkout blocked", "--detail",
                        "PAYEE_ACCOUNT_RESTRICTED", "--area", "payment",
                        "--severity", "critical", "--data-dir", self.data])
        self.assertEqual(code, 0)
        code, out = _run(["blockers", "--data-dir", self.data])
        self.assertIn("paypal", out)
        self.assertIn("critical", out)
        self.assertIn("PAYEE_ACCOUNT_RESTRICTED", out)
        _run(["blockers", "resolve", "paypal", "--data-dir", self.data])
        _, out = _run(["blockers", "--data-dir", self.data])
        self.assertIn("no open blockers", out)
        _, out = _run(["blockers", "--all", "--data-dir", self.data])
        self.assertIn("resolved", out)

    def test_add_without_title_is_refused(self):
        code, out = _run(["blockers", "add", "x", "--data-dir", self.data])
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)
        self.assertFalse((Path(self.data) / "blockers.json").exists())

    def test_resolve_unknown_id_reports_and_fails(self):
        code, out = _run(["blockers", "resolve", "nope", "--data-dir", self.data])
        self.assertEqual(code, 1)
        self.assertIn("nope", out)

    def test_dashboard_renders_the_recorded_blocker(self):
        _run(["blockers", "add", "paypal", "--title", "PayPal checkout blocked",
              "--detail", "PAYEE_ACCOUNT_RESTRICTED on the live payment path",
              "--area", "payment", "--severity", "critical",
              "--data-dir", self.data])
        code, _ = _run(["dashboard", "--data-dir", self.data])
        self.assertEqual(code, 0)
        html = (Path(self.data) / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("PayPal checkout blocked", html)
        self.assertIn("PAYEE_ACCOUNT_RESTRICTED", html)
        self.assertIn("1 open blocker(s)", html)
        self.assertNotIn("://", html)

    def test_dashboard_without_a_register_says_so(self):
        _run(["dashboard", "--data-dir", self.data])
        html = (Path(self.data) / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("No blocker register", html)


if __name__ == "__main__":
    unittest.main()
