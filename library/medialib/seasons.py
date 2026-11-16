"""Seasons of a show in Sonarr: what's on disk, what's missing, and getting a season.

"Missing" means aired, no file, and never had one. An episode whose file was deleted
(the janitor after it was watched, or by hand) is "removed": getting its season again
must not bring it back.
"""

from __future__ import annotations

import time

from medialib.errors import LibraryError
from medialib.fmt import human, parse_utc
from medialib.stack import Kind, Stack


def season_rows(k: Kind, series: dict) -> list[dict]:
    """One row per season for the details dialog (specials only if any are on disk)."""
    episodes = k.arr.get("/episode", seriesId=series["id"])
    removed = removed_episode_ids(k, series["id"])
    queued = {r.get("episodeId") for r in k.queue_for(series["id"])}
    now = time.time()
    rows = []
    for s in sorted(series.get("seasons") or [], key=lambda s: s["seasonNumber"]):
        number = s["seasonNumber"]
        eps = [e for e in episodes if e["seasonNumber"] == number]
        st = s.get("statistics") or {}
        have = sum(1 for e in eps if e.get("hasFile"))
        if number == 0 and not have:
            continue
        missing = [e for e in eps if _missing(e, removed, now)]
        rows.append({
            "number": number, "label": f"Season {number}" if number else "Specials",
            "have": have, "total": len(eps) or st.get("totalEpisodeCount", 0),
            "missing": sum(1 for e in missing if e["id"] not in queued),
            "downloading": sum(1 for e in eps if e["id"] in queued and not e.get("hasFile")),
            "upcoming": sum(1 for e in eps if not _aired(e, now)),
            "removed": sum(1 for e in eps if e["id"] in removed and not e.get("hasFile")),
            "sizeHuman": human(st["sizeOnDisk"]) if st.get("sizeOnDisk") else "",
            "monitored": s.get("monitored"),
        })
    return rows


def get_season(stack: Stack, series_id: int, number: int) -> dict:
    """Make Sonarr download a season's missing episodes (and the rest as they air)."""
    k = stack.kind("show")
    series = k.item(series_id)
    season = next((s for s in series.get("seasons") or [] if s["seasonNumber"] == number), None)
    if season is None:
        raise LibraryError(f"{series['title']} has no season {number}")
    episodes = [e for e in k.arr.get("/episode", seriesId=series_id) if e["seasonNumber"] == number]
    removed = removed_episode_ids(k, series_id)
    now = time.time()
    missing = [e for e in episodes if _missing(e, removed, now)]
    upcoming = [e for e in episodes if not _aired(e, now)]
    if not missing and not upcoming:
        raise LibraryError(f"Nothing to get: every aired episode of {_name(series, number)} "
                           "is on disk or was already watched and removed.")

    # A monitored season (needed so episodes TVDB adds later are wanted too) makes Sonarr
    # monitor all its episodes, and a monitored series is needed for Sonarr to retry after
    # subgate rejects a release. Then put the removed episodes back to unmonitored.
    season["monitored"] = True
    k.arr.send("PUT", f"/series/{series_id}", dict(series, monitored=True))
    gone = [e["id"] for e in episodes if e["id"] in removed and not e.get("hasFile")]
    if gone:
        k.arr.send("PUT", "/episode/monitor", {"episodeIds": gone, "monitored": False})
    k.arr.send("PUT", "/episode/monitor",
               {"episodeIds": [e["id"] for e in missing + upcoming], "monitored": True})

    aired = [e for e in episodes if _aired(e, now)]
    if missing and len(missing) == len(aired):  # nothing of it yet: season packs are fine
        k.arr.command({"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": number})
    elif missing:
        k.arr.command({"name": "EpisodeSearch", "episodeIds": [e["id"] for e in missing]})

    parts = []
    if missing:
        parts.append(f"searching for {len(missing)} missing episode{'s' * (len(missing) != 1)}")
    if upcoming:
        parts.append(f"{len(upcoming)} more as {'they air' if len(upcoming) != 1 else 'it airs'}")
    note = f" Skipped {len(gone)} already watched and removed." if gone else ""
    return {"message": f"{_name(series, number)}: {' and '.join(parts)}.{note}"}


def removed_episode_ids(k: Kind, series_id: int) -> set[int]:
    """Episodes whose file was deleted for any reason but an upgrade."""
    return {h["episodeId"] for h in k.history(series_id)
            if h.get("eventType") == "episodeFileDeleted"
            and (h.get("data") or {}).get("reason") != "Upgrade" and h.get("episodeId")}


def _aired(e: dict, now: float) -> bool:
    air = parse_utc(e.get("airDateUtc"))
    return air is not None and air <= now


def _missing(e: dict, removed: set[int], now: float) -> bool:
    return not e.get("hasFile") and _aired(e, now) and e["id"] not in removed


def _name(series: dict, number: int) -> str:
    return f"{series['title']} {'specials' if number == 0 else f'season {number}'}"
