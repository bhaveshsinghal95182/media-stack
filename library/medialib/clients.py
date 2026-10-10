"""HTTP clients for Radarr/Sonarr, Jellyfin, qBittorrent and public JSON APIs."""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from medialib.errors import LibraryError

CATEGORIES = ("radarr", "tv-sonarr")  # qBittorrent categories the Arrs download into
USER_AGENT = "media-library/1.0 (self-hosted)"  # AniList answers 403 without one


def _with_query(url: str, params: dict) -> str:
    return url + ("?" + urllib.parse.urlencode(params) if params else "")


def http_json(url: str, method: str = "GET", body=None, headers: dict | None = None,
              timeout: int = 60):
    """Send a request to one of the stack's services and decode its JSON answer."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise LibraryError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: "
                           f"{_error_detail(e.read().decode(errors='replace'))}") from None
    except urllib.error.URLError as e:
        raise LibraryError(f"{url.split('/api')[0]} unreachable ({e.reason}); "
                           "is the stack running?") from None
    except TimeoutError:  # a slow answer times out while reading, outside URLError
        raise LibraryError(f"{method} {url.split('?')[0]} got no answer in {timeout} s; "
                           "try again later") from None
    return json.loads(raw) if raw else None


def _error_detail(body: str) -> str:
    """The message out of an Arr error body ({"message": ...} or a list of them)."""
    try:
        j = json.loads(body)
        j = j[0] if isinstance(j, list) and j else j
        body = j.get("message") or j.get("errorMessage") or j.get("title") or body
    except (ValueError, AttributeError):
        pass
    return body[:300]


def fetch_public_json(url: str, body=None, timeout: int = 60):
    """GET (or POST `body` to) a public API outside the stack."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    host = url.split("/")[2]
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LibraryError(f"{host} -> HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LibraryError(f"{host} unreachable ({e})") from None


class Arr:
    """Radarr or Sonarr (API v3)."""

    def __init__(self, base: str, key: str, name: str):
        self.base, self.name = base, name
        self.headers = {"X-Api-Key": key}

    def get(self, path: str, timeout: int = 60, **params):
        return http_json(_with_query(f"{self.base}/api/v3{path}", params),
                         headers=self.headers, timeout=timeout)

    def send(self, method: str, path: str, body=None, timeout: int = 60, **params):
        return http_json(_with_query(f"{self.base}/api/v3{path}", params), method, body,
                         self.headers, timeout)

    def queue(self, **params) -> list[dict]:
        # Without this flag the Arrs hide downloads they grabbed but can't match by name
        # (common for anime), and everything here would think nothing is downloading.
        unknown = "includeUnknownMovieItems" if self.name == "Radarr" else "includeUnknownSeriesItems"
        return self.get("/queue", page=1, pageSize=1000, **{unknown: "true"},
                        **params).get("records", [])

    def remove_from_queue(self, ids: list[int], blocklist: bool, search_again: bool) -> None:
        """Remove queue entries and their torrents (with data) from qBittorrent."""
        self.send("DELETE", "/queue/bulk", {"ids": ids}, removeFromClient="true",
                  blocklist=str(blocklist).lower(), skipRedownload=str(not search_again).lower())

    def command(self, body: dict) -> dict:
        """Start a command without waiting for it."""
        return self.send("POST", "/command", body)

    def run_command(self, body: dict, timeout: int = 300) -> None:
        """Start a command and wait until it finishes."""
        cmd = self.command(body)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            st = self.get(f"/command/{cmd['id']}")
            if st.get("status") == "completed":
                return
            if st.get("status") in ("failed", "aborted", "cancelled"):
                raise LibraryError(f"{self.name} {body['name']} {st['status']}: {st.get('message')}")
        raise LibraryError(f"{self.name} {body['name']} still running after {timeout}s")


class Jellyfin:
    def __init__(self, base: str, key: str):
        # Jellyfin 10.11+ ignores X-Emby-Token; it wants this header.
        self.base, self.headers = base, {"Authorization": f'MediaBrowser Token="{key}"'}

    def get(self, path: str, **params):
        return http_json(_with_query(self.base + path, params), headers=self.headers)

    def send(self, method: str, path: str, body=None, **params):
        return http_json(_with_query(self.base + path, params), method, body,
                         headers=self.headers)


class QBit:
    def __init__(self, base: str, user: str, password: str):
        self.base, self.user, self.password = base, user, password
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def _raw(self, path: str, data: dict | None = None) -> tuple[int, bytes]:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.base}/api/v2/{path}", data=body,
                                     headers={"Referer": self.base})
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except urllib.error.URLError as e:
            raise LibraryError(f"qBittorrent unreachable ({e.reason}); is the stack running?") from None

    def _login(self) -> None:
        # qBittorrent 5 answers a good login with 204 and an empty body, not "Ok.".
        status, body = self._raw("auth/login", {"username": self.user, "password": self.password})
        if status not in (200, 204) or body.strip() == b"Fails.":
            # qBittorrent bans an IP for an hour after repeated failures: don't retry.
            raise LibraryError("qBittorrent login failed (check QBIT_PASSWORD in .env)")
        self.logged_in = True

    def call(self, path: str, data: dict | None = None) -> bytes:
        if not self.logged_in:
            self._login()
        status, body = self._raw(path, data)
        if status == 403:  # session expired
            self._login()
            status, body = self._raw(path, data)
        if status >= 400:
            raise LibraryError(f"qBittorrent {path} -> HTTP {status}")
        return body

    def torrents(self) -> list[dict]:
        """Torrents in the Arrs' categories."""
        return [t for t in json.loads(self.call("torrents/info"))
                if t.get("category") in CATEGORIES]

    def torrents_by_hash(self) -> dict[str, dict]:
        return {t["hash"].lower(): t for t in self.torrents()}

    def file_paths(self, torrent: dict) -> list[str]:
        files = json.loads(self.call("torrents/files", {"hash": torrent["hash"]}))
        return [os.path.join(torrent["save_path"], f["name"]) for f in files]

    def delete(self, hashes: list[str]) -> None:
        """Delete torrents together with their data."""
        self.call("torrents/delete", {"hashes": "|".join(hashes), "deleteFiles": "true"})

    def stop(self, hashes: list[str]) -> None:
        self.call("torrents/stop", {"hashes": "|".join(hashes)})

    def start(self, hashes: list[str]) -> None:
        self.call("torrents/start", {"hashes": "|".join(hashes)})
