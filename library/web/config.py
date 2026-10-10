"""Web app settings, from the container's environment (compose.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = True) -> bool:
    return os.environ.get(name, "1" if default else "0") != "0"


@dataclass(frozen=True)
class WebConfig:
    password: str
    username: str           # empty: any username
    port: int
    import_root: str        # ~/Downloads, mounted read-only
    watchlist_state: str
    watcher_state: str | None   # None: the airing watcher is off
    janitor_state: str | None   # None: watched cleanup is off

    @classmethod
    def from_env(cls) -> WebConfig:
        return cls(
            password=os.environ.get("LIBRARY_PASSWORD", ""),
            username=os.environ.get("LIBRARY_USERNAME", ""),
            port=int(os.environ.get("LIBRARY_PORT", "8099")),
            import_root=os.environ.get("IMPORT_ROOT", "/import"),
            watchlist_state=os.environ.get("WATCHLIST_STATE", "/state/watchlist.json"),
            watcher_state=os.environ.get("WATCHER_STATE", "/state/watcher.json")
            if _flag("LIBRARY_WATCHER") else None,
            janitor_state=os.environ.get("JANITOR_STATE", "/state/janitor.json")
            if _flag("LIBRARY_JANITOR") else None)
