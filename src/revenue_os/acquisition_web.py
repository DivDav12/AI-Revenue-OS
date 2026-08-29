"""Optional web-search source for the Acquisition Agent.

Off unless `--source web` is passed. It uses the SAME grounded web-search
architecture as research.py / competition.py (the `web_search` server
tool) plus the shared CostMeter / LlmCache / budget_gate. No new billing
path.

No-fabrication guarantee: the model is asked to pick relevant threads
*from the search results*, and this code then keeps ONLY the picks whose
URL actually appears in a real `web_search_tool_result` block. A URL the
model made up is dropped. The post text is fenced as UNTRUSTED and the
model is told never to claim the person will buy.

This source reaches publicly-indexed Reddit / Indie Hackers / forum
threads WITHOUT touching those sites - Anthropic's search returns the
public URL and we store it verbatim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .acquisition_sources import AcqRecord, _epoch
from .llm_normalize import (
    CostCeilingExceeded,
    CostMeter,
    DEFAULT_MODEL,
    UNTRUSTED_NOTE,
    _tool_input,
    web_search_tool,
    wrap_untrusted,
)

_PROMPT_VERSION = "1"
_MAX_SEARCHES = 3
_MAX_RESUMES = 2

_RUBRIC = (
    "You help find CURRENT public forum, Q&A, and social posts where a "
    "founder or freelancer is ACTIVELY asking how to get their first "
    "paying customers / clients / users (e.g. 'launched my SaaS, 0 "
    "customers, what now?').\n"
    "Search the web for such posts from the last few weeks. Then call "
    "record_web_leads with the threads you actually found in the search "
    "results that match this intent.\n"
    "Only include a URL that appeared in your search results - never "
    "guess or construct one. Skip retrospective success stories ('how I "
    "got 10k customers'), tutorials, news, and threads where the person "
    "already solved it.\n"
    "You are only labelling public posts. Never claim or imply the "
    "person will buy anything."
)

_TOOL = {
    "name": "record_web_leads",
    "description": "Record the relevant public threads found in the search results.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["leads"],
        "properties": {
            "leads": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "title", "why_relevant"],
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "snippet": {"type": "string"},
                        "why_relevant": {"type": "string"},
                    },
                },
            },
        },
    },
}

_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%Y/%m/%d")


def _norm_url(url: str) -> str:
    p = urlsplit(str(url or "").strip())
    host = p.netloc.lower().split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/')}".lower()


def _parse_page_age(value) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None \
            else dt.astimezone(timezone.utc)
        return dt.isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


def _real_results(response) -> dict[str, dict]:
    """canonical-url -> {url, title, page_age} for every result in a real
    web_search_tool_result block (i.e. URLs the search actually returned)."""
    out: dict[str, dict] = {}
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        if not isinstance(content, list):
            continue
        for item in content:
            url = str(getattr(item, "url", "") or (item.get("url") if isinstance(item, dict) else ""))
            if not url:
                continue
            title = str(getattr(item, "title", "") or (item.get("title") if isinstance(item, dict) else ""))
            page_age = getattr(item, "page_age", None)
            if page_age is None and isinstance(item, dict):
                page_age = item.get("page_age")
            out[_norm_url(url)] = {"url": url, "title": title, "page_age": page_age}
    return out


def _platform_for(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    known = {
        "reddit.com": "Reddit (via web search)",
        "old.reddit.com": "Reddit (via web search)",
        "indiehackers.com": "Indie Hackers (via web search)",
        "news.ycombinator.com": "Hacker News (via web search)",
        "twitter.com": "X (via web search)", "x.com": "X (via web search)",
    }
    return known.get(host, f"{host} (via web search)")


def _count_searches(response) -> tuple[int, bool]:
    seen = errored = 0
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        seen += 1
        if not isinstance(getattr(block, "content", None), list):
            errored += 1
    return seen, bool(errored)


@dataclass
class WebSearchSource:
    """A grounded-web-search source. Budget-gated + cached by the CLI."""

    client: object
    model: str = DEFAULT_MODEL
    max_cost_usd: float = 1.0
    meter: CostMeter = field(default=None)
    cache: object = None
    refresh: bool = False
    name: str = "web"
    searches: int = 0
    any_search_error: bool = False
    cache_hits: int = 0
    cache_misses: int = 0
    ceiling_hit: bool = False

    def __post_init__(self) -> None:
        if self.meter is None:
            self.meter = CostMeter(self.model)

    def _key(self, query: str, since_ts) -> str:
        raw = "\n".join(["acq-web", _PROMPT_VERSION, self.model, query,
                         str(_epoch(since_ts) or "")])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def search(self, query: str, limit: int, *, since_ts=None) -> list[AcqRecord]:
        key = self._key(query, since_ts)
        if self.cache is not None and not self.refresh:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return [AcqRecord(**r) for r in hit["records"]]

        if self.meter.cost_usd >= self.max_cost_usd:
            self.ceiling_hit = True
            raise CostCeilingExceeded(
                f"web-search cost ${self.meter.cost_usd} reached ceiling "
                f"${self.max_cost_usd}")

        window = ""
        epoch = _epoch(since_ts)
        if epoch is not None:
            days = max(1, int((datetime.now(timezone.utc).timestamp() - epoch) / 86400))
            window = f"\nFocus on posts from roughly the last {days} days."

        messages = [{"role": "user", "content": wrap_untrusted(
            f"Intent to search for: {query}{window}")}]
        real: dict[str, dict] = {}
        payload: dict = {}
        for _ in range(_MAX_RESUMES + 1):
            response = self.client.messages.create(
                model=self.model, max_tokens=1500,
                system=[{"type": "text", "text": _RUBRIC + UNTRUSTED_NOTE,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[web_search_tool(max_uses=_MAX_SEARCHES), _TOOL],
                tool_choice={"type": "auto"},
                messages=messages,
            )
            self.meter.add(getattr(response, "usage", None))
            s, err = _count_searches(response)
            self.searches += s
            self.any_search_error = self.any_search_error or err
            real.update(_real_results(response))
            if getattr(response, "stop_reason", None) == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            try:
                payload = _tool_input(response, "record_web_leads")
            except ValueError:
                payload = {"leads": []}
            break
        self.meter.add_searches(self.searches)

        records: list[AcqRecord] = []
        for lead in payload.get("leads", []) or []:
            url = str(lead.get("url", "")).strip()
            match = real.get(_norm_url(url))
            if not match:                       # a URL not in the search results
                continue
            posted_at = _parse_page_age(match.get("page_age"))
            records.append(AcqRecord(
                title=str(lead.get("title") or match.get("title") or url)[:200],
                url=match["url"],               # the URL the search actually returned
                text=str(lead.get("snippet", "")).strip()[:800],
                author="",                      # not reliably available from search
                posted_at=posted_at,
                platform=_platform_for(match["url"]),
                source=self.name,
                query=query,
                meta={
                    "via": "web_search",
                    "why_relevant": str(lead.get("why_relevant", "")).strip()[:300],
                    "page_age_raw": str(match.get("page_age") or ""),
                    "date_basis": ("web search page_age (approximate)"
                                   if posted_at else "no date from search"),
                },
            ))

        if self.cache is not None:
            self.cache.put(key, {"records": [r.__dict__ for r in records],
                                 "model": self.model})
        self.cache_misses += 1
        return records
