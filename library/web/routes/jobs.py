"""Background jobs: the dialog polls one until it finishes."""

from fastapi import APIRouter, Request

from medialib import LibraryError
from web.render import render
from web.services import ServicesDep

router = APIRouter()


@router.get("/jobs/{job_id}")
def job_status(request: Request, services: ServicesDep, job_id: str):
    job = services.jobs.get(job_id)
    if job is None:
        raise LibraryError("that job is gone (the page restarted, or it finished hours ago)")
    # The poll that sees it finish tells the page what to reload.
    events = (job.refresh,) if job.finished and job.refresh else ()
    return render(request, "partials/job.html", {"job": job}, events=events)
