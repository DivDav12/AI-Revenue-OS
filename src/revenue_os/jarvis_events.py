"""JARVIS activity feed - an append-only log of things that ACTUALLY
happened in the command center.

One JSON array at <data-dir>/jarvis_events.json, atomically written,
newest last, capped at _CAP entries. Every entry is a real event a
caller recorded (a job started, an agent ran, a gate was acknowledged, a
mode changed). Nothing here animates or predicts - if it is in the feed,
it occurred.

Standard library only. No database. No network.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .store import now_iso

_CAP = 400


class JarvisEvents:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._events: list[dict] = []

    @classmethod
    def load(cls, path: str | Path) -> "JarvisEvents":
        ev = cls(path)
        if not ev.path.exists():
            return ev
        try:
            raw = json.loads(ev.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return ev            # a corrupt feed is not worth failing the UI
        if isinstance(raw, list):
            ev._events = [e for e in raw if isinstance(e, dict)]
        return ev

    def save(self) -> None:
        self._events = self._events[-_CAP:]
        payload = json.dumps(self._events, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def record(self, kind: str, text: str, *, actor: str = "jarvis", **meta) -> dict:
        entry = {"ts": now_iso(), "kind": str(kind), "text": str(text),
                 "actor": str(actor or "jarvis")}
        entry.update({k: v for k, v in meta.items()
                      if isinstance(v, (str, int, float, bool)) or v is None})
        self._events.append(entry)
        return entry

    def recent(self, n: int = 40) -> list[dict]:
        return list(self._events[-n:][::-1])   # newest first

    def all(self) -> list[dict]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)


def load_events(data_dir: str | Path) -> JarvisEvents:
    return JarvisEvents.load(Path(data_dir) / "jarvis_events.json")


def record_event(data_dir: str | Path, kind: str, text: str, *,
                 actor: str = "jarvis", **meta) -> None:
    """Append one event and persist. Never raises into the caller."""
    try:
        ev = load_events(data_dir)
        ev.record(kind, text, actor=actor, **meta)
        ev.save()
    except Exception:            # the feed must never break a real action
        pass
