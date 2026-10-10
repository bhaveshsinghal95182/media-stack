"""One router per page."""

from web.routes import add, cleanup, following, imports, jobs, library, releases, seasonal, watchlist

ROUTERS = (library.router, releases.router, add.router, watchlist.router, seasonal.router,
           following.router, imports.router, cleanup.router, jobs.router)
