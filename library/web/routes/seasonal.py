"""A season's anime from AniList, ready to follow, add or save for later."""

from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request

from medialib.seasonal import seasonal_anime
from web.render import render
from web.services import ServicesDep, StackDep

router = APIRouter(prefix="/seasonal")
Format = Literal["series", "movie", "all"]
Sort = Literal["popularity", "score", "airing"]
NEVER = float("inf")
SORT_KEYS = {
    "popularity": None,  # AniList's own order
    "score": lambda a: -(a["score"] or 0),
    "airing": lambda a: a["nextAiringAt"] or NEVER,
}


@router.get("")
def seasonal_page(request: Request):
    return render(request, "pages/seasonal.html", {"active": "seasonal"})


@router.get("/grid")
def grid(request: Request, services: ServicesDep, stack: StackDep,
         season: str | None = None, year: int | None = None,
         shown: Annotated[Format, Query(alias="format")] = "series", sort: Sort = "popularity"):
    data = seasonal_anime(stack, season, year)
    items = [a for a in data["items"] if shown == "all" or a["movie"] == (shown == "movie")]
    if SORT_KEYS[sort]:
        items.sort(key=SORT_KEYS[sort])
    for a in items:
        kind, ext_id = ("movie", a["tmdbId"]) if a["movie"] else ("show", a["tvdbId"])
        a["onWatchlist"] = bool(ext_id) and services.watchlist.has(kind, ext_id)
        a["followQuery"] = _follow_query(a)
    return render(request, "partials/seasonal_grid.html",
                  {**data, "items": items, "format": shown, "sort": sort})


def _follow_query(anime: dict) -> str:
    """The follow dialog's query; without a TVDB id the season numbering is a guess."""
    params = {"title": anime["title"], "tvdb_id": anime["tvdbId"],
              "season": anime["tvdbSeason"], "unsure": "false" if anime["tvdbId"] else "true"}
    return urlencode({k: v for k, v in params.items() if v is not None})
