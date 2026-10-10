"""The FastAPI app: routes, password, error pages, and the background services."""

from __future__ import annotations

import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from medialib import LibraryError
from web.config import WebConfig
from web.render import STATIC, notify, render
from web.routes import ROUTERS
from web.security import BasicAuth, SecurityMiddleware
from web.services import Services


def create_app(config: WebConfig | None = None, services: Services | None = None,
               background: bool = True) -> FastAPI:
    config = config or WebConfig.from_env()
    if not config.password:
        raise SystemExit("LIBRARY_PASSWORD is not set")
    services = services or Services(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background:
            services.start_background()
        yield

    app = FastAPI(title="Media Library", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None,
                  dependencies=[Depends(BasicAuth(config.username, config.password))])
    app.state.services = services
    app.add_middleware(SecurityMiddleware)
    # Static files skip the password: they are the page's own CSS and scripts.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    for router in ROUTERS:
        app.include_router(router)

    @app.exception_handler(LibraryError)
    async def library_error(request: Request, exc: LibraryError):
        return _error(request, services.import_folder.display(str(exc)), 400)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        fields = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors())
        return _error(request, f"Bad request ({fields})", 422)

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception):
        traceback.print_exception(exc)
        return _error(request, f"Something broke: {type(exc).__name__}: {exc}", 500)

    return app


def _error(request: Request, message: str, status: int):
    """Actions get a toast; a part of a page shows the error in its place; pages an error page."""
    if request.method != "GET":
        return notify(message, level="error", status=status)
    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        # 200, or htmx would not swap it in and the spinner would stay forever.
        return render(request, "partials/error.html", {"message": message})
    return render(request, "pages/error.html", {"message": message}, status=status)
