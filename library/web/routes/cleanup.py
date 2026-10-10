"""Cleanup: the disk, watched titles the janitor removes, and leftover torrents."""

import time
from typing import Annotated

from fastapi import APIRouter, Form, Request

from medialib import LibraryError, janitor, orphans
from medialib.jobs import Outcome
from web.render import notify, render
from web.services import Services, ServicesDep, StackDep

router = APIRouter(prefix="/cleanup")
JANITOR_CHANGED = ("janitor-changed",)


@router.get("")
def cleanup_page(request: Request):
    return render(request, "pages/cleanup.html", {"active": "cleanup"})


@router.get("/janitor")
def janitor_status(request: Request, services: ServicesDep):
    status = services.janitor.status() if services.janitor else None
    return render(request, "partials/janitor.html", {"janitor": status, "now": time.time()})


@router.post("/janitor/keep")
def keep(request: Request, stack: StackDep, item_id: Annotated[str, Form()],
         user_id: Annotated[str, Form()]):
    janitor.keep(stack, item_id, user_id)
    return render(request, "partials/done_button.html", {"label": "Kept"},
                  toast="Kept: marked as a favourite in Jellyfin.")


@router.post("/janitor/run")
def run(services: ServicesDep):
    _janitor(services).check_now()
    return notify("Checking now…", events=JANITOR_CHANGED)


@router.post("/janitor/force")
def force(services: ServicesDep):
    _janitor(services).check_now(force=True)
    return notify("Removing everything watched…", events=JANITOR_CHANGED)


def _janitor(services: Services) -> janitor.Janitor:
    if not services.janitor:
        raise LibraryError("automatic cleanup is off")
    return services.janitor


@router.get("/orphans")
def orphan_list(request: Request, services: ServicesDep, stack: StackDep):
    found = orphans.orphan_list(stack)
    services.remember_orphans(len(found))
    return render(request, "partials/orphans.html", {"orphans": found})


@router.get("/orphans/count")
def orphan_count(request: Request, services: ServicesDep, stack: StackDep):
    services.remember_orphans(len(orphans.orphan_list(stack)))
    return render(request, "partials/orphan_badge.html")


@router.post("/orphans/delete")
def delete_orphans(request: Request, services: ServicesDep,
                   torrent: Annotated[list[str] | None, Form()] = None):
    hashes = torrent or []
    if not hashes:
        raise LibraryError("nothing selected")

    def work(_log, _progress) -> Outcome:
        result = orphans.orphans_delete(services.make_stack(), hashes)
        if result["ok"]:
            return Outcome(True, f"Deleted. About {result['freedHuman']} freed.")
        return Outcome(False, "Not fully deleted: " + ", ".join(result["still"]))

    job = services.jobs.start(f"Deleting {len(hashes)} leftover torrent(s)", work,
                              refresh="library-changed")
    return render(request, "partials/job.html", {"job": job})
