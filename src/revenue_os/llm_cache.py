"""Content-addressed cache for LLM results.

One JSON object per cache file, atomically written, in the shape of the
other stores. Keys are opaque strings the caller computes (a hash of the
prompt version, the model, and the exact text sent to the model); values
are caller-defined dicts, stamped with `cached_at` on write.

Used by the LLM evaluator (llm_normalize) and the LLM planner (llm_plan),
each with its own cache file. No expiry: `--refresh-eval` /
`--refresh-plan` rebuild entries. Standard library only; never imported
on the deterministic paths.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .store import now_iso


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

    def get(self, key: str) -> dict | None:
        entry = self._by_key.get(key)
        return dict(entry) if entry is not None else None

    def put(self, key: str, value: dict) -> None:
        self._by_key[key] = {**value, "cached_at": now_iso()}

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    def __len__(self) -> int:
        return len(self._by_key)
