"""Distribution adapters (Phase 9) - execute the DISTRIBUTE task.

  adapter.distribute(DistributionRequest) -> DistributionResult

DEPLOY publishes the product page. DISTRIBUTE takes that live page and
makes it known through an ALLOWED channel:

  owned_web / owned_content   - publish / update an announcement page on
                                the operator's OWN GitHub Pages repo
                                (reuses the deploy.py Contents-API transport)
  community_draft / social_draft - produce a ready-to-review DRAFT only.
                                NOTHING is auto-posted. No accounts, no
                                rate-limit bypass, no mass posting.

Fail-closed: no owned channel configured -> BLOCKED, never a fake URL.
cost is always 0 here - no ads, no paid promotion, no money.

A successful OWNED distribution (a real published_url) lets the worker
move LIVE -> ACQUIRING_TRAFFIC. A draft is not "distributed" - it drives
no state change. Distribution NEVER claims traffic; Phase 10's
CHECK_TRAFFIC measures actual visitors.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace

from .deployment import valid_live_url as valid_url
from .store import now_iso

OWNED_CHANNELS: tuple[str, ...] = ("owned_web", "owned_content")
DRAFT_CHANNELS: tuple[str, ...] = ("community_draft", "social_draft")
CHANNELS: tuple[str, ...] = OWNED_CHANNELS + DRAFT_CHANNELS

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_OWNED_FILENAME = {"owned_web": "announce.html", "owned_content": "guide.html"}


def slugify(value: str) -> str:
    s = _SLUG_RE.sub("-", str(value or "").lower()).strip("-")
    return s or "site"


@dataclass
class DistributionRequest:
    opportunity_id: str
    channel: str
    destination: str = ""            # owned target id / repo path
    content: dict = field(default_factory=dict)   # {html} for owned, draft fields otherwise
    live_url: str = ""
    metadata: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.channel.encode("utf-8"))
        h.update(b"\0")
        for k in sorted(self.content):
            h.update(f"{k}={self.content[k]}".encode("utf-8"))
        h.update(self.live_url.encode("utf-8"))
        return h.hexdigest()[:16]


@dataclass
class DistributionResult:
    success: bool
    channel: str
    destination: str = ""
    published_url: str = ""
    distribution_id: str = ""
    draft_only: bool = False
    error: str = ""
    blocked: bool = False
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success, "channel": self.channel,
            "destination": self.destination, "published_url": self.published_url,
            "distribution_id": self.distribution_id,
            "draft_only": self.draft_only, "error": self.error,
            "blocked": self.blocked, "details": dict(self.details),
        }


class DistributionAdapter:
    provider = "base"

    def distribute(self, request: DistributionRequest) -> DistributionResult:  # pragma: no cover
        raise NotImplementedError


class NullDistributionAdapter(DistributionAdapter):
    provider = "none"

    def distribute(self, request: DistributionRequest) -> DistributionResult:
        return DistributionResult(
            success=False, blocked=True, channel=request.channel,
            error="no owned distribution channel is configured - the "
                  "distribution path is ready, a real owned channel must be "
                  "wired")


class FakeDistributionAdapter(DistributionAdapter):
    """Deterministic, offline. Never touches the network. Idempotent per
    (opportunity, channel, content_hash)."""

    provider = "fake"

    def __init__(self, *, base_url: str = "https://fake.dist.test",
                 fail: bool = False, blocked: bool = False, error: str = "",
                 drop_url: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.fail = fail
        self.blocked = blocked
        self.error = error
        self.drop_url = drop_url
        self.calls: list[tuple[str, str]] = []
        self._published: dict[str, DistributionResult] = {}

    def distribute(self, request: DistributionRequest) -> DistributionResult:
        self.calls.append((request.opportunity_id, request.channel))
        if self.blocked:
            return DistributionResult(success=False, blocked=True,
                                      channel=request.channel,
                                      error=self.error or "fake: no channel")
        if self.fail:
            return DistributionResult(success=False, channel=request.channel,
                                      error=self.error or "fake: publish failed")

        digest = request.content_hash()
        key = f"{request.opportunity_id}:{request.channel}:{digest}"
        prior = self._published.get(key)
        if prior is not None:
            return replace(prior, details={**prior.details,
                                           "duplicate_suppressed": True})

        slug = slugify(request.opportunity_id)
        fname = _OWNED_FILENAME.get(request.channel, "announce.html")
        # drop_url: the provider claims success but returns no URL - callers
        # must still treat this as unusable and never fabricate a URL.
        url = "" if self.drop_url else f"{self.base_url}/{slug}/{fname}"
        res = DistributionResult(
            success=True, channel=request.channel,
            destination=f"fake-owned:{slug}/{fname}", published_url=url,
            distribution_id=f"fake-dist-{slug}-{request.channel}-{digest[:8]}",
            details={"content_hash": digest, "published_at": now_iso()})
        if not self.drop_url:
            self._published[key] = res
        return res


class GitHubPagesDistributionAdapter(DistributionAdapter):
    """Real OWNED channel: publish / update an announcement page on the
    operator's own GitHub Pages repo via the EXISTING deploy.py transport.
    Distinct file from DEPLOY's index.html; fail-closed without credentials.
    """

    provider = "github_pages"

    def __init__(self, *, config=None, client=None, environ=None) -> None:
        self._config = config
        self._client = client
        self._environ = environ

    def distribute(self, request: DistributionRequest) -> DistributionResult:
        from . import deploy as _deploy

        if request.channel not in OWNED_CHANNELS:
            return DistributionResult(
                success=False, channel=request.channel,
                error="GitHubPagesDistributionAdapter only serves owned channels")
        try:
            cfg = self._config or _deploy.GitHubPagesConfig.from_env(self._environ)
        except _deploy.DeployError as exc:
            return DistributionResult(success=False, blocked=True,
                                      channel=request.channel, error=str(exc))

        html = str(request.content.get("html") or "")
        if not html.strip():
            return DistributionResult(success=False, channel=request.channel,
                                      error="no announcement html to publish")
        slug = slugify(request.opportunity_id)
        fname = _OWNED_FILENAME.get(request.channel, "announce.html")
        files = {f"{slug}/{fname}": html.encode("utf-8")}
        try:
            res = _deploy.deploy_files(
                cfg, files, client=self._client,
                message=f"distribute {slug}/{fname} ({now_iso()})")
        except _deploy.DeployError as exc:
            return DistributionResult(success=False, channel=request.channel,
                                      error=str(exc))
        url = res["urls"].get(f"{slug}/{fname}", "")
        if not valid_url(url):
            return DistributionResult(success=False, channel=request.channel,
                                      error="distribution returned no usable URL")
        return DistributionResult(
            success=True, channel=request.channel,
            destination=f"{res['repo']}:{slug}/{fname}", published_url=url,
            distribution_id=f"ghp:{res['repo']}:{slug}/{fname}",
            details={"deployed": res["deployed"], "unchanged": res["unchanged"],
                     "commit_sha": res.get("commit_sha", "")})


def default_distribution_adapter() -> DistributionAdapter:
    return NullDistributionAdapter()
