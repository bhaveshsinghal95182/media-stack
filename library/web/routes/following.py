"""Following: shows whose new episodes Sonarr grabs as they air."""

from itertools import groupby
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request

from medialib import LibraryError
from medialib import following as fol
from medialib import rolling
from medialib.jobs import Outcome
from medialib.stack import summarize_lookup
from web.render import day_label, notify, render
from web.services import ServicesDep, StackDep

router = APIRouter(prefix="/following")
Mode = Literal["season", "new", "all", "from"]
# Where the dialog was opened: Add and the watchlist use it to pick what to download.
Origin = Literal["following", "add", "watchlist"]
CHANGED = ("following-changed",)
MAX_RESULTS = 12


@router.get("")
def following_page(request: Request):
    return render(request, "pages/following.html", {"active": "following"})


@router.get("/schedule")
def schedule(request: Request, services: ServicesDep, stack: StackDep):
    data = fol.following(stack, services.watcher)
    days = [(day, list(eps)) for day, eps in groupby(
        data["episodes"], key=lambda e: day_label(e["airAt"]) if e["airAt"] else "Unknown date")]
    watcher = services.watcher.status() if services.watcher else None
    return render(request, "partials/following_schedule.html",
                  {"shows": data["shows"], "days": days, "watcher": watcher})


@router.get("/search")
def search(request: Request, stack: StackDep, q: str = ""):
    k, query = stack.kind("show"), q.strip()
    items = [summarize_lookup(k, r) for r in k.lookup(query)[:MAX_RESULTS]] if query else []
    return render(request, "partials/follow_results.html", {"items": items, "query": query})


@router.get("/follow")
def follow_dialog(request: Request, stack: StackDep, title: str = "", q: str | None = None,
                  tvdb_id: int | None = None, season: int | None = None, unsure: bool = False,
                  origin: Origin = "following"):
    """Pick the Sonarr match: the known TVDB id first, or a title search (`q` searches again)."""
    searching = q is not None
    query = (q if searching else title).strip()
    candidates = fol.follow_candidates(stack, None if searching else tvdb_id, query)
    # A known TVDB id is the right show; so is the only result of a search.
    sure = len(candidates) == 1 or (candidates and tvdb_id and not searching)
    chosen = candidates[0] if sure else None
    hints = {"unsure": str(unsure).lower()} | ({"season": season} if season is not None else {})
    return render(request, "dialogs/follow.html", {
        "title": title or query, "query": query, "candidates": candidates, "chosen": chosen,
        "season": season, "unsure": unsure, "origin": origin,
        "options_url": "/following/follow/options?" + urlencode(hints),
        "options": _options(chosen, season, unsure) if chosen else None})


@router.get("/follow/options")
def follow_options(request: Request, stack: StackDep, tvdb_id: int,
                   season: int | None = None, unsure: bool = False):
    candidates = fol.follow_candidates(stack, tvdb_id, None)
    if not candidates:
        raise LibraryError("Sonarr no longer finds that show; search again")
    return render(request, "partials/follow_options.html",
                  {"options": _options(candidates[0], season, unsure)})


def _options(candidate: dict, season: int | None, unsure: bool) -> dict:
    """What-to-get choices; `season` is AniList's guess at TVDB's numbering."""
    seasons = candidate["seasons"] or [1]
    return {"tvdbId": candidate["tvdbId"], "seasons": seasons, "unsure": unsure,
            "default_season": season if season in seasons else max(seasons),
            "ahead": rolling.DEFAULT_AHEAD, "max_ahead": rolling.MAX_AHEAD}


@router.get("/rolling/preview")
def rolling_preview(request: Request, stack: StackDep, tvdb_id: int, start: int | None = None):
    """Which episode a start number points to (the rolling-window forms)."""
    try:
        ep, error = (rolling.preview(stack, tvdb_id, start), None) if start else (None, None)
    except LibraryError as e:
        ep, error = None, str(e)
    return render(request, "partials/rolling_preview.html", {"ep": ep, "error": error})


@router.post("/follow")
def follow(request: Request, services: ServicesDep, tvdb_id: Annotated[int, Form()],
           mode: Annotated[Mode, Form()], title: Annotated[str, Form()] = "",
           season: Annotated[int | None, Form()] = None,
           start: Annotated[int | None, Form()] = None,
           ahead: Annotated[int, Form()] = rolling.DEFAULT_AHEAD,
           origin: Annotated[Origin, Form()] = "following"):
    if mode == "from" and not start:
        raise LibraryError("Type the episode number to start from")

    def work(_log, _progress) -> Outcome:
        if mode == "from":
            result = rolling.start_window(services.make_stack(), services.rolling_store,
                                          tvdb_id, start, ahead)
        else:
            result = fol.follow(services.make_stack(), tvdb_id, mode,
                                season if mode == "season" else None)
        if origin == "watchlist":
            services.watchlist.remove("show", tvdb_id)
        return Outcome(True, result["message"])

    verb = "Following" if origin == "following" else "Adding"
    job = services.jobs.start(f"{verb} {title or 'show'}", work,
                              refresh="following-changed library-changed watchlist-changed")
    return render(request, "partials/job.html", {"job": job})


@router.post("/{series_id}/unfollow")
def unfollow(services: ServicesDep, stack: StackDep, series_id: int):
    with services.op_lock:
        result = fol.unfollow(stack, series_id)
    return notify(result["message"], events=CHANGED)


@router.post("/episodes/{episode_id}/search")
def search_episode(request: Request, stack: StackDep, episode_id: int):
    result = fol.search_episodes(stack, [episode_id])
    return render(request, "partials/done_button.html", {"label": "Searching…"},
                  toast=result["message"])
