"""Following: shows and anime whose new episodes Sonarr grabs as they come out."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from medialib.clients import Arr
from medialib.errors import LibraryError
from medialib.fmt import episode_code, episode_counts, iso_utc, parse_utc
from medialib.stack import Stack, poster

if TYPE_CHECKING:
    from medialib.watcher import AiringWatcher

MODES = {  # mode -> what Sonarr grabs, for the confirmation message
    "new": "new episodes as they air",
    "season": "season {season} now, then new episodes as they air",
    "all": "every episode now, then new ones as they air",
}
SEASON_WORDS = re.compile(r"\b(season|part|cour)\s*\d+\b|\b\d+(st|nd|rd|th)\s+season\b", re.I)


def _aired(ep: dict, now: float) -> bool:
    air = parse_utc(ep.get("airDateUtc"))
    return air is not None and air <= now


def _refreshing(arr: Arr, series_id: int) -> bool:
    for c in arr.get("/command"):
        body = c.get("body") or {}
        ids = body.get("seriesIds") or ([body["seriesId"]] if body.get("seriesId") else [])
        if (c.get("name") == "RefreshSeries" and c.get("status") in ("queued", "started")
                and (not ids or series_id in ids)):
            return True
    return False


def refresh_series(arr: Arr, series_id: int, new: bool, timeout: int = 120) -> None:
    """Load a series' episode list without racing Sonarr's own refresh.

    Sonarr refreshes a series by itself right after it's added. A second RefreshSeries
    started while one runs makes both insert the episode list, so every episode ends up
    in Sonarr twice and no release name can be matched (seen 2026-10-04). So: wait for
    any running refresh, and for a new series that wait is all that's needed.
    """
    deadline, start, seen = time.time() + timeout, time.time(), False
    while time.time() < deadline:
        if _refreshing(arr, series_id):
            seen = True
        elif seen or not new or arr.get("/episode", seriesId=series_id) or time.time() - start > 15:
            break
        time.sleep(1)
    if not new:
        arr.run_command({"name": "RefreshSeries", "seriesId": series_id})


def follow(stack: Stack, tvdb_id: int, mode: str, season: int | None = None) -> dict:
    """Make Sonarr grab this show's episodes as they air (adding it if needed)."""
    if mode not in MODES:
        raise LibraryError(f"unknown follow mode {mode!r}")
    k = stack.kind("show")
    series = k.in_library(tvdb_id)
    added = series is None
    if added:
        body = k.add_body(k.lookup_id(tvdb_id), monitored=True)
        body.update(monitorNewItems="all",
                    addOptions={"monitor": "none", "searchForMissingEpisodes": False})
        series = k.arr.send("POST", "/series", body)
    refresh_series(k.arr, series["id"], new=added)
    series = k.item(series["id"])
    sid = series["id"]
    numbers = [x["seasonNumber"] for x in series.get("seasons", []) if x["seasonNumber"] > 0]
    latest = max(numbers) if numbers else 1
    target = season if season is not None else latest
    if mode == "season" and numbers and target not in numbers:
        raise LibraryError(f"{series['title']} has no season {target} on TVDB "
                           f"(seasons: {', '.join(map(str, numbers))})")

    now = time.time()
    episodes = k.arr.get("/episode", seriesId=sid)
    wanted = {"all": lambda e: e["seasonNumber"] > 0,
              "season": lambda e: e["seasonNumber"] == target,
              "new": lambda e: False}[mode]
    pick = [e for e in episodes if wanted(e) or not _aired(e, now)]
    if pick:
        k.arr.send("PUT", "/episode/monitor",
                   {"episodeIds": [e["id"] for e in pick], "monitored": True})

    # Episodes TVDB adds later inherit their season's flag, so the season(s) we care about
    # must be monitored too; monitorNewItems covers seasons that don't exist yet.
    seasons = {e["seasonNumber"] for e in pick} | {latest} | ({target} if mode == "season" else set())
    for x in series.get("seasons", []):
        if x["seasonNumber"] in seasons or (mode == "all" and x["seasonNumber"] > 0):
            x["monitored"] = True
    series.update(monitored=True, monitorNewItems="all")
    k.arr.send("PUT", f"/series/{sid}", series)

    if mode == "all":
        k.arr.command({"name": "SeriesSearch", "seriesId": sid})
    elif mode == "season":
        k.arr.command({"name": "SeasonSearch", "seriesId": sid, "seasonNumber": target})
    return {"id": sid, "added": added, "label": f"{series['title']} ({series.get('year')})",
            "message": f"Following {series['title']}: Sonarr grabs "
                       f"{MODES[mode].format(season=target)}."}


def unfollow(stack: Stack, series_id: int) -> dict:
    k = stack.kind("show")
    series = k.item(series_id)
    k.arr.send("PUT", f"/series/{series_id}", dict(series, monitored=False))
    return {"message": f"Stopped following {series['title']}. Files already downloaded stay."}


def follow_candidates(stack: Stack, tvdb_id: int | None, query: str | None) -> list[dict]:
    """Sonarr matches for a show: the known TVDB id, else a title search."""
    k = stack.kind("show")
    res = []
    if tvdb_id:
        try:
            res = [k.lookup_id(tvdb_id)]
        except LibraryError:
            pass
    if not res and query:
        # "Title Season 2" / "Title 2nd Season": TVDB lists seasons under one show.
        term = re.sub(r"[:\-–]\s*$", "", SEASON_WORDS.sub("", query)).strip() or query
        res = k.lookup(term)[:8]
    library = {i["tvdbId"]: i for i in k.items()}
    return [_candidate(r, library.get(r.get("tvdbId"))) for r in res]


def _candidate(r: dict, mine: dict | None) -> dict:
    return {"tvdbId": r.get("tvdbId"), "title": r.get("title"), "year": r.get("year"),
            "network": r.get("network"), "status": r.get("status"), "poster": poster(r),
            "seasons": sorted(x["seasonNumber"] for x in r.get("seasons", [])
                              if x["seasonNumber"] > 0),
            "inLibrary": mine is not None, "following": bool(mine and mine.get("monitored"))}


def following(stack: Stack, watcher: AiringWatcher | None = None, days_back: int = 2,
              days_ahead: int = 14) -> dict:
    """Followed shows plus recent/upcoming episodes and what is happening with each."""
    k = stack.kind("show")
    now = time.time()
    shows = [{"id": s["id"], "title": s["title"], "year": s.get("year"), "poster": poster(s),
              "status": s.get("status"), "network": s.get("network"),
              "anime": s.get("seriesType") == "anime",
              "nextAiring": parse_utc(s.get("nextAiring")),
              "episodes": episode_counts(s.get("statistics"))}
             for s in sorted(k.items(), key=lambda i: i["title"].lower()) if s.get("monitored")]
    calendar = k.arr.get("/calendar", start=iso_utc(now - days_back * 86400),
                         end=iso_utc(now + days_ahead * 86400), includeSeries="true",
                         unmonitored="false")
    torrents = stack.qb.torrents_by_hash()
    progress = {r["episodeId"]: (torrents.get((r.get("downloadId") or "").lower())
                                 or {"progress": 0.0})["progress"]
                for r in k.arr.queue() if r.get("episodeId")}
    episodes = [_episode(e, progress, watcher, now)
                for e in sorted(calendar, key=lambda e: e.get("airDateUtc") or "")
                if (e.get("series") or {}).get("monitored", True)]
    return {"shows": shows, "episodes": episodes}


def _episode(e: dict, progress: dict[int, float], watcher: AiringWatcher | None,
             now: float) -> dict:
    aired = _aired(e, now)
    if e.get("hasFile"):
        status = "downloaded"
    elif e["id"] in progress:
        status = f"downloading {progress[e['id']] * 100:.0f}%"
    elif not e.get("monitored"):
        status = "not wanted"
    elif aired:
        status = "aired, looking"
    else:
        status = "upcoming"
    air = parse_utc(e.get("airDateUtc"))
    series = e.get("series") or {}
    return {"id": e["id"], "seriesId": e.get("seriesId"), "series": series.get("title", "?"),
            "poster": poster(series), "code": episode_code(e["seasonNumber"], e["episodeNumber"]),
            "absolute": e.get("absoluteEpisodeNumber"), "title": e.get("title") or "",
            "airAt": air, "aired": aired, "status": status,
            "watch": watcher.episode_info(e["id"], air, now)
            if watcher and status == "aired, looking" else None}


def search_episodes(stack: Stack, episode_ids: list[int]) -> dict:
    if not episode_ids:
        raise LibraryError("no episodes given")
    stack.sonarr.command({"name": "EpisodeSearch", "episodeIds": episode_ids})
    return {"message": f"Searching every indexer for {len(episode_ids)} episode(s) now."}
