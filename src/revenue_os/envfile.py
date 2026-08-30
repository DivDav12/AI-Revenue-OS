"""Minimal .env loader (standard library only).

Nothing in this project pulls in python-dotenv. `load_env` parses a
`KEY=VALUE` file and puts the pairs into `os.environ` **without
overriding values already set in the real environment** (the shell
always wins). It returns the list of KEY names it set - never the
values, so a caller can log "loaded PAYPAL_CLIENT_ID, ..." safely.

Format: `KEY=VALUE` per line; blank lines and `#` comments ignored; an
optional leading `export `; surrounding single/double quotes stripped;
no variable interpolation.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_env", "find_env_file"]


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        # a trailing " # comment" is only stripped for unquoted values
        val = value
        if val.lstrip()[:1] not in ("'", '"') and " #" in val:
            val = val.split(" #", 1)[0]
        out[key] = _strip_quotes(val)
    return out


def find_env_file(explicit: str | os.PathLike | None = None) -> Path | None:
    """Locate the .env file: an explicit path, then $REVENUE_OS_ENV_FILE,
    then ./.env, then the repo root two levels above this package."""
    if explicit is not None:
        p = Path(explicit)
        return p if p.is_file() else None
    candidates = [
        os.environ.get("REVENUE_OS_ENV_FILE"),
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for entry in candidates:
        if not entry:
            continue
        path = Path(entry)
        if path.is_file():
            return path
    return None


def load_env(path: str | os.PathLike | None = None, *, override: bool = False,
             environ: dict | None = None) -> list[str]:
    """Load KEY=VALUE pairs from `path` (or the first .env found) into
    `environ` (default os.environ). Values already present are kept unless
    `override=True`. Returns the KEY names that were set (never values)."""
    target = environ if environ is not None else os.environ
    env_path = find_env_file(path)
    if env_path is None:
        return []
    try:
        pairs = _parse(env_path.read_text(encoding="utf-8"))
    except OSError:
        return []
    set_keys: list[str] = []
    for key, value in pairs.items():
        if not override and key in target and target[key] != "":
            continue
        target[key] = value
        set_keys.append(key)
    return set_keys
