import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from revenue_os.envfile import load_env, find_env_file


class LoadEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.env_path = self.dir / ".env"
        self.addCleanup(self._tmp.cleanup)

    def test_parses_pairs_comments_quotes_and_export(self):
        self.env_path.write_text(
            "# a comment\n"
            "\n"
            "PAYPAL_CLIENT_ID=abc123\n"
            'PAYPAL_CLIENT_SECRET="s e c r e t"\n'
            "export PAYPAL_ENV=live\n"
            "PLAIN=value # trailing comment\n"
            "QUOTED='keep # this'\n",
            encoding="utf-8",
        )
        target: dict[str, str] = {}
        keys = load_env(self.env_path, environ=target)
        self.assertEqual(
            sorted(keys),
            ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_ENV", "PLAIN", "QUOTED"],
        )
        self.assertEqual(target["PAYPAL_CLIENT_ID"], "abc123")
        self.assertEqual(target["PAYPAL_CLIENT_SECRET"], "s e c r e t")
        self.assertEqual(target["PAYPAL_ENV"], "live")
        self.assertEqual(target["PLAIN"], "value")
        self.assertEqual(target["QUOTED"], "keep # this")

    def test_real_environment_wins_unless_override(self):
        self.env_path.write_text("K=from_file\n", encoding="utf-8")
        target = {"K": "from_shell"}
        load_env(self.env_path, environ=target)
        self.assertEqual(target["K"], "from_shell")
        load_env(self.env_path, environ=target, override=True)
        self.assertEqual(target["K"], "from_file")

    def test_empty_value_is_treated_as_unset(self):
        self.env_path.write_text("K=real\n", encoding="utf-8")
        target = {"K": ""}
        load_env(self.env_path, environ=target)
        self.assertEqual(target["K"], "real")

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(load_env(self.dir / "nope.env", environ={}), [])

    def test_returns_names_never_values(self):
        self.env_path.write_text("SECRET=supersecret\n", encoding="utf-8")
        keys = load_env(self.env_path, environ={})
        self.assertEqual(keys, ["SECRET"])
        self.assertNotIn("supersecret", " ".join(keys))

    def test_find_env_file_prefers_explicit(self):
        self.env_path.write_text("A=1\n", encoding="utf-8")
        self.assertEqual(find_env_file(self.env_path), self.env_path)

    def test_find_env_file_honours_override_var(self):
        self.env_path.write_text("A=1\n", encoding="utf-8")
        old = os.environ.get("REVENUE_OS_ENV_FILE")
        os.environ["REVENUE_OS_ENV_FILE"] = str(self.env_path)
        try:
            self.assertEqual(find_env_file(), self.env_path)
        finally:
            if old is None:
                os.environ.pop("REVENUE_OS_ENV_FILE", None)
            else:
                os.environ["REVENUE_OS_ENV_FILE"] = old


class GitignoreTests(unittest.TestCase):
    def test_env_and_data_are_gitignored(self):
        root = Path(__file__).resolve().parents[1]
        ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", ignored)
        self.assertIn("data/", ignored)


if __name__ == "__main__":
    unittest.main()
