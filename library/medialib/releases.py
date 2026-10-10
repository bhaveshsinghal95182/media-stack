"""See what is downloading for a title, pick another release, or drop one."""

from __future__ import annotations

from medialib.errors import LibraryError
from medialib.fmt import episode_code, episode_counts, eta, human
from medialib.stack import Kind, Stack

SEARCH_TIMEOUT = 240  # indexer searches through Prowlarr can be slow
# Rejections that only say something is downloading or on disk already; the page says
# that once instead of on every release.
NOISE = ("in queue", "existing file", "meets cutoff")


def _season_of(record: dict) -> int | None:
    return record.get("seasonNumber", (record.get("episode") or {}).get("seasonNumber"))


def _queue(k: Kind, item_id: int, season: int | None = None) -> list[dict]:
    params = {} if k.is_movie else {"includeEpisode": "true"}
    recs = [r for r in k.arr.queue(**params) if r.get(k.queue_key) == item_id]
    if season is not None and not k.is_movie:
        recs = [r for r in recs if _season_of(r) == season]
    return recs


def downloads(stack: Stack, kind: str, item_id: int, season: int | None = None) -> list[dict]:
    """What is downloading for this title right now, with live qBittorrent numbers."""
    k = stack.kind(kind)
    torrents = stack.qb.torrents_by_hash()
    groups: dict[str, dict] = {}
    for r in _queue(k, item_id, season):
        did = (r.get("downloadId") or "").lower()
        g = groups.setdefault(did, {"title": r.get("title", "?"), "episodes": set(),
                                    "status": r.get("trackedDownloadState")})
        if ep := r.get("episode"):
            g["episodes"].add(episode_code(ep.get("seasonNumber"), ep.get("episodeNumber")))
    return [_download(did, g, torrents.get(did)) for did, g in groups.items()]


def _download(download_id: str, group: dict, t: dict | None) -> dict:
    out = {"downloadId": download_id, "title": group["title"],
           "episodes": sorted(group["episodes"]), "state": group["status"],
           "progress": None, "seeds": None, "swarmSeeds": None, "speed": "", "eta": "",
           "subgate": ""}
    if t:
        tags = [x.strip() for x in t.get("tags", "").split(",")]
        out.update(progress=t["progress"], state=t["state"],
                   seeds=t["num_seeds"],          # connected to us
                   swarmSeeds=t["num_complete"],  # the tracker's count
                   speed=human(t["dlspeed"]) + "/s", eta=eta(t["eta"]),
                   subgate=next((x.removeprefix("subgate-") for x in tags
                                 if x.startswith("subgate-")), ""))
    return out


def release_info(stack: Stack, kind: str, item_id: int) -> dict:
    k = stack.kind(kind)
    item = k.item(item_id)
    seasons = [] if k.is_movie else [
        {"number": s["seasonNumber"],
         "label": "Specials" if s["seasonNumber"] == 0 else f"Season {s['seasonNumber']}",
         "have": episode_counts(s.get("statistics"))}
        for s in sorted(item.get("seasons", []), key=lambda x: x["seasonNumber"])]
    return {"kind": kind, "id": item_id, "arr": k.arr.name, "seasons": seasons,
            "label": f"{item['title']} ({item.get('year') or '?'})",
            "hasFile": k.is_movie and bool(item.get("hasFile")),
            "downloads": downloads(stack, kind, item_id)}


def release_search(stack: Stack, kind: str, item_id: int, season: int | None = None) -> dict:
    """Ask Radarr/Sonarr to search every indexer now and return the releases for this title."""
    k = stack.kind(kind)
    if k.is_movie:
        raw = k.arr.get("/release", timeout=SEARCH_TIMEOUT, movieId=item_id)
        mine = [r for r in raw if r.get("mappedMovieId") == item_id]
    else:
        if season is None:
            raise LibraryError("pick a season to search")
        raw = k.arr.get("/release", timeout=SEARCH_TIMEOUT, seriesId=item_id,
                        seasonNumber=season)
        mine = [r for r in raw if r.get("mappedSeriesId") == item_id
                and r.get("mappedSeasonNumber") == season]
    current = {d["downloadId"] for d in downloads(stack, kind, item_id, season)}
    out = [_release(k, r, current) for r in mine if r.get("downloadAllowed")]
    out.sort(key=lambda r: (r["current"], r["blocklisted"], bool(r["rejections"]),
                            not k.is_movie and r["scope"] != "season pack",
                            -(r["seeders"] or 0)))
    return {"releases": out, "searched": len(raw)}


def _release(k: Kind, r: dict, current: set[str]) -> dict:
    why = [x for x in r.get("rejections") or [] if not any(n in x.lower() for n in NOISE)]
    eps = r.get("mappedEpisodeNumbers") or []
    scope = "" if k.is_movie else (
        "season pack" if r.get("fullSeason") else ", ".join(f"E{e:02d}" for e in eps))
    return {
        "guid": r["guid"], "indexerId": r["indexerId"], "title": r["title"],
        "indexer": (r.get("indexer") or "").replace(" (Prowlarr)", ""),
        "quality": ((r.get("quality") or {}).get("quality") or {}).get("name", "?"),
        "size": r.get("size") or 0, "sizeHuman": human(r.get("size") or 0),
        "seeders": r.get("seeders"), "leechers": r.get("leechers"),
        "languages": [x["name"] for x in r.get("languages") or [] if x.get("name")],
        "age": r.get("age"), "rejections": why, "scope": scope,
        "blocklisted": any("blocklist" in x.lower() or "blacklist" in x.lower() for x in why),
        "current": (r.get("infoHash") or "").lower() in current}


def _queue_ids(k: Kind, item_id: int, download_ids) -> list[int]:
    want = {d.lower() for d in download_ids}
    return [r["id"] for r in _queue(k, item_id) if (r.get("downloadId") or "").lower() in want]


def release_grab(stack: Stack, kind: str, item_id: int, guid: str, indexer_id: int,
                 replace_current: bool = False) -> dict:
    """Download a specific release, optionally dropping (and blocklisting) what downloads now."""
    k = stack.kind(kind)
    old = [r["id"] for r in _queue(k, item_id)] if replace_current else []
    body = {"guid": guid, "indexerId": indexer_id}
    if k.is_movie:
        body["movieId"] = item_id
    k.arr.send("POST", "/release", body, timeout=120)
    msg = "Sent to qBittorrent. subgate checks it for English subs/audio first."
    if old:
        # Only after the new grab worked; no re-search, or the Arr grabs a third one.
        k.arr.remove_from_queue(old, blocklist=True, search_again=False)
        msg = "Replaced the old download (blocklisted). " + msg
    return {"message": msg}


def release_drop(stack: Stack, kind: str, item_id: int, download_id: str) -> dict:
    """Remove a download, blocklist it, and let the Arr search for the next best release."""
    k = stack.kind(kind)
    ids = _queue_ids(k, item_id, [download_id])
    if not ids:
        raise LibraryError("that download is no longer in the queue; refresh")
    k.arr.remove_from_queue(ids, blocklist=True, search_again=True)
    return {"message": f"Dropped and blocklisted; {k.arr.name} is searching for another release."}
