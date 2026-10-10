"""A title's releases: what downloads now, search the indexers, grab or drop one."""

from typing import Annotated

from fastapi import APIRouter, Form, Request

from medialib import releases
from web.params import MediaKind
from web.render import notify, render
from web.services import ServicesDep, StackDep

router = APIRouter(prefix="/releases/{kind}/{item_id}")


@router.get("")
def releases_dialog(request: Request, stack: StackDep, kind: MediaKind, item_id: int):
    info = releases.release_info(stack, kind, item_id)
    latest = max((s["number"] for s in info["seasons"]), default=None)
    return render(request, "dialogs/releases.html", {"info": info, "latest_season": latest})


@router.get("/downloads")
def downloads(request: Request, stack: StackDep, kind: MediaKind, item_id: int):
    return render(request, "partials/downloads.html",
                  {"kind": kind, "item_id": item_id,
                   "downloads": releases.downloads(stack, kind, item_id)})


@router.get("/search")
def search(request: Request, stack: StackDep, kind: MediaKind, item_id: int,
           season: int | None = None):
    found = releases.release_search(stack, kind, item_id, season)
    return render(request, "partials/release_list.html",
                  {"kind": kind, "item_id": item_id, **found})


@router.post("/grab")
def grab(request: Request, services: ServicesDep, stack: StackDep, kind: MediaKind,
         item_id: int, guid: Annotated[str, Form()], indexer_id: Annotated[int, Form()],
         replace_current: Annotated[bool, Form()] = False):
    with services.op_lock:
        result = releases.release_grab(stack, kind, item_id, guid, indexer_id, replace_current)
    return render(request, "partials/done_button.html", {"label": "Sent"},
                  toast=result["message"], events=("downloads-changed",))


@router.post("/drop")
def drop(services: ServicesDep, stack: StackDep, kind: MediaKind, item_id: int,
         download_id: Annotated[str, Form()]):
    with services.op_lock:
        result = releases.release_drop(stack, kind, item_id, download_id)
    return notify(result["message"], events=("downloads-changed",))
