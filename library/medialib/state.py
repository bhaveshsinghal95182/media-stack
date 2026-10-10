"""JSON state files that survive container restarts (config/library on the host)."""

from __future__ import annotations

import json
import os


def load_json(path: str | None, default):
    """The file's contents, or `default` when it is missing or unreadable."""
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, data, indent: int | None = None) -> None:
    """Write atomically, so a crash mid-write never leaves a half-written file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)
