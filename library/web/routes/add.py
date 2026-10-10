"""Find a movie or show and add it, which searches for a release straight away."""

from typing import Annotated

from fastapi import APIRouter, Form, Request

from medialib import titles
from medialib.stack import summarize_lookup
from web.params import MediaKind
from web.render import render
from web.services import ServicesDep, StackDep

router = APIRouter()
MAX_RESULTS = 12


@router.get("/add")
def add_page(request: Request):
    return render(request, "pages/add.html", {"active": "add"})


@router.get("/add/results")
def results(request: Request, services: ServicesDep, stack: StackDep,
            kind: MediaKind = "movie", q: str = ""):
    k, query = stack.kind(kind), q.strip()
    items = [summarize_lookup(k, r) for r in k.lookup(query)[:MAX_RESULTS]] if query else []
    for item in items:
        item["onWatchlist"] = services.watchlist.has(kind, item["extId"])
    return render(request, "partials/add_results.html", {"items": items, "query": query})


@router.post("/add")
def add(request: Request, stack: StackDep, kind: Annotated[MediaKind, Form()],
        ext_id: Annotated[int, Form()]):
    result = titles.add_title(stack, kind, ext_id)
    label = "Added · searching" if result["added"] else "Searching again"
    return render(request, "partials/done_button.html", {"label": label},
                  toast=result["message"], events=("library-changed",))
