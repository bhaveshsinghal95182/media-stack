"""The library: every movie and show, their details, hiding shows from the TV account, deleting."""

from typing import Annotated

from fastapi import APIRouter, Form, Request

from medialib import rolling, titles
from medialib.details import library_details
from medialib.jobs import Outcome
from medialib.seasons import get_season
from medialib.tv_access import set_tv_hidden
from web.params import MediaKind
from web.render import render
from web.services import ServicesDep, StackDep

router = APIRouter()


@router.get("/")
def library_page(request: Request):
    return render(request, "pages/library.html", {"active": "library"})


@router.get("/library/list")
def library_list(request: Request, stack: StackDep):
    return render(request, "partials/library_list.html", titles.overview(stack))


@router.post("/library/shows/{show_id}/tv-access")
def tv_access(request: Request, stack: StackDep, show_id: int,
              hidden: Annotated[bool, Form()], labelled: Annotated[bool, Form()] = False):
    result = set_tv_hidden(stack, show_id, hidden)
    # The list reloads too: the same toggle sits on the show's row and in its details dialog.
    return render(request, "partials/tv_button.html",
                  {"show_id": show_id, "hidden": result["hidden"], "labelled": labelled},
                  toast=result["message"], events=("library-changed",))


def _details(services, stack, kind: str, item_id: int) -> dict:
    t = library_details(stack, kind, item_id)
    if kind == "show":
        t["rolling"] = rolling.status(stack, services.rolling_store, item_id)
        t["rollingDefaults"] = {"ahead": rolling.DEFAULT_AHEAD, "max_ahead": rolling.MAX_AHEAD}
    return t


def _show_dialog(request: Request, services, stack, show_id: int, message: str):
    # Re-render the details dialog so the change shows straight away.
    return render(request, "dialogs/title.html", {"t": _details(services, stack, "show", show_id)},
                  toast=message, events=("library-changed",))


@router.post("/library/shows/{show_id}/seasons/{number}/get")
def season_get(request: Request, services: ServicesDep, stack: StackDep, show_id: int,
               number: int):
    result = get_season(stack, show_id, number)
    return _show_dialog(request, services, stack, show_id, result["message"])


@router.post("/library/shows/{show_id}/rolling")
def rolling_set(request: Request, services: ServicesDep, stack: StackDep, show_id: int,
                start: Annotated[int, Form()], ahead: Annotated[int, Form()]):
    tvdb_id = stack.kind("show").item(show_id)["tvdbId"]
    with services.op_lock:
        result = rolling.start_window(stack, services.rolling_store, tvdb_id, start, ahead)
    return _show_dialog(request, services, stack, show_id, result["message"])


@router.post("/library/shows/{show_id}/rolling/stop")
def rolling_stop(request: Request, services: ServicesDep, stack: StackDep, show_id: int):
    with services.op_lock:
        result = rolling.stop_window(stack, services.rolling_store, show_id)
    return _show_dialog(request, services, stack, show_id, result["message"])


@router.get("/library/{kind}/{item_id}")
def details_dialog(request: Request, services: ServicesDep, stack: StackDep, kind: MediaKind,
                   item_id: int):
    return render(request, "dialogs/title.html", {"t": _details(services, stack, kind, item_id)})


@router.get("/library/{kind}/{item_id}/delete")
def delete_dialog(request: Request, stack: StackDep, kind: MediaKind, item_id: int):
    return render(request, "dialogs/delete.html",
                  {"plan": titles.delete_plan(stack, kind, item_id)})


@router.post("/library/{kind}/{item_id}/delete")
def delete(request: Request, services: ServicesDep, stack: StackDep, kind: MediaKind,
           item_id: int, exclude: Annotated[bool, Form()] = False):
    k = stack.kind(kind)
    label = k.label(k.item(item_id))

    def work(log, _progress) -> Outcome:
        st = services.make_stack()  # a job thread gets its own clients
        result = titles.delete_execute(st, titles.delete_plan(st, kind, item_id), exclude, log)
        if result["ok"]:
            return Outcome(True, f"Deleted {label}. About {result['freedHuman']} freed.")
        return Outcome(False, "Not fully deleted: " + "; ".join(result["problems"]))

    job = services.jobs.start(f"Deleting {label}", work, refresh="library-changed")
    return render(request, "partials/job.html", {"job": job})
