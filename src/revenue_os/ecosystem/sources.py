"""Opportunity sources - where candidate money-making opportunities come from.

A source yields `model.OpportunityDraft` records and declares `SourceMeta`
(spec section 6). Only two sources touch the network - `HackerNewsDemandSource`
and `RemoteOkSource` - and both go through an injectable `fetch_json`
callable that tests replace, exactly like `paypal.py` / `deploy.py`. The
default registry used everywhere except an explicit `revenue_os discover
--source <real>` is entirely offline.

Policy (spec section 6 + 31): every source is read-only. None of these
create an account, log in, solve a CAPTCHA, or post anything. A source that
would need credentials to be useful declares
`policy_status=HUMAN_SETUP_REQUIRED` and yields nothing until a human wires
it - it never "figures out" a login.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable, Protocol

from ..store import now_iso
from . import model
from .model import OpportunityDraft, SourceMeta

_USER_AGENT = "AI-Revenue-OS/0.1 (autonomous revenue research; +https://github.com/DivDav12/AI-Revenue-OS)"
_TIMEOUT = 6.0


def _http_json(url: str, *, timeout: float = _TIMEOUT):
    """The one network primitive. Injected in tests."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace") or "null")


class OpportunitySource(Protocol):
    meta: SourceMeta

    def discover(self, limit: int) -> list[OpportunityDraft]:
        ...


# ---------------------------------------------------------------------------
# synthetic - reuses opportunity_engine's archetype catalogue, no disk / net
# ---------------------------------------------------------------------------

class SyntheticSource:
    """Deterministic test opportunities. origin stays "synthetic" - these
    must never drive a real revenue decision (spec section 20)."""

    meta = SourceMeta(
        source="synthetic", source_type="synthetic",
        access_method=model.ACCESS_SYNTHETIC, automation_allowed=True,
        policy_status=model.POLICY_OK)

    def __init__(self, seed: int = 0) -> None:
        self.seed = int(seed)

    def discover(self, limit: int) -> list[OpportunityDraft]:
        from ..opportunity_engine import (
            _A, _AUDS, _DEFAULT_KW, _MODIFIERS, _instantiate,
        )

        topics = list(_DEFAULT_KW)
        out: list[OpportunityDraft] = []
        n = max(0, int(limit))
        combos = [(i, j) for i in range(len(_A)) for j in range(len(topics))]
        # deterministic rotation seeded by `seed`
        combos = combos[self.seed % max(1, len(combos)):] + combos[:self.seed % max(1, len(combos))]
        for i, j in combos:
            if len(out) >= n:
                break
            aud = _AUDS[(i + j) % len(_AUDS)]
            mod = _MODIFIERS[(i + j + self.seed) % len(_MODIFIERS)]
            opp = _instantiate(_A[i], topics[j], aud, mod)
            out.append(OpportunityDraft(
                title=opp.title, description=opp.required_work,
                opportunity_type=_type_for_category(opp.category),
                category=opp.category,
                evidence=[f"synthetic archetype {_A[i][0]!r} (seed {self.seed})"],
                source_meta=self.meta,
                source_id=opp.id, discovered_at=now_iso(),
                est_pay_eur=float(opp.est_revenue_eur),
                est_time_minutes=float(opp.effort_points) * 120.0,
                demand_hint=float(opp.probability),
                raw={"archetype": _A[i][0], "target_customer": opp.target_customer}))
        return out


def _type_for_category(category: str) -> str:
    c = (category or "").lower()
    if c in ("template_pack", "information_product", "digital_product",
             "content_business", "data_product"):
        return model.TYPE_DIGITAL_PRODUCT
    if c in ("micro_saas", "saas", "developer_tool", "api_product", "website"):
        return model.TYPE_SOFTWARE_TOOL
    if c == "affiliate":
        return model.TYPE_AFFILIATE
    if c in ("ecommerce", "print_on_demand"):
        return model.TYPE_ECOMMERCE
    if c in ("freelancing", "b2b_service", "b2c_service", "ai_service",
             "automation_service", "niche_service"):
        return model.TYPE_SERVICE
    if c in ("lead_generation", "marketplace", "arbitrage"):
        return model.TYPE_OTHER
    return model.TYPE_OTHER


# ---------------------------------------------------------------------------
# curated local file - real signals a human collected (offline, deterministic)
# ---------------------------------------------------------------------------

class LocalSignalSource:
    """A human-curated JSON list of real opportunity signals. origin="real"
    because a person vouched for each entry. Fully offline."""

    def __init__(self, path, source: str = "curated") -> None:
        from ..sources import LocalFileSource
        self._inner = LocalFileSource(path, name=source)
        self.meta = SourceMeta(
            source=source, source_type="curated_signal",
            access_method=model.ACCESS_CURATED_FILE, automation_allowed=True,
            requires_human=True, policy_status=model.POLICY_OK,
            source_url=str(path))

    def discover(self, limit: int) -> list[OpportunityDraft]:
        out = []
        for sig in self._inner.fetch(max(0, int(limit))):
            out.append(OpportunityDraft(
                title=sig.title, description=sig.text,
                opportunity_type=model.TYPE_OTHER,
                evidence=[t for t in (sig.title, sig.text) if t],
                source_meta=self.meta, source_id=sig.external_id,
                source_url=sig.url, discovered_at=now_iso(),
                raw={"raw_source": sig.source}))
        return out


# ---------------------------------------------------------------------------
# Hacker News - real, keyless, documented public API. Demand-signal source
# (Ask HN / "Who wants to be hired" / "Freelancer? Seeking freelancer?"
# threads surface real, current demand). Reading is fine; the fleet never
# posts to HN (action_class._NO_AUTO_POST).
# ---------------------------------------------------------------------------

_HN_STORIES = "https://hacker-news.firebaseio.com/v0/{feed}.json"
_HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
_DEMAND_MARKERS = ("ask hn", "who wants to be hired", "who is hiring",
                   "seeking freelancer", "freelancer? seeking", "i will pay",
                   "looking for", "need help", "bounty")


class HackerNewsDemandSource:
    meta = SourceMeta(
        source="hacker-news", source_type="demand_signal",
        source_url="https://news.ycombinator.com",
        access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
        requires_login=False, policy_status=model.POLICY_OK)

    def __init__(self, *, feed: str = "askstories",
                 fetch_json: Callable[[str], object] | None = None) -> None:
        self.feed = feed
        self._fetch = fetch_json or _http_json

    def discover(self, limit: int) -> list[OpportunityDraft]:
        n = max(0, min(int(limit), 60))
        if n == 0:
            return []
        try:
            ids = self._fetch(_HN_STORIES.format(feed=self.feed)) or []
        except Exception:                       # noqa: BLE001 - fail closed, no signal
            return []
        out: list[OpportunityDraft] = []
        for iid in list(ids)[: n * 2]:
            if len(out) >= n:
                break
            try:
                item = self._fetch(_HN_ITEM.format(id=iid)) or {}
            except Exception:                   # noqa: BLE001
                continue
            title = str(item.get("title") or "").strip()
            text = str(item.get("text") or "").strip()
            low = f"{title} {text}".lower()
            if not title or not any(m in low for m in _DEMAND_MARKERS):
                continue
            out.append(OpportunityDraft(
                title=title, description=text[:800],
                opportunity_type=model.TYPE_TASK if "hiring" in low or "pay" in low
                else model.TYPE_OTHER,
                evidence=[title] + ([text[:280]] if text else []),
                source_meta=self.meta, source_id=str(item.get("id") or iid),
                source_url=f"https://news.ycombinator.com/item?id={item.get('id') or iid}",
                discovered_at=now_iso(),
                demand_hint=0.35,
                raw={"hn_type": item.get("type"), "by": item.get("by"),
                     "score": item.get("score")}))
        return out


# ---------------------------------------------------------------------------
# RemoteOK - official public JSON API (https://remoteok.com/api). Job / gig
# board -> real freelance & task opportunities. Read-only; they ask for a
# descriptive UA (set above) and attribution (kept in `source_url`).
# ---------------------------------------------------------------------------

_REMOTEOK_API = "https://remoteok.com/api"


class RemoteOkSource:
    meta = SourceMeta(
        source="remoteok", source_type="job_board",
        source_url="https://remoteok.com",
        access_method=model.ACCESS_OFFICIAL_API, automation_allowed=True,
        requires_login=False, policy_status=model.POLICY_OK)

    def __init__(self, *, fetch_json: Callable[[str], object] | None = None,
                 keywords: tuple[str, ...] = ("automation", "script", "data",
                                              "content", "no-code", "ai",
                                              "integration", "prompt")) -> None:
        self._fetch = fetch_json or _http_json
        self.keywords = tuple(k.lower() for k in keywords)

    def discover(self, limit: int) -> list[OpportunityDraft]:
        n = max(0, min(int(limit), 100))
        if n == 0:
            return []
        try:
            rows = self._fetch(_REMOTEOK_API) or []
        except Exception:                       # noqa: BLE001
            return []
        out: list[OpportunityDraft] = []
        for row in rows:
            if len(out) >= n:
                break
            if not isinstance(row, dict) or not row.get("position"):
                continue                        # first element is a legal notice
            position = str(row.get("position") or "").strip()
            company = str(row.get("company") or "").strip()
            tags = [str(t).lower() for t in (row.get("tags") or [])]
            blob = f"{position} {' '.join(tags)}".lower()
            if self.keywords and not any(k in blob for k in self.keywords):
                continue
            out.append(OpportunityDraft(
                title=f"{position}" + (f" ({company})" if company else ""),
                description=str(row.get("description") or "")[:800],
                opportunity_type=model.TYPE_SERVICE,
                category="freelancing",
                evidence=[f"RemoteOK listing: {position}"
                          + (f" at {company}" if company else ""),
                          f"tags: {', '.join(tags)}" if tags else ""],
                source_meta=self.meta, source_id=str(row.get("id") or row.get("slug") or ""),
                source_url=str(row.get("url") or row.get("apply_url") or self.meta.source_url),
                discovered_at=now_iso(),
                demand_hint=0.5,          # a real paid listing = real demand
                raw={"tags": tags, "company": company}))
        return out


# ---------------------------------------------------------------------------
# credentialed sources the fleet must NOT self-provision (spec sections 6,
# 13, 30, 37). Registered so `discover --source <x>` gives a clear
# HUMAN_SETUP_REQUIRED message instead of a crash.
# ---------------------------------------------------------------------------

class HumanSetupRequiredSource:
    def __init__(self, source: str, what: str, env_hint: str) -> None:
        self.meta = SourceMeta(
            source=source, source_type="requires_setup",
            automation_allowed=False, requires_login=True, requires_human=True,
            policy_status=model.POLICY_HUMAN_SETUP_REQUIRED)
        self.what = what
        self.env_hint = env_hint

    def discover(self, limit: int) -> list[OpportunityDraft]:
        return []                       # yields nothing until a human wires it


_HUMAN_SETUP = {
    "upwork": ("Upwork gig discovery", "UPWORK_API_KEY"),
    "fiverr": ("Fiverr buyer-request discovery", "FIVERR_API_KEY"),
    "amazon_associates": ("Amazon affiliate offer discovery", "AMAZON_ASSOCIATES_TAG + PA-API keys"),
    "shopify": ("Shopify / e-commerce product research", "SHOPIFY_API_KEY"),
}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def default_sources() -> list[OpportunitySource]:
    """Offline, deterministic - safe everywhere (tests, simulation, the
    default `discover` run). Real network sources are opt-in by name."""
    return [SyntheticSource()]


#: demand-signal sources (spec: Demand-to-Revenue plan, Step 3) - each
#: wraps a REAL, keyless acquisition_sources.py fetcher with buyer-intent
#: queries via ecosystem.demand_sources. Delegated, not duplicated: the
#: actual OpportunitySource implementation lives in demand_sources.py
#: (alongside the AcqRecord->OpportunityDraft mapping it depends on);
#: build_source() stays the single factory entry point every caller
#: (CLI, tests) already uses.
_DEMAND_SOURCE_NAMES = ("demand-hn", "demand-stackexchange",
                        "demand-lobsters", "demand-lemmy",
                        "demand-stackexchange-recs", "demand-lemmy-buying")


def build_source(name: str, **kw):
    """Factory. 'synthetic' (default, offline), 'hn'/'hackernews',
    'remoteok', 'file' (needs path=...), one of the demand-* sources
    (spec: Step 3 - see _DEMAND_SOURCE_NAMES), or a HUMAN_SETUP_REQUIRED
    name."""
    n = (name or "synthetic").strip().lower()
    if n in ("synthetic", "test"):
        return SyntheticSource(seed=int(kw.get("seed", 0)))
    if n in ("hn", "hackernews", "hacker-news"):
        return HackerNewsDemandSource(feed=kw.get("feed", "askstories"))
    if n in ("remoteok", "remote-ok"):
        return RemoteOkSource()
    if n == "file":
        if not kw.get("path"):
            raise ValueError("source 'file' requires path=")
        return LocalSignalSource(kw["path"], source=kw.get("source", "curated"))
    if n in _DEMAND_SOURCE_NAMES:
        from .demand_sources import build_demand_source
        return build_demand_source(n, **kw)
    if n in _HUMAN_SETUP:
        what, env = _HUMAN_SETUP[n]
        return HumanSetupRequiredSource(n, what, env)
    raise ValueError(
        f"unknown source {name!r} - one of: synthetic, hn, remoteok, file, "
        + ", ".join(_DEMAND_SOURCE_NAMES) + ", "
        + ", ".join(sorted(_HUMAN_SETUP)))
