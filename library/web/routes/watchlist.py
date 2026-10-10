"""Titles saved for later; nothing downloads until they are sent to Radarr/Sonarr."""

from typing import Annotated

from fastapi import APIRouter, Form, Request

from medialib import LibraryError
from medialib.jobs import Outcome
from medialib.watchlist import watchlist_send, watchlist_view
from web.params import MediaKind, title_key
from web.render import notify, render
from web.services import ServicesDep, StackDep

router = APIRouter(prefix="/watchlist")
CHANGED = ("watchlist-changed",)


@router.get("")
def watchlist_page(request: Request):
    return render(request, "pages/watchlist.html", {"active": "watchlist"})


@router.get("/list")
def watchlist_list(request: Request, services: ServicesDep, stack: StackDep):
    return render(request, "partials/watchlist_list.html",
                  {"items": watchlist_view(stack, services.watchlist)})


@router.get("/count")
def count(request: Request):
    return render(request, "partials/watchlist_badge.html")


@router.post("/items")
def save(request: Request, services: ServicesDep, kind: Annotated[MediaKind, Form()],
         ext_id: Annotated[int, Form()], title: Annotated[str, Form()],
         year: Annotated[str, Form()] = "", poster: Annotated[str, Form()] = "",
         overview: Annotated[str, Form()] = ""):
    try:
        new = services.watchlist.add(kind, ext_id, title, year, poster, overview)
    except ValueError as e:
        raise LibraryError(str(e)) from None
    name = " ".join(title.split())
    message = f"{name} saved to your watchlist." if new else f"{name} was already on your watchlist."
    return render(request, "partials/watchlist_button.html", {"saved": True},
                  toast=message, events=CHANGED)


@router.post("/remove")
def remove(services: ServicesDep, kind: Annotated[MediaKind, Form()],
           ext_id: Annotated[int, Form()]):
    gone = services.watchlist.remove(kind, ext_id)
    return notify("Removed from your watchlist." if gone else "It wasn't on your watchlist.",
                  events=CHANGED)


@router.post("/send")
def send(request: Request, services: ServicesDep,
         key: Annotated[list[str] | None, Form()] = None):
    keys = [title_key(k) for k in key or []]
    if not keys:
        raise LibraryError("nothing selected")

    def work(log, _progress) -> Outcome:
        result = watchlist_send(services.make_stack(), services.watchlist, keys, log)
        message = result["message"] + ("" if result["ok"] else " The rest stay on the watchlist.")
        return Outcome(result["ok"], message)

    title = "Downloading 1 title" if len(keys) == 1 else f"Downloading {len(keys)} titles"
    job = services.jobs.start(title, work, refresh="watchlist-changed")
    return render(request, "partials/job.html", {"job": job})
