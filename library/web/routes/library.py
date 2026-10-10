"""The library: every movie and show, hiding shows from the TV account, deleting titles."""

from typing import Annotated

from fastapi import APIRouter, Form, Request

from medialib import titles
from medialib.jobs import Outcome
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
              hidden: Annotated[bool, Form()]):
    result = set_tv_hidden(stack, show_id, hidden)
    return render(request, "partials/tv_button.html",
                  {"show_id": show_id, "hidden": result["hidden"]}, toast=result["message"])


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
