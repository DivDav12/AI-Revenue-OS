import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from revenue_os.deploy import (
    DeployError,
    GitHubClient,
    GitHubPagesConfig,
    deploy_checkout,
    deploy_files,
    deploy_status,
)
from revenue_os.store import Candidate, CandidateStore


class _FakeGitHub(GitHubClient):
    """In-memory Contents API. Records every write; no network."""

    def __init__(self, config, seed=None):
        self.config = config
        self.files = dict(seed or {})           # repo_path -> bytes
        self.puts = []                          # (repo_path, bytes, sha)

    def get_file(self, repo_path):
        if repo_path not in self.files:
            return None
        return {"sha": f"sha-{repo_path}", "content_bytes": self.files[repo_path]}

    def put_file(self, repo_path, content, *, message, sha=None):
        self.puts.append((repo_path, content, sha))
        self.files[repo_path] = content
        return {"content": {"path": repo_path}}


CFG = GitHubPagesConfig(token="secret-tok", owner="divdav12",
                        repo="customer-launch-plan", branch="main")


class ConfigTests(unittest.TestCase):
    def test_from_env_requires_token_and_repo(self):
        with self.assertRaises(DeployError):
            GitHubPagesConfig.from_env({"GITHUB_TOKEN": "x"})
        with self.assertRaises(DeployError):
            GitHubPagesConfig.from_env(
                {"GITHUB_TOKEN": "x", "GITHUB_PAGES_REPO": "no-slash"})

    def test_from_env_parses(self):
        c = GitHubPagesConfig.from_env({
            "GITHUB_TOKEN": "t", "GITHUB_PAGES_REPO": "me/site",
            "GITHUB_PAGES_BRANCH": "gh-pages", "GITHUB_PAGES_SUBDIR": "/plan/"})
        self.assertEqual((c.owner, c.repo, c.branch, c.subdir),
                         ("me", "site", "gh-pages", "plan"))

    def test_public_url_project_pages(self):
        self.assertEqual(
            CFG.public_url("checkout.html"),
            "https://divdav12.github.io/customer-launch-plan/checkout.html")

    def test_public_url_user_pages_and_subdir(self):
        c = GitHubPagesConfig(token="t", owner="me", repo="me.github.io",
                              subdir="shop")
        self.assertEqual(c.public_url("checkout.html"),
                         "https://me.github.io/shop/checkout.html")
        self.assertEqual(c.repo_path("checkout.html"), "shop/checkout.html")


class DeployFilesTests(unittest.TestCase):
    def test_new_files_are_put(self):
        gh = _FakeGitHub(CFG)
        r = deploy_files(CFG, {"checkout.html": b"<html>1</html>"}, client=gh)
        self.assertEqual(r["deployed"], ["checkout.html"])
        self.assertEqual(r["unchanged"], [])
        self.assertEqual(gh.puts[0][0], "checkout.html")
        self.assertIsNone(gh.puts[0][2])   # no sha on create

    def test_unchanged_file_is_skipped(self):
        gh = _FakeGitHub(CFG, seed={"checkout.html": b"same"})
        r = deploy_files(CFG, {"checkout.html": b"same"}, client=gh)
        self.assertEqual(r["unchanged"], ["checkout.html"])
        self.assertEqual(gh.puts, [])

    def test_changed_file_is_put_with_sha(self):
        gh = _FakeGitHub(CFG, seed={"checkout.html": b"old"})
        deploy_files(CFG, {"checkout.html": b"new"}, client=gh)
        self.assertEqual(gh.puts[0][2], "sha-checkout.html")
        self.assertEqual(gh.files["checkout.html"], b"new")

    def test_http_error_message_redacts_token(self):
        import io
        import urllib.error
        import urllib.request

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                "u", 422, "bad", {}, io.BytesIO(b"body mentions secret-tok here"))

        orig = urllib.request.urlopen
        urllib.request.urlopen = boom
        try:
            with self.assertRaises(DeployError) as ctx:
                GitHubClient(CFG).put_file("x", b"y", message="m")
        finally:
            urllib.request.urlopen = orig
        self.assertIn("422", str(ctx.exception))
        self.assertNotIn("secret-tok", str(ctx.exception))


class DeployCheckoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.d / "deliverables" / "cand").mkdir(parents=True)
        store = CandidateStore(self.d / "candidates.json")
        store.put(Candidate(name="cand", description="d", status="launched",
                            offer={"price": 29.9, "currency": "EUR"}))
        store.save()

    def _write_pages(self, checkout=b"<html>checkout</html>", intake=b"<html>intake</html>"):
        base = self.d / "deliverables" / "cand"
        (base / "checkout.html").write_bytes(checkout)
        if intake is not None:
            (base / "intake.html").write_bytes(intake)

    def test_missing_checkout_html_is_a_clear_error(self):
        gh = _FakeGitHub(CFG)
        with self.assertRaises(DeployError) as ctx:
            deploy_checkout(self.d, "cand", client=gh, config=CFG)
        self.assertIn("build-checkout", str(ctx.exception))

    def test_deploy_persists_public_url_on_candidate(self):
        self._write_pages()
        gh = _FakeGitHub(CFG)
        r = deploy_checkout(self.d, "cand", client=gh, config=CFG)
        self.assertEqual(
            r["public_url"],
            "https://divdav12.github.io/customer-launch-plan/checkout.html")
        self.assertEqual(sorted(r["deployed"]), ["checkout.html", "intake.html"])
        cand = CandidateStore.load(self.d / "candidates.json").get("cand")
        self.assertEqual(cand.public_url, r["public_url"])

    def test_deploy_is_idempotent(self):
        self._write_pages()
        gh = _FakeGitHub(CFG)
        deploy_checkout(self.d, "cand", client=gh, config=CFG)
        n_first = len(gh.puts)
        r2 = deploy_checkout(self.d, "cand", client=gh, config=CFG)
        self.assertEqual(len(gh.puts), n_first)          # nothing re-uploaded
        self.assertEqual(sorted(r2["unchanged"]), ["checkout.html", "intake.html"])

    def test_deploy_status(self):
        r = deploy_status(self.d, "cand")
        self.assertFalse(r["checkout_built"])
        self.assertFalse(r["deployed"])
        self._write_pages()
        gh = _FakeGitHub(CFG)
        deploy_checkout(self.d, "cand", client=gh, config=CFG)
        r2 = deploy_status(self.d, "cand")
        self.assertTrue(r2["checkout_built"])
        self.assertTrue(r2["deployed"])
        self.assertTrue(r2["public_url"].endswith("checkout.html"))

    def test_unknown_candidate(self):
        with self.assertRaises(DeployError):
            deploy_checkout(self.d, "nope", client=_FakeGitHub(CFG), config=CFG)


if __name__ == "__main__":
    unittest.main()
