"""Read one key from a dotenv file, then let the process environment override.

Shared by the Telegram token and source API-key loaders so both have identical
semantics: only the requested key is taken from the file, everything else in it
is ignored, and the placeholder value ``changeme`` counts as absent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def read_env_key(env_path: str | Path, key: str) -> Optional[str]:
    value = None
    path = Path(env_path)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, raw = line.partition("=")
            if name.strip() == key:
                value = raw.strip().strip("\"'")
    value = os.environ.get(key, value)
    if not value or value == "changeme":
        return None
    return value
