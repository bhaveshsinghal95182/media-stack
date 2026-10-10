"""Import video files from ~/Downloads (read-only) into the library."""

import os
from typing import Annotated

from fastapi import APIRouter, Form, Request

from medialib import LibraryError, importer
from medialib.jobs import Outcome
from web.params import MediaKind
from web.render import render
from web.services import ServicesDep, StackDep

router = APIRouter(prefix="/import")


@router.get("")
def import_page(request: Request, services: ServicesDep, path: str = ""):
    folder = services.import_folder
    return render(request, "pages/import.html",
                  {"active": "import", "label": folder.label, **folder.browse(path)})


@router.get("/match")
def match_dialog(request: Request, services: ServicesDep, stack: StackDep, kind: MediaKind,
                 path: str, q: str | None = None):
    found = importer.import_candidates(stack, kind, services.import_folder.resolve(path), q)
    results = found["results"]
    return render(request, "dialogs/import_match.html", {
        "kind": kind, "path": path, "query": q or found["guess"], "results": results,
        "chosen": results[0]["extId"] if len(results) == 1 else None})


@router.get("/plan")
def plan(request: Request, services: ServicesDep, stack: StackDep, kind: MediaKind, path: str,
         ext_id: int | None = None, replace: bool = False):
    if ext_id is None:
        return render(request, "partials/import_plan.html", {"plan": None})
    try:
        found = importer.import_plan(stack, kind, services.import_folder.resolve(path), ext_id,
                                     replace)
    except LibraryError as e:  # shown in the dialog, which offers "replace" where it helps
        return render(request, "partials/import_plan.html", {
            "plan": None, "error": services.import_folder.display(str(e)), "replace": replace,
            "offer_replace": "already has a file" in str(e)})
    return render(request, "partials/import_plan.html",
                  {"plan": found, "replace": replace, "offer_replace": True})


@router.post("")
def start_import(request: Request, services: ServicesDep, kind: Annotated[MediaKind, Form()],
                 path: Annotated[str, Form()], ext_id: Annotated[int, Form()],
                 replace: Annotated[bool, Form()] = False):
    source = services.import_folder.resolve(path)

    def work(log, progress) -> Outcome:
        result = importer.import_execute(services.make_stack(), kind, source, ext_id, replace,
                                         log=log, progress=progress)
        return Outcome(True, f"Imported {result['label']}: {result['imported']}. "
                             "Jellyfin picks it up within a minute.")

    job = services.jobs.start(f"Importing {os.path.basename(source)}", work,
                              refresh="library-changed")
    return render(request, "partials/job.html", {"job": job})
