"""Rotate stalled downloads: when a torrent has made almost no progress for a while,
remove it from the Arr queue with blocklist and search_again, so Radarr/Sonarr blocklist
that release and grab the next one.

Progress is judged by qB's `completed` (verified bytes), which only moves when pieces
arrive and pass their hash check. A torrent counts as stalled when it has gained less than
MIN_PROGRESS since its clock was last reset, for STALL_MINUTES. Only torrents that are
actively downloading are timed; queued, stopped (e.g. by the janitor's disk guard) or
checking torrents drop out, and their clock restarts when they become active again.

Fallback: after a rotation the Arr searches by itself, but only takes releases its profile
allows. If FALLBACK_AFTER later it has grabbed nothing, we search once ourselves and take
the best release it refused only for quality (not in the profile, custom format score, size):
something to watch beats nothing. CAM-type and unknown qualities are never taken.
"""

from __future__ import annotations

import os
import re
import threading
import time

from medialib.background import run_forever
from medialib.errors import LibraryError
from medialib.releases import release_grab
from medialib.rescue import grab_episodes
from medialib.state import load_json, save_json

MIB = 1024 ** 2
STALL_MINUTES = float(os.environ.get("STALL_MINUTES", "60"))
MIN_PROGRESS = float(os.environ.get("STALL_MIN_PROGRESS_MB", "10")) * MIB
TICK = 120            # seconds between checks
RESET_GAP = 600       # no check for this long (laptop asleep, library down): restart every clock
SHOW_AFTER = 600      # status() lists torrents without progress for at least this long
MAX_PER_PASS = 10
ACTIVE = {"downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL"}
ARR_OF = {"radarr": "radarr", "tv-sonarr": "sonarr"}   # qB category -> Stack attribute
KIND_OF = {"radarr": "movie", "sonarr": "show"}
FALLBACK_AFTER = 600  # the Arr's own search gets this long before we search ourselves
MAX_FALLBACKS = 2     # a search can take minutes; at most this many per pass
SEARCH_TIMEOUT = 240
WELL_SEEDED = 3       # a fallback with this many seeders is preferred: we're here after a stall
# Rejections that only say a release is below what the profile asks for.
SOFT = re.compile(r"is not wanted in profile|^custom formats .* below .* minimum"
                  r"|is (smaller|larger) than (minimum|maximum) allowed", re.I)
NEVER = {"unknown", "cam", "telesync", "telecine", "workprint", "dvdscr", "regional"}


# --------------------------------------------------------------------------
# Pure helper (tested)
# --------------------------------------------------------------------------

def track(state: dict, torrents: list[dict], now: float, stall_s: float,
          min_progress: float) -> list[str]:
    """Update the per-torrent clocks in `state` and return the stalled hashes, oldest first.

    state: {"lastCheck": float | None,
            "torrents": {hash: {"since", "completed", "name", "progress", "state"}}}
    """
    last = state.get("lastCheck")
    old: dict[str, dict] = state.get("torrents") or {}
    if last is not None and now - last > RESET_GAP:
        for entry in old.values():  # we weren't watching; don't punish torrents for that
            entry["since"] = now

    kept: dict[str, dict] = {}
    stalled: list[str] = []
    for t in torrents:
        if t.get("progress", 0) >= 1 or t.get("state") not in ACTIVE:
            continue  # dropped: its clock restarts when it is active again
        h = t["hash"].lower()
        completed = int(t.get("completed") or 0)
        entry = old.get(h)
        if entry is None or completed < entry["completed"] \
                or completed - entry["completed"] >= min_progress:
            entry = {"since": now, "completed": completed}
        entry.update(name=t.get("name") or "", progress=t.get("progress") or 0.0,
                     state=t["state"])
        kept[h] = entry
        if now - entry["since"] >= stall_s:
            stalled.append(h)

    state["torrents"] = kept
    state["lastCheck"] = now
    stalled.sort(key=lambda h: kept[h]["since"])
    return stalled


def is_soft(rejection: str) -> bool:
    """A rejection only about quality, format score or size (not language, not blocklist)."""
    return bool(SOFT.search(rejection)) and not rejection.lower().startswith("language")


def pick_fallback(releases: list[dict], kind: str, item_id: int,
                  wanted: set[int]) -> dict | None:
    """The best release to grab when the Arr took nothing: allowed ones first, then ones
    refused only for quality. Well seeded, then most wanted episodes, resolution, format
    score, seeders."""
    best, best_key = None, None
    for r in releases:
        quality = (r.get("quality") or {}).get("quality") or {}
        seeders = r.get("seeders") or 0
        if (seeders < 1 or (quality.get("name") or "unknown").lower() in NEVER
                or not all(is_soft(x) for x in r.get("rejections") or [])):
            continue
        if kind == "movie":
            if r.get("mappedMovieId") != item_id:
                continue
            covered = 1
        else:
            if r.get("mappedSeriesId") != item_id:
                continue
            covered = len({e.get("id") for e in r.get("mappedEpisodeInfo") or []} & wanted)
            if not covered:
                continue
        key = (not r.get("rejected"), seeders >= WELL_SEEDED, covered,
               quality.get("resolution") or 0, r.get("customFormatScore") or 0, seeders)
        if best_key is None or key > best_key:
            best, best_key = r, key
    return best


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class StallRotator:
    def __init__(self, make_stack, state_path: str | None, lock: threading.Lock | None = None,
                 stall_minutes: float = STALL_MINUTES, min_progress: float = MIN_PROGRESS,
                 log_size: int = 50):
        self.make_stack, self.state_path = make_stack, state_path
        self.stall_s, self.min_progress = stall_minutes * 60, min_progress
        self.op_lock = lock or threading.Lock()   # shared with the web page's jobs
        self.lock = threading.Lock()              # state and log, for status() from web threads
        self.log_size = log_size
        # fallback: hash -> what a rotated torrent was for, until we've checked it got a
        # replacement: {"kind", "itemId", "episodes", "name", "rotatedAt"}
        self.state: dict = {"lastCheck": None, "torrents": {}, "fallback": {}}
        loaded = load_json(state_path, {})
        if isinstance(loaded, dict) and isinstance(loaded.get("torrents"), dict) \
                and isinstance(loaded.get("lastCheck", 0), (int, float, type(None))):
            self.state = {"lastCheck": loaded.get("lastCheck"), "torrents": loaded["torrents"],
                          "fallback": loaded.get("fallback") if isinstance(
                              loaded.get("fallback"), dict) else {}}
        self.log: list[dict] = []
        self.last_run: float | None = None
        self.last_error: str | None = None
        self._unqueued: set[str] = set()  # stalled hashes already reported as not in a queue

    # -- bookkeeping --

    def _log(self, msg: str) -> None:
        with self.lock:
            self.log.append({"t": time.time(), "msg": msg})
            del self.log[:-self.log_size]

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            with self.lock:
                save_json(self.state_path, self.state)
        except OSError as e:
            self._log(f"Error saving state: {e}")

    def start(self) -> None:
        run_forever(self.tick, TICK, self._failed)

    def _failed(self, e: Exception) -> None:
        self.last_error = str(e)
        self._log(f"Error: {e}")

    # -- one pass --

    def tick(self, now: float | None = None) -> list[str]:
        """Check all downloads once; returns the names of the torrents rotated."""
        now = now if now is not None else time.time()
        stack = self.make_stack()
        torrents = stack.qb.torrents()
        with self.lock:
            stalled = track(self.state, torrents, now, self.stall_s, self.min_progress)
            self._unqueued &= set(self.state["torrents"])
            info = {h: dict(self.state["torrents"][h]) for h in stalled}
        by_hash = {t["hash"].lower(): t for t in torrents}

        queues: dict[str, list[dict]] = {}   # each Arr's queue, fetched at most once a tick
        rotated: list[str] = []
        for h in stalled[:MAX_PER_PASS]:
            name = info[h]["name"]
            arr_attr = ARR_OF.get(by_hash[h].get("category"))
            if arr_attr is None:
                continue
            arr = getattr(stack, arr_attr)
            try:
                if arr_attr not in queues:
                    queues[arr_attr] = arr.queue()
                recs = [r for r in queues[arr_attr] if (r.get("downloadId") or "").lower() == h]
                ids = [r["id"] for r in recs]
                if not ids:
                    if h not in self._unqueued:
                        self._unqueued.add(h)
                        self._log(f"{name} has stalled, but it isn't in {arr.name}'s queue; "
                                  "left alone")
                    continue
                target = self._target(stack, arr_attr, h, recs)
                with self.op_lock:
                    arr.remove_from_queue(ids, blocklist=True, search_again=True)
            except LibraryError as e:
                self._log(f"Couldn't rotate {name}: {e}")
                continue
            with self.lock:
                self.state["torrents"].pop(h, None)
                if target:
                    self.state["fallback"][h] = dict(target, name=name, rotatedAt=now)
            self._unqueued.discard(h)
            minutes = round((now - info[h]["since"]) / 60)
            self._log(f"Rotated {name}: no progress for {minutes} min at "
                      f"{info[h]['progress'] * 100:.1f}%; blocklisted, {arr.name} is "
                      "searching for another release")
            rotated.append(name)

        self._fallbacks(stack, now)
        self._save()
        self.last_run, self.last_error = now, None
        return rotated

    # -- fallback: when the Arr found nothing it accepts --

    @staticmethod
    def _target(stack, arr_attr: str, h: str, recs: list[dict]) -> dict | None:
        """What the stalled download was for, to check later that it got a replacement."""
        if arr_attr == "radarr":
            movie = recs[0].get("movieId")
            return {"kind": "movie", "itemId": movie, "episodes": []} if movie else None
        series = recs[0].get("seriesId")
        eps = sorted({r["episodeId"] for r in recs if r.get("episodeId")})
        if not eps:  # a download Sonarr couldn't match by name: its grab history knows
            series, eps = grab_episodes(stack, h)
        return {"kind": "show", "itemId": series, "episodes": eps} if series and eps else None

    def _fallbacks(self, stack, now: float) -> None:
        with self.lock:
            due = [(h, dict(f)) for h, f in self.state["fallback"].items()
                   if now - f["rotatedAt"] >= FALLBACK_AFTER][:MAX_FALLBACKS]
            for h, _f in due:  # one try each; a release found later comes via the Arr
                del self.state["fallback"][h]
        for _h, f in due:
            try:
                self._fallback(stack, f)
            except LibraryError as e:
                self._log(f"Couldn't look for a replacement for {f['name']}: {e}")

    def _fallback(self, stack, f: dict) -> None:
        k = stack.kind(f["kind"])
        item_id = f["itemId"]
        queue = k.arr.queue()
        if k.is_movie:
            movie = k.item(item_id)
            if movie.get("hasFile") or not movie.get("monitored") \
                    or any(r.get("movieId") == item_id for r in queue):
                return  # the Arr found one itself, or it's no longer wanted
            wanted: set[int] = set()
            releases = k.arr.get("/release", timeout=SEARCH_TIMEOUT, movieId=item_id)
            what = movie.get("title") or f["name"]
        else:
            queued = {r.get("episodeId") for r in queue}
            for r in queue:  # downloads Sonarr can't match carry no episodeId
                if r.get("seriesId") == item_id and not r.get("episodeId") and r.get("downloadId"):
                    queued |= set(grab_episodes(stack, r["downloadId"])[1])
            episodes = {e["id"]: e for e in k.arr.get("/episode", seriesId=item_id)}
            wanted = {e for e in f["episodes"] if e in episodes and e not in queued
                      and not episodes[e].get("hasFile") and episodes[e].get("monitored")}
            if not wanted:
                return
            if len(wanted) == 1:
                releases = k.arr.get("/release", timeout=SEARCH_TIMEOUT,
                                     episodeId=next(iter(wanted)))
            else:
                releases = []
                for season in sorted({episodes[e]["seasonNumber"] for e in wanted}):
                    releases += k.arr.get("/release", timeout=SEARCH_TIMEOUT,
                                          seriesId=item_id, seasonNumber=season)
            what = f"{len(wanted)} episode(s) of {k.item(item_id).get('title') or f['name']}"
        pick = pick_fallback(releases, k.kind, item_id, wanted)
        if not pick:
            self._log(f"No release at all for {what} after dropping {f['name']}; "
                      f"{k.arr.name} keeps looking")
            return
        with self.op_lock:
            release_grab(stack, k.kind, item_id, pick["guid"], pick["indexerId"])
        quality = ((pick.get("quality") or {}).get("quality") or {}).get("name", "?")
        why = "; ".join(pick.get("rejections") or [])
        self._log(f"{k.arr.name} found nothing it accepts after dropping {f['name']}: grabbed "
                  f"{pick['title']} ({quality}, {pick.get('seeders')} seeders"
                  + (f"; below the profile: {why})" if why else ")"))

    def status(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        with self.lock:
            stalling = [
                {"hash": h, "name": e["name"], "progress": e["progress"], "state": e["state"],
                 "since": e["since"], "rotateAt": e["since"] + self.stall_s}
                for h, e in self.state["torrents"].items() if now - e["since"] >= SHOW_AFTER]
            log = [dict(x) for x in reversed(self.log)]
        stalling.sort(key=lambda x: x["since"])
        return {"stallMinutes": self.stall_s / 60, "minProgress": self.min_progress,
                "lastRun": self.last_run, "lastError": self.last_error,
                "stalling": stalling, "log": log}
