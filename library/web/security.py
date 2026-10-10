"""The page is reachable from the LAN and can delete files: a password, and no CSRF.

HTTP Basic auth with a lockout after repeated failures. State-changing requests must
carry htmx's HX-Request header, which a cross-site page cannot send.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import time

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

MAX_FAILURES = 10
LOCKOUT = 600  # seconds
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class BasicAuth:
    """A FastAPI dependency that checks the Authorization header."""

    def __init__(self, username: str, password: str):
        self.username, self.password = username.encode(), password.encode()
        self.failures: dict[str, list[float]] = {}

    def __call__(self, request: Request) -> None:
        ip = request.client.host if request.client else "?"
        recent = [t for t in self.failures.get(ip, []) if t > time.time() - LOCKOUT]
        self.failures[ip] = recent
        if len(recent) >= MAX_FAILURES:
            raise HTTPException(429, "Too many failed logins; try again in 10 minutes.")
        given = request.headers.get("Authorization", "")
        if given.startswith("Basic "):
            if self._valid(given[6:]):
                return
            recent.append(time.time())
            time.sleep(1)  # this dependency is sync, so it sleeps on a worker thread
        raise HTTPException(401, "Login required",
                            {"WWW-Authenticate": 'Basic realm="Media Library", charset="UTF-8"'})

    def _valid(self, token: str) -> bool:
        try:
            user, _, password = base64.b64decode(token).partition(b":")
        except (binascii.Error, ValueError):
            return False
        user_ok = not self.username or hmac.compare_digest(user, self.username)
        return hmac.compare_digest(password, self.password) and user_ok


class SecurityMiddleware(BaseHTTPMiddleware):
    """Refuses state changes without HX-Request; keeps pages out of frames and caches."""

    async def dispatch(self, request, call_next):
        if request.method not in SAFE_METHODS and request.headers.get("HX-Request") != "true":
            return PlainTextResponse("missing HX-Request header", status_code=403)
        response = await call_next(request)
        response.headers.update({"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response
