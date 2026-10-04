"""Free disk space: remove what has been watched, and pause downloads when the disk runs low.

Watched state comes from Jellyfin (any user counts). An episode or movie is removed
GRACE_DAYS after it was last played, unless someone marked it (or its season/show) a
favourite. Episodes: unmonitor (so Sonarr doesn't fetch it again), delete the file through
Sonarr, then delete the torrent once none of its files are still hardlinked into the
library; only then is the space actually free. Movies: the library's full delete.

Library files are hardlinks into qB's downloads, so torrents are matched by inode, which
needs /data mounted locally (the container, LIBRARY_LOCAL_DATA=1).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import core

GB = 1024 ** 3
GRACE_DAYS = float(os.environ.get("JANITOR_GRACE_DAYS", "3"))
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

def parse_jf_date(value: str | None) -> float | None:
    """Jellyfin dates carry 7 fractional digits; Python wants at most 6."""
    if not value:
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


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
            played = parse_jf_date(ud.get("LastPlayedDate"))
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
    code = f"S{e['season'] or 0:02d}E{e['episode'] or 0:02d}"
    return f"{e['series']} {code}"


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class Janitor:
    def __init__(self, make_stack, state_path: str | None, lock: threading.Lock | None = None,
                 data_root: str = "/data", log_size: int = 80):
        self.make_stack, self.state_path, self.data_root = make_stack, state_path, data_root
        self.op_lock = lock or threading.Lock()   # shared with the web page's jobs
        self.lock = threading.Lock()
        self.tick_lock = threading.Lock()
        self.log_size = log_size
        self.state = {"seen": {}, "pending": [], "paused": []}
        if state_path:
            try:
                with open(state_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.state.update({k: loaded[k] for k in self.state if k in loaded})
            except (OSError, ValueError):
                pass
        self.log: list[dict] = []
        self.scheduled: list[dict] = []
        self.kept = 0
        self.disk = {"free": None, "total": None}
        self.last_clean = 0.0
        self.last_error: str | None = None
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
            os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            self._log(f"Error saving state: {e}")

    def free_space(self) -> tuple[int, int]:
        st = os.statvfs(self.data_root)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize

    def start(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.tick()
                    self.last_error = None
                except Exception as e:  # keep running; show it on the page
                    self.last_error = str(e)
                    self._log(f"Error: {e}")
                time.sleep(TICK)
        threading.Thread(target=loop, daemon=True).start()

    def tick(self, now: float | None = None) -> None:
        if not self.tick_lock.acquire(blocking=False):
            return  # a check is already running ("Check now" during a scheduled one)
        try:
            self._tick(now or time.time())
        finally:
            self.tick_lock.release()

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
                stack.qb.call("torrents/stop", {"hashes": "|".join(todo)})
                self.state["paused"] = sorted(set(self.state["paused"]) | set(todo))
                self._log(f"Only {core.human(free)} free: paused {len(todo)} download(s)")
        elif action == "resume":
            have = {t["hash"] for t in stack.qb.torrents()}
            back = [h for h in self.state["paused"] if h in have]
            if back:
                stack.qb.call("torrents/start", {"hashes": "|".join(back)})
            self.state["paused"] = []
            self._log(f"{core.human(free)} free again: resumed {len(back)} download(s)")

    # -- watched cleanup --

    def _jellyfin_watched(self, stack, now: float) -> dict[str, dict]:
        jf = stack.jellyfin
        users = jf.get("/Users")
        if not users:
            raise core.LibraryError("Jellyfin returned no users")
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

    def clean(self, stack, now: float, low: bool) -> None:
        watched = self._jellyfin_watched(stack, now)
        grace = GRACE_DAYS * 86400
        due = [e for e in watched.values() if is_due(e, now, grace, low)]
        due.sort(key=lambda e: e["watchedAt"])
        with self.lock:
            self.kept = sum(1 for e in watched.values() if e["favourite"])
            self.scheduled = sorted(
                ({"label": label(e), "kind": e["kind"], "users": e["users"],
                  "itemId": e["itemId"], "userId": e["userIds"][0],
                  "watchedAt": e["watchedAt"], "removeAt": e["watchedAt"] + grace}
                 for e in watched.values() if not e["favourite"]),
                key=lambda s: s["removeAt"])
        if due or self.state["pending"]:
            self._inodes = None
            with self.op_lock:
                self._remove(stack, due[:MAX_PER_PASS])
                self._release_torrents(stack)

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
                    res = core.delete_execute(stack, core.delete_plan(stack, "movie", m["id"]),
                                              log=lambda _m: None)
                    self._log(f"Removed {label(e)} (watched), freed {res['freedHuman']}"
                              + ("" if res["ok"] else f"; problems: {', '.join(res['problems'])}"))
                else:
                    if episodes is None:
                        episodes = stack.sonarr.get("/series")
                    self._remove_episode(stack, e, episodes)
            except core.LibraryError as err:
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

    def _torrent_files(self, stack, t: dict) -> list[str]:
        files = json.loads(stack.qb.call("torrents/files", {"hash": t["hash"]}))
        return [os.path.join(t["save_path"], f["name"]) for f in files]

    def _inode_map(self, stack) -> dict[int, list[str]]:
        """inode -> hashes of finished torrents holding it; built once per pass."""
        if self._inodes is None:
            self._inodes = {}
            for t in stack.qb.torrents():
                if t["progress"] < 1:
                    continue
                for p in self._torrent_files(stack, t):
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
            for p in self._torrent_files(stack, t):
                try:
                    links.append(os.stat(p).st_nlink)
                except OSError:
                    pass
            if releasable(links):
                stack.qb.delete([h])
                self._log(f"Deleted torrent {t['name']}, freed {core.human(t['size'])}")
            else:
                keep.append(h)  # e.g. a season pack with episodes not watched yet
        self.state["pending"] = keep

    # -- page --

    def status(self) -> dict:
        with self.lock:
            return {"disk": self.disk, "pauseBelow": PAUSE_BELOW, "resumeAbove": RESUME_ABOVE,
                    "paused": len(self.state["paused"]), "graceDays": GRACE_DAYS,
                    "scheduled": [dict(s) for s in self.scheduled], "kept": self.kept,
                    "waitingTorrents": len(self.state["pending"]),
                    "lastClean": self.last_clean or None, "lastError": self.last_error,
                    "log": list(reversed(self.log))}
