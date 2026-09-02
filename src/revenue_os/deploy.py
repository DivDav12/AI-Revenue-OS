"""Static-site deployment for the generated checkout / intake pages.

`build-checkout` writes `deliverables/<candidate>/checkout.html` +
`intake.html` to disk. This module publishes those files to **GitHub
Pages** via the Contents API and records the resulting public URL on the
candidate as `public_url` - which `outreach.resolve_checkout_url()` then
uses as the real checkout link (the `outreach.DEFAULT_CHECKOUT_URL`
constant is only a last-resort fallback).

Why GitHub Pages: free, a stable URL, a plain REST API (no CLI, no
build step), standard-library HTTP only, and idempotent (unchanged
files are skipped). It publishes the operator's own static content -
no code execution, no secrets on the page.

Credentials come from the environment (loaded from .env by the CLI),
never from code or the store:
  GITHUB_TOKEN           fine-grained PAT, Contents: read+write on the repo
  GITHUB_PAGES_REPO      "<owner>/<repo>" (e.g. DivDav12/AI-Revenue-OS)
  GITHUB_PAGES_BRANCH    branch to commit to        (default: main)
  GITHUB_PAGES_SUBDIR    path prefix inside the repo (default: "" = repo root)

Nothing here spends money, sends a message, or touches PayPal.
The token is never written to a log or an exception message.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dataclasses import replace as _replace

from .store import CandidateStore, now_iso

logger = logging.getLogger(__name__)

_API = "https://api.github.com"
_TIMEOUT = 20
_PUBLISHED = ("checkout.html", "intake.html")


class DeployError(RuntimeError):
    """A deploy could not be completed. Message never contains the token."""


@dataclass
class GitHubPagesConfig:
    token: str
    owner: str
    repo: str
    branch: str = "main"
    subdir: str = ""

    @classmethod
    def from_env(cls, environ=None) -> "GitHubPagesConfig":
        env = environ if environ is not None else os.environ
        token = (env.get("GITHUB_TOKEN") or "").strip()
        repo_spec = (env.get("GITHUB_PAGES_REPO") or "").strip()
        branch = (env.get("GITHUB_PAGES_BRANCH") or "main").strip() or "main"
        subdir = (env.get("GITHUB_PAGES_SUBDIR") or "").strip().strip("/")
        if not token or not repo_spec:
            raise DeployError(
                "set GITHUB_TOKEN and GITHUB_PAGES_REPO ('<owner>/<repo>') in the "
                "environment (.env) to deploy the checkout page")
        if "/" not in repo_spec:
            raise DeployError("GITHUB_PAGES_REPO must be '<owner>/<repo>'")
        owner, repo = repo_spec.split("/", 1)
        if not owner or not repo:
            raise DeployError("GITHUB_PAGES_REPO must be '<owner>/<repo>'")
        return cls(token=token, owner=owner.strip(), repo=repo.strip(),
                   branch=branch, subdir=subdir)

    # --- pure URL helpers ---------------------------------------------
    def repo_path(self, filename: str) -> str:
        return f"{self.subdir}/{filename}".lstrip("/") if self.subdir else filename

    def public_base(self) -> str:
        if self.repo.lower() == f"{self.owner.lower()}.github.io":
            base = f"https://{self.owner}.github.io"
        else:
            base = f"https://{self.owner}.github.io/{self.repo}"
        return f"{base}/{self.subdir}".rstrip("/") if self.subdir else base

    def public_url(self, filename: str) -> str:
        return f"{self.public_base()}/{filename}"


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


@dataclass
class GitHubClient:
    """Thin Contents-API wrapper. Injectable in tests."""

    config: GitHubPagesConfig

    def _request(self, method: str, path: str, *, body: dict | None = None):
        url = f"{_API}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "revenue-os-deploy",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 404:
                return 404, {}
            raise DeployError(
                f"GitHub API {exc.code}: {_redact(payload, self.config.token)}"
            ) from None
        except urllib.error.URLError as exc:
            raise DeployError(f"GitHub API unreachable: {exc.reason}") from None

    def get_file(self, repo_path: str) -> dict | None:
        """Return {'sha','content_bytes'} for an existing file, else None."""
        q = urllib.parse.quote(repo_path)
        status, body = self._request(
            "GET", f"/repos/{self.config.owner}/{self.config.repo}/contents/{q}"
            f"?ref={urllib.parse.quote(self.config.branch)}")
        if status == 404 or not body:
            return None
        raw = base64.b64decode((body.get("content") or "").encode("ascii"))
        return {"sha": body.get("sha", ""), "content_bytes": raw}

    def put_file(self, repo_path: str, content: bytes, *, message: str,
                 sha: str | None = None) -> dict:
        q = urllib.parse.quote(repo_path)
        body = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.config.branch,
        }
        if sha:
            body["sha"] = sha
        status, resp = self._request(
            "PUT", f"/repos/{self.config.owner}/{self.config.repo}/contents/{q}",
            body=body)
        if status not in (200, 201):
            raise DeployError(f"GitHub PUT returned {status} for {repo_path}")
        return resp


def deploy_files(config: GitHubPagesConfig, files: dict[str, bytes], *,
                 client: GitHubClient | None = None,
                 message: str | None = None) -> dict:
    """Publish {filename: bytes}. Idempotent: a file whose current content
    already matches is left untouched. Returns a per-file summary."""
    client = client or GitHubClient(config)
    msg = message or f"deploy checkout pages ({now_iso()})"
    deployed: list[str] = []
    unchanged: list[str] = []
    urls: dict[str, str] = {}
    for filename, content in files.items():
        repo_path = config.repo_path(filename)
        existing = client.get_file(repo_path)
        if existing is not None and existing["content_bytes"] == content:
            unchanged.append(filename)
        else:
            client.put_file(repo_path, content, message=msg,
                            sha=(existing or {}).get("sha") or None)
            deployed.append(filename)
        urls[filename] = config.public_url(filename)
    return {"deployed": deployed, "unchanged": unchanged, "urls": urls,
            "branch": config.branch, "repo": f"{config.owner}/{config.repo}"}


def deploy_checkout(data_dir, candidate_name: str, *,
                    client: GitHubClient | None = None,
                    config: GitHubPagesConfig | None = None) -> dict:
    """Publish deliverables/<candidate>/{checkout,intake}.html to GitHub
    Pages and persist the live checkout URL on the candidate."""
    from .action_class import guard_no_money_in_autonomy
    guard_no_money_in_autonomy("activate a paid checkout page")

    data_dir = Path(data_dir)
    store = CandidateStore.load(data_dir / "candidates.json")
    cand = store.get(candidate_name)
    if cand is None:
        raise DeployError(f"unknown candidate: {candidate_name!r}")

    page_dir = data_dir / "deliverables" / candidate_name
    checkout = page_dir / "checkout.html"
    if not checkout.is_file():
        raise DeployError(
            f"{checkout} not found - run `revenue_os build-checkout "
            f"{candidate_name} --price ...` first")

    cfg = config or GitHubPagesConfig.from_env()
    files: dict[str, bytes] = {"checkout.html": checkout.read_bytes()}
    intake = page_dir / "intake.html"
    if intake.is_file():
        files["intake.html"] = intake.read_bytes()

    result = deploy_files(cfg, files, client=client,
                          message=f"deploy {candidate_name} checkout ({now_iso()})")
    public_url = result["urls"]["checkout.html"]

    store.put(_replace(cand, public_url=public_url))
    store.save()

    result["candidate"] = candidate_name
    result["public_url"] = public_url
    result["intake_url"] = result["urls"].get("intake.html")
    result["persisted"] = True
    logger.info("deployed %s: %d updated, %d unchanged -> %s",
                candidate_name, len(result["deployed"]),
                len(result["unchanged"]), public_url)
    return result


def deploy_status(data_dir, candidate_name: str) -> dict:
    store = CandidateStore.load(Path(data_dir) / "candidates.json")
    cand = store.get(candidate_name)
    if cand is None:
        raise DeployError(f"unknown candidate: {candidate_name!r}")
    page_dir = Path(data_dir) / "deliverables" / candidate_name
    return {
        "candidate": candidate_name,
        "public_url": cand.public_url or None,
        "checkout_built": (page_dir / "checkout.html").is_file(),
        "intake_built": (page_dir / "intake.html").is_file(),
        "deployed": bool(cand.public_url),
    }
