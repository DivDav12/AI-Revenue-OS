import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revenue_os import cli
from revenue_os.store import Candidate, CandidateStore


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class DigestTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name
        self.store = CandidateStore(Path(self.data) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def _save(self, *cands):
        for c in cands:
            self.store.put(c)
        self.store.save()

    def test_empty_queue(self):
        code, out = _run(["digest", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("nothing awaiting a human", out)

    def test_quiet_empty_exit_0(self):
        code, out = _run(["digest", "-q", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_groups_by_next_action(self):
        self._save(
            Candidate(name="a", status="shortlisted"),
            Candidate(name="b", status="shortlisted"),
            Candidate(name="c", status="validated"),
        )
        code, out = _run(["digest", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("2 approve or reject", out)
        self.assertIn("1 launch offer", out)
        self.assertIn("|", out)

    def test_quiet_nonempty_exit_1(self):
        self._save(Candidate(name="a", status="shortlisted"))
        code, out = _run(["digest", "-q", "--data-dir", self.data])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")

    def test_stale_suffix(self):
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        self._save(Candidate(
            name="stuck", status="shortlisted",
            history=({"ts": old, "from": "discovered", "to": "shortlisted"},),
        ))
        code, out = _run(["digest", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertIn("(1 stale)", out)


if __name__ == "__main__":
    unittest.main()
