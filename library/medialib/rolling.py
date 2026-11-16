"""Rolling window: keep only the next few unwatched episodes of a very long show wanted.

For shows like One Piece, where TVDB's seasons don't match how anyone watches. The user
picks a starting episode number and a window size; Sonarr gets exactly the next `ahead`
not-yet-watched episodes monitored (so downloaded), and nothing else. Watching in Jellyfin
(or the janitor removing a watched file) moves the window on.
"""

from __future__ import annotations

import threading
import time

from medialib.background import run_forever
from medialib.clients import fetch_public_json
from medialib.errors import LibraryError
from medialib.fmt import episode_code, parse_utc
from medialib.following import refresh_series
from medialib.seasons import removed_episode_ids
from medialib.state import load_json, save_json

DEFAULT_AHEAD = 12
MAX_AHEAD = 50
SEARCH_AGAIN_S = 6 * 3600  # re-search a still-missing window episode after this long
INTERVAL = 300  # background cycle, seconds
SEARCH_KEEP_S = 7 * 86400
SKYHOOK_URL = "https://skyhook.sonarr.tv/v1/tvdb/shows/en/{}"
SKYHOOK_TTL = 3600
_skyhook: dict[int, tuple[float, dict]] = {}  # tvdb id -> (fetched at, answer)


# -- pure helpers --

def episode_order(episodes: list[dict]) -> list[dict]:
    """Sonarr episodes in watch order: seasonNumber > 0 only, sorted by (seasonNumber, episodeNumber)."""
    return sorted((e for e in episodes if (e.get("seasonNumber") or 0) > 0),
                  key=lambda e: (e["seasonNumber"], e.get("episodeNumber") or 0))


def number_of(ep: dict, index: int) -> int:
    """The episode's absoluteEpisodeNumber, else its 1-based position (index + 1) in the order."""
    return ep.get("absoluteEpisodeNumber") or index + 1


def find_start(ordered: list[dict], number: int) -> int:
    """Index of the first episode whose number_of() >= number."""
    if number < 1:
        raise LibraryError("The starting episode number must be 1 or more")
    for i, ep in enumerate(ordered):
        if number_of(ep, i) >= number:
            return i
    last = number_of(ordered[-1], len(ordered) - 1) if ordered else 0
    raise LibraryError(f"Episode {number} is past the last episode ({last})")


def plan_window(ordered: list[dict], start: int, done: set[int],
                ahead: int) -> tuple[int, list[dict]]:
    """(frontier, window): the position after the last done episode, and the next `ahead` to keep."""
    frontier = start
    for i in range(start, len(ordered)):
        if ordered[i]["id"] in done:
            frontier = i + 1
    window = [e for e in ordered[frontier:] if e["id"] not in done][:ahead]
    return frontier, window


def monitor_changes(ordered: list[dict], window_ids: set[int],
                    queued: set[int]) -> tuple[list[int], list[int]]:
    """(to_monitor, to_unmonitor); files and queued downloads are never touched."""
    to_monitor = [e["id"] for e in ordered if e["id"] in window_ids and not e.get("monitored")]
    to_unmonitor = [e["id"] for e in ordered
                    if e["id"] not in window_ids and e.get("monitored")
                    and not e.get("hasFile") and e["id"] not in queued]
    return to_monitor, to_unmonitor


def _aired(ep: dict, now: float) -> bool:
    air = parse_utc(ep.get("airDateUtc"))
    return air is not None and air <= now


def search_due(window: list[dict], queued: set[int], searched: dict[str, float],
               now: float) -> list[int]:
    """Aired window episodes without a file or download that were not searched recently."""
    due = []
    for ep in window:
        if ep.get("hasFile") or ep["id"] in queued or not _aired(ep, now):
            continue
        last = searched.get(str(ep["id"]))
        if last is None or now - last >= SEARCH_AGAIN_S:
            due.append(ep["id"])
    return due


# -- state --

class RollingStore:
    """Rolling-window settings per Sonarr series, saved to a JSON file (thread-safe; path None = memory only)."""

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self._series: dict[int, dict] = {}
        data = load_json(path, {})
        raw = data.get("series") if isinstance(data, dict) else None
        for key, v in (raw if isinstance(raw, dict) else {}).items():
            try:
                self._series[int(key)] = {
                    "start": int(v["start"]), "ahead": int(v["ahead"]),
                    "since": float(v.get("since") or time.time()),
                    "searched": {str(k): float(t) for k, t in (v.get("searched") or {}).items()}}
            except (TypeError, ValueError, KeyError, AttributeError):
                continue

    def _save(self) -> None:
        if self.path:
            save_json(self.path, {"series": {str(i): v for i, v in self._series.items()}})

    @staticmethod
    def _public(entry: dict) -> dict:
        return {"start": entry["start"], "ahead": entry["ahead"], "since": entry["since"]}

    def get(self, series_id: int) -> dict | None:
        with self._lock:
            entry = self._series.get(series_id)
            return self._public(entry) if entry else None

    def all(self) -> dict[int, dict]:
        with self._lock:
            return {i: self._public(v) for i, v in self._series.items()}

    def set(self, series_id: int, start: int, ahead: int) -> None:
        with self._lock:
            entry = self._series.setdefault(series_id, {"since": time.time(), "searched": {}})
            entry.update(start=start, ahead=ahead)
            self._save()

    def remove(self, series_id: int) -> None:
        with self._lock:
            if self._series.pop(series_id, None) is not None:
                self._save()

    def searched(self, series_id: int) -> dict[str, float]:
        with self._lock:
            return dict((self._series.get(series_id) or {}).get("searched", {}))

    def record_search(self, series_id: int, episode_ids: list[int], now: float) -> None:
        with self._lock:
            entry = self._series.get(series_id)
            if entry is None:
                return
            searched = entry["searched"]
            searched.update({str(i): now for i in episode_ids})
            entry["searched"] = {k: t for k, t in searched.items() if now - t <= SEARCH_KEEP_S}
            self._save()


# -- operations on Sonarr --

def watched_paths(stack) -> set[str]:
    """File paths of episodes any Jellyfin user has played."""
    jf = stack.jellyfin
    users = jf.get("/Users")
    if not users:
        raise LibraryError("Jellyfin returned no users")
    paths: set[str] = set()
    for u in users:
        items = jf.get("/Items", userId=u["Id"], Recursive="true", Filters="IsPlayed",
                       IncludeItemTypes="Episode", Fields="Path",
                       enableUserData="true").get("Items", [])
        paths |= {it["Path"] for it in items if it.get("Path")}
    return paths


def done_ids(k, series_id: int, episodes: list[dict], watched: set[str]) -> set[int]:
    """Episodes already watched: their file was removed, or Jellyfin says it was played."""
    done = removed_episode_ids(k, series_id)
    if watched:
        paths = {f["id"]: f.get("path") for f in k.arr.get("/episodefile", seriesId=series_id)}
        done |= {e["id"] for e in episodes
                 if e.get("episodeFileId") and paths.get(e["episodeFileId"]) in watched}
    return done


def _summary(ep: dict, index: int) -> dict:
    return {"id": ep.get("id"), "number": number_of(ep, index),
            "code": episode_code(ep.get("seasonNumber"), ep.get("episodeNumber")),
            "title": ep.get("title") or "", "airAt": parse_utc(ep.get("airDateUtc"))}


def _state(ep: dict, queued: set[int], now: float) -> str:
    if ep.get("hasFile"):
        return "on disk"
    if ep["id"] in queued:
        return "downloading"
    return "looking" if _aired(ep, now) else "to air"


def _plan(stack, store: RollingStore, series_id: int, now: float,
          watched: set[str] | None) -> dict:
    """Everything apply() and status() need, read-only, so the two can't drift."""
    cfg = store.get(series_id)
    if cfg is None:
        raise LibraryError("No rolling window for this series")
    k = stack.kind("show")
    series = k.item(series_id)
    episodes = k.arr.get("/episode", seriesId=series_id)
    ordered = episode_order(episodes)
    queued = {r["episodeId"] for r in k.queue_for(series_id) if r.get("episodeId")}
    if watched is None:
        try:
            watched = watched_paths(stack)
        except LibraryError:
            watched = set()  # removed history still advances the window
    start = find_start(ordered, cfg["start"])
    frontier, window = plan_window(ordered, start, done_ids(k, series_id, episodes, watched),
                                   cfg["ahead"])
    index = {e["id"]: i for i, e in enumerate(ordered)}
    window_ids = {e["id"] for e in window}
    counts = {"on disk": 0, "downloading": 0, "looking": 0, "to air": 0}
    rows = []
    for e in window:
        state = _state(e, queued, now)
        counts[state] += 1
        rows.append(dict(_summary(e, index[e["id"]]), state=state))
    caught_up = all(e["id"] in window_ids for e in ordered[frontier:] if _aired(e, now))
    return {"k": k, "series": series, "ordered": ordered, "queued": queued, "window": window,
            "window_ids": window_ids, "start_index": start,
            "status": {"start": cfg["start"], "ahead": cfg["ahead"], "since": cfg["since"],
                       "position": _summary(ordered[frontier], frontier)
                       if frontier < len(ordered) else None,
                       "caughtUp": caught_up, "window": rows, "counts": counts}}


def apply(stack, store: RollingStore, series_id: int, now: float | None = None,
          watched: set[str] | None = None) -> dict:
    """Make Sonarr's monitoring match the window and search what it is missing."""
    now = now if now is not None else time.time()
    p = _plan(stack, store, series_id, now, watched)
    k, series = p["k"], p["series"]

    # A monitored season makes Sonarr monitor (and RSS-grab) all its episodes, and
    # monitorNewItems would do the same for new seasons: the window owns monitoring.
    # The series itself stays monitored so Sonarr re-searches after subgate rejects a
    # release. This goes before the episode PUTs: flipping a season flag resets its episodes.
    if (not series.get("monitored") or series.get("monitorNewItems") != "none"
            or any(s.get("monitored") for s in series.get("seasons") or [])):
        body = dict(series, monitored=True, monitorNewItems="none",
                    seasons=[dict(s, monitored=False) for s in series.get("seasons") or []])
        k.arr.send("PUT", f"/series/{series_id}", body)
        p = _plan(stack, store, series_id, now, watched)  # Sonarr just changed episode flags

    to_monitor, to_unmonitor = monitor_changes(p["ordered"], p["window_ids"], p["queued"])
    if to_unmonitor:
        k.arr.send("PUT", "/episode/monitor", {"episodeIds": to_unmonitor, "monitored": False})
    if to_monitor:
        k.arr.send("PUT", "/episode/monitor", {"episodeIds": to_monitor, "monitored": True})

    due = search_due(p["window"], p["queued"], store.searched(series_id), now)
    if due:
        k.arr.command({"name": "EpisodeSearch", "episodeIds": due})
        store.record_search(series_id, due, now)
    return dict(p["status"], monitored=len(to_monitor), unmonitored=len(to_unmonitor),
                searched=len(due))


def status(stack, store: RollingStore, series_id: int, now: float | None = None) -> dict | None:
    """The window for the details dialog (read-only); None if the series has none."""
    if store.get(series_id) is None:
        return None
    return _plan(stack, store, series_id, now if now is not None else time.time(), None)["status"]


def start_window(stack, store: RollingStore, tvdb_id: int, start: int, ahead: int) -> dict:
    """Turn the rolling window on for a show, adding it to Sonarr first if needed."""
    if not 1 <= ahead <= MAX_AHEAD:
        raise LibraryError(f"Keep between 1 and {MAX_AHEAD} episodes ahead")
    if start < 1:
        raise LibraryError("The starting episode number must be 1 or more")
    k = stack.kind("show")
    series = k.in_library(tvdb_id)
    added = series is None
    if added:
        body = k.add_body(k.lookup_id(tvdb_id), monitored=True)
        body.update(monitorNewItems="none",
                    addOptions={"monitor": "none", "searchForMissingEpisodes": False})
        series = k.arr.send("POST", "/series", body)
        refresh_series(k.arr, series["id"], new=True)  # never RefreshSeries ourselves
    sid = series["id"]
    ordered = episode_order(k.arr.get("/episode", seriesId=sid))
    first = ordered[find_start(ordered, start)]  # before store.set: a bad number stores nothing
    store.set(sid, start, ahead)
    result = apply(stack, store, sid)
    searching, to_air = result["searched"], result["counts"]["to air"]
    if searching and to_air:
        tail = f"searching {searching} now, the rest come as they air"
    elif searching:
        tail = f"searching {searching} now"
    elif to_air:
        tail = "the rest come as they air"
    else:
        tail = "Sonarr has them all"
    title = series["title"]
    message = (f'{title} from episode {start} "{first.get("title") or "?"}" '
               f'({episode_code(first["seasonNumber"], first["episodeNumber"])}): '
               f"keeping the next {ahead} episodes on disk; {tail}.")
    return {"id": sid, "added": added, "label": f"{title} ({series.get('year')})",
            "message": message}


def stop_window(stack, store: RollingStore, series_id: int) -> dict:
    """Turn the window off; unmonitor what is only waiting, keep files and the series' flag."""
    k = stack.kind("show")
    series = k.item(series_id)
    store.remove(series_id)
    queued = {r["episodeId"] for r in k.queue_for(series_id) if r.get("episodeId")}
    ids = [e["id"] for e in k.arr.get("/episode", seriesId=series_id)
           if e.get("monitored") and not e.get("hasFile") and e["id"] not in queued]
    if ids:
        k.arr.send("PUT", "/episode/monitor", {"episodeIds": ids, "monitored": False})
    return {"message": f"{series['title']}: rolling window off. "
                       "Episodes on disk stay; nothing new is downloaded."}


def _skyhook_show(tvdb_id: int, now: float) -> dict:
    hit = _skyhook.get(tvdb_id)
    if hit and now - hit[0] < SKYHOOK_TTL:
        return hit[1]
    show = fetch_public_json(SKYHOOK_URL.format(tvdb_id))
    _skyhook[tvdb_id] = (now, show)
    return show


def preview(stack, tvdb_id: int, number: int) -> dict:
    """The episode a start number points to, before anything is added."""
    k = stack.kind("show")
    series = k.in_library(tvdb_id)
    if series:
        title, episodes = series["title"], k.arr.get("/episode", seriesId=series["id"])
    else:
        show = _skyhook_show(tvdb_id, time.time())
        title, episodes = show.get("title") or f"tvdb:{tvdb_id}", show.get("episodes") or []
    ordered = episode_order(episodes)
    if not ordered:
        raise LibraryError(f"{title} has no episodes yet")
    last = number_of(ordered[-1], len(ordered) - 1)
    if number < 1 or number > last:
        raise LibraryError(f"{title} has episodes 1–{last}")
    i = find_start(ordered, number)
    return dict(_summary(ordered[i], i), last=last)


# -- background service --

class RollingWindow:
    """Background service: applies every series' rolling window every INTERVAL seconds."""

    def __init__(self, make_stack, store: RollingStore, interval: int = INTERVAL,
                 lock: threading.Lock | None = None, log_size: int = 50):
        self.make_stack = make_stack
        self.store = store
        self.interval = interval
        self.lock = lock or threading.Lock()  # shared with the web page's jobs
        self.log_size = log_size
        self._state_lock = threading.Lock()
        self.log: list[dict] = []
        self.last_run: float | None = None
        self.last_error: str | None = None

    def _log(self, when: float, message: str) -> None:
        with self._state_lock:
            self.log.append({"t": when, "msg": message})
            if len(self.log) > self.log_size:
                self.log[:] = self.log[-self.log_size:] if self.log_size > 0 else []

    def start(self) -> None:
        run_forever(self.cycle, self.interval, self._failed)

    def _failed(self, exc: Exception) -> None:
        with self._state_lock:
            self.last_error = str(exc)
        self._log(time.time(), f"Error: {exc}")

    def series_ids(self) -> set[int]:
        return set(self.store.all())

    def cycle(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        configured = self.store.all()
        if not configured:
            return
        stack = self.make_stack()
        all_series = {s["id"]: s for s in stack.sonarr.get("/series")}
        try:
            watched = watched_paths(stack)
        except LibraryError as e:
            watched = set()
            self._log(now, f"Jellyfin watched list unavailable ({e}); using Sonarr history only")
        last_error = None
        for sid in configured:
            series = all_series.get(sid)
            if series is None:
                self.store.remove(sid)
                self._log(now, f"Series {sid} deleted from Sonarr: rolling window removed")
                continue
            title = series["title"]
            if not series.get("monitored"):
                self.store.remove(sid)
                self._log(now, f"{title} is no longer followed: rolling window removed")
                continue
            try:
                with self.lock:
                    result = apply(stack, self.store, sid, now, watched)
            except LibraryError as e:
                last_error = f"{title}: {e}"
                self._log(now, f"Error: {last_error}")
                continue
            if result["monitored"] or result["unmonitored"] or result["searched"]:
                self._log(now, self._describe(title, result))
        with self._state_lock:
            self.last_run, self.last_error = now, last_error

    @staticmethod
    def _describe(title: str, result: dict) -> str:
        pos = result["position"]
        parts = []
        if result["monitored"]:
            parts.append(f"{result['monitored']} more wanted")
        if result["unmonitored"]:
            parts.append(f"{result['unmonitored']} dropped")
        if result["searched"]:
            parts.append(f"searching {result['searched']}")
        where = f"now at episode {pos['number']}" if pos else "reached the last episode"
        return f"{title}: {where}; {', '.join(parts)}"

    def status(self) -> dict:
        with self._state_lock:
            return {"lastRun": self.last_run, "lastError": self.last_error,
                    "series": len(self.store.all()),
                    "log": [e.copy() for e in reversed(self.log)]}
