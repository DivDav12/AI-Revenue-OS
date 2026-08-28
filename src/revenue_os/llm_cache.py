"""Content-addressed cache of LLM criterion estimates.

One JSON object at <data-dir>/llm_cache.json, atomically written, in the
shape of the other stores. The key is a hash of the prompt version, the
model, and the exact signal text sent to the model, so a re-run scores
unchanged signals for free and returns stable numbers.

No expiry: `run --refresh-eval` is the manual way to rebuild an entry.
Standard library only. Never imported on the keyword path.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import llm_normalize
from .store import now_iso


def _key(signal, model: str) -> str:
    text = getattr(signal, "text", "") or ""
    raw = f"{llm_normalize._PROMPT_VERSION}\n{model}\n{signal.title}\n{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LlmCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_key: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "LlmCache":
        cache = cls(path)
        if not cache.path.exists():
            return cache
        try:
            raw = json.loads(cache.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt llm cache {cache.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"llm cache {cache.path} must contain a JSON object")
        cache._by_key = {k: dict(v) for k, v in raw.items()}
        return cache

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._by_key, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def key(self, signal, model: str) -> str:
        return _key(signal, model)

    def get(self, signal, model: str) -> dict | None:
        entry = self._by_key.get(_key(signal, model))
        return dict(entry) if entry is not None else None

    def put(self, signal, model: str, scores: dict, rationale: str) -> None:
        self._by_key[_key(signal, model)] = {
            "scores": {k: float(v) for k, v in scores.items()},
            "rationale": str(rationale),
            "model": model,
            "cached_at": now_iso(),
        }

    def __len__(self) -> int:
        return len(self._by_key)
