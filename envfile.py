"""Load deploy/.env or .env without overriding a real process environment."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env(root: Path | None = None) -> Path | None:
    base = root or ROOT
    chosen = None
    for path in (base / ".env", base / "deploy" / ".env"):
        if path.is_file():
            chosen = path
            break
    if chosen is None:
        return None
    for raw in chosen.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
    return chosen
