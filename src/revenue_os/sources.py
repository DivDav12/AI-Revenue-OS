"""Opportunity-signal sources.

A Source yields RawSignal records. The external-I/O risk is isolated
here: the default sources (StaticSource, LocalFileSource) are fully
offline and deterministic. HackerNewsSource is the one real source and
is never used by default or in tests.

Dependencies: standard library only (json, urllib).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RawSignal:
    title: str
    url: str = ""
    text: str = ""
    source: str = ""
    external_id: str = ""


class Source(Protocol):
    def fetch(self, limit: int) -> list[RawSignal]:
        ...


def _signal_from_dict(data: dict, source: str) -> RawSignal:
    return RawSignal(
        title=str(data.get("title", "")).strip(),
        url=str(data.get("url", "")).strip(),
        text=str(data.get("text", "")).strip(),
        source=source,
        external_id=str(data.get("external_id", data.get("id", ""))).strip(),
    )


class StaticSource:
    """In-memory source for demos and tests."""

    def __init__(self, signals: list[RawSignal], name: str = "static") -> None:
        self._signals = list(signals)
        self.name = name

    def fetch(self, limit: int) -> list[RawSignal]:
        return self._signals[: max(0, limit)]


class FilteredSource:
    """Wraps another Source, keeping only signals that satisfy a predicate."""

    def __init__(self, inner, predicate, name: str = "filtered") -> None:
        self._inner = inner
        self._predicate = predicate
        self.name = name

    def fetch(self, limit: int) -> list[RawSignal]:
        return [s for s in self._inner.fetch(limit) if self._predicate(s)]


class LocalFileSource:
    """Reads a JSON list of signal dicts from disk. Offline, deterministic."""

    def __init__(self, path: str | Path, name: str = "local-file") -> None:
        self.path = Path(path)
        self.name = name

    def fetch(self, limit: int) -> list[RawSignal]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"signal file not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed signal file {self.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"signal file {self.path} must contain a JSON list")
        return [_signal_from_dict(d, self.name) for d in raw[: max(0, limit)]]


HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/showstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_MAX_ITEMS = 25
_USER_AGENT = "AI-Revenue-OS/0.1 (research; contact via repo)"


def _map_hn_item(item: dict) -> RawSignal:
    """Pure mapping from a Hacker News item dict to a RawSignal."""
    return RawSignal(
        title=str(item.get("title", "")).strip(),
        url=str(item.get("url", "")).strip(),
        text=str(item.get("text", "")).strip(),
        source="hacker-news",
        external_id=str(item.get("id", "")).strip(),
    )


class HackerNewsSource:
    """Real source: the free, keyless Hacker News API. Opt-in only.

    Never invoked by default or in tests. Network access happens here.
    """

    name = "hacker-news"

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def _get_json(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch(self, limit: int) -> list[RawSignal]:
        capped = max(0, min(limit, HN_MAX_ITEMS))
        if capped == 0:
            return []
        ids = self._get_json(HN_TOP_URL) or []
        signals: list[RawSignal] = []
        for item_id in ids[:capped]:
            item = self._get_json(HN_ITEM_URL.format(id=item_id))
            if item:
                signals.append(_map_hn_item(item))
        return signals


_SAMPLE_SIGNALS = [
    RawSignal(
        title="Show HN: an open-source no-code automation platform",
        text="We built a self-serve tool to automate repetitive API workflows.",
        source="sample",
        external_id="s1",
    ),
    RawSignal(
        title="Ask HN: how do you find your first paying customers?",
        text="Bootstrapped founder looking for revenue and pricing advice.",
        source="sample",
        external_id="s2",
    ),
    RawSignal(
        title="Launch: a marketplace for reusable document templates",
        text="MVP is live, free tier plus paid plans.",
        source="sample",
        external_id="s3",
    ),
    RawSignal(
        title="A weekend project with no obvious business model",
        text="Just something I made for fun.",
        source="sample",
        external_id="s4",
    ),
]


def build_source(name: str) -> Source:
    """Factory: 'static' (offline default) or 'hn' (real Hacker News API)."""
    if name == "static":
        return StaticSource(_SAMPLE_SIGNALS)
    if name == "hn":
        return HackerNewsSource()
    raise ValueError(f"unknown source: {name!r} (expected 'static' or 'hn')")
