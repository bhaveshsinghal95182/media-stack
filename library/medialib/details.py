"""What the details dialog shows about one title: its metadata, and its files if we have it."""

from __future__ import annotations

from medialib.fmt import human, parse_utc
from medialib.seasons import season_rows
from medialib.stack import Kind, Stack, image, poster
from medialib.tv_access import tv_hidden_ids

MOVIE_STATUS = {"tba": "Not announced yet", "announced": "Announced", "inCinemas": "In cinemas",
                "released": "Released", "deleted": "Removed from TMDB"}
SHOW_STATUS = {"continuing": "Still airing", "ended": "Ended", "upcoming": "Not aired yet",
               "deleted": "Removed from TVDB"}
MOVIE_SCORES = (("imdb", "IMDb", "{:.1f}"), ("tmdb", "TMDB", "{:.1f}"),
                ("rottenTomatoes", "Rotten Tomatoes", "{:.0f}%"))


def library_details(stack: Stack, kind: str, item_id: int) -> dict:
    """A title in Radarr/Sonarr: its metadata plus what's on disk."""
    k = stack.kind(kind)
    item = k.item(item_id)
    size = k.size(item)
    details = dict(_facts(k, item), id=item_id, inLibrary=True, path=item.get("path") or "",
                   sizeHuman=human(size) if size else "", monitored=item.get("monitored"))
    if k.is_movie:
        details["file"] = _movie_file(item.get("movieFile"))
    else:
        details["seasons"] = season_rows(k, item)
        details["hiddenFromTv"] = item_id in tv_hidden_ids(stack, [item])
    return details


def lookup_details(stack: Stack, kind: str, ext_id: int) -> dict:
    """A title Radarr/Sonarr may not have, from their TMDB/TVDB lookup."""
    k = stack.kind(kind)
    item = k.lookup_id(ext_id)
    return dict(_facts(k, item), inLibrary=bool(item.get("id")))


def _facts(k: Kind, item: dict) -> dict:
    ratings = item.get("ratings") or {}
    if k.is_movie:
        scores = [f"{name} {fmt.format(ratings[key]['value'])}" for key, name, fmt in MOVIE_SCORES
                  if (ratings.get(key) or {}).get("value")]
        status, maker = MOVIE_STATUS.get(item.get("status"), ""), item.get("studio")
    else:
        scores = [f"{ratings['value']:.1f}/10"] if ratings.get("value") else []
        status, maker = SHOW_STATUS.get(item.get("status"), ""), item.get("network")
    runtime = item.get("runtime")
    return {"kind": k.kind, "extId": item.get(k.id_key), "title": item["title"],
            "year": item.get("year"), "overview": item.get("overview") or "",
            "poster": poster(item), "fanart": image(item, "fanart"),
            "genres": item.get("genres") or [], "runtime": f"{runtime} min" if runtime else "",
            "certification": item.get("certification") or "", "maker": maker or "",
            "language": (item.get("originalLanguage") or {}).get("name") or "",
            "scores": scores, "status": status, "nextAiring": parse_utc(item.get("nextAiring")),
            "links": _links(k, item)}


def _links(k: Kind, item: dict) -> list[tuple[str, str]]:
    links = []
    if item.get("imdbId"):
        links.append(("IMDb", f"https://www.imdb.com/title/{item['imdbId']}/"))
    if item.get("tmdbId"):
        links.append(("TMDB", f"https://www.themoviedb.org/{'movie' if k.is_movie else 'tv'}/{item['tmdbId']}"))
    if item.get("tvdbId"):
        links.append(("TVDB", f"https://thetvdb.com/dereferrer/series/{item['tvdbId']}"))
    return links


def _movie_file(f: dict | None) -> dict | None:
    if not f:
        return None
    media = f.get("mediaInfo") or {}
    video = [media.get("resolution"), media.get("videoCodec"), media.get("videoDynamicRangeType")]
    return {"name": f.get("relativePath") or "",
            "quality": ((f.get("quality") or {}).get("quality") or {}).get("name") or "",
            "video": " ".join(v for v in video if v),
            "audio": _languages(media.get("audioLanguages")),
            "subtitles": _languages(media.get("subtitles"))}


def _languages(value: str | None) -> str:
    """'jpn/eng/eng' -> 'jpn, eng'; ffprobe's 'und' (no language tag) -> 'unknown'."""
    codes = ("unknown" if v == "und" else v for v in (value or "").split("/") if v)
    return ", ".join(dict.fromkeys(codes))
