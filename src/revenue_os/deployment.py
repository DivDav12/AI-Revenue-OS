"""Deployment adapters - a clean seam between "a task wants to publish a
built page" and "how the page actually goes live".

  adapter.deploy(DeploymentArtifact) -> DeploymentResult

Two implementations:

  * FakeDeploymentAdapter      - deterministic, offline, for tests / dry runs
  * GitHubPagesDeploymentAdapter - wraps the EXISTING real path in deploy.py
                                   (Contents API -> GitHub Pages)

Fail-closed rules:

  * missing / invalid credentials         -> success=False, blocked=True
  * the deploy attempt failed             -> success=False, blocked=False
  * success but no usable URL             -> treated as a failure by callers
  * a live_url is only ever a URL the provider actually returned - never
    synthesised

Nothing here moves money, captures a payment, sends a customer message, or
posts to a social platform. Publishing a static page to the owner's own
GitHub Pages repo is EUR 0 and is still gated: the DEPLOY task is born
BLOCKED_APPROVAL and a human must release it before the worker runs it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .store import now_iso

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    s = _SLUG_RE.sub("-", str(value or "").lower()).strip("-")
    return s or "site"


def valid_live_url(url: str) -> bool:
    u = str(url or "").strip()
    return u.startswith(("http://", "https://")) and len(u) > len("https://") + 2


# ---------------------------------------------------------------------------

@dataclass
class DeploymentArtifact:
    opportunity_id: str
    slug: str
    files: dict                     # {filename: str | bytes}

    def as_bytes(self) -> dict:
        return {name: (c if isinstance(c, bytes) else str(c).encode("utf-8"))
                for name, c in self.files.items()}

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for name in sorted(self.as_bytes()):
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(self.as_bytes()[name])
            h.update(b"\0")
        return h.hexdigest()[:16]


@dataclass
class DeploymentResult:
    success: bool
    provider: str
    live_url: str = ""
    deployment_id: str = ""
    commit_sha: str = ""
    error: str = ""
    blocked: bool = False           # True = missing config / auth (fail-closed)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success, "provider": self.provider,
            "live_url": self.live_url, "deployment_id": self.deployment_id,
            "commit_sha": self.commit_sha, "error": self.error,
            "blocked": self.blocked, "details": dict(self.details),
        }


class DeploymentAdapter:
    provider = "base"

    #: Phase 6 - is this a deploy channel the owner explicitly authorized for
    #: automation? The Worker's classifier only lets an EXTERNAL_AUTHORIZED
    #: DEPLOY run unattended against an authorized adapter (or after a human
    #: release). Fail closed: default False.
    authorized = False

    def deploy(self, artifact: DeploymentArtifact) -> DeploymentResult:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# fake - deterministic, no network
# ---------------------------------------------------------------------------

class FakeDeploymentAdapter(DeploymentAdapter):
    provider = "fake"
    authorized = True   # an explicitly injected fake channel is authorized

    def __init__(self, *, base_url: str = "https://fake.pages.test",
                 fail: bool = False, blocked: bool = False,
                 error: str = "", drop_url: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.fail = fail
        self.blocked = blocked
        self.error = error
        self.drop_url = drop_url
        self._deployed: dict[str, DeploymentResult] = {}   # slug -> last result

    def deploy(self, artifact: DeploymentArtifact) -> DeploymentResult:
        if self.blocked:
            return DeploymentResult(
                success=False, blocked=True, provider=self.provider,
                error=self.error or "fake adapter: no credentials configured")
        if self.fail:
            return DeploymentResult(
                success=False, provider=self.provider,
                error=self.error or "fake adapter: deploy attempt failed")

        digest = artifact.content_hash()
        prior = self._deployed.get(artifact.slug)
        if prior is not None and prior.details.get("content_hash") == digest:
            return prior                              # idempotent: unchanged content

        # drop_url: provider claims success but returns no URL - callers must
        # still treat this as a failure and never fabricate a URL.
        url = "" if self.drop_url else f"{self.base_url}/{artifact.slug}/index.html"
        res = DeploymentResult(
            success=True, provider=self.provider, live_url=url,
            deployment_id=f"fake-{artifact.slug}-{digest[:8]}",
            commit_sha=digest,
            details={"content_hash": digest, "files": sorted(artifact.files),
                     "deployed_at": now_iso()})
        if not self.drop_url:
            self._deployed[artifact.slug] = res
        return res


# ---------------------------------------------------------------------------
# real - GitHub Pages via the existing deploy.py Contents-API path
# ---------------------------------------------------------------------------

class GitHubPagesDeploymentAdapter(DeploymentAdapter):
    provider = "github_pages"

    def __init__(self, *, config=None, client=None, environ=None) -> None:
        self._config = config
        self._client = client
        self._environ = environ

    @property
    def authorized(self) -> bool:
        """Authorized iff a valid GitHub Pages config resolves (an owner who
        set GITHUB_TOKEN + GITHUB_PAGES_REPO has explicitly authorized this
        channel). No network call - a pure env / config check."""
        from . import deploy as _deploy
        if self._config is not None:
            return True
        try:
            _deploy.GitHubPagesConfig.from_env(self._environ)
            return True
        except Exception:
            return False

    def deploy(self, artifact: DeploymentArtifact) -> DeploymentResult:
        from . import deploy as _deploy

        try:
            cfg = self._config or _deploy.GitHubPagesConfig.from_env(self._environ)
        except _deploy.DeployError as exc:
            return DeploymentResult(success=False, blocked=True,
                                    provider=self.provider, error=str(exc))

        files = {f"{artifact.slug}/{name}": data
                 for name, data in artifact.as_bytes().items()}
        try:
            res = _deploy.deploy_files(
                cfg, files, client=self._client,
                message=f"deploy {artifact.slug} ({now_iso()})")
        except _deploy.DeployError as exc:
            return DeploymentResult(success=False, provider=self.provider,
                                    error=str(exc))

        index_key = f"{artifact.slug}/index.html"
        live_url = res["urls"].get(index_key) or next(iter(res["urls"].values()), "")
        if not valid_live_url(live_url):
            return DeploymentResult(
                success=False, provider=self.provider,
                error="GitHub Pages deploy returned no usable URL",
                details={"deploy_result": {k: res[k] for k in
                                           ("deployed", "unchanged", "repo")}})
        return DeploymentResult(
            success=True, provider=self.provider, live_url=live_url,
            deployment_id=f"ghp:{cfg.owner}/{cfg.repo}:{artifact.slug}",
            commit_sha=res.get("commit_sha", ""),
            details={"deployed": res["deployed"], "unchanged": res["unchanged"],
                     "repo": res["repo"], "branch": res["branch"]})


def default_deployment_adapter() -> DeploymentAdapter:
    """The real adapter. Resolves credentials lazily on deploy()."""
    return GitHubPagesDeploymentAdapter()
