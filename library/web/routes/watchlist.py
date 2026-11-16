"""Titles saved for later, filed into playlists; nothing downloads until they are sent."""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request

from medialib import LibraryError
from medialib.details import lookup_details
from medialib.jobs import Outcome
from medialib.watchlist import watchlist_sections, watchlist_send
from web.params import MediaKind, title_key
from web.render import notify, render
from web.services import ServicesDep, StackDep

router = APIRouter(prefix="/watchlist")
CHANGED = ("watchlist-changed",)
CLOSED = ("close-dialog", "watchlist-changed")


def _item(kind: str, ext_id: int, title: str = "", year: str = "", poster: str = "",
          overview: str = "", saved: dict | None = None) -> dict:
    """The form/query values, filled in from what was saved where they are missing."""
    fallback = saved or {}
    return {"kind": kind, "extId": ext_id,
            "title": " ".join(str(title or fallback.get("title") or "").split()),
            "year": year or fallback.get("year") or "",
            "poster": poster or fallback.get("poster") or "",
            "overview": overview or fallback.get("overview") or ""}


def _names(wl, keys: list[str]) -> str:
    known = {p["key"]: p["name"] for p in wl.playlists()}
    return ", ".join(known[k] for k in keys if k in known)


@router.get("")
def watchlist_page(request: Request):
    return render(request, "pages/watchlist.html", {"active": "watchlist"})


@router.get("/list")
def watchlist_list(request: Request, services: ServicesDep, stack: StackDep):
    return render(request, "partials/watchlist_list.html",
                  {"sections": watchlist_sections(stack, services.watchlist)})


@router.get("/count")
def count(request: Request):
    return render(request, "partials/watchlist_badge.html")


@router.get("/save")
def save_dialog(request: Request, services: ServicesDep, kind: MediaKind, ext_id: int,
                title: str = "", year: str = "", poster: str = "", overview: str = ""):
    """Where a title goes: the watchlist, plus any playlists (or a new one)."""
    saved = services.watchlist.get(kind, ext_id)
    item = _item(kind, ext_id, title, year, poster, overview, saved)
    if not item["title"]:
        raise LibraryError("no title to save")
    current = set((saved or {}).get("playlists") or [])
    playlists = [{**p, "checked": p["key"] in current}
                 for p in services.watchlist.playlists()]
    return render(request, "partials/save_dialog.html",
                  {"item": item, "playlists": playlists, "saved": bool(saved)})


@router.post("/items")
def save(request: Request, services: ServicesDep, kind: Annotated[MediaKind, Form()],
         ext_id: Annotated[int, Form()], title: Annotated[str, Form()],
         year: Annotated[str, Form()] = "", poster: Annotated[str, Form()] = "",
         overview: Annotated[str, Form()] = "",
         playlist: Annotated[list[str] | None, Form()] = None,
         new_playlist: Annotated[str, Form()] = ""):
    wl, before = services.watchlist, services.watchlist.get(kind, ext_id)
    keys = list(dict.fromkeys(playlist or []))
    try:
        if new_playlist.strip():
            keys.append(wl.create_playlist(new_playlist)["key"])
        wl.add(kind, ext_id, title, year, poster, overview, playlists=keys)
    except ValueError as e:
        raise LibraryError(str(e)) from None

    item = _item(kind, ext_id, title, year, poster, overview)
    after = wl.get(kind, ext_id) or item
    name = item["title"]
    filed = after.get("playlists") or []
    if not before:
        where = _names(wl, filed)
        message = f"{name} saved to {where}." if where else f"{name} saved."
    elif before["playlists"] != filed:
        message = f"{name} updated."
    else:
        message = f"{name} was already on your watchlist."
    return render(request, "partials/watchlist_saved.html", {"item": item}, toast=message,
                  events=CLOSED)


@router.post("/remove")
def remove(services: ServicesDep, kind: Annotated[MediaKind, Form()],
           ext_id: Annotated[int, Form()]):
    gone = services.watchlist.remove(kind, ext_id)
    return notify("Removed from your watchlist." if gone else "It wasn't on your watchlist.",
                  events=CHANGED)


@router.get("/assign")
def assign_dialog(request: Request, services: ServicesDep,
                  key: Annotated[list[str] | None, Query()] = None):
    """File the selected titles into a playlist (additive)."""
    keys = list(dict.fromkeys(k for k in key or [] if k))
    if not keys:
        raise LibraryError("nothing selected")
    return render(request, "partials/assign_dialog.html",
                  {"keys": keys, "playlists": services.watchlist.playlists()})


@router.post("/assign")
def assign(services: ServicesDep, key: Annotated[list[str] | None, Form()] = None,
           playlist: Annotated[list[str] | None, Form()] = None,
           new_playlist: Annotated[str, Form()] = ""):
    wl = services.watchlist
    keys = [title_key(k) for k in dict.fromkeys(key or [])]
    if not keys:
        raise LibraryError("nothing selected")
    chosen = list(dict.fromkeys(playlist or []))
    try:
        if new_playlist.strip():
            chosen.append(wl.create_playlist(new_playlist)["key"])
        if not chosen:
            raise LibraryError("pick a playlist")
        changed = sum(wl.add_to_playlists(keys, playlist_key) for playlist_key in chosen)
    except ValueError as e:
        raise LibraryError(str(e)) from None
    names = _names(wl, chosen)
    filed = len(keys) if changed == len(keys) else f"{changed} of {len(keys)}"
    message = (f"Filed {filed} into {names}." if changed
               else f"Already in {names}.")
    return notify(message, events=CLOSED)


@router.get("/playlists/new")
def new_playlist_dialog(request: Request):
    return render(request, "partials/playlist_dialog.html",
                  {"action": "/watchlist/playlists", "playlist": None})


@router.post("/playlists")
def create_playlist(services: ServicesDep, name: Annotated[str, Form()]):
    try:
        playlist = services.watchlist.create_playlist(name)
    except ValueError as e:
        raise LibraryError(str(e)) from None
    return notify(f"“{playlist['name']}” is ready.", events=CLOSED)


@router.get("/playlists/{key}/rename")
def rename_playlist_dialog(request: Request, services: ServicesDep, key: str):
    playlist = services.watchlist.playlist(key)
    if not playlist:
        raise LibraryError("no such playlist")
    return render(request, "partials/playlist_dialog.html",
                  {"action": f"/watchlist/playlists/{key}/rename", "playlist": playlist})


@router.post("/playlists/{key}/rename")
def rename_playlist(services: ServicesDep, key: str, name: Annotated[str, Form()]):
    try:
        changed = services.watchlist.rename_playlist(key, name)
    except ValueError as e:
        raise LibraryError(str(e)) from None
    return notify("Playlist renamed." if changed else "That was already its name.",
                  events=CLOSED)


@router.post("/playlists/{key}/delete")
def delete_playlist(services: ServicesDep, key: str):
    gone = services.watchlist.delete_playlist(key)
    return notify("Playlist deleted. Its titles stay on your watchlist." if gone
                  else "No such playlist.", events=CLOSED)


@router.get("/{kind}/{ext_id}")
def details_dialog(request: Request, services: ServicesDep, stack: StackDep, kind: MediaKind,
                   ext_id: int):
    saved = services.watchlist.get(kind, ext_id)
    try:
        details = lookup_details(stack, kind, ext_id)
    except LibraryError:
        if not saved:
            raise
        details = saved  # the lookup is down: what was saved is better than nothing
    if saved:
        names = {p["key"]: p["name"] for p in services.watchlist.playlists()}
        saved = {**saved, "playlistNames": [names[k] for k in saved["playlists"]
                                            if k in names]}
    return render(request, "dialogs/title.html", {"t": details, "saved": saved})


@router.post("/send")
def send(request: Request, services: ServicesDep,
         key: Annotated[list[str] | None, Form()] = None):
    keys = list(dict.fromkeys(title_key(k) for k in key or []))
    # A show needs its own choice of what to get (the follow dialog), never "everything".
    shows = sum(1 for kind, _ in keys if kind == "show")
    keys = [(kind, ext_id) for kind, ext_id in keys if kind != "show"]
    if not keys:
        raise LibraryError("Pick what to get for a show with its own Download button"
                           if shows else "nothing selected")

    def work(log, _progress) -> Outcome:
        result = watchlist_send(services.make_stack(), services.watchlist, keys, log)
        message = result["message"] + ("" if result["ok"] else " The rest stay on the watchlist.")
        if shows:
            message += f" Skipped {shows} show{'s' * (shows != 1)}: use each one's Download button."
        return Outcome(result["ok"], message)

    title = "Downloading 1 title" if len(keys) == 1 else f"Downloading {len(keys)} titles"
    job = services.jobs.start(title, work, refresh="watchlist-changed")
    return render(request, "partials/job.html", {"job": job})
