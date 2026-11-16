"""Long-lived objects the routes share, and the FastAPI dependencies that hand them out."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request

from medialib import Stack
from medialib.importer import ImportFolder
from medialib.janitor import Janitor
from medialib.jobs import JobRunner
from medialib.rescue import ImportRescuer
from medialib.rolling import RollingStore, RollingWindow
from medialib.seasonal import anime_map
from medialib.stalls import StallRotator
from medialib.watcher import AiringWatcher
from medialib.watchlist import Watchlist
from web.config import WebConfig

ORPHAN_COUNT_TTL = 15 * 60  # the nav badge recounts leftover torrents after this long


class Services:
    def __init__(self, config: WebConfig, make_stack: Callable[[], Stack] = Stack):
        self.config = config
        self.make_stack = make_stack
        self.import_folder = ImportFolder(config.import_root)
        self.watchlist = Watchlist(config.watchlist_state)
        self.op_lock = threading.Lock()  # one state-changing job (or janitor pass) at a time
        self.jobs = JobRunner(self.op_lock, scrub=self.import_folder.display)
        self.rolling_store = RollingStore(config.rolling_state)
        self.rolling = RollingWindow(self.make_stack, self.rolling_store, lock=self.op_lock)
        self.watcher: AiringWatcher | None = None
        self.janitor: Janitor | None = None
        self.stalls: StallRotator | None = None
        self._orphans: tuple[float, int] | None = None  # (counted at, count)
        self._local = threading.local()

    def start_background(self) -> None:
        stack = self.stack()  # fails fast (and the container restarts) on missing settings
        # Warm the AniList->TVDB mapping (5-6 MB) so the Seasonal tab opens quickly.
        threading.Thread(target=_quietly(anime_map), daemon=True).start()
        if self.config.watcher_state:
            self.watcher = AiringWatcher(self.make_stack, self.config.watcher_state,
                                         before_cycle=ImportRescuer())
            self.watcher.start()
            print("airing watcher on", flush=True)
        if self.config.rolling_state:
            self.rolling.start()
            print("rolling windows on", flush=True)
        if self.config.janitor_state and stack.settings.local_data:
            self.janitor = Janitor(self.make_stack, self.config.janitor_state, lock=self.op_lock,
                                   rolling=self.rolling.series_ids)
            self.janitor.start()
            print("watched cleanup on", flush=True)
        if self.config.stall_state:
            self.stalls = StallRotator(self.make_stack, self.config.stall_state, lock=self.op_lock)
            self.stalls.start()
            print("stalled-download rotation on", flush=True)
        print(f"media-library on :{self.config.port} (import folder {self.config.import_root})",
              flush=True)

    def stack(self) -> Stack:
        """This thread's clients (qBittorrent keeps a login session per client)."""
        if not hasattr(self._local, "stack"):
            self._local.stack = self.make_stack()
        return self._local.stack

    def remember_orphans(self, count: int) -> None:
        self._orphans = (time.time(), count)

    def known_orphans(self) -> int | None:
        """The leftover-torrent count, if it was counted recently."""
        if self._orphans and time.time() - self._orphans[0] < ORPHAN_COUNT_TTL:
            return self._orphans[1]
        return None


def _quietly(fn: Callable[[], object]) -> Callable[[], None]:
    def run() -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - only a warm-up; the tab retries on demand
            print(f"warm-up failed: {e}", flush=True)
    return run


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_stack(services: Annotated[Services, Depends(get_services)]) -> Stack:
    return services.stack()


ServicesDep = Annotated[Services, Depends(get_services)]
StackDep = Annotated[Stack, Depends(get_stack)]
