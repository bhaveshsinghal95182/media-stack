"""This season's anime from AniList, mapped to TVDB/TMDB (Fribb/anime-lists)."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from medialib.clients import fetch_public_json
from medialib.errors import LibraryError
from medialib.stack import Stack

ANILIST_URL = "https://graphql.anilist.co"
ANIME_MAP_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json"
SEASONS = ("WINTER", "SPRING", "SUMMER", "FALL")
MAX_PAGES = 8
SEASON_QUERY = """query($s:MediaSeason,$y:Int,$p:Int){Page(page:$p,perPage:50){pageInfo{hasNextPage}
media(season:$s,seasonYear:$y,type:ANIME,sort:POPULARITY_DESC,isAdult:false){id format status
episodes duration title{romaji english} coverImage{large} genres averageScore popularity
studios(isMain:true){nodes{name}} nextAiringEpisode{episode airingAt} description(asHtml:false)
siteUrl}}}"""


class TTLCache:
    """Keeps fetched values for a while, and serves stale ones when a refetch fails."""

    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key, ttl: float, fetch: Callable):
        with self._lock:
            hit = self._data.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            value = fetch()
        except LibraryError:
            if hit:
                return hit[1]
            raise
        with self._lock:
            self._data[key] = (time.time(), value)
        return value


_cache = TTLCache()


def anime_map() -> dict[int, dict]:
    """AniList id -> TVDB/TMDB ids (5-6 MB download, kept for a day)."""
    def load():
        return {e["anilist_id"]: e for e in fetch_public_json(ANIME_MAP_URL) if e.get("anilist_id")}
    return _cache.get("anime-map", 24 * 3600, load)


def current_season(now: float | None = None) -> tuple[str, int]:
    t = time.localtime(now)
    return SEASONS[(t.tm_mon - 1) // 3], t.tm_year


def neighbours(season: str, year: int) -> tuple[tuple[str, int], tuple[str, int]]:
    """The seasons before and after this one."""
    i = SEASONS.index(season)
    return ((SEASONS[i - 1], year - 1 if i == 0 else year),
            (SEASONS[(i + 1) % 4], year + 1 if i == 3 else year))


def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\(Source:[^)]*\)\s*$", "", text.strip(), flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def season_guess(title: str) -> int | None:
    m = re.search(r"\bseason\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+season\b", title or "", re.I)
    return int(m.group(1) or m.group(2)) if m else None


def _season_media(season: str, year: int) -> list[dict]:
    def load():
        media = []
        for page in range(1, MAX_PAGES + 1):
            d = fetch_public_json(ANILIST_URL, {"query": SEASON_QUERY,
                                                "variables": {"s": season, "y": year, "p": page}})
            if d.get("errors"):
                raise LibraryError("AniList: " + "; ".join(x.get("message", "?") for x in d["errors"]))
            media += d["data"]["Page"]["media"]
            if not d["data"]["Page"]["pageInfo"]["hasNextPage"]:
                break
        return media
    return _cache.get(("season", season, year), 30 * 60, load)


def seasonal_anime(stack: Stack, season: str | None = None, year: int | None = None) -> dict:
    cur = current_season()
    season, year = (season or cur[0]).upper(), int(year or cur[1])
    if season not in SEASONS:
        raise LibraryError(f"unknown season {season!r}")
    media = _season_media(season, year)
    try:
        mapping, map_error = anime_map(), None
    except LibraryError as e:
        mapping, map_error = {}, str(e)
    shows = {s["tvdbId"]: s for s in stack.kind("show").items()}
    movies = {m["tmdbId"]: m for m in stack.kind("movie").items()}
    items = [_anime(m, mapping.get(m["id"], {}), shows, movies) for m in media]
    prev, nxt = neighbours(season, year)
    return {"season": season, "year": year, "items": items, "mapError": map_error,
            "prev": prev, "next": nxt, "current": cur}


def _anime(m: dict, ids: dict, shows: dict, movies: dict) -> dict:
    title = m["title"].get("english") or m["title"].get("romaji") or "?"
    is_movie = m.get("format") == "MOVIE"
    tvdb = ids.get("tvdb_id") if isinstance(ids.get("tvdb_id"), int) else None
    tm = ids.get("themoviedb_id")  # {"movie": [id, ...]} or {"tv": id}
    tm_movies = tm.get("movie") if isinstance(tm, dict) else None
    tmdb = tm_movies[0] if isinstance(tm_movies, list) and tm_movies else None
    mine = movies.get(tmdb) if is_movie else shows.get(tvdb)
    nxt = m.get("nextAiringEpisode") or {}
    return {
        "anilistId": m["id"], "title": title, "romaji": m["title"].get("romaji"),
        "cover": (m.get("coverImage") or {}).get("large"), "format": m.get("format"),
        "status": m.get("status"), "episodes": m.get("episodes"),
        "genres": m.get("genres") or [], "score": m.get("averageScore"),
        "popularity": m.get("popularity"),
        "studios": [n["name"] for n in (m.get("studios") or {}).get("nodes", [])],
        "nextEpisode": nxt.get("episode"), "nextAiringAt": nxt.get("airingAt"),
        "synopsis": strip_html(m.get("description") or "")[:600], "url": m.get("siteUrl"),
        "movie": is_movie, "tvdbId": tvdb, "tmdbId": tmdb,
        "tvdbSeason": (ids.get("season") or {}).get("tvdb") or season_guess(title),
        "inLibrary": mine is not None,
        "following": not is_movie and bool(mine and mine.get("monitored"))}
