"""Free disk space: remove what has been watched, and pause downloads when the disk runs low.

Watched state comes from Jellyfin (any user counts). An episode or movie is removed
GRACE_DAYS after it was last played, unless someone marked it (or its season/show) a
favourite. Episodes of shows on a rolling window (rolling.py) have a shorter grace,
ROLLING_GRACE_DAYS, so a long show doesn't pile up watched episodes.

Episodes: unmonitor (so Sonarr doesn't fetch it again), delete the file through Sonarr, then delete the torrent once none of its files are still hardlinked into the
library; only then is the space actually free. Movies: the library's full delete.

Library files are hardlinks into qB's downloads, so torrents are matched by inode, which
needs /data mounted locally (the container, LIBRARY_LOCAL_DATA=1).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from medialib import titles
from medialib.background import run_forever
from medialib.errors import LibraryError
from medialib.fmt import episode_code, human, parse_utc
from medialib.state import load_json, save_json

GB = 1024 ** 3
GRACE_DAYS = float(os.environ.get("JANITOR_GRACE_DAYS", "3"))
ROLLING_GRACE_DAYS = float(os.environ.get("JANITOR_ROLLING_GRACE_DAYS", "1"))
PAUSE_BELOW = float(os.environ.get("JANITOR_PAUSE_BELOW_GB", "20")) * GB
RESUME_ABOVE = float(os.environ.get("JANITOR_RESUME_ABOVE_GB", "25")) * GB
TICK = 120            # disk guard
CLEAN_EVERY = 1800    # watched cleanup (every tick while space is low)
MAX_PER_PASS = 50
MEDIA_ROOT = "/data/media/"
STOPPED = {"stoppedDL", "pausedDL", "stoppedUP", "pausedUP", "error", "missingFiles"}


# --------------------------------------------------------------------------
# Pure helpers (tested)
# --------------------------------------------------------------------------

def merge_watched(per_user: list[tuple[str, list[dict]]], favourites: set[str],
                  seen: dict, now: float) -> dict[str, dict]:
    """Played items of all users, keyed by file path.

    watchedAt is the most recent play by anyone; items marked played without a play
    date (e.g. "mark as watched") count from when we first saw them played.
    """
    out: dict[str, dict] = {}
    for user, items in per_user:
        for it in items:
            path = it.get("Path") or ""
            if not path.startswith(MEDIA_ROOT) or it.get("Type") not in ("Episode", "Movie"):
                continue
            ud = it.get("UserData") or {}
            played = parse_utc(ud.get("LastPlayedDate"))
            e = out.setdefault(path, {
                "path": path, "kind": "movie" if it["Type"] == "Movie" else "episode",
                "itemId": it.get("Id"), "title": it.get("Name") or "",
                "series": it.get("SeriesName"), "season": it.get("ParentIndexNumber"),
                "episode": it.get("IndexNumber"), "users": [], "userIds": [],
                "playedAt": None, "favourite": False})
            e["users"].append(user)
            e["userIds"].append(it.get("_userId"))
            if played and (e["playedAt"] is None or played > e["playedAt"]):
                e["playedAt"] = played
            if ud.get("IsFavorite") or favourites & {it.get("Id"), it.get("SeasonId"),
                                                     it.get("SeriesId")}:
                e["favourite"] = True
    for path, e in out.items():
        first = seen.setdefault(path, now)
        e["watchedAt"] = e["playedAt"] or first
    for path in [p for p in seen if p not in out]:  # unwatched again, or gone
        del seen[path]
    return out


def grace_for(entry: dict, rolling_dirs: list[str], grace_s: float, rolling_s: float) -> float:
    """The grace period for one watched item: shorter for a show on a rolling window."""
    if entry["kind"] == "episode" and any(entry["path"].startswith(d) for d in rolling_dirs):
        return rolling_s
    return grace_s


def is_due(entry: dict, now: float, grace_s: float, low_space: bool) -> bool:
    if entry["favourite"]:
        return False
    return low_space or now >= entry["watchedAt"] + grace_s


def guard_action(free: float, paused: bool, pause_below: float, resume_above: float) -> str | None:
    if free < pause_below:
        return "pause"          # also catches torrents added since the last tick
    if paused and free > resume_above:
        return "resume"
    return None


def releasable(nlinks: list[int]) -> bool:
    """A torrent can go once none of its files are hardlinked into the library."""
    return bool(nlinks) and all(n <= 1 for n in nlinks)


def label(e: dict) -> str:
    if e["kind"] == "movie":
        return e["title"]
    return f"{e['series']} {episode_code(e['season'], e['episode'])}"


def keep(stack, item_id: str, user_id: str) -> None:
    """Mark an item a Jellyfin favourite, which the janitor never removes."""
    stack.jellyfin.send("POST", f"/UserFavoriteItems/{item_id}", userId=user_id)


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class Janitor:
    def __init__(self, make_stack, state_path: str | None, lock: threading.Lock | None = None,
                 data_root: str = "/data", log_size: int = 80,
                 rolling: Callable[[], set[int]] | None = None):
        self.make_stack, self.state_path, self.data_root = make_stack, state_path, data_root
        self.rolling = rolling or set  # Sonarr series ids on a rolling window
        self.op_lock = lock or threading.Lock()   # shared with the web page's jobs
        self.lock = threading.Lock()
        self.tick_lock = threading.Lock()
        self.log_size = log_size
        self.state = {"seen": {}, "pending": [], "paused": []}
        loaded = load_json(state_path, {})
        if isinstance(loaded, dict):
            self.state.update({k: loaded[k] for k in self.state if k in loaded})
        self.log: list[dict] = []
        self.scheduled: list[dict] = []
        self.kept = 0
        self.disk = {"free": None, "total": None}
        self.last_clean = 0.0
        self.last_error: str | None = None
        self.requested = False  # "Check now"/"Force cleanup" started but not finished
        self._inodes: dict[int, list[str]] | None = None

    # -- bookkeeping --

    def _log(self, msg: str) -> None:
        with self.lock:
            self.log.append({"t": time.time(), "msg": msg})
            del self.log[:-self.log_size]

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            save_json(self.state_path, self.state)
        except OSError as e:
            self._log(f"Error saving state: {e}")

    def free_space(self) -> tuple[int, int]:
        st = os.statvfs(self.data_root)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize

    def start(self) -> None:
        run_forever(self.tick, TICK, self._failed)

    def _failed(self, e: Exception) -> None:
        self.last_error = str(e)
        self._log(f"Error: {e}")

    def tick(self, now: float | None = None) -> None:
        if not self.tick_lock.acquire(blocking=False):
            return  # a check is already running ("Check now" during a scheduled one)
        try:
            self._tick(now or time.time())
            self.last_error = None
        finally:
            self.tick_lock.release()

    def check_now(self, force: bool = False) -> None:
        """Start a full check on a thread; `force` also skips the grace period."""
        def run() -> None:
            try:
                if force:
                    self.force_clean()
                else:
                    self.last_clean = 0.0  # due now
                    self.tick()
            except Exception as e:  # noqa: BLE001 - reported like a scheduled run
                self._failed(e)
            finally:
                self.requested = False
        self.requested = True  # before the thread starts, so the page polls straight away
        threading.Thread(target=run, daemon=True).start()

    def force_clean(self) -> bool:
        """Remove everything watched now, skipping the grace period (favourites stay).

        Returns False when a check is already running."""
        if not self.tick_lock.acquire(blocking=False):
            return False
        try:
            self._log("Forced cleanup: grace period skipped")
            self.clean(self.make_stack(), time.time(), True)
            self.last_clean = time.time()
            self._save()
        finally:
            self.tick_lock.release()
        return True

    def _tick(self, now: float) -> None:
        free, total = self.free_space()
        self.disk = {"free": free, "total": total}
        stack = self.make_stack()
        self.guard(stack, free)
        low = free < PAUSE_BELOW
        if low or now - self.last_clean >= CLEAN_EVERY:
            self.clean(stack, now, low)
            self.last_clean = now
        self._save()

    # -- disk guard --

    def guard(self, stack, free: float) -> None:
        action = guard_action(free, bool(self.state["paused"]), PAUSE_BELOW, RESUME_ABOVE)
        if action == "pause":
            todo = [t["hash"] for t in stack.qb.torrents()
                    if t["progress"] < 1 and t["state"] not in STOPPED]
            if todo:
                stack.qb.stop(todo)
                self.state["paused"] = sorted(set(self.state["paused"]) | set(todo))
                self._log(f"Only {human(free)} free: paused {len(todo)} download(s)")
        elif action == "resume":
            have = {t["hash"] for t in stack.qb.torrents()}
            back = [h for h in self.state["paused"] if h in have]
            if back:
                stack.qb.start(back)
            self.state["paused"] = []
            self._log(f"{human(free)} free again: resumed {len(back)} download(s)")

    # -- watched cleanup --

    def _jellyfin_watched(self, stack, now: float) -> dict[str, dict]:
        jf = stack.jellyfin
        users = jf.get("/Users")
        if not users:
            raise LibraryError("Jellyfin returned no users")
        per_user, favourites = [], set()
        for u in users:
            items = jf.get("/Items", userId=u["Id"], Recursive="true", Filters="IsPlayed",
                           IncludeItemTypes="Episode,Movie", Fields="Path",
                           enableUserData="true").get("Items", [])
            for it in items:
                it["_userId"] = u["Id"]
            per_user.append((u["Name"], items))
            favs = jf.get("/Items", userId=u["Id"], Recursive="true", Filters="IsFavorite",
                          IncludeItemTypes="Series,Season,Episode,Movie").get("Items", [])
            favourites |= {f["Id"] for f in favs}
        return merge_watched(per_user, favourites, self.state["seen"], now)

    def _rolling_dirs(self, stack) -> list[str]:
        ids = self.rolling()
        if not ids:
            return []
        return [s["path"].rstrip("/") + "/" for s in stack.sonarr.get("/series") if s["id"] in ids]

    def clean(self, stack, now: float, low: bool) -> None:
        watched = self._jellyfin_watched(stack, now)
        dirs = self._rolling_dirs(stack)
        grace = {path: grace_for(e, dirs, GRACE_DAYS * 86400, ROLLING_GRACE_DAYS * 86400)
                 for path, e in watched.items()}
        due = [e for e in watched.values() if is_due(e, now, grace[e["path"]], low)]
        due.sort(key=lambda e: e["watchedAt"])
        with self.lock:
            self.kept = sum(1 for e in watched.values() if e["favourite"])
            self.scheduled = sorted(
                ({"label": label(e), "kind": e["kind"], "users": e["users"], "path": e["path"],
                  "itemId": e["itemId"], "userId": e["userIds"][0],
                  "watchedAt": e["watchedAt"], "removeAt": e["watchedAt"] + grace[e["path"]]}
                 for e in watched.values() if not e["favourite"]),
                key=lambda s: s["removeAt"])
        if due or self.state["pending"]:
            self._inodes = None
            with self.op_lock:
                self._remove(stack, due[:MAX_PER_PASS])
                self._release_torrents(stack)
            # The list was made before removing; without this the page shows what was just
            # removed until the next pass, up to CLEAN_EVERY later.
            with self.lock:
                self.scheduled = [s for s in self.scheduled if os.path.exists(s["path"])]

    def _remove(self, stack, due: list[dict]) -> None:
        movies = episodes = None
        for e in due:
            try:
                if e["kind"] == "movie":
                    if movies is None:
                        movies = {(m.get("movieFile") or {}).get("path"): m
                                  for m in stack.radarr.get("/movie")}
                    m = movies.get(e["path"])
                    if not m:
                        continue  # not Radarr's (e.g. imported by hand elsewhere)
                    res = titles.delete_execute(
                        stack, titles.delete_plan(stack, "movie", m["id"]), log=lambda _m: None)
                    self._log(f"Removed {label(e)} (watched), freed {res['freedHuman']}"
                              + ("" if res["ok"] else f"; problems: {', '.join(res['problems'])}"))
                else:
                    if episodes is None:
                        episodes = stack.sonarr.get("/series")
                    self._remove_episode(stack, e, episodes)
            except LibraryError as err:
                self._log(f"Couldn't remove {label(e)}: {err}")

    def _remove_episode(self, stack, e: dict, all_series: list[dict]) -> None:
        series = next((s for s in all_series if e["path"].startswith(s["path"].rstrip("/") + "/")),
                      None)
        if not series:
            return
        files = stack.sonarr.get("/episodefile", seriesId=series["id"])
        f = next((x for x in files if x.get("path") == e["path"]), None)
        if not f:
            return
        eps = [x["id"] for x in stack.sonarr.get("/episode", seriesId=series["id"])
               if x.get("episodeFileId") == f["id"]]
        try:
            inode = os.stat(e["path"]).st_ino
        except OSError:
            inode = None
        if eps:  # first, so neither Sonarr nor the airing watcher fetches it again
            stack.sonarr.send("PUT", "/episode/monitor", {"episodeIds": eps, "monitored": False})
        stack.sonarr.send("DELETE", f"/episodefile/{f['id']}")
        holders = self._inode_map(stack).get(inode, []) if inode else []
        self.state["pending"] = sorted(set(self.state["pending"]) | set(holders))
        self._log(f"Removed {label(e)} (watched by {', '.join(e['users'])})")

    def _inode_map(self, stack) -> dict[int, list[str]]:
        """inode -> hashes of finished torrents holding it; built once per pass."""
        if self._inodes is None:
            self._inodes = {}
            for t in stack.qb.torrents():
                if t["progress"] < 1:
                    continue
                for p in stack.qb.file_paths(t):
                    try:
                        self._inodes.setdefault(os.stat(p).st_ino, []).append(t["hash"])
                    except OSError:
                        pass
        return self._inodes

    def _release_torrents(self, stack) -> None:
        live = {t["hash"]: t for t in stack.qb.torrents()}
        keep = []
        for h in self.state["pending"]:
            t = live.get(h)
            if not t:
                continue
            links = []
            for p in stack.qb.file_paths(t):
                try:
                    links.append(os.stat(p).st_nlink)
                except OSError:
                    pass
            if releasable(links):
                stack.qb.delete([h])
                self._log(f"Deleted torrent {t['name']}, freed {human(t['size'])}")
            else:
                keep.append(h)  # e.g. a season pack with episodes not watched yet
        self.state["pending"] = keep

    # -- page --

    def status(self) -> dict:
        with self.lock:
            return {"disk": self.disk, "pauseBelow": PAUSE_BELOW, "resumeAbove": RESUME_ABOVE,
                    "paused": len(self.state["paused"]), "graceDays": GRACE_DAYS,
                    "rollingGraceDays": ROLLING_GRACE_DAYS,
                    "scheduled": [dict(s) for s in self.scheduled], "kept": self.kept,
                    "waitingTorrents": len(self.state["pending"]),
                    "lastClean": self.last_clean or None, "lastError": self.last_error,
                    "running": self.requested or self.tick_lock.locked(),
                    "log": list(reversed(self.log))}
