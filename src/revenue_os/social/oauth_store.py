"""Tiny OAuth token store - `data/oauth_tokens.json` (gitignored via data/).

Holds only what a refresh-token flow needs, per platform:
  {platform: {"refresh_token": "...", "access_token": "...",
              "access_token_expires_at": <epoch>, "scope": "...",
              "updated_at": "<iso>"}}

Never logged. The refresh token is a long-lived secret - it is written
here (local, gitignored) exactly like the .env file, and read back by the
platform client. `revenue_os` never prints it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from ..store import now_iso

_FILENAME = "oauth_tokens.json"


def _path(data_dir) -> Path:
    return Path(data_dir) / _FILENAME


def load_all(data_dir) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except json.JSONDecodeError:
        return {}


def get(data_dir, platform: str) -> dict:
    return dict(load_all(data_dir).get(platform.strip().lower(), {}))


def put(data_dir, platform: str, *, refresh_token: str | None = None,
        access_token: str | None = None, expires_in: float | None = None,
        scope: str | None = None) -> None:
    data = load_all(data_dir)
    key = platform.strip().lower()
    row = dict(data.get(key, {}))
    if refresh_token is not None:
        row["refresh_token"] = refresh_token
    if access_token is not None:
        row["access_token"] = access_token
    if expires_in is not None:
        row["access_token_expires_at"] = time.time() + float(expires_in) - 60
    if scope is not None:
        row["scope"] = scope
    row["updated_at"] = now_iso()
    data[key] = row

    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def access_token_valid(row: dict) -> bool:
    return bool(row.get("access_token")) and time.time() < float(
        row.get("access_token_expires_at", 0) or 0)
