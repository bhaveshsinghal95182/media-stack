"""Templates, the filters they use, and the htmx headers that go with a response."""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from medialib.fmt import human
from web.icons import ICONS

HERE = Path(__file__).parent
STATIC = HERE / "static"
NAV = (  # (key, path, label)
    ("library", "/", "Library"),
    ("add", "/add", "Add"),
    ("watchlist", "/watchlist", "Watchlist"),
    ("seasonal", "/seasonal", "Seasonal"),
    ("following", "/following", "Following"),
    ("import", "/import", "Import"),
    ("cleanup", "/cleanup", "Cleanup"),
)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

def rel_time(ts: float | None, now: float | None = None) -> str:
    """'in 2d 4h' / '35m ago'."""
    if ts is None:
        return ""
    delta = ts - (time.time() if now is None else now)
    a = abs(delta)
    if a < 3600:
        text = f"{max(1, round(a / 60))}m"
    elif a < 86400:
        text = f"{int(a // 3600)}h {int(a % 3600 // 60)}m"
    else:
        text = f"{int(a // 86400)}d {int(a % 86400 // 3600)}h"
    return f"in {text}" if delta >= 0 else f"{text} ago"


def clock(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts):%H:%M}"


def stamp(ts: float) -> str:
    """A log line's time: 'Mon 14:05'."""
    return f"{datetime.fromtimestamp(ts):%a %H:%M}"


def day_label(ts: float, now: float | None = None) -> str:
    day = date.fromtimestamp(ts)
    diff = (day - date.fromtimestamp(time.time() if now is None else now)).days
    named = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(diff)
    return named or f"{day:%A} {day.day} {day:%b}"


def gigabytes(n: float) -> str:
    return f"{n / 1024**3:.0f} GB"


def static_url(name: str) -> str:
    """A static file's URL, changing whenever the file does (so caches never go stale)."""
    try:
        version = int((STATIC / name).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


def _nav_badges(request: Request) -> dict:
    services = request.app.state.services
    return {"watchlist_count": len(services.watchlist.items()),
            "orphan_count": services.known_orphans()}


templates = Jinja2Templates(directory=HERE / "templates", context_processors=[_nav_badges])
templates.env.filters.update(rel_time=rel_time, clock=clock, stamp=stamp, day_label=day_label,
                             human=human, gigabytes=gigabytes)
templates.env.globals.update(nav=NAV, static_url=static_url, icons=ICONS)


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------

def _trigger(toast: str | None, level: str, events: tuple[str, ...]) -> dict[str, str]:
    """HX-Trigger fires page events (e.g. 'library-changed') and shows a toast."""
    triggers: dict = dict.fromkeys(events)
    if toast:
        triggers["toast"] = {"message": toast, "level": level}
    return {"HX-Trigger": json.dumps(triggers)} if triggers else {}


def render(request: Request, template: str, context: dict | None = None, *,
           toast: str | None = None, level: str = "ok", events: tuple[str, ...] = (),
           status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context or {}, status_code=status,
                                      headers=_trigger(toast, level, events))


def notify(message: str | None, *, level: str = "ok", events: tuple[str, ...] = (),
           status: int = 204) -> Response:
    """No content, only a toast and events (204: htmx leaves the page as it is)."""
    return Response(status_code=status, headers=_trigger(message, level, events))
